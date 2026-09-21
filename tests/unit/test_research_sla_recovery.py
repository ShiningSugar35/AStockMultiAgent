"""Failure-first acceptance for durable scheduler and target recovery."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_sla import (
    LlmTakeoverResult,
    LlmTakeoverResultStatus,
    ResearchSchedulerRunStatus,
    ResearchSchedulerTaskStatus,
    ResearchTargetState,
    ResearchTaskCategory,
)

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 17, tzinfo=UTC)
SPECS = [("fetch", "A", ResearchTaskCategory.NETWORK, [], "input-v1")]


@pytest.fixture
def service(tmp_path: Path) -> CurrentResearchSlaService:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    return CurrentResearchSlaService(state, ObjectStore(tmp_path / "objects"), clock=lambda: START)


@pytest.mark.parametrize("change", ["input", "candidate", "category", "dependency", "budget"])
def test_request_identity_rejects_changed_contract(service, change):
    service.create_run(request_id="same", task_specs=SPECS, budget_seconds=1200)
    specs = list(SPECS)
    budget = 1200
    if change == "input":
        specs = [("fetch", "A", ResearchTaskCategory.NETWORK, [], "input-v2")]
    elif change == "candidate":
        specs = [("fetch", "B", ResearchTaskCategory.NETWORK, [], "input-v1")]
    elif change == "category":
        specs = [("fetch", "A", ResearchTaskCategory.CPU, [], "input-v1")]
    elif change == "dependency":
        specs += [("analyze", "A", ResearchTaskCategory.LLM, ["fetch"], "analysis-v1")]
    else:
        budget += 1
    with pytest.raises(ValueError, match="request.*(contract|input|graph|budget)"):
        service.create_run(request_id="same", task_specs=specs, budget_seconds=budget)


def test_same_request_preserves_deadline_and_accepts_reordered_specs(service):
    specs = SPECS + [("shared", None, ResearchTaskCategory.CPU, [], "shared-v1")]
    first = service.create_run(request_id="same", task_specs=specs, budget_seconds=1200)
    service.clock = lambda: START + timedelta(seconds=300)
    second = service.create_run(
        request_id="same", task_specs=list(reversed(specs)), budget_seconds=1200
    )
    assert second == first


def test_concurrent_first_creation_returns_one_canonical_run(service, monkeypatch):
    barrier = Barrier(2)
    original = service._validate_task_graph

    def both_validate(tasks):
        original(tasks)
        barrier.wait(timeout=10)

    monkeypatch.setattr(service, "_validate_task_graph", both_validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.create_run, request_id="race", task_specs=SPECS) for _ in range(2)
        ]
        runs = [future.result(timeout=20) for future in futures]
    assert runs[0] == runs[1]
    assert len(service.tasks(runs[0].run_id)) == 1


def test_finished_run_receipt_is_not_rewritten_by_execute(service):
    run = service.create_run(request_id="finished", task_specs=SPECS)
    first = service.execute(run.run_id, {"fetch": lambda _task: "artifact:ok"})
    service.clock = lambda: START + timedelta(seconds=20)
    second = service.execute(run.run_id, {})
    assert second == first


def test_stale_task_snapshot_cannot_overwrite_completed_result(service):
    run = service.create_run(request_id="stale", task_specs=SPECS)
    started = service._transition_tasks_running(service.tasks(run.run_id), run=run, at=START)[0]
    service._transition_task(
        started, ResearchSchedulerTaskStatus.COMPLETED, result_artifact_id="artifact:ok", at=START
    )
    with pytest.raises(ValueError, match="stale"):
        service._transition_task(
            started, ResearchSchedulerTaskStatus.FAILED, error_code="late", at=START
        )
    assert service.tasks(run.run_id)[0].result_artifact_id == "artifact:ok"


def test_expired_task_lease_recovers_without_resetting_deadline(service):
    run = service.create_run(request_id="recover", task_specs=SPECS, budget_seconds=1200)
    started = service._transition_tasks_running(service.tasks(run.run_id), run=run, at=START)[0]
    assert service.recover_expired_tasks(run.run_id) == 0
    service.clock = lambda: START + timedelta(seconds=601)
    assert service.recover_expired_tasks(run.run_id) == 1
    with pytest.raises(ValueError, match="stale"):
        service._transition_task(
            started,
            ResearchSchedulerTaskStatus.COMPLETED,
            result_artifact_id="artifact:late",
            at=service.clock(),
        )
    result = service.execute(run.run_id, {"fetch": lambda _task: "artifact:recovered"})
    assert result.status is ResearchSchedulerRunStatus.COMPLETED
    assert result.deadline_at == run.deadline_at


def _upsert(service, *, priority=1, reason="discovery"):
    return service.upsert_target(
        instrument_id="XSHE:002155",
        company_id="002155",
        state=ResearchTargetState.POTENTIAL,
        source_reason=reason,
        dependency_fingerprint="dep-v1",
        module_versions={"financial": "f1"},
        priority=priority,
    )


def _registry(service):
    return ResearchSubjectRegistryService(InvestorOrchestrationStore(service.state.path))


def test_target_priority_and_reason_update_reaches_existing_subject_registry(service):
    _upsert(service)
    service.clock = lambda: START + timedelta(seconds=1)
    _upsert(service, priority=7, reason="user follows")
    watch = _registry(service).current_watchlist()
    assert watch[0].metadata["priority"] == 7
    assert watch[0].reason == "user follows"


def test_target_projection_retry_repairs_interrupted_subject_sync(service, monkeypatch):
    original = service._sync_subject_registry

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected interrupted sync")

    monkeypatch.setattr(service, "_sync_subject_registry", unavailable)
    with pytest.raises(RuntimeError, match="interrupted"):
        _upsert(service)
    monkeypatch.setattr(service, "_sync_subject_registry", original)
    _upsert(service)
    assert len(_registry(service).current_watchlist()) == 1


def _takeover(service):
    run = service.create_run(
        request_id="takeover",
        task_specs=SPECS + [("child", "A", ResearchTaskCategory.CPU, ["fetch"], "child-v1")],
        budget_seconds=1200,
    )

    def fail(_task):
        raise RuntimeError("provider failure")

    service.execute(run.run_id, {"fetch": fail})
    task = next(task for task in service.tasks(run.run_id) if task.node_id == "fetch")
    packet = service.takeover_packet(
        request_id=run.request_id,
        task_id=task.task_id,
        trusted_artifact_ids=[],
        attempted_actions=[],
        missing_requirement="missing source",
        allowed_write_paths=[],
        expected_output_schema="RecoveredSource",
        dependency_fingerprint="dep-v1",
    )
    obj = service.objects.put_json({"schema_version": "test-source-v1"})
    service.state.register_artifact(
        artifact_id="source:repaired",
        artifact_type="RecoveredSource",
        schema_version="test-source-v1",
        object_hash=obj.sha256,
        input_hashes=[],
    )
    result = LlmTakeoverResult(
        packet_id=packet.packet_id,
        request_id=packet.request_id,
        run_id=packet.run_id,
        task_id=task.task_id,
        generation=packet.generation,
        dependency_fingerprint=packet.dependency_fingerprint,
        status=LlmTakeoverResultStatus.RESOLVED,
        result_artifact_id="source:repaired",
    )
    return run, task, packet, result


def test_takeover_rechecks_deadline_after_expensive_validation(service):
    run, task, _, result = _takeover(service)

    def slow_validate(_artifact_id):
        service.clock = lambda: run.deadline_at + timedelta(seconds=1)

    with pytest.raises(ValueError, match="deadline"):
        service.apply_takeover_result(result, validate_result=slow_validate)
    assert service._task(task.task_id).status is ResearchSchedulerTaskStatus.FAILED


def test_takeover_packet_failure_rolls_back_task_and_descendants(service):
    run, task, _, result = _takeover(service)
    before = service.tasks(run.run_id)
    # Fault injection is confined to the fixture database, never production state.
    with service.state.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_takeover BEFORE UPDATE ON research_llm_takeover "
            "WHEN NEW.status='RESOLVED' "
            "BEGIN SELECT RAISE(ABORT, 'injected packet failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.apply_takeover_result(result, validate_result=lambda _artifact_id: None)
    assert service._task(task.task_id).status is ResearchSchedulerTaskStatus.FAILED
    assert service.tasks(run.run_id) == before


def test_unexpired_interrupted_branch_does_not_block_independent_ready_work(service):
    specs = SPECS + [("independent", "B", ResearchTaskCategory.CPU, [], "other-v1")]
    run = service.create_run(request_id="independent-recovery", task_specs=specs)
    fetch = next(task for task in service.tasks(run.run_id) if task.node_id == "fetch")
    service._transition_tasks_running([fetch], run=run, at=START)
    called = []

    def independent(_task):
        called.append("independent")
        return "artifact:independent"

    receipt = service.execute(run.run_id, {"independent": independent})
    assert called == ["independent"]
    assert receipt.status is ResearchSchedulerRunStatus.RUNNING
    states = {task.node_id: task.status for task in service.tasks(run.run_id)}
    assert states["fetch"] is ResearchSchedulerTaskStatus.RUNNING
    assert states["independent"] is ResearchSchedulerTaskStatus.COMPLETED
    service.clock = lambda: START + timedelta(seconds=601)
    final = service.execute(run.run_id, {"fetch": lambda _task: "artifact:recovered"})
    assert final.status is ResearchSchedulerRunStatus.COMPLETED
    assert called == ["independent"]


def test_shared_reducer_can_depend_on_all_candidate_nodes(service):
    specs = [
        ("evaluate", "A", ResearchTaskCategory.NETWORK, [], "a"),
        ("evaluate", "B", ResearchTaskCategory.NETWORK, [], "b"),
        ("rank", None, ResearchTaskCategory.CPU, ["evaluate"], "ranking-v1"),
    ]
    run = service.create_run(request_id="fan-in", task_specs=specs)
    reducer = next(task for task in service.tasks(run.run_id) if task.node_id == "rank")
    assert len(reducer.dependencies) == 2

    def rank(_task):
        assert all(
            task.status is ResearchSchedulerTaskStatus.COMPLETED
            for task in service.tasks(run.run_id)
            if task.node_id == "evaluate"
        )
        return "artifact:ranked"

    receipt = service.execute(
        run.run_id,
        {
            "evaluate": lambda task: f"artifact:{task.candidate_id}",
            "rank": rank,
        },
    )
    assert receipt.status is ResearchSchedulerRunStatus.COMPLETED


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_output_schema", "AnotherOutput"),
        ("trusted_artifact_ids", ["source:another"]),
        ("attempted_actions", ["another action"]),
        ("missing_requirement", "another missing fact"),
        ("allowed_write_paths", ["runtime/another-repair"]),
    ],
)
def test_pending_takeover_rejects_changed_full_contract(service, field, value):
    _, _, packet, _ = _takeover(service)
    arguments = {
        "request_id": packet.request_id,
        "task_id": packet.task_id,
        "trusted_artifact_ids": packet.trusted_artifact_ids,
        "attempted_actions": packet.attempted_actions,
        "missing_requirement": packet.missing_requirement,
        "allowed_write_paths": packet.allowed_write_paths,
        "expected_output_schema": packet.expected_output_schema,
        "dependency_fingerprint": packet.dependency_fingerprint,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match="contract"):
        service.takeover_packet(**arguments)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside",
        "/tmp/outside",
        "C:\\outside",
        "src/../../outside",
        "\\\\server\\share",
    ],
)
def test_takeover_write_scope_cannot_escape_project(service, unsafe_path):
    _, _, packet, _ = _takeover(service)
    with pytest.raises(ValueError, match="path"):
        service.takeover_packet(
            request_id=packet.request_id,
            task_id=packet.task_id,
            trusted_artifact_ids=[],
            attempted_actions=[],
            missing_requirement=packet.missing_requirement,
            allowed_write_paths=[unsafe_path],
            expected_output_schema=packet.expected_output_schema,
            dependency_fingerprint=packet.dependency_fingerprint,
        )
