"""Stable CLI surface for continuous investment monitoring."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from pydantic import ValidationError

from astock.monitoring.config import load_continuous_monitor_config
from astock.monitoring.daemon import run_daemon, spawn_daemon
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.monitoring.service import ContinuousMonitorService
from astock.schemas.continuous_monitoring import (
    MonitorRuleRequest,
    MonitorTargetReason,
    MonitorTaskStatus,
)
from astock.schemas.reference_data import Market

_DAEMON_START_TIMEOUT_SECONDS = 30.0


def _daemon_start_matches(status: Any, owner_id: str) -> bool:
    """Match the durable daemon lease, not an OS launcher pid.

    On Windows a virtual-environment launcher can have a different pid from the
    interpreter process that owns the SQLite daemon lease. The owner id is the
    stable start-attempt identity across that process boundary.
    """

    return status.state.value == "RUNNING" and status.owner_id == owner_id


def register_continuous_monitor_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def build() -> tuple[Any, ContinuousMonitorRepository, ContinuousMonitorService, Any]:
        paths, state, objects = services()
        config = load_continuous_monitor_config(paths.root / "configs" / "continuous_monitor.yaml")
        repo = ContinuousMonitorRepository(state, objects)
        service = ContinuousMonitorService(paths, state, objects, config, repository=repo)
        return paths, repo, service, config

    @app.command("continuous-monitor-schema")
    def continuous_monitor_schema() -> None:
        emit(
            {
                "schema_version": "continuous-monitor-cli-v1",
                "target_reasons": [item.value for item in MonitorTargetReason],
                "rule_request": MonitorRuleRequest.model_json_schema(),
                "safety": {
                    "broker_execution_allowed": False,
                    "news_can_directly_trade": False,
                    "natural_language_rule_execution_allowed": False,
                },
            }
        )

    @app.command("continuous-monitor-enroll")
    def continuous_monitor_enroll(
        symbol: Annotated[str, typer.Argument(help="Six-digit A-share code.")],
        market: Annotated[Market, typer.Option(case_sensitive=False)],
        company_id: Annotated[str | None, typer.Option()] = None,
        name: Annotated[str | None, typer.Option()] = None,
        reason: Annotated[
            MonitorTargetReason, typer.Option(case_sensitive=False)
        ] = MonitorTargetReason.MANUAL,
        alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    ) -> None:
        _, _, service, _ = build()
        try:
            target = service.enroll(
                symbol=symbol,
                market=market,
                company_id=company_id or symbol,
                display_name=name or symbol,
                reason=reason,
                aliases=alias or [],
            )
        except (ValidationError, ValueError) as exc:
            emit({"status": "REJECTED", "failure_code": "INVALID_MONITOR_TARGET"})
            raise typer.Exit(code=2) from exc
        emit({"status": "MONITORED", **target.model_dump(mode="json")})

    @app.command("continuous-monitor-remove")
    def continuous_monitor_remove(target_id: Annotated[str, typer.Argument()]) -> None:
        _, repo, _, _ = build()
        emit(
            {
                "status": "REMOVED"
                if repo.remove_target(target_id, at=datetime.now(UTC))
                else "NO_CHANGE",
                "target_id": target_id,
            }
        )

    @app.command("continuous-monitor-rule-add")
    def continuous_monitor_rule_add(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, dir_okay=False, readable=True, resolve_path=True),
        ],
    ) -> None:
        _, repo, _, _ = build()
        try:
            request = MonitorRuleRequest.model_validate_json(
                request_file.read_text(encoding="utf-8")
            )
            rule = repo.add_rule(request)
        except (OSError, ValidationError, ValueError) as exc:
            emit({"status": "REJECTED", "failure_code": "INVALID_MONITOR_RULE"})
            raise typer.Exit(code=2) from exc
        emit({"status": "ACTIVE", **rule.model_dump(mode="json")})

    @app.command("continuous-monitor-cycle")
    def continuous_monitor_cycle(
        live: Annotated[bool, typer.Option("--live")] = False,
    ) -> None:
        _, _, service, _ = build()
        report = service.run_cycle(owner_id="manual-cycle", live=live)
        emit(report.model_dump(mode="json"))
        if report.status.value == "FAILED":
            raise typer.Exit(code=2)

    @app.command("continuous-monitor-daemon")
    def continuous_monitor_daemon(
        live: Annotated[bool, typer.Option("--live")] = False,
        interval_seconds: Annotated[int | None, typer.Option(min=30, max=3600)] = None,
        owner_id: Annotated[str | None, typer.Option("--owner-id", hidden=True)] = None,
    ) -> None:
        _, repo, service, config = build()
        code = run_daemon(
            service,
            repo,
            config,
            live=live,
            interval_seconds=interval_seconds,
            owner_id=owner_id,
        )
        if code:
            raise typer.Exit(code=code)

    @app.command("continuous-monitor-start")
    def continuous_monitor_start(
        live: Annotated[bool, typer.Option("--live")] = False,
        interval_seconds: Annotated[int | None, typer.Option(min=30, max=3600)] = None,
    ) -> None:
        paths, repo, _, config = build()
        current = repo.daemon_status()
        if current.state.value in {"RUNNING", "STOPPING"} and not _lease_stale(
            current.heartbeat_at, config.daemon.lease_seconds
        ):
            emit({"status": "ALREADY_RUNNING", **current.model_dump(mode="json")})
            return
        owner_id = f"continuous-monitor:{uuid4().hex}"
        launcher_pid = spawn_daemon(
            paths.root,
            live=live,
            interval_seconds=interval_seconds or config.wake_interval_seconds,
            owner_id=owner_id,
        )
        deadline = time.monotonic() + _DAEMON_START_TIMEOUT_SECONDS
        status = repo.daemon_status()
        while time.monotonic() < deadline:
            status = repo.daemon_status()
            if _daemon_start_matches(status, owner_id):
                emit({"status": "STARTED", **status.model_dump(mode="json")})
                return
            time.sleep(0.2)
        emit(
            {
                "status": "START_FAILED",
                "pid": launcher_pid,
                "owner_id": owner_id,
                "daemon": status.model_dump(mode="json"),
            }
        )
        raise typer.Exit(code=2)

    @app.command("continuous-monitor-stop")
    def continuous_monitor_stop() -> None:
        _, repo, _, _ = build()
        requested = repo.request_daemon_stop(at=datetime.now(UTC))
        emit({"status": "STOP_REQUESTED" if requested else "ALREADY_STOPPED"})

    @app.command("continuous-monitor-status")
    def continuous_monitor_status() -> None:
        _, repo, _, _ = build()
        targets = repo.active_targets(limit=500)
        emit(
            {
                "schema_version": "continuous-monitor-status-v1",
                "daemon": repo.daemon_status().model_dump(mode="json"),
                "active_target_count": len(targets),
                "targets": [
                    {
                        "target_id": item.target_id,
                        "symbol": item.symbol,
                        "market": item.market.value,
                        "company_id": item.company_id,
                        "display_name": item.display_name,
                        "reasons": [reason.value for reason in item.reasons],
                        "last_price": item.last_price,
                        "last_market_at": item.last_market_at,
                        "last_review_at": item.last_review_at,
                    }
                    for item in targets
                ],
                "pending_tasks": repo.list_tasks(pending_only=True, limit=100),
                "unresolved_events": repo.list_events(unresolved_only=True, limit=100),
                "recent_runs": repo.recent_runs(limit=5),
                "broker_execution_allowed": False,
            }
        )

    @app.command("continuous-monitor-events")
    def continuous_monitor_events(
        unresolved_only: Annotated[bool, typer.Option("--unresolved-only")] = False,
        limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    ) -> None:
        _, repo, _, _ = build()
        emit({"events": repo.list_events(unresolved_only=unresolved_only, limit=limit)})

    @app.command("continuous-monitor-ack")
    def continuous_monitor_ack(event_id: Annotated[str, typer.Argument()]) -> None:
        _, repo, _, _ = build()
        emit(
            {
                "status": "ACKNOWLEDGED"
                if repo.acknowledge_event(event_id, at=datetime.now(UTC))
                else "NO_CHANGE",
                "event_id": event_id,
            }
        )

    @app.command("continuous-monitor-tasks")
    def continuous_monitor_tasks(
        pending_only: Annotated[bool, typer.Option("--pending-only")] = False,
        limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    ) -> None:
        _, repo, _, _ = build()
        emit({"tasks": repo.list_tasks(pending_only=pending_only, limit=limit)})

    @app.command("continuous-monitor-task-claim")
    def continuous_monitor_task_claim(
        owner_id: Annotated[str, typer.Option("--owner-id")],
        lease_seconds: Annotated[int, typer.Option(min=60, max=3600)] = 900,
    ) -> None:
        _, repo, _, _ = build()
        claimed = repo.claim_next_task(
            owner_id=owner_id,
            at=datetime.now(UTC),
            lease_seconds=lease_seconds,
        )
        emit({"status": "CLAIMED" if claimed else "EMPTY", "task": claimed})

    @app.command("continuous-monitor-task-complete")
    def continuous_monitor_task_complete(
        task_id: Annotated[str, typer.Argument()],
        owner_id: Annotated[str, typer.Option("--owner-id")],
    ) -> None:
        _, repo, _, _ = build()
        changed = repo.finish_task(
            task_id,
            owner_id=owner_id,
            succeeded=True,
            at=datetime.now(UTC),
        )
        emit({"status": "COMPLETED" if changed else "REJECTED", "task_id": task_id})
        if not changed:
            raise typer.Exit(code=2)

    @app.command("continuous-monitor-task-fail")
    def continuous_monitor_task_fail(
        task_id: Annotated[str, typer.Argument()],
        owner_id: Annotated[str, typer.Option("--owner-id")],
        error: Annotated[str, typer.Option("--error")] = "research task failed",
    ) -> None:
        _, repo, _, _ = build()
        changed = repo.finish_task(
            task_id,
            owner_id=owner_id,
            succeeded=False,
            error=error,
            at=datetime.now(UTC),
        )
        emit({"status": "FAILED" if changed else "REJECTED", "task_id": task_id})
        if not changed:
            raise typer.Exit(code=2)

    @app.command("continuous-monitor-task-status")
    def continuous_monitor_task_status(
        task_id: Annotated[str, typer.Argument()],
        status: Annotated[MonitorTaskStatus, typer.Option(case_sensitive=False)],
    ) -> None:
        _, repo, _, _ = build()
        changed = repo.update_task_status(task_id, status=status, at=datetime.now(UTC))
        emit(
            {
                "status": "UPDATED" if changed else "NO_CHANGE",
                "task_id": task_id,
                "task_status": status.value,
            }
        )

    @app.command("continuous-monitor-reviewed")
    def continuous_monitor_reviewed(target_id: Annotated[str, typer.Argument()]) -> None:
        _, repo, _, _ = build()
        repo.mark_reviewed(target_id, at=datetime.now(UTC))
        emit({"status": "REVIEW_BOUNDARY_UPDATED", "target_id": target_id})


def _lease_stale(heartbeat: datetime | None, lease_seconds: int) -> bool:
    return (
        heartbeat is None or heartbeat.timestamp() + lease_seconds < datetime.now(UTC).timestamp()
    )


__all__ = ["register_continuous_monitor_commands"]
