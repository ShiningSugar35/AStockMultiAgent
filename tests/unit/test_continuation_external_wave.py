"""Frozen resolver waves retain ownership and completion uncertainty across interruption."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from astock.core.hashing import content_hash
from astock.core.request_budget import current_request_budget
from astock.investor_orchestration.run_ownership import (
    ScheduledRunInProgress,
    schedule_run_ownership,
)
from astock.research.continuation import external_dispatch_id
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_acquisition import AcquisitionCapability
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuationStatus,
    ExternalResearchTaskStatus,
)
from astock.schemas.research_sla import ResearchSchedulerTaskStatus, ResearchTaskCategory
from tests.unit.test_current_research_continuation import (
    _capture_annual_report,
    _request,
    _service,
)


class _FrozenHandler:
    def __init__(self, objects, result, barrier=None) -> None:
        self.objects = objects
        self.result = result
        self.barrier = barrier
        self.calls = 0

    def __call__(self, task) -> str:
        budget = current_request_budget()
        assert budget is not None
        budget.reserve_attempt()
        self.calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        self.result = self.result.model_copy(update={"dispatch_id": external_dispatch_id(task)})
        ref = self.objects.put_json(self.result.model_dump(mode="json"))
        return "CurrentResearchAutomaticResolution:" + ref.sha256

    def cancel(self) -> None:
        if self.barrier is not None:
            self.barrier.abort()


def _result(started, capture: str, *, task_id: str | None = None):
    return CurrentResearchAutomaticResolution(
        continuation_id=started.continuation_id,
        task_id=task_id or started.external_tasks[0].task_id,
        external_operation_id="operation:" + (task_id or started.external_tasks[0].task_id),
        capture_artifact_ids=[capture],
    )


def test_independent_gaps_run_concurrently_then_bind_on_coordinator(tmp_path: Path) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, False])
    started = service.start(_request())
    first = started.external_tasks[0]
    second = first.model_copy(
        update={
            "task_id": first.task_id + ":interim",
            "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }
    )
    service._persist(started.model_copy(update={"external_tasks": [first, second]}))
    capture = _capture_annual_report(state, objects, started.company_id)
    barrier = Barrier(2)
    handlers = {
        task.task_id: _FrozenHandler(
            objects,
            _result(started, capture, task_id=task.task_id),
            barrier,
        )
        for task in (first, second)
    }
    resolved = service.resolve_external_wave(started.continuation_id, handlers)
    assert [handler.calls for handler in handlers.values()] == [1, 1]
    assert all(
        task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND for task in resolved.external_tasks
    )
    assert len(resolved.automatic_resolution_artifact_ids) == 2
    assert service.resume(started.continuation_id).status is (
        CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    )
    assert acquisition.calls == 2


def test_crash_after_fetch_before_continuation_commit_reuses_frozen_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    result = _result(started, _capture_annual_report(state, objects, started.company_id))
    handler = _FrozenHandler(objects, result)
    apply = service._apply_automatic_resolution

    def interrupted(*_args):
        raise RuntimeError("simulated interruption after frozen worker result")

    monkeypatch.setattr(service, "_apply_automatic_resolution", interrupted)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        service.resolve_external_wave(started.continuation_id, {result.task_id: handler})
    monkeypatch.setattr(service, "_apply_automatic_resolution", apply)
    restored = service.resolve_external_wave(started.continuation_id, {result.task_id: handler})
    assert handler.calls == 1
    assert restored.external_tasks[0].capture_artifact_ids == result.capture_artifact_ids
    result = handler.result
    replay = service.apply_automatic_resolution(
        result.model_copy(update={"created_at": result.created_at + timedelta(seconds=1)})
    )
    assert replay == restored
    with pytest.raises(ValueError, match="completed operation"):
        service.apply_automatic_resolution(
            result.model_copy(
                update={
                    "capture_artifact_ids": [],
                    "failure_code": "CONFLICTING_CALLBACK",
                }
            )
        )


def test_unknown_expired_dispatch_is_not_reexecuted_and_late_capture_stays_same_request(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, False])
    started = service.start(_request())
    prepared = service.prepare_external_wave(started.continuation_id)
    external_task = prepared.external_tasks[0]
    assert external_task.scheduler_run_id is not None
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    dispatched = next(
        task for task in scheduler.tasks(external_task.scheduler_run_id)
        if task.node_id == external_task.task_id
    )
    assert external_task.dispatch_id == external_dispatch_id(dispatched)
    scheduler._transition_task(
        dispatched,
        ResearchSchedulerTaskStatus.RUNNING,
        lease_owner="interrupted-owner",
        lease_expires_at=service.clock() - timedelta(seconds=1),
        at=service.clock(),
    )
    result = _result(started, _capture_annual_report(state, objects, started.company_id))
    handler = _FrozenHandler(objects, result)
    unknown = service.resolve_external_wave(started.continuation_id, {result.task_id: handler})
    assert unknown.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    assert handler.calls == 0
    service.clock = lambda: started.deadline_at + timedelta(seconds=1)
    assert (
        service.resolve_external_wave(
            started.continuation_id,
            {result.task_id: handler},
        ).status
        is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    )
    assert handler.calls == 0
    result = result.model_copy(update={"dispatch_id": unknown.external_tasks[0].dispatch_id})
    bound = service.apply_automatic_resolution(result)
    resumed = service.resume(bound.continuation_id)
    assert resumed.continuation_id == started.continuation_id
    assert resumed.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    assert acquisition.network_modes == [True, False]


def test_second_coordinator_cannot_mutate_active_continuation(tmp_path: Path) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    result = _result(started, _capture_annual_report(state, objects, started.company_id))
    with schedule_run_ownership(state.path, "research-continuation", started.continuation_id):
        with pytest.raises(ScheduledRunInProgress):
            service.apply_automatic_resolution(result)
    assert service.get(started.continuation_id) == started


def test_absolute_scheduler_deadline_cannot_be_extended_on_restart(tmp_path: Path) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)

    def create(deadline):
        return scheduler.create_run(
            request_id="original-deadline",
            budget_seconds=None,
            deadline_at=deadline,
            task_specs=[("gap", "600519", ResearchTaskCategory.NETWORK, [], content_hash("input"))],
        )

    first = create(started.deadline_at)
    assert create(started.deadline_at).run_id == first.run_id
    with pytest.raises(ValueError):
        create(started.deadline_at + timedelta(seconds=1))


def test_unknown_branch_does_not_block_independent_ready_or_local_evidence(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request())
    unknown_task = started.external_tasks[0]
    ready_task = unknown_task.model_copy(
        update={
            "task_id": unknown_task.task_id + ":independent",
            "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }
    )
    record = service._persist(
        started.model_copy(update={"external_tasks": [unknown_task, ready_task]})
    )
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    record = service._bind_external_wave(record, scheduler)
    run_id = record.external_tasks[0].scheduler_run_id
    assert run_id is not None
    unknown_dispatch = next(
        task for task in scheduler.tasks(run_id) if task.node_id == unknown_task.task_id
    )
    scheduler._transition_task(
        unknown_dispatch,
        ResearchSchedulerTaskStatus.RUNNING,
        lease_owner="lost-owner",
        lease_expires_at=service.clock() - timedelta(seconds=1),
        at=service.clock(),
    )
    capture = _capture_annual_report(state, objects, started.company_id)
    handler = _FrozenHandler(objects, _result(started, capture, task_id=ready_task.task_id))
    resolved = service.resolve_external_wave(
        started.continuation_id, {ready_task.task_id: handler}
    )
    assert handler.calls == 1
    assert resolved.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    assert any(
        task.task_id == ready_task.task_id
        and task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
        for task in resolved.external_tasks
    )
    service.clock = lambda: started.deadline_at + timedelta(seconds=1)
    consumed = service.resume(started.continuation_id)
    assert acquisition.network_modes == [True, False]
    assert (
        consumed.current_acquisition_report_artifact_id
        != started.current_acquisition_report_artifact_id
    )
    assert consumed.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    assert [task.task_id for task in consumed.external_tasks] == [unknown_task.task_id]
    assert consumed.external_tasks[0].dispatch_id == record.external_tasks[0].dispatch_id
    assert consumed.automatic_rounds_completed == started.automatic_rounds_completed


def test_cancelled_task_without_dispatch_is_not_unknown(tmp_path: Path) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    record = service._bind_external_wave(started, scheduler)
    run_id = record.external_tasks[0].scheduler_run_id
    assert run_id is not None
    tasks = scheduler.tasks(run_id)
    assert tasks
    for task in tasks:
        cancelled = scheduler._transition_task(
            task,
            ResearchSchedulerTaskStatus.CANCELLED,
            error_code="REQUEST_CANCELLED",
            at=service.clock(),
        )
        assert cancelled.status is ResearchSchedulerTaskStatus.CANCELLED
    persisted = scheduler.tasks(run_id)
    assert all(task.status is ResearchSchedulerTaskStatus.CANCELLED for task in persisted)
    assert all(task.dispatch_attempted is False for task in persisted)
    result = service.resolve_external_wave(started.continuation_id, {})
    assert result.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert all(
        task.last_failure_code == "EXTERNAL_NOT_DISPATCHED" for task in result.external_tasks
    )


def test_first_callback_must_match_prebound_generation_even_after_round_reset(
    tmp_path: Path,
) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request())
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    bound = service._bind_external_wave(started, scheduler)
    task = bound.external_tasks[0]
    wrong = _result(started, _capture_annual_report(state, objects, started.company_id))
    with pytest.raises(ValueError, match="dispatch generation"):
        service.apply_automatic_resolution(wrong)
    with pytest.raises(ValueError, match="dispatch generation"):
        service.apply_automatic_resolution(wrong.model_copy(update={"dispatch_id": "old"}))
    known_failure = CurrentResearchAutomaticResolution(
        continuation_id=started.continuation_id,
        task_id=task.task_id,
        dispatch_id=task.dispatch_id,
        external_operation_id="known-failed-operation",
        failure_code="KNOWN_SOURCE_FAILURE",
    )
    service.apply_automatic_resolution(known_failure)
    reset = service.resume(started.continuation_id)
    assert reset.external_tasks[0].dispatch_id is None
    stale = wrong.model_copy(update={"dispatch_id": task.dispatch_id})
    with pytest.raises(ValueError, match="dispatch generation"):
        service.apply_automatic_resolution(stale)
    rebound = service._bind_external_wave(reset, scheduler)
    assert rebound.external_tasks[0].dispatch_id != task.dispatch_id
    with pytest.raises(ValueError, match="dispatch generation"):
        service.apply_automatic_resolution(stale)


def test_local_admission_failure_gets_fresh_generation_without_replaying_sibling(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request())
    captured_task = started.external_tasks[0]
    unknown_task = captured_task.model_copy(
        update={
            "task_id": captured_task.task_id + ":unknown",
            "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }
    )
    record = service._persist(
        started.model_copy(update={"external_tasks": [captured_task, unknown_task]})
    )
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    record = service._bind_external_wave(record, scheduler)
    run_id = record.external_tasks[0].scheduler_run_id
    assert run_id is not None
    unknown_dispatch = next(
        task for task in scheduler.tasks(run_id) if task.node_id == unknown_task.task_id
    )
    scheduler._transition_task(
        unknown_dispatch,
        ResearchSchedulerTaskStatus.RUNNING,
        lease_owner="lost-owner",
        lease_expires_at=service.clock() - timedelta(seconds=1),
        at=service.clock(),
    )
    handler = _FrozenHandler(
        objects, _result(started, _capture_annual_report(state, objects, started.company_id))
    )
    bound = service.resolve_external_wave(
        started.continuation_id, {captured_task.task_id: handler}
    )
    # Keep both needs in the locally generated acquisition response.
    acquire = acquisition.acquire

    def still_missing(*args, **kwargs):
        report = acquire(*args, **kwargs)
        need = report.external_research_needs[0]
        updated = report.model_copy(
            update={
                "report_id": report.report_id + ":two-needs",
                "external_research_needs": [
                    need,
                    need.model_copy(
                        update={"capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM}
                    ),
                ]
            }
        )
        reference = objects.put_json(updated.model_dump(mode="json"))
        state.register_artifact(
            artifact_id=updated.report_id,
            artifact_type="CurrentResearchAcquisitionReport",
            schema_version=updated.schema_version,
            object_hash=reference.sha256,
            input_hashes=[],
        )
        return updated

    acquisition.acquire = still_missing
    consumed = service.resume(bound.continuation_id)
    before = next(task for task in bound.external_tasks if task.task_id == captured_task.task_id)
    after = next(task for task in consumed.external_tasks if task.task_id == captured_task.task_id)
    assert after.status is ExternalResearchTaskStatus.PENDING
    assert after.dispatch_id is None
    assert after.resolution_hash is None
    fresh = service.prepare_external_wave(started.continuation_id)
    retried_task = next(
        task for task in fresh.external_tasks if task.task_id == captured_task.task_id
    )
    unknown_after = next(
        task for task in fresh.external_tasks if task.task_id == unknown_task.task_id
    )
    assert retried_task.dispatch_id != before.dispatch_id
    assert unknown_after.dispatch_id == next(
        task for task in bound.external_tasks if task.task_id == unknown_task.task_id
    ).dispatch_id
    with pytest.raises(ValueError, match="dispatch generation"):
        service.apply_automatic_resolution(handler.result)
    assert handler.result.external_operation_id is not None
    retry = _FrozenHandler(objects, handler.result.model_copy(update={
        "external_operation_id": handler.result.external_operation_id + ":retry",
    }))
    retried = service.resolve_external_wave(
        started.continuation_id, {captured_task.task_id: retry},
        selected_task_ids={captured_task.task_id},
    )
    assert handler.calls == retry.calls == 1
    assert acquisition.calls == 2
    assert retried.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    assert len(retried.automatic_resolution_artifact_ids) == 2


def _actual_host_handoffs(service, state, objects):
    from astock.research.web_tools import advance_current_web
    from astock.schemas.web_tools import CurrentResearchWebBatch, HostWebCapabilities, HostWebTool

    host = HostWebCapabilities(
        host_id="antigravity", invocation_id="wave-protocol-fixture", observed_at=service.clock(),
        expires_at=service.clock() + timedelta(minutes=5),
        search=HostWebTool(availability="AVAILABLE", tool_name="search_web", billing="INCLUDED"),
    )
    record = service.get(service._continuation_id(_request()))
    assert record is not None
    view = advance_current_web(
        service.paths, state, objects, record.continuation_id,
        CurrentResearchWebBatch(continuation_id=record.continuation_id, host_capabilities=host),
        continuation=service,
    )
    operations = {item["task_id"]: item for item in view["web_operations"]}
    assert all(item["status"] == "WAITING_HOST" for item in operations.values())
    return host, operations


def _report_needs(service, report, needs):
    updated = report.model_copy(update={
        "report_id": report.report_id + ":projected-needs", "external_research_needs": needs,
    })
    ref = service.objects.put_json(updated.model_dump(mode="json"))
    service.state.register_artifact(
        artifact_id=updated.report_id, artifact_type="CurrentResearchAcquisitionReport",
        schema_version=updated.schema_version, object_hash=ref.sha256, input_hashes=[],
    )
    return updated


def test_actual_host_wait_blocks_only_its_capability_and_preserves_its_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request())
    waiting = started.external_tasks[0]
    independent = waiting.model_copy(update={
        "task_id": waiting.task_id + ":market", "capability": AcquisitionCapability.DAILY_MARKET,
    })
    service._persist(started.model_copy(update={"external_tasks": [waiting, independent]}))
    _, operations = _actual_host_handoffs(service, state, objects)
    prepared = service.get(started.continuation_id)
    assert prepared is not None
    before = next(task for task in prepared.external_tasks if task.task_id == waiting.task_id)
    assert set(operations) == {waiting.task_id}
    acquire = acquisition.acquire

    def both_missing(*args, **kwargs):
        report = acquire(*args, **kwargs)
        need = report.external_research_needs[0]
        return _report_needs(service, report, [
            need, need.model_copy(update={"capability": AcquisitionCapability.DAILY_MARKET}),
        ])

    monkeypatch.setattr(acquisition, "acquire", both_missing)
    resumed = service.resume(started.continuation_id)
    retained = next(task for task in resumed.external_tasks if task.task_id == waiting.task_id)
    assert acquisition.calls == 2
    assert acquisition.network_modes == [True, True]
    assert acquisition.blocked_modes[-1] == frozenset({waiting.capability})
    assert retained == before
    assert resumed.automatic_rounds_completed == prepared.automatic_rounds_completed + 1
    assert resumed.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED


def test_external_operation_rejects_unbound_wrong_generation_and_other_operation(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request())
    task = started.external_tasks[0]
    with pytest.raises(ValueError, match="dispatch generation"):
        service.record_external_operation(started.continuation_id, task.task_id, "absent", "fake")
    _, operations = _actual_host_handoffs(service, state, objects)
    prepared = service.get(started.continuation_id)
    assert prepared is not None
    current = prepared.external_tasks[0]
    assert current.dispatch_id is not None
    operation = operations[current.task_id]["operation_id"]
    with pytest.raises(ValueError, match="another operation"):
        service.record_external_operation(
            started.continuation_id, task.task_id, current.dispatch_id, "different",
        )
    with pytest.raises(ValueError, match="another operation"):
        service.release_external_operation(
            started.continuation_id, task.task_id, current.dispatch_id, "different",
        )
    released = service.release_external_operation(
        started.continuation_id, task.task_id, current.dispatch_id, operation,
    )
    assert released.external_tasks[0].external_operation_id is None
    assert service.release_external_operation(
        started.continuation_id, task.task_id, current.dispatch_id, operation,
    ) == released
    assert acquisition.calls == 1

def test_selected_wave_preserves_unreturned_host_generation_across_local_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True, False])
    started = service.start(_request())
    first = started.external_tasks[0]
    second = first.model_copy(update={
        "task_id": first.task_id + ":interim",
        "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
    })
    service._persist(started.model_copy(update={"external_tasks": [first, second]}))
    host, operations = _actual_host_handoffs(service, state, objects)
    prepared = service.get(started.continuation_id)
    assert prepared is not None
    before = {task.task_id: task for task in prepared.external_tasks}
    assert all(task.dispatch_id and task.scheduler_run_id for task in before.values())
    assert all(task.external_operation_id for task in before.values())
    assert acquisition.calls == 1
    acquire = acquisition.acquire

    def consume(*args, **kwargs):
        report = acquire(*args, **kwargs)
        if acquisition.calls == 2:
            report = report.model_copy(update={
                "report_id": report.report_id + ":interim",
                "external_research_needs": [
                    report.external_research_needs[0].model_copy(update={
                        "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                    }),
                ],
            })
            ref = objects.put_json(report.model_dump(mode="json"))
            state.register_artifact(
                artifact_id=report.report_id, artifact_type="CurrentResearchAcquisitionReport",
                schema_version=report.schema_version, object_hash=ref.sha256, input_hashes=[],
            )
        return report

    monkeypatch.setattr(acquisition, "acquire", consume)
    capture = _capture_annual_report(state, objects, started.company_id)
    first_handler = _FrozenHandler(objects, _result(
        started, capture, task_id=first.task_id,
    ).model_copy(update={"external_operation_id": operations[first.task_id]["operation_id"]}))
    partial = service.resolve_external_wave(
        started.continuation_id, {first.task_id: first_handler}, selected_task_ids={first.task_id},
    )
    waiting = next(task for task in partial.external_tasks if task.task_id == second.task_id)
    assert waiting.status is ExternalResearchTaskStatus.PENDING
    assert waiting.last_failure_code is None
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    assert waiting.scheduler_run_id is not None
    scheduled = next(
        task for task in scheduler.tasks(waiting.scheduler_run_id) if task.node_id == second.task_id
    )
    assert scheduled.status is ResearchSchedulerTaskStatus.READY
    assert scheduled.dispatch_attempted is False
    resumed = service.resume(started.continuation_id)
    retained = next(task for task in resumed.external_tasks if task.task_id == second.task_id)
    assert retained.dispatch_id == before[second.task_id].dispatch_id
    assert retained.scheduler_run_id == before[second.task_id].scheduler_run_id
    assert resumed.automatic_rounds_completed == prepared.automatic_rounds_completed
    assert acquisition.network_modes == [True, False]
    from astock.research.web_tools import WebToolExecutionService
    from astock.schemas.web_tools import HostWebResult, WebToolHit

    web = WebToolExecutionService(service.paths.root, state, objects, clock=service.clock)
    second_operation = operations[second.task_id]
    response = HostWebResult(
        operation_id=second_operation["operation_id"], dispatch_id=second_operation["dispatch_id"],
        invocation_id=host.invocation_id, tool_name="search_web", status="SUCCEEDED",
        observed_at=service.clock(),
        hits=[WebToolHit(
            url="https://www.sse.com.cn/disclosure/listedinfo/interim.pdf", title="interim report",
        )],
    )
    assert web.complete_host(response)["status"] == "SUCCEEDED"
    second_handler = _FrozenHandler(objects, _result(
        started, capture, task_id=second.task_id,
    ).model_copy(update={"external_operation_id": second_operation["operation_id"]}))
    service.resolve_external_wave(
        started.continuation_id, {second.task_id: second_handler},
        selected_task_ids={second.task_id},
    )
    assert service.resume(started.continuation_id).status is (
        CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    )
    assert first_handler.calls == second_handler.calls == 1


def test_selected_external_unknown_identity_is_rejected_before_preparing_or_spending(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    handler = _FrozenHandler(objects, _result(
        started, _capture_annual_report(state, objects, started.company_id),
    ))
    with pytest.raises(ValueError, match="existing task handlers"):
        service.resolve_external_wave(
            started.continuation_id, {"unknown": handler}, selected_task_ids={"unknown"},
        )
    assert service.get(started.continuation_id) == started
    assert handler.calls == 0
    assert acquisition.calls == 1

def _continuation_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json
    from datetime import UTC, datetime

    import typer
    from typer.testing import CliRunner

    from astock.research import continuation_cli

    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True])
    now = datetime.now(UTC)
    service.clock = lambda: now
    monkeypatch.setattr(
        continuation_cli, "CurrentResearchContinuationService", lambda *_args: service,
    )

    def no_paid_call(*_args, **_kwargs):
        pytest.fail("default or included-host continuation must not call the paid adapter")

    monkeypatch.setattr(
        "astock.providers.deepseek_tools.DeepSeekSearchFetchClient.execute", no_paid_call,
    )
    app = typer.Typer()
    continuation_cli.register_current_research_continuation_commands(
        app, lambda: (service.paths, state, objects), lambda value: typer.echo(json.dumps(value)),
    )
    request_file = tmp_path / "continuation-request.json"
    request_file.write_text(_request().model_dump_json(), encoding="utf-8")
    return CliRunner(), app, service, acquisition, request_file, now


@pytest.mark.parametrize("capability", [
    AcquisitionCapability.FINANCIAL_ANNUAL, AcquisitionCapability.DAILY_MARKET,
])
def test_default_start_and_resume_without_host_continue_unbound_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: AcquisitionCapability,
) -> None:
    import json

    runner, app, service, acquisition, request_file, _ = _continuation_cli(tmp_path, monkeypatch)
    acquire = acquisition.acquire

    def acquire_capability(*args, **kwargs):
        report = acquire(*args, **kwargs)
        return _report_needs(
            service, report,
            [need.model_copy(update={"capability": capability})
             for need in report.external_research_needs],
        )

    monkeypatch.setattr(acquisition, "acquire", acquire_capability)
    result = runner.invoke(app, ["research-current-continuation-start", str(request_file)])
    assert result.exit_code == 0, result.exception
    payload = json.loads(result.stdout)
    assert payload["status"] == "AUTO_RESOLUTION_REQUIRED"
    record = service.get(payload["continuation_id"])
    assert record is not None
    if capability is AcquisitionCapability.FINANCIAL_ANNUAL:
        operation = payload["web_operations"][0]
        assert operation["status"] == "HOST_CAPABILITY_REQUIRED"
        assert operation["request"]["continuation_dispatch_id"] == (
            record.external_tasks[0].dispatch_id
        )
    else:
        assert payload["web_operations"] == []
    assert record.external_tasks[0].scheduler_run_id is not None
    assert record.external_tasks[0].external_operation_id is None
    assert acquisition.calls == 1
    resumed = runner.invoke(app, ["research-current-continuation-resume", record.continuation_id])
    assert resumed.exit_code == 0, resumed.exception
    after = service.get(record.continuation_id)
    assert after is not None
    assert acquisition.calls == 2
    assert acquisition.network_modes == [True, True]
    assert acquisition.blocked_modes == [frozenset(), frozenset()]
    assert after.automatic_rounds_completed == record.automatic_rounds_completed + 1
    assert after.deadline_at == record.deadline_at
    assert after.external_tasks[0].dispatch_id != record.external_tasks[0].dispatch_id
    assert after.external_tasks[0].external_operation_id is None
    schema = runner.invoke(app, ["research-current-continuation-schema"])
    assert schema.exit_code == 0
    assert "web_batch" in json.loads(schema.stdout)


def test_default_resume_cli_consumes_actual_host_batch_in_same_prepared_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from astock.schemas.web_tools import (
        CurrentResearchWebBatch,
        HostWebCapabilities,
        HostWebResult,
        HostWebTool,
        WebToolHit,
    )

    runner, app, service, acquisition, request_file, now = _continuation_cli(tmp_path, monkeypatch)
    host = HostWebCapabilities(
        host_id="antigravity", invocation_id="native-cli-fixture", observed_at=now,
        expires_at=now + timedelta(minutes=5),
        search=HostWebTool(
            availability="AVAILABLE", tool_name="search_web", billing="INCLUDED",
        ),
    )
    host_file = tmp_path / "host-tools.json"
    host_file.write_text(host.model_dump_json(), encoding="utf-8")
    started = runner.invoke(app, [
        "research-current-continuation-start", str(request_file),
        "--host-capabilities", str(host_file),
    ])
    assert started.exit_code == 0, started.exception
    payload = json.loads(started.stdout)
    operation = payload["web_operations"][0]
    assert operation["status"] == "WAITING_HOST"
    response = HostWebResult(
        operation_id=operation["operation_id"], dispatch_id=operation["dispatch_id"],
        invocation_id=host.invocation_id, tool_name="search_web", status="SUCCEEDED",
        observed_at=now,
        hits=[WebToolHit(
            url="https://www.sse.com.cn/disclosure/listedinfo/report.pdf",
            title="issuer annual report",
        )],
    )
    batch = CurrentResearchWebBatch(
        continuation_id=payload["continuation_id"], host_capabilities=host, host_results=[response],
    )
    batch_file = tmp_path / "host-results.json"
    batch_file.write_text(batch.model_dump_json(), encoding="utf-8")
    before = service.get(payload["continuation_id"])
    assert before is not None
    resumed = runner.invoke(app, [
        "research-current-continuation-resume", payload["continuation_id"],
        "--web-batch", str(batch_file),
    ])
    assert resumed.exit_code == 0, resumed.exception
    returned = json.loads(resumed.stdout)
    assert any(
        view["operation_id"] == operation["operation_id"] and view["status"] == "SUCCEEDED"
        for view in returned["web_operations"]
    )
    after = service.get(payload["continuation_id"])
    assert after is not None
    assert after.external_tasks[0].dispatch_id == before.external_tasks[0].dispatch_id
    assert acquisition.calls == 1
    batch_file.write_text(batch.model_copy(update={
        "continuation_id": "different-request",
    }).model_dump_json(), encoding="utf-8")
    rejected = runner.invoke(app, [
        "research-current-continuation-resume", payload["continuation_id"],
        "--web-batch", str(batch_file),
    ])
    assert rejected.exit_code != 0
    assert service.get(payload["continuation_id"]) == after


def test_replayed_host_success_cannot_clear_dispatched_original_unknown(
    tmp_path: Path,
) -> None:
    from astock.research.web_tools import advance_current_web
    from astock.schemas.web_tools import CurrentResearchWebBatch, HostWebResult, WebToolHit

    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request())
    host, operations = _actual_host_handoffs(service, state, objects)
    task_id = started.external_tasks[0].task_id
    operation = operations[task_id]
    response = HostWebResult(
        operation_id=operation["operation_id"], dispatch_id=operation["dispatch_id"],
        invocation_id=host.invocation_id, tool_name="search_web", status="SUCCEEDED",
        observed_at=service.clock(),
        hits=[WebToolHit(
            url="https://www.sse.com.cn/disclosure/listedinfo/annual.pdf", title="annual report",
        )],
    )
    batch = CurrentResearchWebBatch(
        continuation_id=started.continuation_id, host_capabilities=host, host_results=[response],
    )
    advance_current_web(
        service.paths, state, objects, started.continuation_id, batch, continuation=service,
    )
    prepared = service.get(started.continuation_id)
    assert prepared is not None
    task = prepared.external_tasks[0]
    assert task.scheduler_run_id is not None and task.dispatch_id is not None
    scheduler = CurrentResearchSlaService(state, objects, clock=service.clock)
    dispatched = next(item for item in scheduler.tasks(task.scheduler_run_id)
                      if item.node_id == task_id)
    running = scheduler._transition_task(
        dispatched, ResearchSchedulerTaskStatus.RUNNING, lease_owner="lost-original-worker",
        lease_expires_at=service.clock() - timedelta(seconds=1), at=service.clock(),
    )
    assert running.dispatch_attempted is True
    unknown = service.resolve_external_wave(started.continuation_id, {})
    assert unknown.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
    replayed = advance_current_web(
        service.paths, state, objects, started.continuation_id, batch,
        continuation=service, resume_unbound=True,
    )
    assert replayed["status"] == "EXTERNAL_COMPLETION_UNKNOWN"
    after = service.get(started.continuation_id)
    assert after == unknown
    assert acquisition.calls == 1
    assert scheduler.tasks(task.scheduler_run_id)[0] == running
    with pytest.raises(ValueError, match="original completion must be reconciled"):
        service.release_external_operation(
            started.continuation_id, task_id, task.dispatch_id, operation["operation_id"],
        )
