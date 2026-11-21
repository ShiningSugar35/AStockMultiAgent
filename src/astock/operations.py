"""Bounded storage lifecycle planning and operational SLO reporting."""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shutil
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
from astock.storage_retention import (
    active_linked_worktree_roots,
    iter_matching_tree_roots,
    path_has_redirect,
    process_identity,
    remove_tree,
    snapshot_tree,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_SCAN_LIMIT = 100_000
_StorageCategory = Literal[
    "TASK_SCRATCHPAD",
    "OBJECT_STORE",
    "RUNTIME_TMP",
    "REPORT_STAGING",
    "REPORT_OUTPUT",
    "LOG_BACKUP",
    "QUALITY_RUN_TMP",
    "LONGRUN_SCRATCH",
    "STALE_WORKTREE",
    "RUNTIME_TEST_SCRATCH",
    "AI_BRIDGE_TMP",
    "QUALITY_RUN_EVIDENCE",
    "REVIEWED_SCRATCH",
]
_AUTO_TREE_CATEGORIES = {
    "TASK_SCRATCHPAD",
    "QUALITY_RUN_TMP",
    "LONGRUN_SCRATCH",
    "STALE_WORKTREE",
    "RUNTIME_TEST_SCRATCH",
    "AI_BRIDGE_TMP",
    "QUALITY_RUN_EVIDENCE",
    "REVIEWED_SCRATCH",
}
_REVIEWED_SCRATCH_ROOTS = (
    "scratch", ".scratchpad", ".ai-bridge/reviews", "runtime/longrun",
    "runtime/acceptance", "runtime/baseline-gate-check", "runtime/validation",
    "runtime/pytest", "runtime/tool-cache", "runtime/owner-override",
    "runtime/live-validation-v2", "runtime/phase6-evidence-run", "runtime/workers",
    "runtime/tmp", "runtime/backups", "runtime/uv-cache",
)
_TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "ABORTED"}


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


class StorageTreePolicy(_Model):
    retention_hours: int = Field(ge=1)
    orphan_retention_hours: int | None = Field(default=None, ge=1)
    patterns: list[str] = Field(default_factory=list)
    max_depth: int = Field(default=1, ge=1, le=8)
    keep_latest: int = Field(default=0, ge=0, le=1000)
    scan_limit: int = Field(default=10_000, ge=1, le=100_000)


class StorageAutomationPolicy(_Model):
    enabled: bool
    interval_hours: int = Field(ge=1, le=168)
    max_delete_bytes_per_run: int = Field(ge=1)


class StorageWatermarks(_Model):
    runtime_warning_bytes: int = Field(ge=0)
    runtime_critical_bytes: int = Field(ge=0)
    object_store_warning_bytes: int = Field(ge=0)
    report_warning_bytes: int = Field(ge=0)
    temp_warning_bytes: int = Field(ge=0)
    ephemeral_warning_bytes: int = Field(default=4 * 1024**3, ge=0)
    ephemeral_critical_bytes: int = Field(default=8 * 1024**3, ge=0)
    volume_free_warning_bytes: int = Field(default=40 * 1024**3, ge=0)
    volume_free_critical_bytes: int = Field(default=20 * 1024**3, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> StorageWatermarks:
        if self.runtime_critical_bytes < self.runtime_warning_bytes:
            raise ValueError("runtime critical watermark must be >= warning watermark")
        if self.ephemeral_critical_bytes < self.ephemeral_warning_bytes:
            raise ValueError("ephemeral critical watermark must be >= warning watermark")
        if self.volume_free_critical_bytes > self.volume_free_warning_bytes:
            raise ValueError("volume critical free watermark must be <= warning watermark")
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
    quality_run_tmp: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=2,
            orphan_retention_hours=24,
            patterns=["tmp"],
            max_depth=2,
        )
    )
    longrun_scratch: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=24,
            patterns=["pytest*", "cache-*", "process-tmp", "tmp*"],
            max_depth=5,
        )
    )
    stale_worktrees: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=48, patterns=["*"], max_depth=1
        )
    )
    runtime_test_scratch: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=24,
            patterns=["pytest-*", "uv-cache-*"],
            max_depth=1,
        )
    )
    ai_bridge_tmp: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=24, patterns=["tmp*"], max_depth=1
        )
    )
    quality_run_evidence: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=720, orphan_retention_hours=720,
            patterns=["*"], max_depth=1, keep_latest=20,
        )
    )
    reviewed_scratch: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(
            retention_hours=24, patterns=["*"], max_depth=4,
        )
    )
    task_scratchpad: StorageTreePolicy = Field(
        default_factory=lambda: StorageTreePolicy(retention_hours=1, patterns=["*"], max_depth=1)
    )
    automation: StorageAutomationPolicy = Field(
        default_factory=lambda: StorageAutomationPolicy(
            enabled=True,
            interval_hours=6,
            max_delete_bytes_per_run=20 * 1024**3,
        )
    )
    watermarks: StorageWatermarks
    operations_slo: OperationsSLOPolicy


class StorageCandidate(_Model):
    category: _StorageCategory
    relative_path: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    entry_type: Literal["FILE", "TREE"] = "FILE"
    file_count: int = Field(default=1, ge=0)
    dir_count: int = Field(default=0, ge=0)
    referenced: bool = False
    tree_fingerprint: str | None = None
    scan_complete: bool = True
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
    ephemeral_bytes: int = Field(default=0, ge=0)
    volume_free_bytes: int = Field(default=0, ge=0)
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
    deleted_directory_count: int = Field(default=0, ge=0)
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

        tree_candidates = self._ephemeral_tree_candidates(instant)
        candidates.extend(tree_candidates)
        scanned_count += sum(item.file_count for item in tree_candidates)
        scanned_bytes += sum(item.byte_size for item in tree_candidates)

        candidates.sort(key=lambda item: (item.category, item.relative_path))
        eligible = [item for item in candidates if item.eligible]
        sizes = [snapshot_tree(path) for path in (
            self.paths.runtime, self.paths.objects, self.paths.runtime / "tmp", self.paths.reports,
        )]
        runtime_bytes, object_store_bytes, temp_bytes, report_bytes = (
            value.byte_size for value in sizes
        )
        truncated |= not all(value.complete for value in sizes)
        ephemeral_bytes = self._nonoverlapping_tree_bytes(tree_candidates)
        volume_free_bytes = shutil.disk_usage(self.paths.root).free
        watermark_status = self._watermark(
            runtime_bytes,
            object_store_bytes,
            temp_bytes,
            report_bytes,
            ephemeral_bytes,
            volume_free_bytes,
        )
        identity = self._plan_identity_payload(candidates, scan_truncated=truncated)
        return StorageLifecyclePlan(
            plan_id=content_hash(identity),
            generated_at=instant,
            candidates=candidates,
            scanned_file_count=scanned_count,
            scanned_bytes=scanned_bytes,
            eligible_file_count=sum(
                item.file_count if item.entry_type == "TREE" else 1 for item in eligible
            ),
            eligible_bytes=sum(item.byte_size for item in eligible),
            referenced_object_count=sum(
                item.category == "OBJECT_STORE" and item.referenced for item in candidates
            ),
            scan_truncated=truncated,
            runtime_bytes=runtime_bytes,
            object_store_bytes=object_store_bytes,
            temp_bytes=temp_bytes,
            report_bytes=report_bytes,
            ephemeral_bytes=ephemeral_bytes,
            volume_free_bytes=volume_free_bytes,
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
        """Record one immutable, idempotent safety-audit receipt."""

        run_id = content_hash(
            {
                "kind": "storage_lifecycle_audit",
                "plan_id": report.plan_id,
                "status": report.status,
                "finding_codes": report.finding_codes,
            }
        )
        self._record_storage_receipt(
            run_id=run_id,
            plan_id=report.plan_id,
            run_kind="AUDIT",
            status=report.status,
            finding_codes_json=json.dumps(report.finding_codes, ensure_ascii=False),
            eligible_file_count=report.eligible_file_count,
            eligible_bytes=report.eligible_bytes,
            deleted_file_count=0,
            deleted_bytes=0,
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
            path = self.paths.root / item.relative_path
            if not self._allowed_candidate_path(path, item.category):
                findings.add("CANDIDATE_OUTSIDE_ALLOWED_ROOT")
                blocking.add("CANDIDATE_OUTSIDE_ALLOWED_ROOT")
            if item.entry_type == "TREE" and item.category not in _AUTO_TREE_CATEGORIES:
                findings.add("TREE_CATEGORY_NOT_EPHEMERAL")
                blocking.add("TREE_CATEGORY_NOT_EPHEMERAL")
            if item.entry_type == "FILE" and item.category in _AUTO_TREE_CATEGORIES:
                findings.add("EPHEMERAL_TREE_CATEGORY_NOT_TREE")
                blocking.add("EPHEMERAL_TREE_CATEGORY_NOT_TREE")
            if not item.scan_complete and item.eligible:
                findings.add("INCOMPLETE_TREE_MARKED_FOR_DELETE")
                blocking.add("INCOMPLETE_TREE_MARKED_FOR_DELETE")
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
        selected = {item.category for item in plan.candidates if item.eligible}
        referenced = self._referenced_hashes() if "OBJECT_STORE" in selected else set()
        referenced_reports = (
            self._referenced_report_output_names() if "REPORT_OUTPUT" in selected else set()
        )
        active_staging_keys = (
            self._active_report_staging_keys() if "REPORT_STAGING" in selected else set()
        )
        retention_refs, refs_complete = self._retention_references()
        deleted_count = 0
        deleted_directories = 0
        deleted_bytes = 0
        skipped: list[str] = []
        for item in plan.candidates:
            if not item.eligible:
                continue
            path = self.paths.root / item.relative_path
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
            if item.entry_type == "TREE":
                owner_reason = self._ancestor_owner_protection(path)
                if owner_reason:
                    skipped.append(f"{owner_reason}:{item.relative_path}")
                    continue
                if item.category in {
                    "TASK_SCRATCHPAD", "REVIEWED_SCRATCH", "STALE_WORKTREE",
                } and not (
                    self._task_scratchpad_terminal(path)
                ):
                    skipped.append(f"TASK_NOT_DISPOSABLE:{item.relative_path}")
                    continue
                if not path.exists():
                    skipped.append(f"ALREADY_MISSING:{item.relative_path}")
                    continue
                if item.category == "STALE_WORKTREE" and path in active_linked_worktree_roots(
                    self.paths.root
                ):
                    skipped.append(f"BECAME_ACTIVE_WORKTREE:{item.relative_path}")
                    continue
                if item.category in {"QUALITY_RUN_TMP", "QUALITY_RUN_EVIDENCE"}:
                    run_root = path.parent if item.category == "QUALITY_RUN_TMP" else path
                    if self._quality_run_state(run_root) not in {"TERMINAL", "ORPHAN"}:
                        skipped.append(f"QUALITY_RUN_OWNER_UNVERIFIED:{item.relative_path}")
                        continue
                    if item.category == "QUALITY_RUN_EVIDENCE" and (
                        not refs_complete or self._reference_mentions(run_root, retention_refs)
                    ):
                        skipped.append(f"BECAME_REFERENCED_QUALITY_RUN:{item.relative_path}")
                        continue
                current = snapshot_tree(path)
                if (
                    not current.complete
                    or current.byte_size != item.byte_size
                    or current.file_count != item.file_count
                    or current.dir_count != item.dir_count
                    or current.latest_mtime_ns != item.mtime_ns
                    or (item.tree_fingerprint is not None
                        and current.fingerprint != item.tree_fingerprint)
                ):
                    skipped.append(f"CHANGED_SINCE_PLAN:{item.relative_path}")
                    continue
                try:
                    remove_tree(path)
                except OSError:
                    skipped.append(f"DELETE_FAILED:{item.relative_path}")
                    continue
                deleted_count += item.file_count
                deleted_directories += item.dir_count + 1
                deleted_bytes += item.byte_size
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
            deleted_directory_count=deleted_directories,
            skipped_file_count=len(skipped),
            skip_reasons=sorted(skipped),
        )

    def record_run(self, run: StorageLifecycleRun) -> None:
        """Persist one immutable, idempotent cleanup-execution receipt."""

        run_id = content_hash(
            {
                "kind": "storage_lifecycle_execution",
                "plan_id": run.plan_id,
                "deleted_file_count": run.deleted_file_count,
                "deleted_directory_count": run.deleted_directory_count,
                "deleted_bytes": run.deleted_bytes,
                "skip_reasons": run.skip_reasons,
            }
        )
        self._record_storage_receipt(
            run_id=run_id,
            plan_id=run.plan_id,
            run_kind="EXECUTION",
            status="PASS",
            finding_codes_json=json.dumps(run.skip_reasons, ensure_ascii=False),
            eligible_file_count=0,
            eligible_bytes=0,
            deleted_file_count=run.deleted_file_count,
            deleted_bytes=run.deleted_bytes,
        )

    def _record_storage_receipt(
        self,
        *,
        run_id: str,
        plan_id: str,
        run_kind: Literal["AUDIT", "EXECUTION"],
        status: Literal["PASS", "FAIL"],
        finding_codes_json: str,
        eligible_file_count: int,
        eligible_bytes: int,
        deleted_file_count: int,
        deleted_bytes: int,
    ) -> None:
        """Insert once and verify any existing semantic receipt before reuse."""

        expected = (
            plan_id,
            run_kind,
            status,
            finding_codes_json,
            eligible_file_count,
            eligible_bytes,
            deleted_file_count,
            deleted_bytes,
        )
        created_at = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO storage_lifecycle_audit_run("
                "run_id,plan_id,run_kind,status,finding_codes_json,eligible_file_count,eligible_bytes,"
                "deleted_file_count,deleted_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, *expected, created_at),
            )
            row = connection.execute(
                "SELECT plan_id,run_kind,status,finding_codes_json,eligible_file_count,"
                "eligible_bytes,deleted_file_count,deleted_bytes "
                "FROM storage_lifecycle_audit_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("storage lifecycle receipt disappeared after insert")
            actual = tuple(row[index] for index in range(8))
            if actual != expected:
                raise ValueError("storage lifecycle receipt identity collision")

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
        runtime_bytes = snapshot_tree(self.paths.runtime).byte_size
        object_bytes = snapshot_tree(self.paths.objects).byte_size
        temp_bytes = snapshot_tree(self.paths.runtime / "tmp").byte_size
        report_bytes = snapshot_tree(self.paths.reports).byte_size
        ephemeral_bytes = sum(
            item.byte_size for item in self._ephemeral_tree_candidates(instant)
        )
        volume_free_bytes = shutil.disk_usage(self.paths.root).free
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
        if (
            self._watermark(
                runtime_bytes,
                object_bytes,
                temp_bytes,
                report_bytes,
                ephemeral_bytes,
                volume_free_bytes,
            )
            != "OK"
        ):
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
                            f'WHERE "{column}" IS NOT NULL LIMIT {_REFERENCE_SCAN_LIMIT + 1}'
                        ).fetchall()
                    except sqlite3.Error as exc:
                        raise ValueError("object reference inventory is incomplete") from exc
                    if len(rows) > _REFERENCE_SCAN_LIMIT:
                        raise ValueError("object reference inventory exceeds its bounded scan")
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

    def _ephemeral_tree_candidates(self, instant: datetime) -> list[StorageCandidate]:
        candidates: list[StorageCandidate] = []

        retention_refs, refs_complete = self._retention_references()

        # A task explicitly marks disposable scratch after its work has completed.
        # Unknown or active directories remain untouched regardless of age.
        for target in iter_matching_tree_roots(
            self.paths.root / ".scratchpad",
            self.policy.task_scratchpad.patterns,
            max_depth=self.policy.task_scratchpad.max_depth,
        ):
            ready = self._task_scratchpad_terminal(target)
            scratch_policy = self.policy.task_scratchpad
            if target.name.startswith("quality-"):
                run_root = self.paths.root / ".ai-bridge" / "quality-runs" / (
                    target.name.removeprefix("quality-")
                )
                if self._quality_run_state(run_root) == "ORPHAN":
                    hours = self.policy.quality_run_tmp.orphan_retention_hours
                    ready = ready and hours is not None
                    if hours is not None:
                        scratch_policy = scratch_policy.model_copy(
                            update={"retention_hours": hours}
                        )
            candidates.append(self._tree_candidate(
                "TASK_SCRATCHPAD", target, scratch_policy, instant,
                referenced=not ready, protected_reason="TASK_SCRATCHPAD_OWNERSHIP_UNVERIFIED",
            ))

        quality_roots = [self.paths.root / ".ai-bridge" / "quality-runs"]
        # A registered worktree protects source, not its own expired pytest workspace.
        # Reuse identical TTL/orphan/change-since-plan checks; never recurse into source.
        worktree_parent = (self.paths.runtime / "worktrees").resolve()
        for linked in sorted(active_linked_worktree_roots(self.paths.root)):
            if (
                linked.parent == worktree_parent
                and not linked.is_symlink()
                and not linked.is_junction()
            ):
                quality_roots.append(linked / ".ai-bridge" / "quality-runs")
        for quality_root in quality_roots:
            bridge_root = quality_root.parent
            if (
                not quality_root.is_dir()
                or bridge_root.is_symlink()
                or bridge_root.is_junction()
                or quality_root.is_symlink()
                or quality_root.is_junction()
            ):
                continue
            try:
                quality_runs = list(quality_root.iterdir())
            except OSError:
                quality_runs = []
            quality_runs = sorted(
                (path for path in quality_runs if path.is_dir()),
                key=lambda path: path.name, reverse=True,
            )
            retained = set(quality_runs[:self.policy.quality_run_evidence.keep_latest])
            for run_root in quality_runs[:self.policy.quality_run_evidence.scan_limit]:
                if path_has_redirect(self.paths.root, run_root):
                    continue
                state = self._quality_run_state(run_root)
                terminal = state == "TERMINAL"
                orphan = state == "ORPHAN"
                evidence_protected = (
                    not (terminal or orphan) or run_root in retained
                    or not refs_complete or self._reference_mentions(run_root, retention_refs)
                )
                evidence = self._tree_candidate(
                    "QUALITY_RUN_EVIDENCE", run_root,
                    self.policy.quality_run_evidence, instant,
                    referenced=evidence_protected,
                    protected_reason=(
                        "QUALITY_RUN_OWNER_UNVERIFIED" if not (terminal or orphan)
                        else "QUALITY_RUN_RECENT_PROTECTED" if run_root in retained
                        else "QUALITY_RUN_REFERENCE_PROTECTED"
                    ),
                )
                candidates.append(evidence)
                target = run_root / "tmp"
                if not target.is_dir() or path_has_redirect(self.paths.root, target):
                    continue
                quality_policy = self.policy.quality_run_tmp
                hours = quality_policy.orphan_retention_hours if orphan else None
                policy = (
                    quality_policy.model_copy(update={"retention_hours": hours})
                    if hours is not None else quality_policy
                )
                candidate = self._tree_candidate(
                    "QUALITY_RUN_TMP", target, policy, instant,
                    referenced=not (terminal or (orphan and hours is not None)),
                    protected_reason="QUALITY_RUN_NOT_TERMINAL_PROTECTED",
                )
                if orphan and candidate.eligible:
                    candidate = candidate.model_copy(
                        update={"reason": "QUALITY_RUN_ORPHAN_EXPIRED"}
                    )
                candidates.append(candidate)

        longrun_root = self.paths.runtime / "longrun"
        for target in iter_matching_tree_roots(
            longrun_root,
            self.policy.longrun_scratch.patterns,
            max_depth=self.policy.longrun_scratch.max_depth,
        ):
            candidates.append(
                self._tree_candidate(
                    "LONGRUN_SCRATCH",
                    target,
                    self.policy.longrun_scratch,
                    instant,
                )
            )

        active_worktrees = active_linked_worktree_roots(self.paths.root)
        worktree_root = self.paths.runtime / "worktrees"
        if worktree_root.is_dir():
            try:
                worktrees = list(worktree_root.iterdir())
            except OSError:
                worktrees = []
            for target in worktrees:
                if not target.is_dir() or target.is_symlink():
                    continue
                active = target.resolve() in active_worktrees
                candidates.append(
                    self._tree_candidate(
                        "STALE_WORKTREE",
                        target,
                        self.policy.stale_worktrees,
                        instant,
                        referenced=active or not self._task_scratchpad_terminal(target),
                        protected_reason=(
                            "ACTIVE_GIT_WORKTREE_PROTECTED" if active
                            else "WORKTREE_OWNERSHIP_UNVERIFIED"
                        ),
                    )
                )

        for target in iter_matching_tree_roots(
            self.paths.runtime,
            self.policy.runtime_test_scratch.patterns,
            max_depth=self.policy.runtime_test_scratch.max_depth,
        ):
            candidates.append(
                self._tree_candidate(
                    "RUNTIME_TEST_SCRATCH",
                    target,
                    self.policy.runtime_test_scratch,
                    instant,
                )
            )

        ai_bridge_root = self.paths.root / ".ai-bridge"
        for target in iter_matching_tree_roots(
            ai_bridge_root,
            self.policy.ai_bridge_tmp.patterns,
            max_depth=self.policy.ai_bridge_tmp.max_depth,
        ):
            candidates.append(
                self._tree_candidate(
                    "AI_BRIDGE_TMP",
                    target,
                    self.policy.ai_bridge_tmp,
                    instant,
                )
            )
        candidates.extend(self._reviewed_scratch_candidates(instant))
        # A disposable parent already includes its children; count and delete once.
        disposable = [
            Path(item.relative_path) for item in candidates
            if item.eligible and item.entry_type == "TREE"
        ]
        unique: dict[str, StorageCandidate] = {}
        for item in candidates:
            child = Path(item.relative_path)
            if any(child != parent and child.is_relative_to(parent) for parent in disposable):
                continue
            previous = unique.get(item.relative_path)
            if previous is None or item.eligible:
                unique[item.relative_path] = item
        return list(unique.values())

    @staticmethod
    def _read_control_json(path: Path, *, max_bytes: int = 65_536) -> dict[str, object] | None:
        try:
            if path.is_symlink() or path.is_junction() or path.stat().st_size > max_bytes:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _owner_identity_state(owner: dict[str, object]) -> str:
        pid = owner.get("owner_pid")
        created = owner.get("owner_create_time")
        if (
            not isinstance(pid, int) or isinstance(pid, bool)
            or not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(created)
            or owner.get("owned_worker_reaped") is False
        ):
            return "UNKNOWN"
        actual = process_identity(pid)
        if actual.status == "UNKNOWN":
            return "UNKNOWN"
        if actual.status == "ALIVE":
            if actual.creation_time is None:
                return "UNKNOWN"
            if abs(actual.creation_time - created) <= 0.001:
                return "ACTIVE"
        return "DEAD"

    def _ancestor_owner_protection(self, path: Path) -> str | None:
        for current in (path, *path.parents):
            if current == self.paths.root or not current.is_relative_to(self.paths.root):
                break
            marker = current / "owner.json"
            if marker.exists() or marker.is_symlink():
                owner = self._read_control_json(marker)
                if owner is None or owner.get("schema_version") != "astock-owned-run-v1":
                    return "OWNER_IDENTITY_UNVERIFIED"
                state = self._owner_identity_state(owner)
                if state != "DEAD":
                    return "ACTIVE_OWNER_PROTECTED" if state == "ACTIVE" else (
                        "OWNER_IDENTITY_UNVERIFIED"
                    )
        return None

    def _quality_run_state(self, run_root: Path) -> str:
        """Unknown owner/permissions never become an age-only orphan deletion."""
        if path_has_redirect(self.paths.root, run_root):
            return "UNKNOWN"
        owner_path = run_root / "owner.json"
        owner: dict[str, object] | None = None
        if owner_path.exists() or owner_path.is_symlink() or owner_path.is_junction():
            owner = self._read_control_json(owner_path)
            if (
                owner is None or owner.get("schema_version") != "astock-owned-run-v1"
                or owner.get("owned_worker_reaped") is False
            ):
                return "UNKNOWN"
            try:
                declared_root = Path(str(owner.get("project_root"))).resolve()
                if (
                    owner.get("run_id") != run_root.name
                    or declared_root != run_root.parents[2].resolve()
                    or owner.get("status") not in _TERMINAL_RUN_STATUSES | {"RUNNING"}
                ):
                    return "UNKNOWN"
                scratch = Path(str(owner.get("scratch_dir")))
                expected_scratch = {
                    run_root / "tmp",
                    run_root.parents[2] / ".scratchpad" / f"quality-{run_root.name}",
                }
                if scratch not in expected_scratch or path_has_redirect(self.paths.root, scratch):
                    return "UNKNOWN"
                identity = self._owner_identity_state(owner)
                if identity in {"ACTIVE", "UNKNOWN"}:
                    return identity
            except (OSError, ValueError, TypeError):
                return "UNKNOWN"
        result = self._read_control_json(run_root / "result.json")
        if result is not None and (
            result.get("owned_worker_reaped") is False
            or result.get("failure_reason") == "TERMINATION_UNCONFIRMED"
        ):
            return "UNKNOWN"
        if result is not None and result.get("run_id", run_root.name) == run_root.name and (
            type(result.get("child_exit_code")) is int
            or type(result.get("returncode")) is int
        ):
            return "TERMINAL"
        if owner is not None:
            return "TERMINAL" if owner.get("status") in _TERMINAL_RUN_STATUSES else "ORPHAN"
        return "UNKNOWN"

    def _disposal_marker(self, path: Path) -> dict[str, object] | None:
        marker = path / ".cleanup-ready"
        if path_has_redirect(self.paths.root, marker) or not marker.is_file():
            return None
        try:
            if marker.stat().st_size > 65_536:
                return None
        except OSError:
            return None
        value = self._read_control_json(marker)
        if value is None:
            # Existing plain terminal markers remain compatible, without immediate bypass.
            try:
                raw = marker.read_text(encoding="utf-8")
                if raw.strip() and not raw.lstrip().startswith("{"):
                    return {"legacy": True}
                return None
            except (OSError, UnicodeError):
                return None
        try:
            if (
                value.get("schema_version") != "astock-disposable-scratch-v1"
                or Path(str(value.get("project_root"))).resolve() != self.paths.root.resolve()
                or value.get("relative_path") != path.relative_to(self.paths.root).as_posix()
                or not isinstance(value.get("run_id"), str) or not value["run_id"]
                or not isinstance(value.get("reason"), str) or not value["reason"]
            ):
                return None
            reviewed = datetime.fromisoformat(str(value.get("reviewed_at")))
            if reviewed.tzinfo is None:
                return None
        except (OSError, ValueError, TypeError):
            return None
        return value

    def _task_scratchpad_terminal(self, path: Path) -> bool:
        """A matching terminal/dead-owner receipt or explicit review proves disposal."""
        if path.parent == self.paths.root / ".scratchpad" and path.name.startswith("quality-"):
            run_id = path.name.removeprefix("quality-")
            root = self.paths.root / ".ai-bridge" / "quality-runs" / run_id
            return bool(run_id) and self._quality_run_state(root) in {"TERMINAL", "ORPHAN"}
        if (path / "owner.json").exists():
            return False  # Only the quality-run owner contract is understood here.
        return self._disposal_marker(path) is not None

    def _reviewed_scratch_bases(self) -> list[Path]:
        bases = [self.paths.root / name for name in _REVIEWED_SCRATCH_ROOTS]
        worktree_parent = self.paths.runtime / "worktrees"
        for linked in sorted(active_linked_worktree_roots(self.paths.root)):
            if linked.parent != worktree_parent or path_has_redirect(self.paths.root, linked):
                continue
            bases.append(linked / "runtime" / "tmp")
            # Only recognisable temporary/cache subtrees may carry a reviewed marker.
            # A registered worktree's root, sources and durable control files are excluded.
            bases.extend(iter_matching_tree_roots(
                linked / ".ai-bridge",
                ["tmp", "tmp-*", "pytest-*", "*-pytest-tmp", "process-tmp",
                 "uv-cache", "npm-cache", "xdg-cache", "quality-cache"],
                max_depth=3,
            ))
        return bases

    def _reviewed_scratch_candidates(self, instant: datetime) -> list[StorageCandidate]:
        rows: list[StorageCandidate] = []
        remaining = self.policy.reviewed_scratch.scan_limit
        for base in self._reviewed_scratch_bases():
            if not base.is_dir() or path_has_redirect(self.paths.root, base):
                continue
            stack = [(base, 0)]
            while stack and remaining > 0:
                path, depth = stack.pop()
                remaining -= 1
                if path_has_redirect(self.paths.root, path):
                    continue
                marker = self._disposal_marker(path)
                if marker is not None:
                    candidate = self._tree_candidate(
                        "REVIEWED_SCRATCH", path, self.policy.reviewed_scratch, instant,
                        referenced=not self._task_scratchpad_terminal(path),
                        protected_reason="SCRATCH_OWNER_UNVERIFIED",
                    )
                    if (
                        marker.get("allow_immediate") is True and candidate.scan_complete
                        and not candidate.referenced
                    ):
                        candidate = candidate.model_copy(update={
                            "eligible": True, "reason": "REVIEWED_DISPOSABLE_SCRATCH",
                        })
                    rows.append(candidate)
                    continue
                if depth < self.policy.reviewed_scratch.max_depth:
                    try:
                        stack.extend(
                            (child, depth + 1) for child in path.iterdir()
                            if child.is_dir() and not child.is_symlink() and not child.is_junction()
                        )
                    except OSError:
                        continue
        return rows

    def _retention_references(self) -> tuple[str, bool]:
        """Keep receipts referenced by current docs or unfinished durable work."""
        files = list(self.paths.root.glob("*.md"))
        for folder, pattern in (("docs", "*.md"), ("planning", "*.yaml")):
            base = self.paths.root / folder
            if base.is_dir() and not path_has_redirect(self.paths.root, base):
                files.extend(base.rglob(pattern))
        bridge = self.paths.root / ".ai-bridge"
        files.extend(bridge / name for name in ("current-plan.md", "agent-status.md"))
        long_runs = bridge / "long-runs"
        if long_runs.is_dir() and not path_has_redirect(self.paths.root, long_runs):
            for path in long_runs.glob("*.json"):
                data = self._read_control_json(path, max_bytes=2 * 1024**2)
                if data is None:
                    return "", False
                if str(data.get("status", "")).lower() not in {
                    "done", "completed", "cancelled", "failed", "aborted", "archived", "superseded",
                }:
                    files.append(path)
        chunks: list[str] = []
        remaining = 16 * 1024**2
        for path in files[:2000]:
            if not path.exists():
                continue
            if path_has_redirect(self.paths.root, path):
                return "", False
            try:
                size = path.stat().st_size
                if size > min(remaining, 2 * 1024**2):
                    return "", False
                chunks.append(path.read_text(encoding="utf-8"))
                remaining -= size
            except (OSError, UnicodeError):
                return "", False
        references = "\n".join(chunks).replace("\\\\", "/").replace("\\", "/").casefold()
        return references, len(files) <= 2000

    def _reference_mentions(self, path: Path, references: str) -> bool:
        relative = path.relative_to(self.paths.root).as_posix().casefold()
        return relative in references or (
            bool(re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", path.name))
            and path.name.casefold() in references
        )

    @staticmethod
    def _nonoverlapping_tree_bytes(candidates: list[StorageCandidate]) -> int:
        roots: list[Path] = []
        total = 0
        for item in sorted(candidates, key=lambda value: len(Path(value.relative_path).parts)):
            path = Path(item.relative_path)
            if any(path.is_relative_to(parent) for parent in roots):
                continue
            roots.append(path)
            total += item.byte_size
        return total

    def _tree_candidate(
        self,
        category: _StorageCategory,
        path: Path,
        policy: StorageTreePolicy,
        instant: datetime,
        *,
        referenced: bool = False,
        protected_reason: str = "TREE_PROTECTED",
    ) -> StorageCandidate:
        owner_reason = self._ancestor_owner_protection(path)
        if owner_reason:
            referenced = True
            protected_reason = owner_reason
        snapshot = snapshot_tree(path)
        cutoff = instant - timedelta(hours=policy.retention_hours)
        modified = datetime.fromtimestamp(snapshot.latest_mtime_ns / 1_000_000_000, UTC)
        expired = modified <= cutoff
        eligible = expired and not referenced and snapshot.complete
        relative = path.resolve().relative_to(self.paths.root.resolve()).as_posix()
        return StorageCandidate(
            category=category,
            relative_path=relative,
            byte_size=snapshot.byte_size,
            mtime_ns=snapshot.latest_mtime_ns,
            entry_type="TREE",
            file_count=snapshot.file_count,
            dir_count=snapshot.dir_count,
            tree_fingerprint=snapshot.fingerprint,
            scan_complete=snapshot.complete,
            referenced=referenced,
            eligible=eligible,
            reason=(
                "TREE_SCAN_INCOMPLETE"
                if not snapshot.complete
                else protected_reason
                if referenced
                else f"{category}_EXPIRED"
                if eligible
                else f"{category}_NOT_EXPIRED"
            ),
        )

    def automatic_plan(self, *, now: datetime | None = None) -> StorageLifecyclePlan:
        """Create a policy-approved plan containing only auto-deletable ephemeral trees."""

        instant = (now or datetime.now(UTC)).astimezone(UTC)
        candidates = self._ephemeral_tree_candidates(instant)
        budget = self.policy.automation.max_delete_bytes_per_run
        used = 0
        bounded: list[StorageCandidate] = []
        for item in sorted(candidates, key=lambda value: (value.mtime_ns, value.relative_path)):
            if item.eligible and used + item.byte_size > budget:
                item = item.model_copy(
                    update={"eligible": False, "reason": "AUTO_DELETE_BUDGET_DEFERRED"}
                )
            elif item.eligible:
                used += item.byte_size
            bounded.append(item)
        bounded.sort(key=lambda item: (item.category, item.relative_path))
        eligible = [item for item in bounded if item.eligible]
        sizes = [snapshot_tree(path) for path in (
            self.paths.runtime, self.paths.objects, self.paths.runtime / "tmp", self.paths.reports,
        )]
        runtime_bytes, object_store_bytes, temp_bytes, report_bytes = (
            value.byte_size for value in sizes
        )
        truncated = not all(value.complete for value in sizes) or any(
            not item.scan_complete for item in bounded
        )
        ephemeral_bytes = self._nonoverlapping_tree_bytes(bounded)
        volume_free_bytes = shutil.disk_usage(self.paths.root).free
        identity = self._plan_identity_payload(bounded, scan_truncated=truncated)
        return StorageLifecyclePlan(
            plan_id=content_hash(identity),
            generated_at=instant,
            candidates=bounded,
            scanned_file_count=sum(item.file_count for item in bounded),
            scanned_bytes=sum(item.byte_size for item in bounded),
            eligible_file_count=sum(item.file_count for item in eligible),
            eligible_bytes=sum(item.byte_size for item in eligible),
            referenced_object_count=0,
            scan_truncated=truncated,
            runtime_bytes=runtime_bytes,
            object_store_bytes=object_store_bytes,
            temp_bytes=temp_bytes,
            report_bytes=report_bytes,
            ephemeral_bytes=ephemeral_bytes,
            volume_free_bytes=volume_free_bytes,
            watermark_status=self._watermark(
                runtime_bytes,
                object_store_bytes,
                temp_bytes,
                report_bytes,
                ephemeral_bytes,
                volume_free_bytes,
            ),
        )

    def automatic_cleanup(
        self,
        *,
        apply: bool,
        now: datetime | None = None,
    ) -> tuple[StorageLifecyclePlan, StorageLifecycleAudit, StorageLifecycleRun | None]:
        """Audit and optionally execute only the explicitly auto-safe ephemeral categories."""

        plan = self.automatic_plan(now=now)
        self.persist_plan(plan)
        report = self.audit(plan)
        self.record_audit(report)
        if not apply:
            return plan, report, None
        if not self.policy.automation.enabled:
            raise ValueError("storage lifecycle automation is disabled by policy")
        run = self.run(plan, confirm=True)
        self.record_run(run)
        return plan, report, run

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
        ephemeral_bytes: int,
        volume_free_bytes: int,
    ) -> Literal["OK", "WARNING", "CRITICAL"]:
        marks = self.policy.watermarks
        if (
            runtime_bytes >= marks.runtime_critical_bytes
            or ephemeral_bytes >= marks.ephemeral_critical_bytes
            or volume_free_bytes <= marks.volume_free_critical_bytes
        ):
            return "CRITICAL"
        if (
            runtime_bytes >= marks.runtime_warning_bytes
            or object_bytes >= marks.object_store_warning_bytes
            or temp_bytes >= marks.temp_warning_bytes
            or report_bytes >= marks.report_warning_bytes
            or ephemeral_bytes >= marks.ephemeral_warning_bytes
            or volume_free_bytes <= marks.volume_free_warning_bytes
        ):
            return "WARNING"
        return "OK"

    def _allowed_candidate_path(self, path: Path, category: str) -> bool:
        if path_has_redirect(self.paths.root, path):
            return False
        path = path.absolute()
        file_roots = {
            "OBJECT_STORE": self.paths.objects,
            "RUNTIME_TMP": self.paths.runtime / "tmp",
            "REPORT_STAGING": self.paths.report_staging,
            "REPORT_OUTPUT": self.paths.reports / "output",
            "LOG_BACKUP": self.paths.logs,
        }
        root = file_roots.get(category)
        if root is not None:
            try:
                path.resolve().relative_to(root.resolve())
                return path.is_file() or not path.exists()
            except ValueError:
                return False

        def direct_child(parent: Path) -> bool:
            try:
                return path.parent.resolve() == parent.resolve()
            except OSError:
                return False

        def matches(patterns: list[str]) -> bool:
            return any(
                fnmatch.fnmatchcase(path.name.casefold(), pattern.casefold())
                for pattern in patterns
            )

        if category in {"QUALITY_RUN_TMP", "QUALITY_RUN_EVIDENCE"}:
            roots = [self.paths.root]
            worktree_parent = (self.paths.runtime / "worktrees").resolve()
            roots.extend(
                linked for linked in active_linked_worktree_roots(self.paths.root)
                if linked.parent == worktree_parent
                and not linked.is_symlink() and not linked.is_junction()
            )
            for base in roots:
                quality_root = base / ".ai-bridge" / "quality-runs"
                try:
                    relative = path.relative_to(quality_root.resolve())
                except ValueError:
                    continue
                if (
                    (category == "QUALITY_RUN_TMP" and len(relative.parts) == 2
                     and relative.parts[-1] == "tmp")
                    or (category == "QUALITY_RUN_EVIDENCE" and len(relative.parts) == 1)
                ):
                    # Reject redirection at every component, including Windows junctions.
                    chain = [base / ".ai-bridge", quality_root, path.parent, path]
                    return not any(item.is_symlink() or item.is_junction() for item in chain)
            return False
        if category == "REVIEWED_SCRATCH":
            return any(
                path == base or path.is_relative_to(base)
                for base in self._reviewed_scratch_bases()
            )
        if category == "LONGRUN_SCRATCH":
            longrun_root = self.paths.runtime / "longrun"
            try:
                relative = path.relative_to(longrun_root.resolve())
            except ValueError:
                return False
            return (
                1 <= len(relative.parts) <= self.policy.longrun_scratch.max_depth
                and matches(self.policy.longrun_scratch.patterns)
            )
        if category == "STALE_WORKTREE":
            return direct_child(self.paths.runtime / "worktrees")
        if category == "RUNTIME_TEST_SCRATCH":
            return direct_child(self.paths.runtime) and matches(
                self.policy.runtime_test_scratch.patterns
            )
        if category == "TASK_SCRATCHPAD":
            scratch_root = self.paths.root / ".scratchpad"
            marker = path / ".cleanup-ready"
            return (
                direct_child(scratch_root)
                and matches(self.policy.task_scratchpad.patterns)
                and not any(
                    item.is_symlink() or item.is_junction()
                    for item in (scratch_root, path, marker)
                )
                and (path.is_dir() or not path.exists())
            )
        if category == "AI_BRIDGE_TMP":
            return direct_child(self.paths.root / ".ai-bridge") and matches(
                self.policy.ai_bridge_tmp.patterns
            )
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
    "StorageAutomationPolicy",
    "StorageCandidate",
    "StorageClassPolicy",
    "StorageLifecycleAudit",
    "StorageLifecyclePlan",
    "StorageLifecyclePolicy",
    "StorageLifecycleRun",
    "StorageLifecycleService",
    "StorageTreePolicy",
    "StorageWatermarks",
    "load_storage_lifecycle_policy",
]
