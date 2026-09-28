from __future__ import annotations

import pytest

from astock.investor_orchestration.host_continuation import (
    AgentHost,
    ChatInvocationPolicy,
    HostContinuationPolicy,
)


@pytest.mark.parametrize("elapsed", [0, 1799])
def test_chat_stays_within_primary_window(elapsed: int) -> None:
    result = HostContinuationPolicy.evaluate(
        host=AgentHost.CHATGPT_CHAT,
        elapsed_seconds=elapsed,
        completed_units=1,
        total_units=6,
    )
    assert not result.overall_deadline_enforced
    assert result.activation_deadline_enforced
    assert not result.continuation_required
    assert not result.invocation_stop_required


@pytest.mark.parametrize("completed", [0, 3, 4, 5])
@pytest.mark.parametrize("elapsed", [1800, 2099, 2100, 2401])
def test_unfinished_chat_hands_off_regardless_of_completion_ratio(
    completed: int,
    elapsed: int,
) -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=elapsed,
        completed_units=completed,
        total_units=6,
    )
    assert result.continuation_required and result.scheduled_creation_required
    assert result.partial_delivery_required
    assert result.schedule_cadence_seconds == 3600
    assert result.invocation_stop_required == (elapsed >= 2100)
    assert result.remaining_invocation_seconds == max(0, 2100 - elapsed)


def test_completed_round_does_not_create_child() -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=2100,
        completed_units=6,
        total_units=6,
    )
    assert result.invocation_stop_required
    assert not result.continuation_required


def test_existing_child_prevents_duplicate_creation() -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=1800,
        completed_units=5,
        total_units=6,
        existing_platform_task_id="opaque-child",
    )
    assert result.continuation_required
    assert not result.scheduled_creation_required
    assert not result.run_immediately


@pytest.mark.parametrize("elapsed", [2100, 2400, 12000, 86400])
def test_other_agents_have_no_overall_deadline(elapsed: int) -> None:
    result = HostContinuationPolicy.evaluate(
        host="OTHER_AGENT",
        elapsed_seconds=elapsed,
        completed_units=0,
        total_units=10,
    )
    assert not result.overall_deadline_enforced
    assert not result.continuation_required
    assert not result.invocation_stop_required


@pytest.mark.parametrize(
    "completed,total,elapsed",
    [(-1, 6, 0), (7, 6, 0), (1, 0, 0), (1, 6, -1)],
)
def test_invalid_progress_rejected(completed: int, total: int, elapsed: int) -> None:
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(
            host="CHATGPT_CHAT",
            elapsed_seconds=elapsed,
            completed_units=completed,
            total_units=total,
        )


def test_invalid_host_or_task_rejected() -> None:
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(
            host="UNKNOWN",
            elapsed_seconds=2500,
            completed_units=1,
            total_units=6,
        )
    with pytest.raises(ValueError, match="cannot be blank"):
        HostContinuationPolicy.evaluate(
            host="CHATGPT_CHAT",
            elapsed_seconds=2500,
            completed_units=1,
            total_units=6,
            existing_platform_task_id=" ",
        )


@pytest.mark.parametrize(
    "closeout,hard,cadence",
    [(2100, 2100, 3600), (1800, 2400, 3600), (1800, 2100, 1800)],
)
def test_policy_cannot_extend_user_limit_or_invent_subhourly_recurrence(
    closeout: int,
    hard: int,
    cadence: int,
) -> None:
    with pytest.raises(ValueError):
        ChatInvocationPolicy(
            closeout_after_seconds=closeout,
            hard_limit_seconds=hard,
            continuation_cadence_seconds=cadence,
        )
