"""Independent WP35/36 acceptance: takeover while other graph work is live.

Faults are injected only into a per-test database/object directory. These tests do
not modify production state or treat a synthetic handler as investment evidence.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_sla import (
    LlmTakeoverPacket,
    LlmTakeoverResult,
    LlmTakeoverResultStatus,
    ResearchSchedulerRun,
    ResearchSchedulerRunStatus,
    ResearchSchedulerTask,
    ResearchSchedulerTaskStatus,
    ResearchTaskCategory,
)

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 18, tzinfo=UTC)


@pytest.fixture
def scheduler(tmp_path: Path) -> CurrentResearchSlaService:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    return CurrentResearchSlaService(state, ObjectStore(tmp_path / "objects"), clock=lambda: START)


def _register(service: CurrentResearchSlaService, name: str) -> str:
    ref = service.objects.put_json({"schema_version": "acceptance-source-v1", "name": name})
    artifact_id = f"acceptance-source:{name}"
    service.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="AcceptanceSource",
        schema_version="acceptance-source-v1",
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return artifact_id


def _fail(_task: ResearchSchedulerTask) -> str:
    raise RuntimeError("isolated provider failure")


def _failed_run(
    service: CurrentResearchSlaService,
) -> tuple[ResearchSchedulerRun, ResearchSchedulerTask]:
    run = service.create_run(
        request_id="independent-takeover-acceptance",
        task_specs=[("fetch", "XSHE:000001", ResearchTaskCategory.NETWORK, [], "input-v1")],
        budget_seconds=1200,
    )
    service.execute(run.run_id, {"fetch": _fail})
    return run, service.tasks(run.run_id)[0]


def _packet(
    service: CurrentResearchSlaService,
    run: ResearchSchedulerRun,
    task: ResearchSchedulerTask,
    trusted_artifact_ids: tuple[str, ...] = (),
) -> LlmTakeoverPacket:
    return service.takeover_packet(
        request_id=run.request_id,
        task_id=task.task_id,
        trusted_artifact_ids=trusted_artifact_ids,
        attempted_actions=["bounded recorded provider attempt"],
        missing_requirement="recover the exact failed node",
        allowed_write_paths=[],
        expected_output_schema="AcceptanceSource",
        dependency_fingerprint=task.input_fingerprint,
    )


def _result(packet: LlmTakeoverPacket, artifact_id: str) -> LlmTakeoverResult:
    return LlmTakeoverResult(
        packet_id=packet.packet_id,
        request_id=packet.request_id,
        run_id=packet.run_id,
        task_id=packet.task_id,
        generation=packet.generation,
        dependency_fingerprint=packet.dependency_fingerprint,
        status=LlmTakeoverResultStatus.RESOLVED,
        result_artifact_id=artifact_id,
    )


def _pending_count(service: CurrentResearchSlaService) -> int:
    with service.state.connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM research_llm_takeover").fetchone()[0])


def test_takeover_success_during_another_live_branch_keeps_suffix_runnable(scheduler):
    """A live executor must not seal a stale PARTIAL over a repaired READY suffix."""
    service = scheduler
    specs = [
        ("fetch", "XSHE:000001", ResearchTaskCategory.NETWORK, [], "input-v1"),
        ("analyze", "XSHE:000001", ResearchTaskCategory.CPU, ["fetch"], "analysis-v1"),
        ("unrelated", "XSHG:600000", ResearchTaskCategory.NETWORK, [], "other-v1"),
    ]
    run = service.create_run(
        request_id="live-takeover-overlap", task_specs=specs, budget_seconds=1200
    )
    repaired_id = _register(service, "repaired")
    unrelated_id = _register(service, "unrelated")
    analysis_id = _register(service, "analysis")
    entered = Event()
    release = Event()
    calls: list[str] = []

    def unrelated(_task: ResearchSchedulerTask) -> str:
        calls.append("unrelated")
        entered.set()
        if not release.wait(timeout=15):
            raise RuntimeError("test synchronization timed out")
        return unrelated_id

    def analyze(_task: ResearchSchedulerTask) -> str:
        calls.append("analyze")
        return analysis_id

    handlers = {"fetch": _fail, "unrelated": unrelated, "analyze": analyze}
    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(service.execute, run.run_id, handlers)
        try:
            assert entered.wait(timeout=10)
            end = time.monotonic() + 10
            while True:
                tasks = {task.node_id: task for task in service.tasks(run.run_id)}
                if (
                    tasks["fetch"].status is ResearchSchedulerTaskStatus.FAILED
                    and tasks["analyze"].status is ResearchSchedulerTaskStatus.CANCELLED
                ):
                    break
                assert time.monotonic() < end, "executor did not persist the injected failure"
                time.sleep(0.02)
            packet = _packet(service, run, tasks["fetch"])
            service.apply_takeover_result(
                _result(packet, repaired_id), validate_result=lambda _artifact_id: None
            )
            assert (
                service._task(tasks["analyze"].task_id).status is ResearchSchedulerTaskStatus.READY
            )
        finally:
            release.set()
        first_receipt = execution.result(timeout=15)
    # An executor may yield after detecting a concurrent revision, but the same
    # graph must remain resumable and retain successes. No new request is allowed.
    assert first_receipt.status in {
        ResearchSchedulerRunStatus.RUNNING,
        ResearchSchedulerRunStatus.COMPLETED,
    }, "stale execution sealed an unfinished, already repaired graph"
    final = service.execute(run.run_id, handlers)
    assert final.status is ResearchSchedulerRunStatus.COMPLETED
    assert final.deadline_at == run.deadline_at
    assert calls.count("unrelated") == 1
    assert calls.count("analyze") == 1
    assert all(
        task.status is ResearchSchedulerTaskStatus.COMPLETED for task in service.tasks(run.run_id)
    )


def test_takeover_packet_cannot_mark_an_unregistered_reference_trusted(scheduler):
    run, task = _failed_run(scheduler)
    with pytest.raises(ValueError):
        _packet(scheduler, run, task, ("acceptance-source:does-not-exist",))
    assert _pending_count(scheduler) == 0


def test_takeover_packet_rejects_corrupt_trusted_source_without_consuming_a_round(scheduler):
    run, task = _failed_run(scheduler)
    source_id = _register(scheduler, "trusted")
    record = scheduler.state.artifact_record(source_id)
    assert record is not None
    # Deliberate byte corruption in the fixture ObjectStore, never shared runtime.
    scheduler.objects.path_for(str(record["object_hash"])).write_bytes(b"corrupt fixture")
    with pytest.raises((ValueError, StorageError)):
        _packet(scheduler, run, task, (source_id,))
    assert _pending_count(scheduler) == 0


def test_takeover_packet_accepts_registered_intact_source_and_reuses_pending_round(scheduler):
    run, task = _failed_run(scheduler)
    source_id = _register(scheduler, "trusted")
    packet = _packet(scheduler, run, task, (source_id,))
    assert _packet(scheduler, run, task, (source_id,)) == packet
    assert _pending_count(scheduler) == 1


@pytest.mark.parametrize("field", ["request_id", "run_id", "task_id", "dependency_fingerprint"])
def test_result_for_another_context_never_consumes_pending_packet(scheduler, field):
    run, task = _failed_run(scheduler)
    packet = _packet(scheduler, run, task)
    result = _result(packet, _register(scheduler, "repair"))
    result = result.model_copy(update={field: "other-context"})
    before = scheduler.tasks(run.run_id)
    with pytest.raises(ValueError):
        scheduler.apply_takeover_result(result, validate_result=lambda _artifact_id: None)
    assert scheduler.tasks(run.run_id) == before
    with scheduler.state.connect() as connection:
        row = connection.execute(
            "SELECT status FROM research_llm_takeover WHERE packet_id=?", (packet.packet_id,)
        ).fetchone()
    assert row["status"] == "PENDING"


def test_repair_between_dependency_check_and_cancel_does_not_cancel_valid_suffix(
    scheduler, monkeypatch
):
    service = scheduler
    run = service.create_run(
        request_id="repair-at-dependency-transition",
        task_specs=[
            ("fetch", "XSHE:000001", ResearchTaskCategory.NETWORK, [], "input-v1"),
            ("analyze", "XSHE:000001", ResearchTaskCategory.CPU, ["fetch"], "analysis-v1"),
        ],
        budget_seconds=1200,
    )
    repaired = _register(service, "parent-repair")
    analysis = _register(service, "dependent-analysis")
    original = service._transition_cached_task
    injected = False

    def interleaved(connection, current_run, task, status, **kwargs):
        nonlocal injected
        if (
            not injected
            and task.node_id == "analyze"
            and status is ResearchSchedulerTaskStatus.CANCELLED
            and kwargs.get("error_code") == "DEPENDENCY_NOT_COMPLETED"
        ):
            injected = True
            parent = next(item for item in service.tasks(run.run_id) if item.node_id == "fetch")
            packet = _packet(service, run, parent)
            service.apply_takeover_result(
                _result(packet, repaired), validate_result=lambda _artifact_id: None
            )
        return original(connection, current_run, task, status, **kwargs)

    monkeypatch.setattr(service, "_transition_cached_task", interleaved)
    result = service.execute(run.run_id, {"fetch": _fail, "analyze": lambda _task: analysis})
    if result.status is ResearchSchedulerRunStatus.RUNNING:
        result = service.execute(run.run_id, {"analyze": lambda _task: analysis})
    assert injected
    assert result.status is ResearchSchedulerRunStatus.COMPLETED
    assert result.deadline_at == run.deadline_at
    assert all(
        task.status is ResearchSchedulerTaskStatus.COMPLETED for task in service.tasks(run.run_id)
    )
