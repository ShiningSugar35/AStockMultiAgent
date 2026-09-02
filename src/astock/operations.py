"""Bounded storage lifecycle planning and operational SLO reporting."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.settings import ProjectPaths

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_StorageCategory = Literal[
    "OBJECT_STORE", "RUNTIME_TMP", "REPORT_STAGING", "REPORT_OUTPUT", "LOG_BACKUP"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageClassPolicy(_Model):
    retention_hours: int | None = Field(default=None, ge=1)
    orphan_retention_days: int | None = Field(default=None, ge=1)
    scan_limit: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def one_retention(self) -> StorageClassPolicy:
        if (self.retention_hours is None) == (self.orphan_retention_days is None):
            raise ValueError("storage class policy requires exactly one retention horizon")
        return self


class StorageWatermarks(_Model):
    runtime_warning_bytes: int = Field(ge=0)
    runtime_critical_bytes: int = Field(ge=0)
    object_store_warning_bytes: int = Field(ge=0)
    report_warning_bytes: int = Field(ge=0)
    temp_warning_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> StorageWatermarks:
        if self.runtime_critical_bytes < self.runtime_warning_bytes:
            raise ValueError("runtime critical watermark must be >= warning watermark")
        return self


class OperationsSLOPolicy(_Model):
    evidence_freshness_target_seconds: int = Field(ge=1)
    monitor_backlog_warning: int = Field(ge=0)
    provider_degraded_warning: int = Field(ge=0)
    report_success_rate_target: float = Field(ge=0, le=1)


class StorageLifecyclePolicy(_Model):
    schema_version: Literal["storage-lifecycle-policy-v1"]
    object_store: StorageClassPolicy
    runtime_tmp: StorageClassPolicy
    report_staging: StorageClassPolicy
    report_output: StorageClassPolicy
    logs: StorageClassPolicy
    watermarks: StorageWatermarks
    operations_slo: OperationsSLOPolicy


class StorageCandidate(_Model):
    category: _StorageCategory
    relative_path: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    referenced: bool = False
    eligible: bool
    reason: str = Field(min_length=1)


class StorageLifecyclePlan(_Model):
    schema_version: Literal["storage-lifecycle-plan-v1"] = "storage-lifecycle-plan-v1"
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    candidates: list[StorageCandidate]
    scanned_file_count: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)
    eligible_file_count: int = Field(ge=0)
    eligible_bytes: int = Field(ge=0)
    referenced_object_count: int = Field(ge=0)
    scan_truncated: bool = False
    runtime_bytes: int = Field(ge=0)
    object_store_bytes: int = Field(ge=0)
    temp_bytes: int = Field(ge=0)
    report_bytes: int = Field(ge=0)
    watermark_status: Literal["OK", "WARNING", "CRITICAL"]
    deletion_requires_confirmation: Literal[True] = True


class StorageLifecycleAudit(_Model):
    schema_version: Literal["storage-lifecycle-audit-v1"] = "storage-lifecycle-audit-v1"
    plan_id: str
    status: Literal["PASS", "FAIL"]
    finding_codes: list[str]
    protected_referenced_objects: int = Field(ge=0)
    eligible_file_count: int = Field(ge=0)
    eligible_bytes: int = Field(ge=0)


class StorageLifecycleRun(_Model):
    schema_version: Literal["storage-lifecycle-run-v1"] = "storage-lifecycle-run-v1"
    plan_id: str
    confirmed: Literal[True]
    deleted_file_count: int = Field(ge=0)
    deleted_bytes: int = Field(ge=0)
    skipped_file_count: int = Field(ge=0)
    skip_reasons: list[str]


class OperationsSLOReport(_Model):
    schema_version: Literal["operations-slo-report-v1"] = "operations-slo-report-v1"
    generated_at: datetime
    universe_formal_coverage_artifact_count: int = Field(ge=0)
    provider_degraded_count: int = Field(ge=0)
    open_circuit_count: int = Field(ge=0)
    latest_evidence_age_seconds: int | None = Field(default=None, ge=0)
    evidence_freshness_status: Literal["PASS", "WARN", "UNKNOWN"]
    research_route_count: int = Field(ge=0)
    report_total_count: int = Field(ge=0)
    report_published_count: int = Field(ge=0)
    report_success_rate: float | None = Field(default=None, ge=0, le=1)
    report_recovered_count: int = Field(ge=0)
    monitor_pending_task_count: int = Field(ge=0)
    monitor_failed_task_count: int = Field(ge=0)
    monitor_failed_run_count: int = Field(ge=0)
    monitor_partial_run_count: int = Field(ge=0)
    mean_monitor_run_seconds: float | None = Field(default=None, ge=0)
    monitor_recovery_observation_count: int = Field(ge=0)
    mean_monitor_recovery_seconds: float | None = Field(default=None, ge=0)
    skill_token_cost_total: int = Field(ge=0)
    skill_tokens_per_research_route: float | None = Field(default=None, ge=0)
    runtime_bytes: int = Field(ge=0)
    object_store_bytes: int = Field(ge=0)
    temp_bytes: int = Field(ge=0)
    report_bytes: int = Field(ge=0)
    runtime_growth_bytes: int | None = None
    object_store_growth_bytes: int | None = None
    temp_growth_bytes: int | None = None
    report_growth_bytes: int | None = None
    finding_codes: list[str]
    status: Literal["PASS", "WARN"]


def load_storage_lifecycle_policy(path: Path) -> StorageLifecyclePolicy:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StorageLifecyclePolicy.model_validate(value)


class StorageLifecycleService:
    """Plan bounded cleanup from existing indexes; deletion is opt-in and fail-closed."""

    def __init__(
        self,
        paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
        policy: StorageLifecyclePolicy | None = None,
    ) -> None:
        self.paths = paths
        self.state = state
        self.objects = objects
        self.policy = policy or load_storage_lifecycle_policy(
            paths.root / "configs" / "storage_lifecycle.yaml"
        )

    def plan(self, *, now: datetime | None = None) -> StorageLifecyclePlan:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        referenced = self._referenced_hashes()
        candidates: list[StorageCandidate] = []
        truncated = False
        scanned_count = 0
        scanned_bytes = 0

        object_rows, object_truncated = self._scan_files(
            self.paths.objects, self.policy.object_store.scan_limit
        )
        truncated |= object_truncated
        cutoff = instant - timedelta(days=self.policy.object_store.orphan_retention_days or 0)
        for path, stat in object_rows:
            scanned_count += 1
            scanned_bytes += stat.st_size
            digest = path.name
            is_object = bool(_SHA256.fullmatch(digest))
            is_referenced = is_object and digest in referenced
            expired = datetime.fromtimestamp(stat.st_mtime, UTC) <= cutoff
            eligible = is_object and expired and not is_referenced
            candidates.append(
                self._candidate(
                    "OBJECT_STORE",
                    path,
                    stat,
                    referenced=is_referenced,
                    eligible=eligible,
                    reason=(
                        "REFERENCED_OBJECT_PROTECTED"
                        if is_referenced
                        else "UNREFERENCED_OBJECT_EXPIRED"
                        if eligible
                        else "OBJECT_NOT_EXPIRED"
                        if is_object
                        else "INVALID_OBJECT_STORE_ENTRY_PROTECTED"
                    ),
                )
            )

        tmp_rows, tmp_truncated = self._scan_files(
            self.paths.runtime / "tmp", self.policy.runtime_tmp.scan_limit
        )
        truncated |= tmp_truncated
        tmp_cutoff = instant - timedelta(hours=self.policy.runtime_tmp.retention_hours or 0)
        for path, stat in tmp_rows:
            scanned_count += 1
            scanned_bytes += stat.st_size
            expired = datetime.fromtimestamp(stat.st_mtime, UTC) <= tmp_cutoff
            candidates.append(
                self._candidate(
                    "RUNTIME_TMP",
                    path,
                    stat,
                    referenced=False,
                    eligible=expired,
                    reason="RUNTIME_TMP_EXPIRED" if expired else "RUNTIME_TMP_NOT_EXPIRED",
                )
            )

        active_staging_keys = self._active_report_staging_keys()
        staging_rows, staging_truncated = self._scan_files(
            self.paths.report_staging, self.policy.report_staging.scan_limit
        )
        truncated |= staging_truncated
        staging_cutoff = instant - timedelta(hours=self.policy.report_staging.retention_hours or 0)
        for path, stat in staging_rows:
            scanned_count += 1
            scanned_bytes += stat.st_size
            expired = datetime.fromtimestamp(stat.st_mtime, UTC) <= staging_cutoff
            try:
                relative = path.resolve().relative_to(self.paths.report_staging.resolve())
                report_key = relative.parts[0] if relative.parts else ""
            except ValueError:
                report_key = ""
            active = report_key in active_staging_keys
            eligible = expired and not active
            candidates.append(
                self._candidate(
                    "REPORT_STAGING",
                    path,
                    stat,
                    referenced=active,
                    eligible=eligible,
                    reason=(
                        "ACTIVE_REPORT_STAGING_PROTECTED"
                        if active
                        else "REPORT_STAGING_EXPIRED"
                        if eligible
                        else "REPORT_STAGING_NOT_EXPIRED"
                    ),
                )
            )

        report_refs = self._referenced_report_output_names()
        report_output_root = self.paths.reports / "output"
        report_rows, report_truncated = self._scan_files(
            report_output_root, self.policy.report_output.scan_limit
        )
        truncated |= report_truncated
        report_cutoff = instant - timedelta(
            days=self.policy.report_output.orphan_retention_days or 0
        )
        for path, stat in report_rows:
            scanned_count += 1
            scanned_bytes += stat.st_size
            expired = datetime.fromtimestamp(stat.st_mtime, UTC) <= report_cutoff
            referenced_report = path.name in report_refs
            eligible = expired and not referenced_report
            candidates.append(
                self._candidate(
                    "REPORT_OUTPUT",
                    path,
                    stat,
                    referenced=referenced_report,
                    eligible=eligible,
                    reason=(
                        "REFERENCED_REPORT_PROTECTED"
                        if referenced_report
                        else "UNREFERENCED_REPORT_EXPIRED"
                        if eligible
                        else "REPORT_OUTPUT_NOT_EXPIRED"
                    ),
                )
            )

        log_rows, log_truncated = self._scan_files(self.paths.logs, self.policy.logs.scan_limit)
        truncated |= log_truncated
        log_cutoff = instant - timedelta(hours=self.policy.logs.retention_hours or 0)
        active_name = self._active_log_name()
        for path, stat in log_rows:
            scanned_count += 1
            scanned_bytes += stat.st_size
            expired = datetime.fromtimestamp(stat.st_mtime, UTC) <= log_cutoff
            is_active = path.name == active_name
            eligible = expired and not is_active
            candidates.append(
                self._candidate(
                    "LOG_BACKUP",
                    path,
                    stat,
                    referenced=is_active,
                    eligible=eligible,
                    reason=(
                        "ACTIVE_LOG_PROTECTED"
                        if is_active
                        else "LOG_BACKUP_EXPIRED"
                        if eligible
                        else "LOG_BACKUP_NOT_EXPIRED"
                    ),
                )
            )

        candidates.sort(key=lambda item: (item.category, item.relative_path))
        eligible = [item for item in candidates if item.eligible]
        runtime_bytes = self._bounded_tree_size(self.paths.runtime, 100_000)
        object_store_bytes = self._bounded_tree_size(self.paths.objects, 100_000)
        temp_bytes = self._bounded_tree_size(self.paths.runtime / "tmp", 100_000)
        report_bytes = self._bounded_tree_size(self.paths.reports, 100_000)
        watermark_status = self._watermark(
            runtime_bytes, object_store_bytes, temp_bytes, report_bytes
        )
        identity = self._plan_identity_payload(candidates, scan_truncated=truncated)
        return StorageLifecyclePlan(
            plan_id=content_hash(identity),
            generated_at=instant,
            candidates=candidates,
            scanned_file_count=scanned_count,
            scanned_bytes=scanned_bytes,
            eligible_file_count=len(eligible),
            eligible_bytes=sum(item.byte_size for item in eligible),
            referenced_object_count=sum(
                item.category == "OBJECT_STORE" and item.referenced for item in candidates
            ),
            scan_truncated=truncated,
            runtime_bytes=runtime_bytes,
            object_store_bytes=object_store_bytes,
            temp_bytes=temp_bytes,
            report_bytes=report_bytes,
            watermark_status=watermark_status,
        )

    def persist_plan(self, plan: StorageLifecyclePlan) -> None:
        """Persist the exact reviewed plan so audit/run can resume across processes."""

        expected = content_hash(
            self._plan_identity_payload(plan.candidates, scan_truncated=plan.scan_truncated)
        )
        if expected != plan.plan_id:
            raise ValueError("storage lifecycle plan identity mismatch")
        now_text = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO storage_lifecycle_plan(plan_id,plan_json,generated_at,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET "
                "plan_json=excluded.plan_json, generated_at=excluded.generated_at",
                (
                    plan.plan_id,
                    plan.model_dump_json(),
                    plan.generated_at.isoformat(),
                    now_text,
                ),
            )

    def load_plan(self, plan_id: str) -> StorageLifecyclePlan | None:
        """Load one exact persisted plan and verify its content identity."""

        if not _SHA256.fullmatch(plan_id):
            return None
        with closing(self.state.connect()) as connection:
            if not self._table_exists(connection, "storage_lifecycle_plan"):
                return None
            row = connection.execute(
                "SELECT plan_json FROM storage_lifecycle_plan WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            plan = StorageLifecyclePlan.model_validate_json(str(row[0]))
        except ValueError:
            return None
        expected = content_hash(
            self._plan_identity_payload(plan.candidates, scan_truncated=plan.scan_truncated)
        )
        if expected != plan_id or plan.plan_id != plan_id:
            return None
        return plan

    def record_audit(self, report: StorageLifecycleAudit) -> None:
        """Record one immutable safety audit receipt for operational traceability."""

        created_at = datetime.now(UTC).isoformat()
        run_id = content_hash(
            {
                "kind": "storage_lifecycle_audit",
                "plan_id": report.plan_id,
                "status": report.status,
                "finding_codes": report.finding_codes,
                "created_at": created_at,
            }
        )
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO storage_lifecycle_audit_run("
                "run_id,plan_id,run_kind,status,finding_codes_json,eligible_file_count,eligible_bytes,"
                "deleted_file_count,deleted_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    report.plan_id,
                    "AUDIT",
                    report.status,
                    json.dumps(report.finding_codes, ensure_ascii=False),
                    report.eligible_file_count,
                    report.eligible_bytes,
                    0,
                    0,
                    created_at,
                ),
            )

    def record_slo_snapshot(self, report: OperationsSLOReport) -> None:
        """Persist a bounded SLO snapshot without creating another metrics store."""

        snapshot_id = content_hash(report.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO operations_slo_snapshot("
                "snapshot_id,status,finding_codes_json,evidence_freshness_status,"
                "latest_evidence_age_seconds,provider_degraded_count,open_circuit_count,"
                "report_total_count,report_published_count,report_success_rate,"
                "monitor_pending_task_count,monitor_failed_task_count,runtime_bytes,"
                "object_store_bytes,temp_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    report.status,
                    json.dumps(report.finding_codes, ensure_ascii=False),
                    report.evidence_freshness_status,
                    report.latest_evidence_age_seconds,
                    report.provider_degraded_count,
                    report.open_circuit_count,
                    report.report_total_count,
                    report.report_published_count,
                    report.report_success_rate,
                    report.monitor_pending_task_count,
                    report.monitor_failed_task_count,
                    report.runtime_bytes,
                    report.object_store_bytes,
                    report.temp_bytes,
                    report.generated_at.isoformat(),
                ),
            )

    def _plan_identity_payload(
        self,
        candidates: list[StorageCandidate],
        *,
        scan_truncated: bool,
    ) -> dict[str, object]:
        return {
            "schema_version": "storage-lifecycle-plan-v1",
            "policy": self.policy.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "scan_truncated": scan_truncated,
        }
    def audit(self, plan: StorageLifecyclePlan) -> StorageLifecycleAudit:
        findings: set[str] = set()
        blocking: set[str] = set()
        if plan.scan_truncated:
            findings.add("SCAN_TRUNCATED")
        for item in plan.candidates:
            path = (self.paths.root / item.relative_path).resolve()
            if not self._allowed_candidate_path(path, item.category):
                findings.add("CANDIDATE_OUTSIDE_ALLOWED_ROOT")
                blocking.add("CANDIDATE_OUTSIDE_ALLOWED_ROOT")
            if item.referenced and item.eligible:
                findings.add("REFERENCED_CANDIDATE_MARKED_FOR_DELETE")
                blocking.add("REFERENCED_CANDIDATE_MARKED_FOR_DELETE")
        return StorageLifecycleAudit(
            plan_id=plan.plan_id,
            status="PASS" if not blocking else "FAIL",
            finding_codes=sorted(findings),
            protected_referenced_objects=sum(
                item.category == "OBJECT_STORE" and item.referenced for item in plan.candidates
            ),
            eligible_file_count=plan.eligible_file_count,
            eligible_bytes=plan.eligible_bytes,
        )

    def run(self, plan: StorageLifecyclePlan, *, confirm: bool) -> StorageLifecycleRun:
        if not confirm:
            raise ValueError("storage lifecycle deletion requires explicit confirmation")
        audit = self.audit(plan)
        if audit.status != "PASS":
            raise ValueError("storage lifecycle plan failed audit")
        referenced = self._referenced_hashes()
        referenced_reports = self._referenced_report_output_names()
        active_staging_keys = self._active_report_staging_keys()
        deleted_count = 0
        deleted_bytes = 0
        skipped: list[str] = []
        for item in plan.candidates:
            if not item.eligible:
                continue
            path = (self.paths.root / item.relative_path).resolve()
            if not self._allowed_candidate_path(path, item.category):
                skipped.append(f"OUTSIDE_ALLOWED_ROOT:{item.relative_path}")
                continue
            if item.category == "OBJECT_STORE" and path.name in referenced:
                skipped.append(f"BECAME_REFERENCED:{item.relative_path}")
                continue
            if item.category == "REPORT_OUTPUT" and path.name in referenced_reports:
                skipped.append(f"BECAME_REFERENCED_REPORT:{item.relative_path}")
                continue
            if item.category == "REPORT_STAGING":
                try:
                    relative = path.relative_to(self.paths.report_staging.resolve())
                    report_key = relative.parts[0] if relative.parts else ""
                except ValueError:
                    report_key = ""
                if report_key in active_staging_keys:
                    skipped.append(f"BECAME_ACTIVE_STAGING:{item.relative_path}")
                    continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                skipped.append(f"ALREADY_MISSING:{item.relative_path}")
                continue
            except OSError:
                skipped.append(f"STAT_FAILED:{item.relative_path}")
                continue
            if stat.st_mtime_ns != item.mtime_ns or stat.st_size != item.byte_size:
                skipped.append(f"CHANGED_SINCE_PLAN:{item.relative_path}")
                continue
            try:
                path.unlink()
            except OSError:
                skipped.append(f"DELETE_FAILED:{item.relative_path}")
                continue
            deleted_count += 1
            deleted_bytes += item.byte_size
        return StorageLifecycleRun(
            plan_id=plan.plan_id,
            confirmed=True,
            deleted_file_count=deleted_count,
            deleted_bytes=deleted_bytes,
            skipped_file_count=len(skipped),
            skip_reasons=sorted(skipped),
        )

    def record_run(self, run: StorageLifecycleRun) -> None:
        """Persist one cleanup execution receipt without mutating canonical facts."""

        created_at = datetime.now(UTC).isoformat()
        run_id = content_hash(
            {
                "kind": "storage_lifecycle_execution",
                "plan_id": run.plan_id,
                "deleted_file_count": run.deleted_file_count,
                "deleted_bytes": run.deleted_bytes,
                "skip_reasons": run.skip_reasons,
                "created_at": created_at,
            }
        )
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO storage_lifecycle_audit_run("
                "run_id,plan_id,run_kind,status,finding_codes_json,eligible_file_count,eligible_bytes,"
                "deleted_file_count,deleted_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    run.plan_id,
                    "EXECUTION",
                    "PASS",
                    json.dumps(run.skip_reasons, ensure_ascii=False),
                    0,
                    0,
                    run.deleted_file_count,
                    run.deleted_bytes,
                    created_at,
                ),
            )

    def operations_slo_report(
        self,
        *,
        now: datetime | None = None,
        baseline_runtime_bytes: int | None = None,
        baseline_object_store_bytes: int | None = None,
        baseline_temp_bytes: int | None = None,
        baseline_report_bytes: int | None = None,
    ) -> OperationsSLOReport:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        with closing(self.state.connect()) as connection:
            provider_degraded = self._count(
                connection,
                "provider_health",
                "status NOT IN ('HEALTHY','NOT_PROBED')",
            )
            open_circuit = self._count(
                connection,
                "source_circuit_breaker",
                "state='OPEN'",
            )
            route_count = self._count(connection, "research_production_route_index")
            formal_coverage = self._count(
                connection,
                "artifact_registry",
                "type='UniverseCoverageProof'",
            )
            report_total = self._count(connection, "report_manifest")
            report_published = self._count(
                connection, "report_manifest", "publish_status='PUBLISHED'"
            )
            report_recovered = self._count(
                connection, "report_manifest", "recovered_existing=1"
            )
            pending_tasks = self._count(
                connection,
                "continuous_monitor_task",
                "status IN ('PENDING','CLAIMED')",
            )
            failed_tasks = self._count(
                connection, "continuous_monitor_task", "status='FAILED'"
            )
            failed_runs = self._count(
                connection, "continuous_monitor_run", "status='FAILED'"
            )
            partial_runs = self._count(
                connection, "continuous_monitor_run", "status='PARTIAL'"
            )
            skill_cost = self._sum(connection, "skill_usage_event_index", "token_cost")
            latest_availability = self._scalar(
                connection,
                "SELECT MAX(availability_at) FROM source_snapshot_index "
                "WHERE fetch_status='SUCCEEDED'",
            )
            run_rows = self._rows(
                connection,
                "SELECT started_at,ended_at,status FROM continuous_monitor_run "
                "ORDER BY started_at ASC LIMIT 200",
            )

        latest_age: int | None = None
        if latest_availability:
            try:
                latest = datetime.fromisoformat(str(latest_availability)).astimezone(UTC)
                latest_age = max(0, int((instant - latest).total_seconds()))
            except ValueError:
                latest_age = None
        fresh_target = self.policy.operations_slo.evidence_freshness_target_seconds
        freshness: Literal["PASS", "WARN", "UNKNOWN"] = (
            "UNKNOWN"
            if latest_age is None
            else "PASS"
            if latest_age <= fresh_target
            else "WARN"
        )
        durations: list[float] = []
        recoveries: list[float] = []
        recovery_started: datetime | None = None
        for row in run_rows:
            try:
                started = datetime.fromisoformat(str(row[0]))
                ended = datetime.fromisoformat(str(row[1]))
                status = str(row[2])
            except (TypeError, ValueError):
                continue
            durations.append(max(0.0, (ended - started).total_seconds()))
            if status in {"FAILED", "PARTIAL"} and recovery_started is None:
                recovery_started = ended
            elif status == "SUCCEEDED" and recovery_started is not None:
                recoveries.append(max(0.0, (ended - recovery_started).total_seconds()))
                recovery_started = None
        report_rate = report_published / report_total if report_total else None
        skill_tokens_per_route = skill_cost / route_count if route_count else None
        runtime_bytes = self._bounded_tree_size(self.paths.runtime, 100_000)
        object_bytes = self._bounded_tree_size(self.paths.objects, 100_000)
        temp_bytes = self._bounded_tree_size(self.paths.runtime / "tmp", 100_000)
        report_bytes = self._bounded_tree_size(self.paths.reports, 100_000)
        runtime_growth = (
            runtime_bytes - baseline_runtime_bytes
            if baseline_runtime_bytes is not None
            else None
        )
        object_growth = (
            object_bytes - baseline_object_store_bytes
            if baseline_object_store_bytes is not None
            else None
        )
        temp_growth = temp_bytes - baseline_temp_bytes if baseline_temp_bytes is not None else None
        report_growth = (
            report_bytes - baseline_report_bytes if baseline_report_bytes is not None else None
        )
        findings: set[str] = set()
        if freshness == "WARN":
            findings.add("EVIDENCE_FRESHNESS_SLO_MISSED")
        if provider_degraded >= self.policy.operations_slo.provider_degraded_warning > 0:
            findings.add("PROVIDER_DEGRADATION_PRESENT")
        if pending_tasks >= self.policy.operations_slo.monitor_backlog_warning > 0:
            findings.add("MONITOR_BACKLOG_HIGH")
        if (
            report_rate is not None
            and report_rate < self.policy.operations_slo.report_success_rate_target
        ):
            findings.add("REPORT_SUCCESS_RATE_LOW")
        if self._watermark(runtime_bytes, object_bytes, temp_bytes, report_bytes) != "OK":
            findings.add("STORAGE_WATERMARK_EXCEEDED")
        return OperationsSLOReport(
            generated_at=instant,
            universe_formal_coverage_artifact_count=formal_coverage,
            provider_degraded_count=provider_degraded,
            open_circuit_count=open_circuit,
            latest_evidence_age_seconds=latest_age,
            evidence_freshness_status=freshness,
            research_route_count=route_count,
            report_total_count=report_total,
            report_published_count=report_published,
            report_success_rate=round(report_rate, 6) if report_rate is not None else None,
            report_recovered_count=report_recovered,
            monitor_pending_task_count=pending_tasks,
            monitor_failed_task_count=failed_tasks,
            monitor_failed_run_count=failed_runs,
            monitor_partial_run_count=partial_runs,
            mean_monitor_run_seconds=(
                round(sum(durations) / len(durations), 6) if durations else None
            ),
            monitor_recovery_observation_count=len(recoveries),
            mean_monitor_recovery_seconds=(
                round(sum(recoveries) / len(recoveries), 6) if recoveries else None
            ),
            skill_token_cost_total=skill_cost,
            skill_tokens_per_research_route=(
                round(skill_tokens_per_route, 6) if skill_tokens_per_route is not None else None
            ),
            runtime_bytes=runtime_bytes,
            object_store_bytes=object_bytes,
            temp_bytes=temp_bytes,
            report_bytes=report_bytes,
            runtime_growth_bytes=runtime_growth,
            object_store_growth_bytes=object_growth,
            temp_growth_bytes=temp_growth,
            report_growth_bytes=report_growth,
            finding_codes=sorted(findings),
            status="WARN" if findings else "PASS",
        )

    def _referenced_hashes(self) -> set[str]:
        result: set[str] = set()
        with closing(self.state.connect()) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table in tables:
                columns = [
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                ]
                for column in columns:
                    lower = column.lower()
                    if "hash" not in lower:
                        continue
                    try:
                        rows = connection.execute(
                            f'SELECT DISTINCT "{column}" FROM "{table}" '
                            f'WHERE "{column}" IS NOT NULL LIMIT 100000'
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        self._collect_hashes(row[0], result)
        return result

    @staticmethod
    def _collect_hashes(value: object, target: set[str]) -> None:
        if value is None:
            return
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                return
        text = str(value)
        if _SHA256.fullmatch(text):
            target.add(text)
            return
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            stack = [parsed]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
                elif isinstance(current, str) and _SHA256.fullmatch(current):
                    target.add(current)

    def _candidate(
        self,
        category: _StorageCategory,
        path: Path,
        stat: os.stat_result,
        *,
        referenced: bool,
        eligible: bool,
        reason: str,
    ) -> StorageCandidate:
        relative = path.resolve().relative_to(self.paths.root.resolve()).as_posix()
        return StorageCandidate(
            category=category,
            relative_path=relative,
            byte_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            referenced=referenced,
            eligible=eligible,
            reason=reason,
        )

    @staticmethod
    def _scan_files(root: Path, limit: int) -> tuple[list[tuple[Path, os.stat_result]], bool]:
        if not root.exists():
            return [], False
        rows: list[tuple[Path, os.stat_result]] = []
        truncated = False
        try:
            iterator = root.rglob("*")
            for path in iterator:
                if not path.is_file():
                    continue
                try:
                    rows.append((path, path.stat()))
                except OSError:
                    continue
                if len(rows) >= limit:
                    truncated = True
                    break
        except OSError:
            return rows, True
        return rows, truncated

    @staticmethod
    def _bounded_tree_size(root: Path, limit: int) -> int:
        rows, _ = StorageLifecycleService._scan_files(root, limit)
        return sum(stat.st_size for _, stat in rows)

    def _active_report_staging_keys(self) -> set[str]:
        with closing(self.state.connect()) as connection:
            if not self._table_exists(connection, "checkpoint"):
                return set()
            rows = connection.execute(
                "SELECT scope_key FROM checkpoint WHERE scope_type='report' "
                "AND status IN ('PENDING','STAGED') LIMIT 10000"
            ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def _referenced_report_output_names(self) -> set[str]:
        with closing(self.state.connect()) as connection:
            if not self._table_exists(connection, "report_manifest"):
                return set()
            rows = connection.execute(
                "SELECT output_file_name FROM report_manifest "
                "WHERE output_file_name IS NOT NULL "
                "AND publish_status IN ('STAGED','PUBLISHED','DEGRADED') LIMIT 100000"
            ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def _active_log_name(self) -> str:
        try:
            raw = yaml.safe_load(self.paths.logging_policy.read_text(encoding="utf-8"))
            return str(raw.get("file_name") or "astock-operational.jsonl")
        except (OSError, AttributeError, yaml.YAMLError):
            return "astock-operational.jsonl"

    def _watermark(
        self,
        runtime_bytes: int,
        object_bytes: int,
        temp_bytes: int,
        report_bytes: int,
    ) -> Literal["OK", "WARNING", "CRITICAL"]:
        marks = self.policy.watermarks
        if runtime_bytes >= marks.runtime_critical_bytes:
            return "CRITICAL"
        if (
            runtime_bytes >= marks.runtime_warning_bytes
            or object_bytes >= marks.object_store_warning_bytes
            or temp_bytes >= marks.temp_warning_bytes
            or report_bytes >= marks.report_warning_bytes
        ):
            return "WARNING"
        return "OK"

    def _allowed_candidate_path(self, path: Path, category: str) -> bool:
        roots = {
            "OBJECT_STORE": self.paths.objects,
            "RUNTIME_TMP": self.paths.runtime / "tmp",
            "REPORT_STAGING": self.paths.report_staging,
            "REPORT_OUTPUT": self.paths.reports / "output",
            "LOG_BACKUP": self.paths.logs,
        }
        root = roots.get(category)
        if root is None:
            return False
        try:
            path.resolve().relative_to(root.resolve())
            return path.is_file() or not path.exists()
        except ValueError:
            return False

    @staticmethod
    def _table_exists(connection: object, table: str) -> bool:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def _count(self, connection: object, table: str, where: str | None = None) -> int:
        if not self._table_exists(connection, table):
            return 0
        query = f'SELECT COUNT(*) FROM "{table}"'
        if where:
            query += f" WHERE {where}"
        return int(connection.execute(query).fetchone()[0])  # type: ignore[attr-defined]

    def _sum(self, connection: object, table: str, column: str) -> int:
        if not self._table_exists(connection, table):
            return 0
        row = connection.execute(  # type: ignore[attr-defined]
            f'SELECT COALESCE(SUM("{column}"),0) FROM "{table}"'
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _scalar(connection: object, query: str) -> object | None:
        try:
            row = connection.execute(query).fetchone()  # type: ignore[attr-defined]
        except Exception:
            return None
        return row[0] if row is not None else None

    @staticmethod
    def _rows(connection: object, query: str) -> list[sqlite3.Row]:
        try:
            rows = connection.execute(query).fetchall()  # type: ignore[attr-defined]
        except Exception:
            return []
        return [row for row in rows if isinstance(row, sqlite3.Row)]


__all__ = [
    "OperationsSLOReport",
    "StorageCandidate",
    "StorageLifecycleAudit",
    "StorageLifecyclePlan",
    "StorageLifecyclePolicy",
    "StorageLifecycleRun",
    "StorageLifecycleService",
    "load_storage_lifecycle_policy",
]
