"""WP57 regression tests: scratchpad cleanup requires a terminal owner receipt."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.operations import StorageLifecycleService, load_storage_lifecycle_policy
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> StorageLifecycleService:
    paths = ProjectPaths(
        root=tmp_path,
        runtime=tmp_path / "runtime",
        objects=tmp_path / "runtime" / "objects" / "sha256",
        parquet=tmp_path / "runtime" / "data" / "parquet",
        manifests=tmp_path / "runtime" / "manifests",
        state_db=tmp_path / "runtime" / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    return StorageLifecycleService(
        paths,
        state,
        ObjectStore(paths.objects),
        load_storage_lifecycle_policy(PROJECT_ROOT / "configs" / "storage_lifecycle.yaml"),
    )


def _old_tree(root: Path, *, age_hours: int = 48) -> None:
    stamp = (NOW - timedelta(hours=age_hours)).timestamp()
    for item in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(item, (stamp, stamp))
    os.utime(root, (stamp, stamp))


def _scratch_candidate(service: StorageLifecycleService, name: str):
    return next(
        item
        for item in service.automatic_plan(now=NOW).candidates
        if item.relative_path == f".scratchpad/{name}"
    )


def test_quality_scratch_is_protected_until_matching_terminal_receipt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    name = "quality-run-a"
    scratch = tmp_path / ".scratchpad" / name
    scratch.mkdir(parents=True)
    (scratch / "interrupted.tmp").write_text("temporary")
    _old_tree(scratch)
    candidate = _scratch_candidate(service, name)
    assert candidate.referenced and not candidate.eligible

    receipt = tmp_path / ".ai-bridge" / "quality-runs" / "run-a" / "result.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"child_exit_code":1}')
    candidate = _scratch_candidate(service, name)
    assert not candidate.referenced and candidate.eligible
    plan = service.automatic_plan(now=NOW)
    assert service.audit(plan).status == "PASS"
    run = service.run(plan, confirm=True)
    assert run.deleted_file_count == 1
    assert not scratch.exists()
    assert receipt.exists()


def test_task_scratch_is_protected_until_marked_completed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    name = "wp-57-smoke"
    scratch = tmp_path / ".scratchpad" / name
    scratch.mkdir(parents=True)
    (scratch / "debug.sql").write_text("SELECT 1")
    _old_tree(scratch)
    assert not _scratch_candidate(service, name).eligible
    (scratch / ".cleanup-ready").write_text("complete")
    _old_tree(scratch)
    assert _scratch_candidate(service, name).eligible
    plan = service.automatic_plan(now=NOW)
    run = service.run(plan, confirm=True)
    assert run.deleted_file_count == 2
    assert not scratch.exists()


def test_scratch_after_plan_change_or_terminal_revocation_is_not_deleted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scratch = tmp_path / ".scratchpad" / "wp-57-work"
    scratch.mkdir(parents=True)
    (scratch / ".cleanup-ready").write_text("done")
    (scratch / "debug.log").write_text("a")
    _old_tree(scratch)
    plan = service.automatic_plan(now=NOW)
    (scratch / "new.txt").write_text("changed")
    result = service.run(plan, confirm=True)
    assert scratch.exists()
    assert any("CHANGED_SINCE_PLAN" in message for message in result.skip_reasons)

    _old_tree(scratch)
    plan = service.automatic_plan(now=NOW)
    (scratch / ".cleanup-ready").unlink()
    result = service.run(plan, confirm=True)
    assert scratch.exists()
    assert any("TASK_NOT_DISPOSABLE" in message for message in result.skip_reasons)


def test_unknown_scratch_names_not_automatically_removed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    scratch = tmp_path / ".scratchpad" / "other-agent-private"
    scratch.mkdir(parents=True)
    (scratch / "untouched.txt").write_text("another agent")
    _old_tree(scratch)
    plan = service.automatic_plan(now=NOW)
    candidate = next(
        item for item in plan.candidates if item.relative_path == ".scratchpad/other-agent-private"
    )
    assert candidate.referenced and not candidate.eligible
    service.run(plan, confirm=True)
    assert (scratch / "untouched.txt").read_text() == "another agent"


def test_symlinked_scratch_tree_never_selected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    outside = tmp_path / "other-agent-owned"
    outside.mkdir()
    (outside / ".completed").write_text("do not touch")
    scratch = tmp_path / ".scratchpad"
    scratch.mkdir()
    try:
        (scratch / "wp-external").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    plan = service.automatic_plan(now=NOW)
    assert all(item.relative_path != ".scratchpad/wp-external" for item in plan.candidates)
    assert (outside / ".completed").exists()
