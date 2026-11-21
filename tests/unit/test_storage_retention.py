"""Owner, reference and filesystem counterexamples for the existing lifecycle service."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.operations import StorageLifecycleService, load_storage_lifecycle_policy
from astock.settings import ProjectPaths
from astock.storage_retention import (
    ProcessIdentity,
    path_has_redirect,
    process_identity,
    snapshot_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def service_at(root: Path) -> StorageLifecycleService:
    paths = ProjectPaths(
        root=root, runtime=root / "runtime",
        objects=root / "runtime" / "objects" / "sha256",
        parquet=root / "runtime" / "data" / "parquet",
        manifests=root / "runtime" / "manifests",
        state_db=root / "runtime" / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    policy = load_storage_lifecycle_policy(PROJECT_ROOT / "configs" / "storage_lifecycle.yaml")
    return StorageLifecycleService(paths, state, ObjectStore(paths.objects), policy)


def write(path: Path, value: bytes | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())


def age(path: Path, hours: int) -> None:
    timestamp = time.time() - hours * 3600
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not child.is_symlink() and not child.is_junction():
            os.utime(child, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


def quality_owner(service: StorageLifecycleService, run_id: str) -> tuple[Path, Path]:
    root = service.paths.root
    run = root / ".ai-bridge" / "quality-runs" / run_id
    scratch = root / ".scratchpad" / f"quality-{run_id}"
    write(scratch / "payload.bin", b"temporary")
    write(run / "owner.json", {
        "schema_version": "astock-owned-run-v1",
        "run_id": run_id,
        "owner_pid": 12345,
        "owner_create_time": 1000.0,
        "started_at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
        "status": "RUNNING",
        "scratch_dir": str(scratch),
        "project_root": str(root),
    })
    return run, scratch


def review_marker(service: StorageLifecycleService, target: Path) -> None:
    write(target / ".cleanup-ready", {
        "schema_version": "astock-disposable-scratch-v1",
        "run_id": "review-fixture",
        "project_root": str(service.paths.root),
        "relative_path": target.relative_to(service.paths.root).as_posix(),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reason": "Verified reproducible test workspace, owner finished.",
        "allow_immediate": True,
    })


def test_process_identity_reads_current_owner_and_rejects_invalid_pid() -> None:
    identity = process_identity(os.getpid())
    assert identity.status == "ALIVE"
    assert identity.creation_time is not None
    assert 0 < identity.creation_time <= time.time()
    assert process_identity(-1).status == "UNKNOWN"
    assert process_identity(0).status == "UNKNOWN"
    assert process_identity(2**64).status == "UNKNOWN"


def test_snapshot_budget_reports_incomplete_and_rename_changes_fingerprint(tmp_path: Path) -> None:
    write(tmp_path / "one", b"123")
    write(tmp_path / "two", b"456")
    assert not snapshot_tree(tmp_path, max_entries=1).complete
    before = snapshot_tree(tmp_path)
    stamp = tmp_path.stat().st_mtime_ns
    (tmp_path / "one").rename(tmp_path / "new")
    os.utime(tmp_path, ns=(stamp, stamp))
    after = snapshot_tree(tmp_path)
    assert before.byte_size == after.byte_size
    assert before.file_count == after.file_count
    assert before.latest_mtime_ns == after.latest_mtime_ns
    assert before.fingerprint != after.fingerprint


@pytest.mark.parametrize("identity", [
    ProcessIdentity("ALIVE", 1000.0), ProcessIdentity("UNKNOWN"),
])
def test_live_or_unqueryable_owner_survives_even_with_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: ProcessIdentity,
) -> None:
    service = service_at(tmp_path)
    run, scratch = quality_owner(service, "20260101T010000Z-11111111")
    write(run / "result.json", {"run_id": run.name, "child_exit_code": 0})
    age(run, 1000)
    age(scratch, 1000)
    monkeypatch.setattr("astock.operations.process_identity", lambda pid: identity)
    plan = service.automatic_plan()
    candidate = next(c for c in plan.candidates if c.relative_path == (
        scratch.relative_to(tmp_path).as_posix()
    ))
    assert candidate.referenced and not candidate.eligible
    assert service.run(plan, confirm=True).deleted_bytes == 0
    assert scratch.is_dir()


@pytest.mark.parametrize("identity", [
    ProcessIdentity("DEAD"), ProcessIdentity("ALIVE", 2000.0),
])
def test_dead_or_reused_owner_requires_orphan_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: ProcessIdentity,
) -> None:
    service = service_at(tmp_path)
    _, scratch = quality_owner(service, "20260101T010000Z-11111111")
    age(scratch, 3)
    monkeypatch.setattr("astock.operations.process_identity", lambda pid: identity)
    first = service.automatic_plan()
    candidate = next(c for c in first.candidates if c.relative_path.startswith(".scratchpad/"))
    assert not candidate.eligible
    age(scratch, 30)
    final = service.automatic_plan()
    run = service.run(final, confirm=True)
    assert run.deleted_bytes == len(b"temporary")
    assert not scratch.exists()


def test_unconfirmed_descendants_remain_protected_after_parent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    run, scratch = quality_owner(service, "20260101T010000Z-11111111")
    write(run / "result.json", {
        "run_id": run.name, "child_exit_code": 1, "returncode": 1,
        "owned_worker_reaped": False, "failure_reason": "TERMINATION_UNCONFIRMED",
    })
    age(run, 1000)
    age(scratch, 1000)
    monkeypatch.setattr("astock.operations.process_identity", lambda pid: ProcessIdentity("DEAD"))
    plan = service.automatic_plan()
    assert not any(candidate.eligible for candidate in plan.candidates)
    assert service.run(plan, confirm=True).deleted_bytes == 0
    assert scratch.is_dir()


def test_unowned_age_alone_does_not_prove_orphan(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    scratch = tmp_path / ".ai-bridge" / "quality-runs" / "unknown" / "tmp"
    write(scratch / "data", b"unknown work")
    age(scratch, 1000)
    plan = service.automatic_plan()
    candidate = next(c for c in plan.candidates if c.relative_path.endswith("/tmp"))
    assert candidate.referenced and not candidate.eligible
    assert service.run(plan, confirm=True).deleted_bytes == 0


def test_quality_evidence_ttl_keeps_current_references_and_recent_receipts(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    service.policy = service.policy.model_copy(update={
        "quality_run_evidence": service.policy.quality_run_evidence.model_copy(
            update={"keep_latest": 1}
        ),
    })
    roots = []
    for index in range(3):
        path = tmp_path / ".ai-bridge" / "quality-runs" / f"2026010{index + 1}T010000Z-11111111"
        write(path / "result.json", {"run_id": path.name, "child_exit_code": 1})
        write(path / "output.log", b"old diagnostic")
        age(path, 1000)
        roots.append(path)
    write(tmp_path / "docs" / "current.md", str(roots[0].relative_to(tmp_path)).encode())
    plan = service.automatic_plan()
    eligible = {c.relative_path for c in plan.candidates if c.eligible}
    assert eligible == {roots[1].relative_to(tmp_path).as_posix()}
    run = service.run(plan, confirm=True)
    assert run.deleted_directory_count == 1
    assert roots[0].exists() and not roots[1].exists() and roots[2].exists()


def test_new_reference_after_plan_prevents_quality_receipt_deletion(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    service.policy = service.policy.model_copy(update={
        "quality_run_evidence": service.policy.quality_run_evidence.model_copy(
            update={"keep_latest": 0}
        ),
    })
    path = tmp_path / ".ai-bridge" / "quality-runs" / "20260101T010000Z-11111111"
    write(path / "result.json", {"child_exit_code": 0})
    age(path, 1000)
    plan = service.automatic_plan()
    assert any(c.eligible for c in plan.candidates)
    write(tmp_path / "README.md", str(path.relative_to(tmp_path)).encode())
    run = service.run(plan, confirm=True)
    assert path.exists()
    assert any(reason.startswith("BECAME_REFERENCED_QUALITY_RUN") for reason in run.skip_reasons)


def test_reviewed_scratch_immediate_cleanup_and_empty_directory_accounting(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    target = tmp_path / "scratch" / "reviewed"
    (target / "empty").mkdir(parents=True)
    write(target / "test.db", b"regenerable")
    write(tmp_path / "scratch" / "unknown" / "source.py", b"preserve")
    review_marker(service, target)
    empty_roots = [tmp_path / "runtime" / name for name in ("backups", "tmp")]
    for empty in empty_roots:
        review_marker(service, empty)
    plan = service.automatic_plan()
    assert service.audit(plan).status == "PASS"
    candidate = next(c for c in plan.candidates if c.relative_path == "scratch/reviewed")
    assert candidate.eligible and candidate.reason == "REVIEWED_DISPOSABLE_SCRATCH"
    run = service.run(plan, confirm=True)
    assert not target.exists() and not any(path.exists() for path in empty_roots)
    assert (tmp_path / "scratch" / "unknown" / "source.py").is_file()
    assert run.deleted_directory_count == 4


def test_misbound_marker_and_incomplete_snapshot_are_not_disposable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    target = tmp_path / "scratch" / "reviewed"
    write(target / "data", b"keep")
    review_marker(service, target)
    marker = json.loads((target / ".cleanup-ready").read_text())
    marker["relative_path"] = "scratch/another-task"
    write(target / ".cleanup-ready", marker)
    assert not any(c.eligible for c in service.automatic_plan().candidates)
    review_marker(service, target)
    actual = snapshot_tree
    monkeypatch.setattr(
        "astock.operations.snapshot_tree",
        lambda path: replace(actual(path), complete=False) if path == target else actual(path),
    )
    candidate = next(
        c for c in service.automatic_plan().candidates if c.relative_path == "scratch/reviewed"
    )
    assert not candidate.scan_complete and not candidate.eligible


def test_longrun_scratch_with_live_ancestor_owner_survives_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    run = tmp_path / "runtime" / "longrun" / "active"
    scratch = run / "pytest-old"
    write(scratch / "data", b"still needed")
    write(run / "owner.json", {
        "schema_version": "astock-owned-run-v1",
        "owner_pid": 12345, "owner_create_time": 1000.0,
    })
    age(run, 1000)
    monkeypatch.setattr(
        "astock.operations.process_identity", lambda pid: ProcessIdentity("ALIVE", 1000.0),
    )
    plan = service.automatic_plan()
    candidate = next(c for c in plan.candidates if c.category == "LONGRUN_SCRATCH")
    assert candidate.referenced and not candidate.eligible
    service.run(plan, confirm=True)
    assert (scratch / "data").is_file()


def test_truncated_canonical_reference_inventory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    with service.state.transaction() as connection:
        connection.execute("CREATE TABLE reference_fixture(object_hash TEXT)")
        connection.executemany(
            "INSERT INTO reference_fixture VALUES(?)",
            [(f"{index:064x}",) for index in range(3)],
        )
    monkeypatch.setattr("astock.operations._REFERENCE_SCAN_LIMIT", 2)
    with pytest.raises(ValueError, match="reference inventory exceeds"):
        service._referenced_hashes()


def test_unregistered_worktree_without_disposal_proof_keeps_source(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    source = tmp_path / "runtime" / "worktrees" / "unregistered"
    write(source / "unfinished.py", b"preserve unknown changes")
    age(source, 1000)
    plan = service.automatic_plan()
    candidate = next(c for c in plan.candidates if c.category == "STALE_WORKTREE")
    assert candidate.referenced and not candidate.eligible
    assert candidate.reason == "WORKTREE_OWNERSHIP_UNVERIFIED"
    service.run(plan, confirm=True)
    assert (source / "unfinished.py").is_file()


def test_owner_unconfirmed_descendants_protect_when_result_was_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    run, scratch = quality_owner(service, "20260101T010000Z-11111111")
    owner = json.loads((run / "owner.json").read_text())
    owner.update(status="FAILED", owned_worker_reaped=False)
    write(run / "owner.json", owner)
    age(run, 1000)
    age(scratch, 1000)
    monkeypatch.setattr("astock.operations.process_identity", lambda pid: ProcessIdentity("DEAD"))
    assert not any(candidate.eligible for candidate in service.automatic_plan().candidates)


def test_nested_redirect_is_removed_without_traversing_target(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    target = tmp_path / "scratch" / "reviewed"
    outside = tmp_path / "protected"
    write(target / "temporary", b"throw away")
    write(outside / "original", b"preserve")
    link = target / "current"
    try:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                check=True, capture_output=True, timeout=5,
            )
        else:
            link.symlink_to(outside, target_is_directory=True)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("platform does not permit a directory redirect fixture")
    assert path_has_redirect(tmp_path, link / "original")
    review_marker(service, target)
    snapshot = snapshot_tree(target)
    assert snapshot.complete and snapshot.link_count == 1
    assert snapshot.byte_size < 1000
    plan = service.automatic_plan()
    service.run(plan, confirm=True)
    assert (outside / "original").read_bytes() == b"preserve"
    assert not target.exists()



def test_registered_worktree_review_removes_only_temporary_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    linked = tmp_path / "runtime" / "worktrees" / "registered"
    source = linked / "src"
    scratch = linked / "runtime" / "tmp"
    cache = linked / ".ai-bridge" / "audit" / "uv-cache"
    quality_cache = linked / ".ai-bridge" / "quality-cache"
    write(source / "unfinished.py", b"unique uncommitted work")
    write(scratch / "fixture.db", b"reproducible")
    write(cache / "download.whl", b"reproducible")
    write(quality_cache / "example", b"reproducible")
    monkeypatch.setattr("astock.operations.active_linked_worktree_roots", lambda root: {linked})
    for path in (linked, source, scratch, cache, quality_cache):
        review_marker(service, path)
    plan = service.automatic_plan()
    assert not service._allowed_candidate_path(linked, "REVIEWED_SCRATCH")
    assert not service._allowed_candidate_path(source, "REVIEWED_SCRATCH")
    assert {c.relative_path for c in plan.candidates if c.eligible} == {
        scratch.relative_to(tmp_path).as_posix(), cache.relative_to(tmp_path).as_posix(),
        quality_cache.relative_to(tmp_path).as_posix(),
    }
    service.run(plan, confirm=True)
    assert (source / "unfinished.py").is_file()
    assert not scratch.exists() and not cache.exists() and not quality_cache.exists()


def test_reviewed_worktree_cache_cannot_bypass_unknown_ancestor_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_at(tmp_path)
    linked = tmp_path / "runtime" / "worktrees" / "registered"
    cache = linked / ".ai-bridge" / "audit" / "uv-cache"
    write(cache / "unfinished", b"protect")
    write(linked / "owner.json", {
        "schema_version": "astock-owned-run-v1", "owner_pid": 12345,
        "owner_create_time": 1000.0, "status": "RUNNING",
    })
    monkeypatch.setattr("astock.operations.active_linked_worktree_roots", lambda root: {linked})
    monkeypatch.setattr(
        "astock.operations.process_identity", lambda pid: ProcessIdentity("UNKNOWN"),
    )
    review_marker(service, cache)
    age(cache, 1000)
    plan = service.automatic_plan()
    assert not any(c.eligible for c in plan.candidates)
    service.run(plan, confirm=True)
    assert (cache / "unfinished").is_file()



def test_removed_protected_scratch_does_not_block_a_persisted_plan(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    protected = tmp_path / ".scratchpad" / "owner-cleans-this"
    reviewed = tmp_path / "scratch" / "reviewed"
    write(protected / "data", b"belongs to another owner")
    write(reviewed / "data", b"disposable")
    review_marker(service, reviewed)
    plan = service.automatic_plan()
    service.persist_plan(plan)
    (protected / "data").unlink()
    protected.rmdir()  # Its owning runner finishes before the separate cleanup invocation.
    restored = service.load_plan(plan.plan_id)
    assert restored is not None and service.audit(restored).status == "PASS"
    service.run(restored, confirm=True)
    assert not reviewed.exists()



@pytest.mark.parametrize("unreadable", ["directory", "gitdir", "dangling_redirect"])
def test_unreadable_git_worktree_registry_cannot_delete_unique_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str,
) -> None:
    service = service_at(tmp_path)
    source = tmp_path / "runtime" / "worktrees" / "unique-work"
    write(source / "unfinished.py", b"unique uncommitted source")
    review_marker(service, source)
    age(source, 1000)
    # A genuinely absent registry is distinguishable from a later unreadable one.
    plan = service.automatic_plan()
    assert any(c.eligible and c.relative_path.endswith("unique-work") for c in plan.candidates)
    metadata_root = tmp_path / ".git" / "worktrees"
    gitdir = metadata_root / "registered" / "gitdir"
    write(gitdir, str(source / ".git").encode("utf-8"))
    original_iterdir, original_read = Path.iterdir, Path.read_text

    def inaccessible_directory(path: Path):
        if path == metadata_root:
            raise PermissionError("registry temporarily unreadable")
        return original_iterdir(path)

    def inaccessible_gitdir(path: Path, *args, **kwargs):
        if path == gitdir:
            raise PermissionError("registration temporarily unreadable")
        return original_read(path, *args, **kwargs)

    if unreadable == "directory":
        monkeypatch.setattr(Path, "iterdir", inaccessible_directory)
    elif unreadable == "gitdir":
        monkeypatch.setattr(Path, "read_text", inaccessible_gitdir)
    else:
        gitdir.unlink()
        gitdir.parent.rmdir()
        metadata_root.rmdir()
        target = tmp_path / "registry-target"
        target.mkdir()
        try:
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(metadata_root), str(target)],
                    check=True, capture_output=True, timeout=5,
                )
            else:
                metadata_root.symlink_to(target, target_is_directory=True)
        except (OSError, subprocess.SubprocessError):
            pytest.skip("platform does not permit a directory redirect fixture")
        target.rmdir()
    with pytest.raises(ValueError, match="Git worktree inventory is incomplete"):
        service.automatic_plan()
    with pytest.raises(ValueError, match="Git worktree inventory is incomplete"):
        service.run(plan, confirm=True)
    assert (source / "unfinished.py").read_bytes() == b"unique uncommitted source"
