"""Regression cases for recoverable infrastructure failures, not weaker fact gates."""
from datetime import timedelta

import pytest

from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuationStatus,
)
from tests.unit.test_current_research_continuation import NOW, _request, _service


def test_public_exhaustion_never_requests_private_material(tmp_path):
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(max_rounds=1))
    result = service.run_to_terminal(
        started.continuation_id,
        resolve_external=lambda record, task: CurrentResearchAutomaticResolution(
            continuation_id=record.continuation_id,
            task_id=task.task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        ),
        execute_team=lambda *_: pytest.fail("incomplete evidence cannot enter team"),
    )
    assert result.status.value == "PUBLIC_DATA_UNAVAILABLE"
    assert result.automatic_budget_exhausted
    assert not result.private_material_required
    assert result.manual_actions == []
    assert service.status(result.continuation_id)["user_assistance_request_allowed"] is False
    assert acquisition.calls == 1
    assert not result.investor_view_allowed


def test_bounded_budget_remains_usable_after_start(tmp_path):
    service, _, _, _ = _service(tmp_path, gap_rounds=[True])
    record = service.start(_request(budget_seconds=900))
    resumed = service.resume(record.continuation_id)
    assert resumed.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED


def test_stale_question_time_does_not_exhaust_current_acquisition(tmp_path):
    service, _, _, _ = _service(
        tmp_path, gap_rounds=[True], clock=NOW + timedelta(hours=2)
    )
    record = service.start(_request())
    assert record.started_at == NOW + timedelta(hours=2)
    assert record.deadline_at == record.started_at + timedelta(seconds=1800)
    assert service.resume(record.continuation_id).automatic_budget_exhausted is False


def test_excessive_budget_rejected_before_acquisition(tmp_path):
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    with pytest.raises(ValueError, match="budget"):
        service.start(_request(budget_seconds=7200))
    assert acquisition.calls == 0


def test_transient_agent_tool_timeout_uses_remaining_rounds(tmp_path):
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    record = service.start(_request(max_rounds=2))
    calls = []

    def resolver(current, task):
        calls.append(task.task_id)
        raise TimeoutError("transport failed with details that must not enter artifacts")

    result = service.run_to_terminal(
        record.continuation_id,
        resolve_external=resolver,
        execute_team=lambda *_: pytest.fail("no evidence"),
    )
    assert len(calls) == 2
    assert acquisition.calls == 2
    assert len(result.automatic_resolution_artifact_ids) == 2
    assert not result.private_material_required
    assert result.manual_actions == []


def test_resolver_cannot_return_a_different_existing_task(tmp_path):
    service, _, _, _ = _service(tmp_path, gap_rounds=[True])
    record = service.start(_request(max_rounds=1))
    first = record.external_tasks[0]
    second = first.model_copy(update={"task_id": first.task_id + ":other"})
    record = service._persist(record.model_copy(update={"external_tasks": [first, second]}))
    with pytest.raises(ValueError, match="requested task"):
        service.run_to_terminal(
            record.continuation_id,
            resolve_external=lambda current, task: CurrentResearchAutomaticResolution(
                continuation_id=current.continuation_id,
                task_id=second.task_id,
                failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
            ),
            execute_team=lambda *_: pytest.fail("no evidence"),
        )
    stored = service.get(record.continuation_id)
    assert stored is not None
    assert stored.automatic_resolution_artifact_ids == []


def test_programming_errors_are_not_hidden_as_network_recovery(tmp_path):
    service, _, _, _ = _service(tmp_path, gap_rounds=[True])
    record = service.start(_request())

    def resolver(*_):
        raise TypeError("programming defect")

    with pytest.raises(TypeError, match="programming defect"):
        service.run_to_terminal(
            record.continuation_id, resolve_external=resolver, execute_team=lambda *_: None
        )
