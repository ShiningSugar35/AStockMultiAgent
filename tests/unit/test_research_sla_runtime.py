from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> CurrentResearchSlaService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return CurrentResearchSlaService(state, ObjectStore(tmp_path / "objects"))


def test_target_pool_is_idempotent_and_incremental_closure_is_precise(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.upsert_target(
        instrument_id="XSHE:002155",
        company_id="002155",
        state=ResearchTargetState.RESEARCHED,
        source_reason="formal research",
        dependency_fingerprint="dep-v1",
        module_versions={
            "financial": "f1",
            "governance": "g1",
            "valuation": "v1",
            "committee": "c1",
            "portfolio": "p1",
        },
        triggers=["FINANCIAL_REPORT", "PRICE_TRIGGER"],
        priority=10,
    )
    second = service.upsert_target(
        instrument_id="XSHE:002155",
        company_id="002155",
        state=ResearchTargetState.REVIEW_DUE,
        source_reason="new interim report",
        dependency_fingerprint="dep-v2",
        module_versions={
            "financial": "f2",
            "governance": "g1",
            "valuation": "v1",
            "committee": "c1",
            "portfolio": "p1",
        },
        triggers=["FINANCIAL_REPORT", "PRICE_TRIGGER"],
        priority=20,
    )
    assert first.target_id == second.target_id
    assert service.active_targets()[0].state is ResearchTargetState.REVIEW_DUE

    plan = service.incremental_plan(
        second,
        current_versions={
            "financial": "f3",
            "governance": "g1",
            "valuation": "v1",
            "committee": "c1",
            "portfolio": "p1",
        },
        dependencies={
            "valuation": ["financial"],
            "committee": ["valuation", "governance"],
            "portfolio": ["committee"],
        },
    )
    assert plan.changed_modules == ["financial"]
    assert plan.rerun_modules == ["committee", "financial", "portfolio", "valuation"]
    assert plan.unchanged_modules == ["governance"]

    registry = ResearchSubjectRegistryService(InvestorOrchestrationStore(service.state.path))
    watchlist = registry.current_watchlist()
    assert len(watchlist) == 1
    assert watchlist[0].instrument_id == "XSHE:002155"
    assert watchlist[0].metadata["target_state"] == ResearchTargetState.REVIEW_DUE.value

    service.upsert_target(
        instrument_id="XSHE:002155",
        company_id="002155",
        state=ResearchTargetState.EXITED,
        source_reason="no longer tracked",
        dependency_fingerprint="dep-v3",
        module_versions=second.module_versions,
    )
    assert registry.current_watchlist() == ()


def test_target_pool_rejects_cross_company_identity_reuse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.upsert_target(
        instrument_id="XSHG:600000",
        company_id="600000",
        state=ResearchTargetState.POTENTIAL,
        source_reason="screen",
        dependency_fingerprint="a",
        module_versions={},
    )
    with pytest.raises(ValueError, match="company identity"):
        service.upsert_target(
            instrument_id="XSHG:600000",
            company_id="600001",
            state=ResearchTargetState.POTENTIAL,
            source_reason="wrong identity",
            dependency_fingerprint="b",
            module_versions={},
        )


def test_scheduler_runs_independent_backend_and_llm_tasks_in_parallel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(
        request_id="parallel-request",
        budget_seconds=30,
        task_specs=[
            ("fetch-a", "A", ResearchTaskCategory.NETWORK, [], "fetch-a-v1"),
            ("analyze-b", "B", ResearchTaskCategory.LLM, [], "analyze-b-v1"),
            ("join-a", "A", ResearchTaskCategory.CPU, ["fetch-a"], "join-a-v1"),
        ],
    )
    starts: dict[str, float] = {}
    finishes: dict[str, float] = {}
    lock = threading.Lock()

    def handler(task):
        with lock:
            starts[task.node_id] = time.perf_counter()
        if task.node_id in {"fetch-a", "analyze-b"}:
            time.sleep(0.18)
        else:
            time.sleep(0.02)
        with lock:
            finishes[task.node_id] = time.perf_counter()
        return f"artifact:{task.node_id}"

    started = time.perf_counter()
    result = service.execute(
        run.run_id,
        {"fetch-a": handler, "analyze-b": handler, "join-a": handler},
        resource_limits={
            ResearchTaskCategory.NETWORK: 1,
            ResearchTaskCategory.CPU: 1,
            ResearchTaskCategory.LLM: 1,
            ResearchTaskCategory.WRITE: 1,
        },
    )
    wall = time.perf_counter() - started
    assert result.status is ResearchSchedulerRunStatus.COMPLETED
    start_gap = abs(starts["fetch-a"] - starts["analyze-b"])
    overlap = min(finishes["fetch-a"], finishes["analyze-b"]) - max(
        starts["fetch-a"], starts["analyze-b"]
    )
    # Durable SQLite/ObjectStore transitions add platform-dependent fixed overhead;
    # prove concurrency from the actual handler intervals instead of a sub-second
    # absolute wall-clock threshold. End-to-end speedup is covered separately below.
    assert start_gap < 0.05
    assert overlap > 0.12
    assert wall < 1.5
    assert starts["join-a"] >= finishes["fetch-a"]
    assert all(
        task.status is ResearchSchedulerTaskStatus.COMPLETED for task in service.tasks(run.run_id)
    )


def test_scheduler_parallel_ready_queue_beats_serial_wall_time(tmp_path: Path) -> None:
    service = _service(tmp_path)
    specs = [
        ("task-a", None, ResearchTaskCategory.CPU, [], "task-a-v1"),
        ("task-b", None, ResearchTaskCategory.CPU, [], "task-b-v1"),
        ("task-c", None, ResearchTaskCategory.CPU, [], "task-c-v1"),
    ]

    def handler(task):
        time.sleep(0.6)
        return f"artifact:{task.node_id}"

    handlers = {item[0]: handler for item in specs}
    serial = service.create_run(
        request_id="serial-wall-benchmark",
        budget_seconds=30,
        task_specs=specs,
    )
    started = time.perf_counter()
    serial_result = service.execute(
        serial.run_id,
        handlers,
        resource_limits={
            ResearchTaskCategory.NETWORK: 1,
            ResearchTaskCategory.CPU: 1,
            ResearchTaskCategory.LLM: 1,
            ResearchTaskCategory.WRITE: 1,
        },
    )
    serial_wall = time.perf_counter() - started

    parallel = service.create_run(
        request_id="parallel-wall-benchmark",
        budget_seconds=30,
        task_specs=specs,
    )
    started = time.perf_counter()
    parallel_result = service.execute(
        parallel.run_id,
        handlers,
        resource_limits={
            ResearchTaskCategory.NETWORK: 1,
            ResearchTaskCategory.CPU: 3,
            ResearchTaskCategory.LLM: 1,
            ResearchTaskCategory.WRITE: 1,
        },
    )
    parallel_wall = time.perf_counter() - started

    assert serial_result.status is ResearchSchedulerRunStatus.COMPLETED
    assert parallel_result.status is ResearchSchedulerRunStatus.COMPLETED
    assert parallel_wall <= serial_wall * 0.60


def test_scheduler_failure_cancels_only_its_dependency_branch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(
        request_id="branch-failure",
        budget_seconds=30,
        task_specs=[
            ("bad", "A", ResearchTaskCategory.NETWORK, [], "bad-v1"),
            ("bad-child", "A", ResearchTaskCategory.CPU, ["bad"], "bad-child-v1"),
            ("good", "B", ResearchTaskCategory.LLM, [], "good-v1"),
        ],
    )

    def bad(_task):
        raise RuntimeError("provider down")

    def good(task):
        return f"artifact:{task.node_id}"

    result = service.execute(
        run.run_id,
        {"bad": bad, "bad-child": good, "good": good},
    )
    assert result.status is ResearchSchedulerRunStatus.PARTIAL
    by_node = {task.node_id: task for task in service.tasks(run.run_id)}
    assert by_node["bad"].status is ResearchSchedulerTaskStatus.FAILED
    assert by_node["bad-child"].status is ResearchSchedulerTaskStatus.CANCELLED
    assert by_node["good"].status is ResearchSchedulerTaskStatus.COMPLETED


def test_llm_takeover_packet_is_bounded_to_same_request_and_generation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(
        request_id="takeover-request",
        budget_seconds=30,
        task_specs=[
            ("parse-special", "600000", ResearchTaskCategory.CPU, [], "special-v1"),
        ],
    )

    def fail(_task):
        raise RuntimeError("unsupported issuer layout")

    failed = service.execute(run.run_id, {"parse-special": fail})
    assert failed.status is ResearchSchedulerRunStatus.FAILED
    task = service.tasks(run.run_id)[0]
    # The positive fixture supplies real registered bytes; a bare identifier is
    # not a trusted source. Negative coverage lives in takeover_acceptance.
    source = service.objects.put_json({"schema_version": "recorded-issuer-source-v1"})
    service.state.register_artifact(
        artifact_id="artifact:raw",
        artifact_type="RecordedIssuerSource",
        schema_version="recorded-issuer-source-v1",
        object_hash=source.sha256,
        input_hashes=[],
    )
    packet = service.takeover_packet(
        request_id="takeover-request",
        task_id=task.task_id,
        trusted_artifact_ids=["artifact:raw"],
        attempted_actions=["canonical parser failed"],
        missing_requirement="locate the issuer-specific table",
        allowed_write_paths=["src/astock/financial_sources/"],
        expected_output_schema="FinancialFactPack",
        dependency_fingerprint="deps-v1",
    )
    assert packet.task_id == task.task_id
    assert packet.deadline_at == run.deadline_at
    assert packet.round_index == 1
    with pytest.raises(ValueError, match="another request"):
        service.takeover_packet(
            request_id="other-request",
            task_id=task.task_id,
            trusted_artifact_ids=[],
            attempted_actions=[],
            missing_requirement="x",
            allowed_write_paths=[],
            expected_output_schema="X",
            dependency_fingerprint="deps-v1",
        )


def test_llm_takeover_validated_result_resumes_only_cancelled_dependency_suffix(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run = service.create_run(
        request_id="takeover-resume",
        budget_seconds=30,
        task_specs=[
            ("parse-special", "600000", ResearchTaskCategory.CPU, [], "special-v1"),
            (
                "valuation",
                "600000",
                ResearchTaskCategory.CPU,
                ["parse-special"],
                "valuation-v1",
            ),
            ("unrelated", "000001", ResearchTaskCategory.CPU, [], "unrelated-v1"),
        ],
    )

    def fail(_task):
        raise RuntimeError("unsupported issuer layout")

    def ok(task):
        return f"artifact:{task.node_id}"

    partial = service.execute(
        run.run_id,
        {"parse-special": fail, "valuation": ok, "unrelated": ok},
    )
    assert partial.status is ResearchSchedulerRunStatus.PARTIAL
    by_node = {task.node_id: task for task in service.tasks(run.run_id)}
    assert by_node["valuation"].status is ResearchSchedulerTaskStatus.CANCELLED
    assert by_node["unrelated"].status is ResearchSchedulerTaskStatus.COMPLETED

    packet = service.takeover_packet(
        request_id="takeover-resume",
        task_id=by_node["parse-special"].task_id,
        trusted_artifact_ids=[],
        attempted_actions=["deterministic parser failed"],
        missing_requirement="issuer-specific table mapping",
        allowed_write_paths=["src/astock/financial_sources/"],
        expected_output_schema="FinancialFactPack",
        dependency_fingerprint="deps-v1",
    )
    output_ref = service.objects.put_json({"schema_version": "financial-fact-pack-test-v1"})
    service.state.register_artifact(
        artifact_id="FinancialFactPack:repaired",
        artifact_type="FinancialFactPack",
        schema_version="financial-fact-pack-test-v1",
        object_hash=output_ref.sha256,
        input_hashes=[],
    )
    validated: list[str] = []
    resumed = service.apply_takeover_result(
        LlmTakeoverResult(
            packet_id=packet.packet_id,
            request_id=packet.request_id,
            run_id=packet.run_id,
            task_id=packet.task_id,
            generation=packet.generation,
            dependency_fingerprint=packet.dependency_fingerprint,
            status=LlmTakeoverResultStatus.RESOLVED,
            result_artifact_id="FinancialFactPack:repaired",
            validation_codes=["SOURCE_AND_NUMERIC_CHECKS_PASSED"],
        ),
        validate_result=lambda artifact_id: validated.append(artifact_id),
    )
    assert resumed.status is ResearchSchedulerRunStatus.RUNNING
    assert validated == ["FinancialFactPack:repaired"]
    by_node = {task.node_id: task for task in service.tasks(run.run_id)}
    assert by_node["parse-special"].status is ResearchSchedulerTaskStatus.COMPLETED
    assert by_node["valuation"].status is ResearchSchedulerTaskStatus.READY
    assert by_node["unrelated"].status is ResearchSchedulerTaskStatus.COMPLETED

    completed = service.execute(run.run_id, {"valuation": ok, "unrelated": ok})
    assert completed.status is ResearchSchedulerRunStatus.COMPLETED


def test_llm_takeover_is_limited_to_two_rounds(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(
        request_id="takeover-budget",
        budget_seconds=30,
        task_specs=[("parse-special", "600000", ResearchTaskCategory.CPU, [], "special-v1")],
    )
    service.execute(
        run.run_id,
        {"parse-special": lambda _task: (_ for _ in ()).throw(RuntimeError("bad"))},
    )
    task = service.tasks(run.run_id)[0]
    for expected_round in (1, 2):
        packet = service.takeover_packet(
            request_id="takeover-budget",
            task_id=task.task_id,
            trusted_artifact_ids=[],
            attempted_actions=[f"round-{expected_round}"],
            missing_requirement="repair parser",
            allowed_write_paths=[],
            expected_output_schema="FinancialFactPack",
            dependency_fingerprint="deps-v1",
        )
        assert packet.round_index == expected_round
        service.apply_takeover_result(
            LlmTakeoverResult(
                packet_id=packet.packet_id,
                request_id=packet.request_id,
                run_id=packet.run_id,
                task_id=packet.task_id,
                generation=packet.generation,
                dependency_fingerprint=packet.dependency_fingerprint,
                status=LlmTakeoverResultStatus.UNRESOLVED,
                validation_codes=["STILL_UNRESOLVED"],
            ),
            validate_result=lambda _artifact_id: None,
        )
    with pytest.raises(ValueError, match="retry budget"):
        service.takeover_packet(
            request_id="takeover-budget",
            task_id=task.task_id,
            trusted_artifact_ids=[],
            attempted_actions=["round-3"],
            missing_requirement="repair parser",
            allowed_write_paths=[],
            expected_output_schema="FinancialFactPack",
            dependency_fingerprint="deps-v1",
        )


def test_deadline_marks_running_work_cancelled_without_waiting_for_handler(tmp_path: Path) -> None:
    start = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
    times = iter([start, start, start + timedelta(seconds=2), start + timedelta(seconds=2)])
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = CurrentResearchSlaService(
        state,
        ObjectStore(tmp_path / "objects"),
        clock=lambda: next(times, start + timedelta(seconds=2)),
    )
    run = service.create_run(
        request_id="deadline-request",
        budget_seconds=1,
        task_specs=[("slow", "A", ResearchTaskCategory.NETWORK, [], "slow-v1")],
    )
    result = service.execute(run.run_id, {"slow": lambda _task: "artifact:slow"})
    assert result.status in {
        ResearchSchedulerRunStatus.CANCELLED,
        ResearchSchedulerRunStatus.PARTIAL,
    }
    assert service.tasks(run.run_id)[0].status is ResearchSchedulerTaskStatus.CANCELLED
