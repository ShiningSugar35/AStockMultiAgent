"""Local CLI for scheduled source preparation, semantic handoff and typed replay.

Read/status commands never fetch. Explicit ``schedule-source-prepare --live`` is the
only command here that performs bounded source acquisition; no command grants task,
account, semantic-model or trading authorization.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from astock.core.errors import AStockError
from astock.investor_orchestration.models import ScheduledRunRequest, ScheduledWindow
from astock.investor_orchestration.paper_replay import CanonicalConfirmedPaperReplayAdapter
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditRequest,
    ScheduledInputCoverageService,
    load_input_audit_policy,
)
from astock.investor_orchestration.scheduled_market_views import ScheduledIntradayViews
from astock.investor_orchestration.scheduled_preparation import ScheduledSourcePreparationService
from astock.investor_orchestration.scheduled_semantic_worker import (
    ScheduledSemanticSubmission,
    ScheduledSemanticWorkInProgress,
    ScheduledSemanticWorkService,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore, default_state_path
from astock.investor_orchestration.utils import utc_now
from astock.schemas import Frequency


def _existing_store(database: Path | None) -> InvestorOrchestrationStore:
    # Check the path before constructing the store: construction may create
    # parent directories, which a failed read/status command must not do.
    path = database if database is not None else default_state_path()
    if not path.is_file():
        raise typer.BadParameter("the canonical state database must already be initialized")
    return InvestorOrchestrationStore(path)


def register_source_audit_commands(app: typer.Typer) -> None:
    @app.command("schedule-market-locator")
    def market_locator(
        instrument_id: Annotated[str, typer.Argument()],
        frequency: Annotated[Frequency, typer.Option()] = Frequency.M5,
        window: Annotated[ScheduledWindow, typer.Option()] = ScheduledWindow.INTRADAY,
        at: Annotated[str | None, typer.Option()] = None,
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Verify existing closed 5m/60m raw-backed prices; no fetch or database creation."""
        audit = ScheduledInputCoverageService(_existing_store(database), load_input_audit_policy())
        try:
            cutoff = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else utc_now()
            price = ScheduledIntradayViews(audit.state, audit.objects).latest(
                instrument_id, frequency, as_of=cutoff
            )
        except (ValueError, AStockError, OSError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        fresh = (cutoff - price.observed_at).total_seconds() <= (
            audit.policy.maximum_price_age_seconds[window]
        )
        typer.echo(
            json.dumps(
                {
                    "status": "CHECKED" if fresh else "STALE",
                    "scope": "MARKET_ONLY",
                    "formal_research_complete": False,
                    "locator": price.locator,
                    "frequency": price.bar.frequency.value,
                    "instrument_id": f"{price.bar.market.value}:{price.bar.symbol}",
                    "price": str(price.bar.close),
                    "observed_at": price.observed_at.isoformat(),
                    "available_at": price.available_at.isoformat(),
                    "as_of": cutoff.isoformat(),
                    "source_revisions": price.source_revisions,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not fresh:
            raise typer.Exit(code=3)

    @app.command("schedule-input-schema")
    def input_schema() -> None:
        """Print the captured-input schema and current versioned audit policy."""
        typer.echo(
            json.dumps(
                {
                    "request_schema": ScheduledInputAuditRequest.model_json_schema(),
                    "run_schema": ScheduledRunRequest.model_json_schema(),
                    "source_audit_policy": load_input_audit_policy().model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("schedule-input-register")
    def register_input(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Verify captured sources and register the result; incomplete checks exit 3."""
        request = ScheduledInputAuditRequest.model_validate_json(request_file.read_bytes())
        audit = ScheduledInputCoverageService(_existing_store(database), load_input_audit_policy())
        artifact_id = audit.register(request)
        report = audit.verify_registered(artifact_id)
        typer.echo(
            json.dumps(
                {"artifact_id": artifact_id, "report": report.model_dump(mode="json")},
                ensure_ascii=False,
                indent=2,
            )
        )
        if not report.all_families_checked:
            raise typer.Exit(code=3)

    @app.command("schedule-input-status")
    def input_status(
        artifact_id: Annotated[str, typer.Argument()],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Revalidate the persisted check against its original source records."""
        audit = ScheduledInputCoverageService(_existing_store(database), load_input_audit_policy())
        report = audit.verify_registered(artifact_id)
        typer.echo(report.model_dump_json(indent=2))
        if not report.all_families_checked:
            raise typer.Exit(code=3)

    @app.command("schedule-run-stage")
    def stage_run(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Stage exact source/semantic identities for one already-consented pending bucket."""
        request = ScheduledRunRequest.model_validate_json(request_file.read_bytes())
        store = _existing_store(database)
        service = ScheduledResearchService(
            store,
            InvestorSessionPreflightService(store),
            paper_replay_adapter=CanonicalConfirmedPaperReplayAdapter.from_store(store),
        )
        try:
            staged = service.stage(request)
        except (ValueError, AStockError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        checkpoint = store.get_scheduled_checkpoint(staged.binding_id, staged.schedule_bucket)
        typer.echo(
            json.dumps(
                {
                    "status": "STAGED",
                    "request": staged.model_dump(mode="json"),
                    "checkpoint_status": None if checkpoint is None else checkpoint["status"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("schedule-run-pending")
    def pending_run(
        binding_id: Annotated[str, typer.Argument()],
        schedule_bucket: Annotated[str, typer.Argument()],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Read the immutable pending identity workers must use when preparing evidence."""
        store = _existing_store(database)
        checkpoint = store.get_scheduled_checkpoint(binding_id, schedule_bucket)
        if checkpoint is None:
            typer.echo(json.dumps({"status": "MISSING"}, ensure_ascii=False, indent=2))
            raise typer.Exit(code=3)
        request = ScheduledRunRequest.model_validate(checkpoint["request"])
        typer.echo(
            json.dumps(
                {
                    "status": checkpoint["status"],
                    "request": request.model_dump(mode="json"),
                    "degradation_reasons": list(checkpoint["degradation_reasons"]),
                    "final_receipt_id": checkpoint["final_receipt_id"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("schedule-source-prepare")
    def prepare_sources(
        binding_id: Annotated[str, typer.Argument()],
        schedule_bucket: Annotated[str, typer.Argument()],
        live: Annotated[bool, typer.Option("--live")] = False,
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Capture and freeze all five source families for one pending local bucket."""
        store = _existing_store(database)
        try:
            result = ScheduledSourcePreparationService(store).prepare_pending(
                binding_id,
                schedule_bucket,
                live=live,
            )
        except (ValueError, AStockError, OSError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(
            json.dumps(
                {
                    "status": "SOURCES_STAGED",
                    "binding_id": binding_id,
                    "schedule_bucket": schedule_bucket,
                    "source_report_ids": result.source_report_ids,
                    "subject_bindings": [
                        [domain.value, instrument] for domain, instrument in result.subject_bindings
                    ],
                    "interval_start": result.interval_start.isoformat(),
                    "interval_end": result.interval_end.isoformat(),
                    "evidence_cutoff": result.evidence_cutoff.isoformat(),
                    "logical_external_call_budget": result.logical_external_call_budget,
                    "semantic_worker_required": result.request.semantic_capability_receipt_id
                    is None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("schedule-source-prepare-all")
    def prepare_all_sources(
        binding_id: Annotated[str, typer.Argument()],
        live: Annotated[bool, typer.Option("--live")] = False,
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Prepare every currently pending bucket for one consented local binding."""
        store = _existing_store(database)
        requests = store.pending_scheduled_requests(binding_id)
        service = ScheduledSourcePreparationService(store)
        prepared = []
        failures = []
        for request in requests:
            try:
                result = service.prepare_pending(
                    binding_id,
                    request.schedule_bucket,
                    live=live,
                )
            except (ValueError, AStockError, OSError) as exc:
                failures.append(
                    {
                        "schedule_bucket": request.schedule_bucket,
                        "failure_class": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
                continue
            prepared.append(
                {
                    "schedule_bucket": request.schedule_bucket,
                    "source_report_ids": result.source_report_ids,
                    "evidence_cutoff": result.evidence_cutoff.isoformat(),
                    "logical_external_call_budget": result.logical_external_call_budget,
                }
            )
        typer.echo(
            json.dumps(
                {
                    "status": "DEGRADED" if failures else "COMPLETED",
                    "binding_id": binding_id,
                    "prepared": prepared,
                    "failures": failures,
                    "semantic_worker_created": False,
                    "actual_execution_allowed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if failures:
            raise typer.Exit(code=3)

    @app.command("schedule-semantic-claim")
    def semantic_claim(
        binding_id: Annotated[str, typer.Argument()],
        schedule_bucket: Annotated[str, typer.Argument()],
        owner_id: Annotated[str, typer.Option("--owner-id")],
        lease_seconds: Annotated[int, typer.Option(min=60, max=7200)] = 1800,
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Claim one prepared bucket and emit its privacy-minimized semantic work item."""
        store = _existing_store(database)
        try:
            work = ScheduledSemanticWorkService(store).claim(
                binding_id,
                schedule_bucket,
                owner_id=owner_id,
                lease_seconds=lease_seconds,
            )
        except (ValueError, AStockError, ScheduledSemanticWorkInProgress) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(work.model_dump_json(indent=2))

    @app.command("schedule-semantic-submit")
    def semantic_submit(
        binding_id: Annotated[str, typer.Argument()],
        schedule_bucket: Annotated[str, typer.Argument()],
        submission_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Submit registered capability artifacts plus exact typed subject results."""
        store = _existing_store(database)
        try:
            submission = ScheduledSemanticSubmission.model_validate_json(
                submission_file.read_bytes()
            )
            staged = ScheduledSemanticWorkService(store).submit(
                binding_id,
                schedule_bucket,
                submission,
            )
        except (ValueError, AStockError, ScheduledSemanticWorkInProgress) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(
            json.dumps(
                {
                    "status": "SEMANTIC_STAGED",
                    "binding_id": binding_id,
                    "schedule_bucket": schedule_bucket,
                    "semantic_capability_receipt_id": staged.semantic_capability_receipt_id,
                    "semantic_result_artifact_ids": staged.semantic_result_artifact_ids,
                    "actual_execution_allowed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("schedule-semantic-release")
    def semantic_release(
        binding_id: Annotated[str, typer.Argument()],
        schedule_bucket: Annotated[str, typer.Argument()],
        owner_id: Annotated[str, typer.Option("--owner-id")],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Release an unfinished semantic lease without changing the frozen bucket."""
        released = ScheduledSemanticWorkService(_existing_store(database)).release(
            binding_id,
            schedule_bucket,
            owner_id=owner_id,
        )
        typer.echo(
            json.dumps(
                {
                    "status": "RELEASED" if released else "NOT_OWNED",
                    "binding_id": binding_id,
                    "schedule_bucket": schedule_bucket,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not released:
            raise typer.Exit(code=3)

    @app.command("schedule-run-file")
    def run_file(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        database: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Run a typed local binding with source-check references; no implicit consent."""
        request = ScheduledRunRequest.model_validate_json(request_file.read_bytes())
        store = _existing_store(database)
        service = ScheduledResearchService(
            store,
            InvestorSessionPreflightService(store),
            paper_replay_adapter=CanonicalConfirmedPaperReplayAdapter.from_store(store),
        )
        receipt = service.run(request)
        typer.echo(receipt.model_dump_json(indent=2))
        if receipt.outcome.value in {"BLOCKED", "FAILED", "DEGRADED"}:
            raise typer.Exit(code=3)
