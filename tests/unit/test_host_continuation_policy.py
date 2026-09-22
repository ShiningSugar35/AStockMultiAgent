from __future__ import annotations

import pytest

from astock.investor_orchestration.host_continuation import (
    AgentHost,
    HostContinuationPolicy,
)


@pytest.mark.parametrize("elapsed", [0, 2399, 2400])
def test_chat_does_not_handoff_before_strictly_exceeding_forty_minutes(elapsed: int) -> None:
    result = HostContinuationPolicy.evaluate(
        host=AgentHost.CHATGPT_CHAT,
        elapsed_seconds=elapsed,
        completed_units=1,
        total_units=6,
    )
    assert not result.continuation_required
    assert not result.scheduled_creation_required
    assert not result.run_immediately


def test_chat_handoff_requires_less_than_two_thirds_progress() -> None:
    trigger = HostContinuationPolicy.evaluate(
        host=AgentHost.CHATGPT_CHAT,
        elapsed_seconds=2401,
        completed_units=3,
        total_units=6,
    )
    assert trigger.continuation_required
    assert trigger.scheduled_creation_required
    assert trigger.schedule_cadence_seconds == 3600
    assert trigger.run_immediately
    assert trigger.reason_code == "CHATGPT_HOURLY_CONTINUATION_REQUIRED"

    exact_two_thirds = HostContinuationPolicy.evaluate(
        host=AgentHost.CHATGPT_CHAT,
        elapsed_seconds=3600,
        completed_units=4,
        total_units=6,
    )
    assert not exact_two_thirds.continuation_required
    assert exact_two_thirds.reason_code == "CHATGPT_PROGRESS_AT_OR_ABOVE_TWO_THIRDS"


def test_existing_chat_scheduled_binding_prevents_duplicate_creation() -> None:
    result = HostContinuationPolicy.evaluate(
        host=AgentHost.CHATGPT_CHAT,
        elapsed_seconds=4000,
        completed_units=1,
        total_units=6,
        existing_platform_task_id="task-opaque-1",
    )
    assert result.continuation_required
    assert not result.scheduled_creation_required
    assert not result.run_immediately
    assert result.reason_code == "CHATGPT_CONTINUATION_ALREADY_BOUND"


@pytest.mark.parametrize("elapsed", [2701, 3600, 12_000, 86_400])
def test_other_agents_never_inherit_an_overall_research_deadline(elapsed: int) -> None:
    result = HostContinuationPolicy.evaluate(
        host=AgentHost.OTHER_AGENT,
        elapsed_seconds=elapsed,
        completed_units=0,
        total_units=10,
    )
    assert not result.overall_deadline_enforced
    assert not result.continuation_required
    assert not result.scheduled_creation_required
    assert result.reason_code == "OTHER_AGENT_NO_OVERALL_RESEARCH_DEADLINE"


@pytest.mark.parametrize(
    ("completed", "total"),
    [(-1, 6), (7, 6), (1, 0), (1, -1)],
)
def test_progress_contract_rejects_invalid_denominators(completed: int, total: int) -> None:
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(
            host=AgentHost.CHATGPT_CHAT,
            elapsed_seconds=2500,
            completed_units=completed,
            total_units=total,
        )


def test_unknown_host_and_blank_bound_task_are_rejected() -> None:
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(
            host="UNKNOWN",
            elapsed_seconds=2500,
            completed_units=1,
            total_units=6,
        )
    with pytest.raises(ValueError, match="cannot be blank"):
        HostContinuationPolicy.evaluate(
            host=AgentHost.CHATGPT_CHAT,
            elapsed_seconds=2500,
            completed_units=1,
            total_units=6,
            existing_platform_task_id=" ",
        )
