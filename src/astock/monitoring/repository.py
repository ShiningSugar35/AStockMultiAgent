"""SQLite/ObjectStore repository for durable continuous-monitor state."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.continuous_monitoring import (
    ContinuousMonitorTarget,
    MonitorDaemonState,
    MonitorDaemonStatus,
    MonitorEvent,
    MonitorResearchTask,
    MonitorRule,
    MonitorRuleRequest,
    MonitorRunReport,
    MonitorSource,
    MonitorTargetEnrollRequest,
    MonitorTargetStatus,
    MonitorTaskPriority,
)


def _process_is_alive(pid: int) -> bool:
    """Fail closed when a recorded daemon PID still refers to a live process."""

    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER is Windows' ordinary "no such PID" result.
            # All other OpenProcess failures are treated as alive to preserve
            # singleton safety when liveness cannot be proven.
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ContinuousMonitorRepository:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def enroll(self, request: MonitorTargetEnrollRequest) -> ContinuousMonitorTarget:
        target_id = f"monitor-target:{request.market.value}:{request.symbol}"
        existing = self.get_target(target_id)
        now = request.created_at.astimezone(UTC)
        if existing is None:
            target = ContinuousMonitorTarget(
                target_id=target_id,
                symbol=request.symbol,
                market=request.market,
                company_id=request.company_id,
                display_name=request.display_name,
                reasons=[request.reason],
                aliases=sorted(set(request.aliases)),
                enrolled_at=now,
                updated_at=now,
            )
        else:
            if existing.company_id != request.company_id:
                raise ValueError("monitor target company identity cannot change")
            target = existing.model_copy(
                update={
                    "display_name": request.display_name,
                    "reasons": sorted(
                        {*existing.reasons, request.reason}, key=lambda item: item.value
                    ),
                    "aliases": sorted({*existing.aliases, *request.aliases}),
                    "status": MonitorTargetStatus.ACTIVE,
                    "updated_at": now,
                }
            )
        ref = self.objects.put_json(target.model_dump(mode="json"))
        reasons_json = json.dumps([item.value for item in target.reasons], separators=(",", ":"))
        aliases_json = json.dumps(target.aliases, ensure_ascii=False, separators=(",", ":"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO continuous_monitor_target("
                "target_id,market,symbol,company_id,display_name,reasons_json,aliases_json,status,"
                "object_hash,enrolled_at,updated_at,last_price,high_watermark_price,last_market_at,"
                "last_review_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(target_id) DO UPDATE SET display_name=excluded.display_name,"
                "reasons_json=excluded.reasons_json,aliases_json=excluded.aliases_json,"
                "status=excluded.status,object_hash=excluded.object_hash,updated_at=excluded.updated_at",
                (
                    target.target_id,
                    target.market.value,
                    target.symbol,
                    target.company_id,
                    target.display_name,
                    reasons_json,
                    aliases_json,
                    target.status.value,
                    ref.sha256,
                    target.enrolled_at.isoformat(),
                    target.updated_at.isoformat(),
                    target.last_price,
                    target.high_watermark_price,
                    target.last_market_at.isoformat() if target.last_market_at else None,
                    target.last_review_at.isoformat() if target.last_review_at else None,
                ),
            )
        self._register_revision(
            "ContinuousMonitorTarget", target.target_id, target.schema_version, ref.sha256
        )
        return target

    def remove_target(self, target_id: str, *, at: datetime) -> bool:
        with self.state.transaction() as connection:
            changed = connection.execute(
                "UPDATE continuous_monitor_target SET status='REMOVED',updated_at=? "
                "WHERE target_id=? AND status!='REMOVED'",
                (at.astimezone(UTC).isoformat(), target_id),
            ).rowcount
        return changed == 1

    def get_target(self, target_id: str) -> ContinuousMonitorTarget | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT object_hash,last_price,high_watermark_price,last_market_at,last_review_at,status "  # noqa: E501
                "FROM continuous_monitor_target WHERE target_id=?",
                (target_id,),
            ).fetchone()
        if row is None:
            return None
        target = ContinuousMonitorTarget.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )
        return target.model_copy(
            update={
                "last_price": _optional_float(row["last_price"]),
                "high_watermark_price": _optional_float(row["high_watermark_price"]),
                "last_market_at": _optional_datetime(row["last_market_at"]),
                "last_review_at": _optional_datetime(row["last_review_at"]),
                "status": MonitorTargetStatus(str(row["status"])),
            }
        )

    def active_targets(self, *, limit: int) -> list[ContinuousMonitorTarget]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT target_id FROM continuous_monitor_target WHERE status='ACTIVE' "
                "ORDER BY updated_at,target_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            target for row in rows if (target := self.get_target(str(row["target_id"]))) is not None
        ]

    def update_market_state(
        self,
        target_id: str,
        *,
        last_price: float,
        observed_at: datetime,
    ) -> None:
        target = self.get_target(target_id)
        if target is None:
            raise ValueError("unknown continuous monitor target")
        high = max(target.high_watermark_price or last_price, last_price)
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE continuous_monitor_target SET last_price=?,high_watermark_price=?,"
                "last_market_at=?,updated_at=? WHERE target_id=?",
                (
                    last_price,
                    high,
                    observed_at.astimezone(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    target_id,
                ),
            )

    def mark_reviewed(self, target_id: str, *, at: datetime) -> None:
        with self.state.transaction() as connection:
            if (
                connection.execute(
                    "UPDATE continuous_monitor_target SET last_review_at=?,updated_at=? WHERE target_id=?",  # noqa: E501
                    (at.astimezone(UTC).isoformat(), at.astimezone(UTC).isoformat(), target_id),
                ).rowcount
                != 1
            ):
                raise ValueError("unknown continuous monitor target")

    def add_rule(self, request: MonitorRuleRequest) -> MonitorRule:
        if self.get_target(request.target_id) is None:
            raise ValueError("continuous monitor rule target does not exist")
        identity = request.model_dump(mode="json", exclude={"created_at"})
        rule_id = "monitor-rule:" + content_hash(identity)
        rule = MonitorRule(**request.model_dump(), rule_id=rule_id)
        ref = self.objects.put_json(rule.model_dump(mode="json"))
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT object_hash FROM continuous_monitor_rule WHERE rule_id=?", (rule_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO continuous_monitor_rule("
                    "rule_id,target_id,metric,comparison,threshold,action,severity,cooldown_seconds,"
                    "affected_modules_json,active,object_hash,created_at,last_triggered_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.target_id,
                        rule.metric.value,
                        rule.comparison.value,
                        rule.threshold,
                        rule.action.value,
                        rule.severity.value,
                        rule.cooldown_seconds,
                        json.dumps(
                            [item.value for item in rule.affected_modules], separators=(",", ":")
                        ),
                        1,
                        ref.sha256,
                        rule.created_at.astimezone(UTC).isoformat(),
                        None,
                    ),
                )
            elif str(existing["object_hash"]) != ref.sha256:
                raise ValueError("continuous monitor rule identity collision")
        self.state.register_artifact(
            artifact_id=rule.rule_id,
            artifact_type="MonitorRule",
            schema_version=rule.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return rule

    def rules(self, target_id: str) -> list[MonitorRule]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT object_hash,last_triggered_at FROM continuous_monitor_rule "
                "WHERE target_id=? AND active=1 ORDER BY rule_id",
                (target_id,),
            ).fetchall()
        result: list[MonitorRule] = []
        for row in rows:
            rule = MonitorRule.model_validate_json(self.objects.get_bytes(str(row["object_hash"])))
            result.append(
                rule.model_copy(
                    update={"last_triggered_at": _optional_datetime(row["last_triggered_at"])}
                )
            )
        return result

    def mark_rule_triggered(self, rule_id: str, *, at: datetime) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE continuous_monitor_rule SET last_triggered_at=? WHERE rule_id=?",
                (at.astimezone(UTC).isoformat(), rule_id),
            )

    def record_event(self, event: MonitorEvent) -> tuple[MonitorEvent, bool]:
        ref = self.objects.put_json(event.model_dump(mode="json"))
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT object_hash FROM continuous_monitor_event WHERE dedupe_key=?",
                (event.dedupe_key,),
            ).fetchone()
            if existing is not None:
                stored = MonitorEvent.model_validate_json(
                    self.objects.get_bytes(str(existing["object_hash"]))
                )
                return stored, False
            connection.execute(
                "INSERT INTO continuous_monitor_event("
                "event_id,target_id,event_type,severity,observed_at,available_at,source,source_ref,"
                "payload_hash,dedupe_key,affected_modules_json,requires_research,object_hash,"
                "acknowledged_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.target_id,
                    event.event_type.value,
                    event.severity.value,
                    event.observed_at.astimezone(UTC).isoformat(),
                    event.available_at.astimezone(UTC).isoformat(),
                    event.source.value,
                    event.source_ref,
                    event.payload_hash,
                    event.dedupe_key,
                    json.dumps(
                        [item.value for item in event.affected_modules], separators=(",", ":")
                    ),
                    int(event.requires_research),
                    ref.sha256,
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=event.event_id,
            artifact_type="MonitorEvent",
            schema_version=event.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return event, True

    def ensure_task(self, event: MonitorEvent) -> tuple[MonitorResearchTask | None, bool]:
        if not event.requires_research or not event.affected_modules:
            return None, False
        priority = _priority_for_event(event)
        task_id = "monitor-task:" + content_hash({"event_id": event.event_id})
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT object_hash FROM continuous_monitor_task WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                stored = MonitorResearchTask.model_validate_json(
                    self.objects.get_bytes(str(existing["object_hash"]))
                )
                return stored, False
            now = datetime.now(UTC)
            task = MonitorResearchTask(
                task_id=task_id,
                event_id=event.event_id,
                target_id=event.target_id,
                company_id=event.company_id,
                requested_modules=event.affected_modules,
                priority=priority,
                available_at=event.available_at,
                created_at=now,
                updated_at=now,
            )
            ref = self.objects.put_json(task.model_dump(mode="json"))
            connection.execute(
                "INSERT INTO continuous_monitor_task("
                "task_id,event_id,target_id,company_id,requested_modules_json,priority,status,"
                "object_hash,attempts,claimed_by,claim_expires_at,last_error,"
                "available_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id,
                    task.event_id,
                    task.target_id,
                    task.company_id,
                    json.dumps(
                        [item.value for item in task.requested_modules], separators=(",", ":")
                    ),
                    task.priority.value,
                    task.status.value,
                    ref.sha256,
                    task.attempts,
                    None,
                    None,
                    None,
                    task.available_at.astimezone(UTC).isoformat(),
                    task.created_at.astimezone(UTC).isoformat(),
                    task.updated_at.astimezone(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=task.task_id,
            artifact_type="MonitorResearchTask",
            schema_version=task.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return task, True

    def events_missing_research_tasks(self, *, limit: int = 500) -> list[MonitorEvent]:
        """Return durable research events whose idempotent task materialization is missing."""

        if limit <= 0:
            return []
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT e.object_hash FROM continuous_monitor_event e "
                "LEFT JOIN continuous_monitor_task t ON t.event_id=e.event_id "
                "WHERE e.requires_research=1 AND t.event_id IS NULL "
                "ORDER BY e.available_at,e.event_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            MonitorEvent.model_validate_json(self.objects.get_bytes(str(row["object_hash"])))
            for row in rows
        ]

    def repair_rule_trigger_state(self) -> int:
        """Restore cooldown timestamps from trigger events after a partial cycle."""

        with self.state.transaction() as connection:
            rows = connection.execute(
                "SELECT r.rule_id,MAX(e.available_at) AS triggered_at "
                "FROM continuous_monitor_rule r "
                "JOIN continuous_monitor_event e ON e.source_ref=r.rule_id "
                "WHERE e.event_type IN ('PRICE_TRIGGER','DRAWDOWN_TRIGGER') "
                "AND (r.last_triggered_at IS NULL OR r.last_triggered_at<e.available_at) "
                "GROUP BY r.rule_id ORDER BY r.rule_id"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE continuous_monitor_rule SET last_triggered_at=? WHERE rule_id=?",
                    (str(row["triggered_at"]), str(row["rule_id"])),
                )
        return len(rows)

    def list_events(
        self, *, unresolved_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "WHERE acknowledged_at IS NULL" if unresolved_only else ""
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                f"SELECT event_id,target_id,event_type,severity,observed_at,available_at,source,"
                f"source_ref,requires_research,acknowledged_at FROM continuous_monitor_event {where} "  # noqa: E501
                "ORDER BY available_at DESC,event_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_event(self, event_id: str, *, at: datetime) -> bool:
        with self.state.transaction() as connection:
            changed = connection.execute(
                "UPDATE continuous_monitor_event SET acknowledged_at=? "
                "WHERE event_id=? AND acknowledged_at IS NULL",
                (at.astimezone(UTC).isoformat(), event_id),
            ).rowcount
        return changed == 1

    def list_tasks(self, *, pending_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status='PENDING'" if pending_only else ""
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                f"SELECT task_id,event_id,target_id,company_id,requested_modules_json,"
                f"priority,status,attempts,claimed_by,claim_expires_at,last_error,"
                f"available_at,updated_at "
                f"FROM continuous_monitor_task {where} "
                "ORDER BY CASE priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 "
                "WHEN 'NORMAL' THEN 2 ELSE 3 END,available_at,task_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_task(
        self,
        *,
        owner_id: str,
        at: datetime,
        lease_seconds: int = 900,
    ) -> dict[str, Any] | None:
        now = at.astimezone(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT task_id FROM continuous_monitor_task WHERE available_at<=? AND "
                "(status='PENDING' OR (status='CLAIMED' AND claim_expires_at IS NOT NULL "
                "AND claim_expires_at<=?)) ORDER BY CASE priority WHEN 'URGENT' THEN 0 "
                "WHEN 'HIGH' THEN 1 WHEN 'NORMAL' THEN 2 ELSE 3 END,available_at,task_id LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            task_id = str(row["task_id"])
            changed = connection.execute(
                "UPDATE continuous_monitor_task SET status='CLAIMED',attempts=attempts+1,"
                "claimed_by=?,claim_expires_at=?,last_error=NULL,updated_at=? WHERE task_id=? AND "
                "(status='PENDING' OR (status='CLAIMED' AND claim_expires_at IS NOT NULL "
                "AND claim_expires_at<=?))",
                (
                    owner_id,
                    expires.isoformat(),
                    now.isoformat(),
                    task_id,
                    now.isoformat(),
                ),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT task_id,event_id,target_id,company_id,requested_modules_json,"
                "priority,status,attempts,claimed_by,claim_expires_at,last_error,"
                "available_at,updated_at "
                "FROM continuous_monitor_task WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def finish_task(
        self,
        task_id: str,
        *,
        owner_id: str,
        succeeded: bool,
        at: datetime,
        error: str | None = None,
    ) -> bool:
        now = at.astimezone(UTC).isoformat()
        status = "COMPLETED" if succeeded else "FAILED"
        with self.state.transaction() as connection:
            changed = connection.execute(
                "UPDATE continuous_monitor_task SET status=?,claimed_by=NULL,claim_expires_at=NULL,"
                "last_error=?,updated_at=? WHERE task_id=? AND status='CLAIMED' AND claimed_by=?",
                (status, None if succeeded else error, now, task_id, owner_id),
            ).rowcount
        return changed == 1

    def update_task_status(
        self,
        task_id: str,
        *,
        status: Any,
        at: datetime,
    ) -> bool:
        value = status.value if hasattr(status, "value") else str(status)
        if value not in {"PENDING", "CLAIMED", "COMPLETED", "FAILED"}:
            raise ValueError("invalid continuous monitor task status")
        now = at.astimezone(UTC).isoformat()
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,attempts,status FROM continuous_monitor_task WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + (
                1 if value == "CLAIMED" and row["status"] != "CLAIMED" else 0
            )
            changed = connection.execute(
                "UPDATE continuous_monitor_task SET status=?,attempts=?,updated_at=? WHERE task_id=?",  # noqa: E501
                (value, attempts, now, task_id),
            ).rowcount
        return changed == 1

    def cursor(self, target_id: str, source: MonitorSource) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT cursor,failure_count,retry_after,updated_at FROM continuous_monitor_source_cursor "  # noqa: E501
                "WHERE target_id=? AND source=?",
                (target_id, source.value),
            ).fetchone()
        return dict(row) if row is not None else None

    def source_due(
        self, target_id: str, source: MonitorSource, *, at: datetime, cadence: int
    ) -> bool:
        row = self.cursor(target_id, source)
        if row is None:
            return True
        retry_after = _optional_datetime(row["retry_after"])
        if retry_after is not None and at.astimezone(UTC) < retry_after:
            return False
        updated_at = _optional_datetime(row["updated_at"])
        return updated_at is None or at.astimezone(UTC) >= updated_at + timedelta(seconds=cadence)

    def source_success(
        self,
        target_id: str,
        source: MonitorSource,
        *,
        cursor: str | None,
        at: datetime,
    ) -> None:
        self._upsert_cursor(
            target_id, source, cursor=cursor, failure_count=0, retry_after=None, at=at
        )

    def source_failure(
        self,
        target_id: str,
        source: MonitorSource,
        *,
        at: datetime,
        retry_backoff_seconds: tuple[int, ...],
    ) -> int:
        existing = self.cursor(target_id, source)
        count = int(existing["failure_count"]) + 1 if existing else 1
        backoff = retry_backoff_seconds[min(count - 1, len(retry_backoff_seconds) - 1)]
        cursor = str(existing["cursor"]) if existing and existing["cursor"] is not None else None
        self._upsert_cursor(
            target_id,
            source,
            cursor=cursor,
            failure_count=count,
            retry_after=at.astimezone(UTC) + timedelta(seconds=backoff),
            at=at,
        )
        return count

    def record_run(self, report: MonitorRunReport) -> None:
        ref = self.objects.put_json(report.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO continuous_monitor_run("
                "run_id,owner_id,started_at,ended_at,status,live,target_count,event_count,task_count,"
                "source_success_json,source_failure_json,object_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
                (
                    report.run_id,
                    report.owner_id,
                    report.started_at.astimezone(UTC).isoformat(),
                    report.ended_at.astimezone(UTC).isoformat(),
                    report.status.value,
                    int(report.live),
                    report.target_count,
                    report.event_count,
                    report.task_count,
                    json.dumps(
                        {key.value: value for key, value in report.source_success.items()},
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        {key.value: value for key, value in report.source_failure.items()},
                        separators=(",", ":"),
                    ),
                    ref.sha256,
                ),
            )
        self.state.register_artifact(
            artifact_id=report.run_id,
            artifact_type="MonitorRunReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )

    def recent_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT run_id,started_at,ended_at,status,live,target_count,event_count,task_count,"
                "source_success_json,source_failure_json FROM continuous_monitor_run "
                "ORDER BY started_at DESC,run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_daemon(
        self,
        *,
        owner_id: str,
        pid: int,
        at: datetime,
        lease_seconds: int,
        details: dict[str, object] | None = None,
    ) -> bool:
        now = at.astimezone(UTC)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id,pid,state,heartbeat_at FROM continuous_monitor_daemon "
                "WHERE singleton_id='default'"
            ).fetchone()
            if row is None:
                raise RuntimeError("continuous monitor daemon singleton is missing")
            heartbeat = _optional_datetime(row["heartbeat_at"])
            active = str(row["state"]) in {
                MonitorDaemonState.RUNNING.value,
                MonitorDaemonState.STOPPING.value,
            }
            stale = heartbeat is None or heartbeat + timedelta(seconds=lease_seconds) < now
            different_owner = str(row["owner_id"]) != owner_id
            if active and different_owner:
                if not stale:
                    return False
                recorded_pid = int(row["pid"] or 0)
                if _process_is_alive(recorded_pid):
                    return False
            connection.execute(
                "UPDATE continuous_monitor_daemon SET owner_id=?,pid=?,state='RUNNING',started_at=?,"  # noqa: E501
                "heartbeat_at=?,stop_requested=0,details_json=?,updated_at=? WHERE singleton_id='default'",  # noqa: E501
                (
                    owner_id,
                    pid,
                    now.isoformat(),
                    now.isoformat(),
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    now.isoformat(),
                ),
            )
        return True

    def heartbeat_daemon(
        self,
        owner_id: str,
        *,
        at: datetime,
        last_run_id: str | None = None,
    ) -> bool:
        now = at.astimezone(UTC)
        with self.state.transaction() as connection:
            changed = connection.execute(
                "UPDATE continuous_monitor_daemon SET heartbeat_at=?,last_run_id=COALESCE(?,last_run_id),"  # noqa: E501
                "updated_at=? WHERE singleton_id='default' AND owner_id=? "
                "AND state IN ('RUNNING','STOPPING')",
                (now.isoformat(), last_run_id, now.isoformat(), owner_id),
            ).rowcount
        return changed == 1

    def daemon_stop_requested(self, owner_id: str) -> bool:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT stop_requested FROM continuous_monitor_daemon "
                "WHERE singleton_id='default' AND owner_id=?",
                (owner_id,),
            ).fetchone()
        return row is None or bool(row["stop_requested"])

    def request_daemon_stop(self, *, at: datetime) -> bool:
        now = at.astimezone(UTC)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM continuous_monitor_daemon WHERE singleton_id='default'"
            ).fetchone()
            if row is None or str(row["state"]) == MonitorDaemonState.STOPPED.value:
                return False
            connection.execute(
                "UPDATE continuous_monitor_daemon SET stop_requested=1,state='STOPPING',updated_at=? "  # noqa: E501
                "WHERE singleton_id='default'",
                (now.isoformat(),),
            )
        return True

    def release_daemon(self, owner_id: str, *, at: datetime, failed: bool = False) -> None:
        now = at.astimezone(UTC)
        state = MonitorDaemonState.FAILED if failed else MonitorDaemonState.STOPPED
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE continuous_monitor_daemon SET state=?,stop_requested=0,heartbeat_at=?,updated_at=? "  # noqa: E501
                "WHERE singleton_id='default' AND owner_id=?",
                (state.value, now.isoformat(), now.isoformat(), owner_id),
            )

    def daemon_status(self) -> MonitorDaemonStatus:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM continuous_monitor_daemon WHERE singleton_id='default'"
            ).fetchone()
        if row is None:
            raise RuntimeError("continuous monitor daemon singleton is missing")
        return MonitorDaemonStatus(
            owner_id=str(row["owner_id"]) if row["owner_id"] is not None else None,
            pid=int(row["pid"]) if row["pid"] is not None else None,
            state=MonitorDaemonState(str(row["state"])),
            started_at=_optional_datetime(row["started_at"]),
            heartbeat_at=_optional_datetime(row["heartbeat_at"]),
            stop_requested=bool(row["stop_requested"]),
            last_run_id=str(row["last_run_id"]) if row["last_run_id"] is not None else None,
            details=json.loads(str(row["details_json"])),
            updated_at=_required_datetime(row["updated_at"]),
        )

    def _upsert_cursor(
        self,
        target_id: str,
        source: MonitorSource,
        *,
        cursor: str | None,
        failure_count: int,
        retry_after: datetime | None,
        at: datetime,
    ) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO continuous_monitor_source_cursor("
                "target_id,source,cursor,failure_count,retry_after,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(target_id,source) DO UPDATE SET cursor=excluded.cursor,"
                "failure_count=excluded.failure_count,retry_after=excluded.retry_after,"
                "updated_at=excluded.updated_at",
                (
                    target_id,
                    source.value,
                    cursor,
                    failure_count,
                    retry_after.astimezone(UTC).isoformat() if retry_after else None,
                    at.astimezone(UTC).isoformat(),
                ),
            )

    def _register_revision(
        self,
        artifact_type: str,
        stable_id: str,
        schema_version: str,
        object_hash: str,
    ) -> None:
        self.state.register_artifact(
            artifact_id=f"{artifact_type}:{stable_id}:{object_hash[:16]}",
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=object_hash,
            input_hashes=[],
        )


def _priority_for_event(event: MonitorEvent) -> MonitorTaskPriority:
    if event.severity.value == "CRITICAL":
        return MonitorTaskPriority.URGENT
    if event.severity.value == "MATERIAL":
        return MonitorTaskPriority.HIGH
    if event.severity.value == "WATCH":
        return MonitorTaskPriority.NORMAL
    return MonitorTaskPriority.LOW


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError("continuous monitor numeric state has an unsupported type")


def _required_datetime(value: object) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ValueError("required UTC datetime is missing")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["ContinuousMonitorRepository"]
