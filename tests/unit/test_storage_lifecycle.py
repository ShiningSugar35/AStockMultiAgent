"""Defect-first tests for storage lifecycle planning, audit, run, and operations SLO."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.operations import (
    OperationsSLOPolicy,
    StorageCandidate,
    StorageClassPolicy,
    StorageLifecyclePolicy,
    StorageLifecycleService,
    StorageWatermarks,
    load_storage_lifecycle_policy,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(
    *,
    object_retention_days: int = 14,
    tmp_retention_hours: int = 24,
    staging_retention_hours: int = 24,
    report_retention_days: int = 90,
    log_retention_hours: int = 336,
    scan_limit: int = 500,
    object_scan_limit: int = 500,
    runtime_warning: int = 5 * 1024**3,
    runtime_critical: int = 10 * 1024**3,
    object_warning: int = 4 * 1024**3,
    report_warning: int = 1 * 1024**3,
    temp_warning: int = 1 * 1024**3,
    evidence_freshness: int = 86400,
    backlog_warning: int = 100,
    provider_warning: int = 1,
    report_target: float = 0.95,
) -> StorageLifecyclePolicy:
    return StorageLifecyclePolicy(
        schema_version="storage-lifecycle-policy-v1",
        object_store=StorageClassPolicy(
            orphan_retention_days=object_retention_days,
            scan_limit=object_scan_limit,
        ),
        runtime_tmp=StorageClassPolicy(retention_hours=tmp_retention_hours, scan_limit=scan_limit),
        report_staging=StorageClassPolicy(
            retention_hours=staging_retention_hours, scan_limit=scan_limit
        ),
        report_output=StorageClassPolicy(
            orphan_retention_days=report_retention_days, scan_limit=scan_limit
        ),
        logs=StorageClassPolicy(retention_hours=log_retention_hours, scan_limit=scan_limit),
        watermarks=StorageWatermarks(
            runtime_warning_bytes=runtime_warning,
            runtime_critical_bytes=runtime_critical,
            object_store_warning_bytes=object_warning,
            report_warning_bytes=report_warning,
            temp_warning_bytes=temp_warning,
        ),
        operations_slo=OperationsSLOPolicy(
            evidence_freshness_target_seconds=evidence_freshness,
            monitor_backlog_warning=backlog_warning,
            provider_degraded_warning=provider_warning,
            report_success_rate_target=report_target,
        ),
    )


def _make_paths(tmp_path: Path) -> ProjectPaths:
    return ProjectPaths(
        root=tmp_path,
        runtime=tmp_path / "runtime",
        objects=tmp_path / "runtime" / "objects" / "sha256",
        parquet=tmp_path / "runtime" / "data" / "parquet",
        manifests=tmp_path / "runtime" / "manifests",
        state_db=tmp_path / "runtime" / "state.sqlite",
    )


def _make_state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "runtime" / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state


def _write_file(path: Path, content: bytes = b"test", mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _make_object(objects_root: Path, sha256: str, data: bytes = b"obj") -> Path:
    path = objects_root / sha256[:2] / sha256[2:4] / sha256
    _write_file(path, data)
    return path


def _dummy_sha256(seed: str = "a") -> str:
    """Generate a deterministic valid sha256 for arbitrary test seeds."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


class TestStorageLifecyclePolicy:
    def test_valid_policy_from_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "storage_lifecycle.yaml"
        config_path.write_text(
            """
schema_version: storage-lifecycle-policy-v1
object_store:
  orphan_retention_days: 14
  scan_limit: 100
runtime_tmp:
  retention_hours: 24
  scan_limit: 100
report_staging:
  retention_hours: 24
  scan_limit: 100
report_output:
  orphan_retention_days: 90
  scan_limit: 100
logs:
  retention_hours: 336
  scan_limit: 100
watermarks:
  runtime_warning_bytes: 5368709120
  runtime_critical_bytes: 10737418240
  object_store_warning_bytes: 4294967296
  report_warning_bytes: 1073741824
  temp_warning_bytes: 1073741824
operations_slo:
  evidence_freshness_target_seconds: 86400
  monitor_backlog_warning: 100
  provider_degraded_warning: 1
  report_success_rate_target: 0.95
""",
            encoding="utf-8",
        )
        policy = load_storage_lifecycle_policy(config_path)
        assert policy.schema_version == "storage-lifecycle-policy-v1"
        assert policy.object_store.orphan_retention_days == 14
        assert policy.operations_slo.evidence_freshness_target_seconds == 86400

    def test_policy_rejects_both_retentions(self) -> None:
        with pytest.raises(ValidationError):
            StorageClassPolicy(orphan_retention_days=14, retention_hours=24, scan_limit=100)

    def test_policy_rejects_no_retention(self) -> None:
        with pytest.raises(ValidationError):
            StorageClassPolicy(scan_limit=100)

    def test_watermarks_reject_inverted(self) -> None:
        with pytest.raises(ValidationError):
            StorageWatermarks(
                runtime_warning_bytes=2000,
                runtime_critical_bytes=1000,
                object_store_warning_bytes=1000,
                report_warning_bytes=1000,
                temp_warning_bytes=1000,
            )


# ---------------------------------------------------------------------------
# StorageLifecycleService.plan
# ---------------------------------------------------------------------------


class TestStorageLifecyclePlan:
    def test_plan_empty_directories(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        assert plan.scanned_file_count == 0
        assert plan.eligible_file_count == 0
        assert plan.watermark_status == "OK"
        assert plan.deletion_requires_confirmation is True
        assert isinstance(plan.plan_id, str) and len(plan.plan_id) == 64

    def test_plan_categorizes_object_store_referenced(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("a")
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,"
                "object_hash,input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                ("test-artifact:1", "TestType", "v1", sha, "[]", "2026-01-01T00:00:00Z"),
            )
        _make_object(paths.objects, sha)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        referenced = [c for c in plan.candidates if c.referenced]
        assert len(referenced) == 1
        assert referenced[0].category == "OBJECT_STORE"
        assert referenced[0].eligible is False
        assert referenced[0].reason == "REFERENCED_OBJECT_PROTECTED"

    def test_plan_detects_expired_unreferenced_objects(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("b")
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        unreferenced_expired = [
            c
            for c in plan.candidates
            if c.category == "OBJECT_STORE" and c.eligible and not c.referenced
        ]
        assert len(unreferenced_expired) == 1
        assert unreferenced_expired[0].reason == "UNREFERENCED_OBJECT_EXPIRED"

    def test_plan_marks_not_expired_objects(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("c")
        _make_object(paths.objects, sha)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        not_expired = [
            c
            for c in plan.candidates
            if c.category == "OBJECT_STORE" and c.reason == "OBJECT_NOT_EXPIRED"
        ]
        assert len(not_expired) == 1

    def test_plan_covers_report_staging(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        staging_file = paths.report_staging / "report1" / "output.docx"
        old_time = time.time() - 48 * 3600
        _write_file(staging_file, b"report", mtime=old_time)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        staging = [c for c in plan.candidates if c.category == "REPORT_STAGING"]
        assert len(staging) == 1
        assert staging[0].eligible is True
        assert staging[0].reason == "REPORT_STAGING_EXPIRED"

    def test_plan_protects_active_report_staging(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        report_key = "active-report"
        state.set_checkpoint(
            scope_type="report",
            scope_key=report_key,
            cursor={"phase": "render"},
            status="STAGED",
        )
        staged = paths.report_staging / report_key / "report.docx"
        _write_file(staged, b"staged", mtime=time.time() - 48 * 3600)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        candidate = next(c for c in plan.candidates if c.relative_path.endswith("report.docx"))
        assert candidate.category == "REPORT_STAGING"
        assert candidate.referenced is True
        assert candidate.eligible is False
        assert candidate.reason == "ACTIVE_REPORT_STAGING_PROTECTED"

    def test_plan_marks_old_unreferenced_report_output_eligible(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        output = paths.reports / "output" / "orphan.docx"
        _write_file(output, b"orphan", mtime=time.time() - 120 * 86400)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        candidate = next(c for c in plan.candidates if c.relative_path.endswith("orphan.docx"))
        assert candidate.category == "REPORT_OUTPUT"
        assert candidate.referenced is False
        assert candidate.eligible is True
        assert candidate.reason == "UNREFERENCED_REPORT_EXPIRED"

    def test_plan_protects_registered_report_output(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        output = paths.reports / "output" / "kept.docx"
        _write_file(output, b"kept", mtime=time.time() - 120 * 86400)
        with state.connect() as connection:
            connection.execute(
                "INSERT INTO report_manifest(report_key,request_hash,input_hashes_json,"
                "template_version,renderer,renderer_version,output_format,privacy_level,"
                "citation_level,citations_json,assets_json,output_file_name,publish_status,"
                "publish_attempts,destination_policy,recovered_existing,created_at,"
                "manifest_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "kept-report", "rh", "[]", "v1", "DOCX", "1.0", "DOCX",
                    "INTERNAL_PRIVATE", "SUMMARY", "{}", "{}", "kept.docx",
                    "PUBLISHED", 1, "RUNTIME", 0, "2026-01-01T00:00:00Z", "{}",
                    "2026-01-01T00:00:00Z",
                ),
            )
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        candidate = next(c for c in plan.candidates if c.relative_path.endswith("kept.docx"))
        assert candidate.category == "REPORT_OUTPUT"
        assert candidate.referenced is True
        assert candidate.eligible is False
        assert candidate.reason == "REFERENCED_REPORT_PROTECTED"

    def test_plan_covers_log_backups(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        log_file = paths.logs / "astock-operational.jsonl.1"
        old_time = time.time() - 400 * 3600
        _write_file(log_file, b"log", mtime=old_time)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        logs = [c for c in plan.candidates if c.category == "LOG_BACKUP"]
        assert len(logs) == 1
        assert logs[0].eligible is True
        assert logs[0].reason == "LOG_BACKUP_EXPIRED"

    def test_plan_active_log_protected(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        log_file = paths.logs / "astock-operational.jsonl"
        _write_file(log_file, b"active log")
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        active = [
            c
            for c in plan.candidates
            if c.category == "LOG_BACKUP" and c.reason == "ACTIVE_LOG_PROTECTED"
        ]
        assert len(active) == 1
        assert active[0].eligible is False

    def test_plan_covers_runtime_tmp(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        tmp_file = paths.runtime / "tmp" / "smoke.log"
        old_time = time.time() - 48 * 3600
        _write_file(tmp_file, b"tmp", mtime=old_time)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        tmp = [c for c in plan.candidates if c.category == "RUNTIME_TMP"]
        assert len(tmp) == 1
        assert tmp[0].eligible is True
        assert tmp[0].reason == "RUNTIME_TMP_EXPIRED"

    def test_plan_sorted_by_category_and_path(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("d")
        _make_object(paths.objects, sha)
        _write_file(paths.report_staging / "b" / "x.docx", b"x")
        _write_file(paths.report_staging / "a" / "y.docx", b"y")
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        cats = [c.category for c in plan.candidates]
        assert cats == sorted(cats)

    def test_plan_scan_truncation(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        for i in range(5):
            _write_file(paths.runtime / "tmp" / f"file{i}.txt", f"data{i}".encode())
        objects = ObjectStore(paths.objects)
        policy = _make_policy(scan_limit=2, object_scan_limit=2)
        service = StorageLifecycleService(paths, state, objects, policy)
        plan = service.plan()
        assert plan.scan_truncated is True
        assert plan.scanned_file_count == 2

    def test_plan_watermark_warning(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        _write_file(paths.runtime / "tmp" / "big.bin", b"x" * 2048)
        policy = _make_policy(temp_warning=1024)
        service = StorageLifecycleService(paths, state, objects, policy)
        plan = service.plan()
        assert plan.watermark_status == "WARNING"

    def test_plan_report_watermark_warning(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        _write_file(paths.reports / "output" / "large.docx", b"x" * 2048)
        policy = _make_policy(report_warning=1024)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), policy)
        plan = service.plan()
        assert plan.report_bytes >= 2048
        assert plan.watermark_status == "WARNING"

    def test_plan_invalid_object_store_entry_protected(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        bad_file = paths.objects / "not-a-hash"
        old_time = time.time() - 30 * 86400
        _write_file(bad_file, b"junk", mtime=old_time)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        bad = [c for c in plan.candidates if "INVALID_OBJECT_STORE_ENTRY" in c.reason]
        assert len(bad) == 1
        assert bad[0].eligible is False


# ---------------------------------------------------------------------------
# StorageLifecycleService.audit
# ---------------------------------------------------------------------------


class TestStorageLifecycleAudit:
    def test_audit_pass_clean_plan(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        audit = service.audit(plan)
        assert audit.status == "PASS"
        assert audit.finding_codes == []

    def test_audit_fail_referenced_marked_for_delete(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        fake_candidate = StorageCandidate(
            category="OBJECT_STORE",
            relative_path="runtime/objects/sha256/aa/bb/aaaabbbb",
            byte_size=0,
            mtime_ns=0,
            referenced=True,
            eligible=True,
            reason="TEST",
        )
        plan_with_bad = plan.model_copy(
            update={"candidates": list(plan.candidates) + [fake_candidate]}
        )
        audit = service.audit(plan_with_bad)
        assert audit.status == "FAIL"
        assert "REFERENCED_CANDIDATE_MARKED_FOR_DELETE" in audit.finding_codes

    def test_audit_scan_truncated_is_nonblocking_batch_warning(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan().model_copy(update={"scan_truncated": True})
        audit = service.audit(plan)
        assert audit.status == "PASS"
        assert "SCAN_TRUNCATED" in audit.finding_codes


# ---------------------------------------------------------------------------
# StorageLifecycleService.run
# ---------------------------------------------------------------------------


class TestStorageLifecycleRun:
    def test_run_rejects_without_confirm(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        with pytest.raises(ValueError, match="requires explicit confirmation"):
            service.run(plan, confirm=False)

    def test_run_rejects_if_audit_fails(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        bad = StorageCandidate(
            category="OBJECT_STORE",
            relative_path="../escape.bin",
            byte_size=0,
            mtime_ns=0,
            referenced=False,
            eligible=True,
            reason="TEST",
        )
        plan = plan.model_copy(update={"candidates": [*plan.candidates, bad]})
        with pytest.raises(ValueError, match="failed audit"):
            service.run(plan, confirm=True)

    def test_run_deletes_expired_files(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("f")  # 'f' is valid hex
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        eligible_count = plan.eligible_file_count
        assert eligible_count >= 1
        run = service.run(plan, confirm=True)
        assert run.confirmed is True
        assert run.deleted_file_count >= 1
        assert run.deleted_bytes > 0
        assert not obj_path.exists()

    def test_run_skips_already_missing(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("b")  # valid hex
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        # Delete the file before run
        obj_path.unlink()
        run = service.run(plan, confirm=True)
        assert run.deleted_file_count == 0
        assert any("ALREADY_MISSING" in r for r in run.skip_reasons)

    def test_run_skips_changed_since_plan(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("c")  # valid hex (different from others)
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        # Modify the file after plan (different size and mtime)
        time.sleep(0.05)
        _write_file(obj_path, b"modified content here", mtime=time.time())
        run = service.run(plan, confirm=True)
        assert run.deleted_file_count == 0
        assert any("CHANGED_SINCE_PLAN" in r for r in run.skip_reasons)

    def test_run_skips_windows_locked_file(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("i")
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        original_unlink = Path.unlink

        def locked_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
            if self == obj_path:
                raise OSError("Permission denied (Windows file lock)")
            return original_unlink(self, *args, **kwargs)

        pathlib.Path.unlink = locked_unlink  # type: ignore[assignment]
        try:
            run = service.run(plan, confirm=True)
            assert run.deleted_file_count == 0
            assert any("DELETE_FAILED" in r for r in run.skip_reasons)
        finally:
            pathlib.Path.unlink = original_unlink  # type: ignore[assignment]

    def test_run_skips_outside_allowed_root(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        bad_candidate = StorageCandidate(
            category="OBJECT_STORE",
            relative_path="../escape/path.bin",
            byte_size=0,
            mtime_ns=0,
            referenced=False,
            eligible=True,
            reason="TEST",
        )
        plan_bad = plan.model_copy(update={"candidates": list(plan.candidates) + [bad_candidate]})
        # Audit will fail due to CANDIDATE_OUTSIDE_ALLOWED_ROOT, so run raises
        with pytest.raises(ValueError, match="failed audit"):
            service.run(plan_bad, confirm=True)

    def test_run_skips_newly_referenced_objects(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("j")
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        # Register the hash in artifact_registry after plan (simulating concurrent use)
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,"
                "object_hash,input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                ("late-artifact:1", "TestType", "v1", sha, "[]", "2026-01-01T00:00:00Z"),
            )
        run = service.run(plan, confirm=True)
        assert run.deleted_file_count == 0
        assert any("BECAME_REFERENCED" in r for r in run.skip_reasons)

    def test_run_idempotent_on_empty_plan(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        run1 = service.run(plan, confirm=True)
        run2 = service.run(plan, confirm=True)
        assert run1.deleted_file_count == 0
        assert run2.deleted_file_count == 0


    def test_truncated_scan_can_make_bounded_cleanup_progress(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        old_time = time.time() - 48 * 3600
        for index in range(9):
            _write_file(paths.runtime / "tmp" / f"old-{index}.tmp", b"x", mtime=old_time)
        policy = _make_policy(scan_limit=3, object_scan_limit=3)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), policy)
        deleted = 0
        for _ in range(4):
            plan = service.plan()
            audit = service.audit(plan)
            assert audit.status == "PASS"
            if plan.scan_truncated:
                assert "SCAN_TRUNCATED" in audit.finding_codes
            run = service.run(plan, confirm=True)
            deleted += run.deleted_file_count
            if not any(paths.runtime.joinpath("tmp").glob("*.tmp")):
                break
        assert deleted == 9
        assert not any(paths.runtime.joinpath("tmp").glob("*.tmp"))


# ---------------------------------------------------------------------------
# OperationsSLOReport
# ---------------------------------------------------------------------------


class TestOperationsSLOReport:
    def test_slo_report_clean_state(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.status == "PASS"
        assert report.finding_codes == []
        assert report.evidence_freshness_status == "UNKNOWN"
        assert report.provider_degraded_count == 0
        assert report.monitor_pending_task_count == 0
        assert report.runtime_bytes >= 0
        assert report.object_store_bytes >= 0

    def test_slo_report_detects_degraded_providers(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO provider_health(provider_id,capability_hash,status,"
                "last_probe_at,failure_count,last_error_class) "
                "VALUES(?,?,?,?,?,?)",
                ("test-provider", "test-cap-hash", "DEGRADED", "2026-01-01T00:00:00Z",
                 0, None),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.provider_degraded_count >= 1
        assert "PROVIDER_DEGRADATION_PRESENT" in report.finding_codes

    def test_slo_report_detects_high_backlog(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        now_iso = datetime.now(UTC).isoformat()
        with state.connect() as conn:
            # Insert required parent rows for FK constraints
            for i in range(10):
                conn.execute(
                    "INSERT INTO continuous_monitor_target(target_id,market,symbol,"
                    "company_id,display_name,reasons_json,aliases_json,status,"
                    "object_hash,enrolled_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"tgt-{i}", "XSHG", f"60000{i}", f"co-{i}", f"Company {i}",
                     "[]", "[]", "ACTIVE", "aa" * 32, now_iso, now_iso),
                )
                conn.execute(
                    "INSERT INTO continuous_monitor_event(event_id,target_id,"
                    "event_type,severity,observed_at,available_at,source,"
                    "payload_hash,dedupe_key,affected_modules_json,"
                    "requires_research,object_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"evt-{i}", f"tgt-{i}", "SCHEDULED_REVIEW_DUE", "INFO",
                     now_iso, now_iso, "test", "aa" * 32, f"dedupe-{i}",
                     "[]", 0, "aa" * 32, now_iso),
                )
            for i in range(10):
                conn.execute(
                    "INSERT INTO continuous_monitor_task(task_id,event_id,target_id,"
                    "company_id,requested_modules_json,priority,status,object_hash,"
                    "attempts,available_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"task-{i}", f"evt-{i}", f"tgt-{i}", f"co-{i}",
                     "[]", "NORMAL", "PENDING", "aa" * 32, 0,
                     now_iso, now_iso, now_iso),
                )
        objects = ObjectStore(paths.objects)
        policy = _make_policy(backlog_warning=5)
        service = StorageLifecycleService(paths, state, objects, policy)
        report = service.operations_slo_report()
        assert report.monitor_pending_task_count >= 10
        assert "MONITOR_BACKLOG_HIGH" in report.finding_codes

    def test_slo_report_evidence_freshness_pass(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        now_iso = datetime.now(UTC).isoformat()
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO source_snapshot_index(snapshot_id,source_id,object_hash,"
                "fetched_at,availability_at,fetch_status) "
                "VALUES(?,?,?,?,?,?)",
                ("snap:1", "src:1", _dummy_sha256("k"),
                 now_iso, now_iso, "SUCCEEDED"),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.evidence_freshness_status == "PASS"
        assert report.latest_evidence_age_seconds is not None
        assert report.latest_evidence_age_seconds <= 10

    def test_slo_report_evidence_freshness_warn(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        old_time = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO source_snapshot_index(snapshot_id,source_id,object_hash,"
                "fetched_at,availability_at,fetch_status) "
                "VALUES(?,?,?,?,?,?)",
                ("snap:2", "src:2", _dummy_sha256("n"),
                 old_time, old_time, "SUCCEEDED"),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.evidence_freshness_status == "WARN"
        assert "EVIDENCE_FRESHNESS_SLO_MISSED" in report.finding_codes

    def test_slo_report_includes_disk_usage(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        _make_object(paths.objects, _dummy_sha256("q"), b"data" * 100)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.object_store_bytes > 0

    def test_slo_report_report_success_rate(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        with state.connect() as conn:
            for i in range(4):
                conn.execute(
                    "INSERT INTO report_manifest(report_key,request_hash,"
                    "input_hashes_json,template_version,renderer,renderer_version,"
                    "output_format,privacy_level,citation_level,citations_json,"
                    "assets_json,publish_status,publish_attempts,destination_policy,"
                    "recovered_existing,created_at,manifest_json,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"rpt-{i}", f"rh-{i}", "[]", "v1", "DOCX", "1.0",
                     "DOCX", "INTERNAL_PRIVATE", "SUMMARY", "{}", "{}",
                     "PUBLISHED", 1, "LOCAL", 0, "2026-01-01T00:00:00Z",
                     "{}", "2026-01-01T00:00:00Z"),
                )
            conn.execute(
                "INSERT INTO report_manifest(report_key,request_hash,"
                "input_hashes_json,template_version,renderer,renderer_version,"
                "output_format,privacy_level,citation_level,citations_json,"
                "assets_json,publish_status,publish_attempts,destination_policy,"
                "recovered_existing,created_at,manifest_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("rpt-fail", "rh-fail", "[]", "v1", "DOCX", "1.0",
                 "DOCX", "INTERNAL_PRIVATE", "SUMMARY", "{}", "{}",
                 "FAILED", 1, "LOCAL", 0, "2026-01-01T00:00:00Z",
                 "{}", "2026-01-01T00:00:00Z"),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        report = service.operations_slo_report()
        assert report.report_total_count == 5
        assert report.report_published_count == 4
        assert report.report_success_rate is not None
        assert abs(report.report_success_rate - 0.8) < 0.01

    def test_slo_report_watermark_warning(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        _write_file(paths.runtime / "tmp" / "big.bin", b"x" * 2048)
        objects = ObjectStore(paths.objects)
        policy = _make_policy(temp_warning=1024)
        service = StorageLifecycleService(paths, state, objects, policy)
        report = service.operations_slo_report()
        assert "STORAGE_WATERMARK_EXCEEDED" in report.finding_codes


# ---------------------------------------------------------------------------
# Reference hash collection
# ---------------------------------------------------------------------------


class TestReferenceHashCollection:
    def test_collects_hashes_from_artifact_registry(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("r")
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,"
                "object_hash,input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                ("test:1", "T", "v1", sha, json.dumps([_dummy_sha256("s")]),
                 "2026-01-01T00:00:00Z"),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        referenced = service._referenced_hashes()
        assert sha in referenced
        assert _dummy_sha256("s") in referenced

    def test_collects_hashes_from_json_arrays(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("t")
        with state.connect() as conn:
            conn.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,"
                "object_hash,input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                ("test:2", "T", "v1", _dummy_sha256("u"),
                 json.dumps({"hashes": [sha]}), "2026-01-01T00:00:00Z"),
            )
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        referenced = service._referenced_hashes()
        assert sha in referenced


# ---------------------------------------------------------------------------
# Integration: plan → audit → run flow
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_full_plan_audit_run_cycle(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        sha = _dummy_sha256("v")
        obj_path = _make_object(paths.objects, sha)
        old_time = time.time() - 30 * 86400
        os.utime(obj_path, (old_time, old_time))
        staging_file = paths.report_staging / "old" / "report.docx"
        _write_file(staging_file, b"old report", mtime=old_time)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        # At least staging file should be eligible; object may or may not be depending on hash
        assert plan.eligible_file_count >= 1
        assert plan.deletion_requires_confirmation is True
        audit = service.audit(plan)
        assert audit.status == "PASS"
        assert audit.finding_codes == []
        run = service.run(plan, confirm=True)
        assert run.confirmed is True
        assert run.deleted_file_count >= 1
        assert run.deleted_bytes > 0

    def test_plan_audit_fail_blocks_run(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan = service.plan()
        bad = StorageCandidate(
            category="OBJECT_STORE",
            relative_path="../escape.bin",
            byte_size=0,
            mtime_ns=0,
            referenced=False,
            eligible=True,
            reason="TEST",
        )
        plan = plan.model_copy(update={"candidates": [*plan.candidates, bad]})
        audit = service.audit(plan)
        assert audit.status == "FAIL"
        with pytest.raises(ValueError, match="failed audit"):
            service.run(plan, confirm=True)

    def test_plan_idempotent_for_same_state(self, tmp_path: Path) -> None:
        """Two plans on the same empty state should produce the same structure."""
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        objects = ObjectStore(paths.objects)
        service = StorageLifecycleService(paths, state, objects, _make_policy())
        plan1 = service.plan()
        plan2 = service.plan()
        assert plan1.plan_id == plan2.plan_id
        assert plan1.scanned_file_count == plan2.scanned_file_count
        assert plan1.eligible_file_count == plan2.eligible_file_count
        assert len(plan1.candidates) == len(plan2.candidates)


# ---------------------------------------------------------------------------
# Operational receipts
# ---------------------------------------------------------------------------


class TestOperationalReceipts:
    def test_records_audit_execution_and_slo_receipts(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        service.persist_plan(plan)
        audit = service.audit(plan)
        service.record_audit(audit)
        service.record_audit(audit)
        run = service.run(plan, confirm=True)
        service.record_run(run)
        service.record_run(run)
        slo = service.operations_slo_report()
        service.record_slo_snapshot(slo)
        with state.connect() as connection:
            receipt_counts = {
                str(row["run_kind"]): int(row["count"])
                for row in connection.execute(
                    "SELECT run_kind,COUNT(*) AS count FROM storage_lifecycle_audit_run "
                    "GROUP BY run_kind"
                ).fetchall()
            }
            slo_count = connection.execute(
                "SELECT COUNT(*) FROM operations_slo_snapshot"
            ).fetchone()[0]
        assert receipt_counts == {"AUDIT": 1, "EXECUTION": 1}
        assert slo_count == 1

    def test_duplicate_audit_receipt_fails_closed_if_existing_semantics_drift(
        self, tmp_path: Path
    ) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        service.persist_plan(plan)
        audit = service.audit(plan)
        service.record_audit(audit)
        with state.connect() as connection:
            connection.execute(
                "UPDATE storage_lifecycle_audit_run SET eligible_bytes=eligible_bytes+1 "
                "WHERE run_kind='AUDIT'"
            )

        with pytest.raises(ValueError, match="receipt identity collision"):
            service.record_audit(audit)


# ---------------------------------------------------------------------------
# Persisted plan recovery
# ---------------------------------------------------------------------------


class TestPersistedPlanRecovery:
    def test_persisted_plan_round_trip_across_service_instances(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        service.persist_plan(plan)
        recovered = StorageLifecycleService(
            paths, state, ObjectStore(paths.objects), _make_policy()
        ).load_plan(plan.plan_id)
        assert recovered is not None
        assert recovered.plan_id == plan.plan_id
        assert recovered.candidates == plan.candidates

    def test_tampered_persisted_plan_fails_closed(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        paths.ensure_directories()
        state = _make_state(tmp_path)
        service = StorageLifecycleService(paths, state, ObjectStore(paths.objects), _make_policy())
        plan = service.plan()
        service.persist_plan(plan)
        with state.connect() as connection:
            connection.execute(
                "UPDATE storage_lifecycle_plan SET plan_json=? WHERE plan_id=?",
                ('{"plan_id":"broken"}', plan.plan_id),
            )
        assert service.load_plan(plan.plan_id) is None

# ---------------------------------------------------------------------------
# CLI smoke (imports and command registration)
# ---------------------------------------------------------------------------


class TestOperationsCLI:
    def test_import_operations_cli(self) -> None:
        from astock.operations_cli import register_operations_commands
        assert callable(register_operations_commands)

    def test_cli_commands_registered(self) -> None:
        import typer

        from astock.operations_cli import register_operations_commands

        test_app = typer.Typer()
        def dummy_services() -> tuple[None, None, None]:
            return None, None, None

        def dummy_emit(_: object) -> None:
            return None

        register_operations_commands(test_app, dummy_services, dummy_emit)
        commands = {cmd.name for cmd in test_app.registered_commands}
        expected = {
            "storage-lifecycle-plan",
            "storage-lifecycle-audit",
            "storage-lifecycle-run",
            "operations-slo-report",
        }
        assert expected.issubset(commands)
