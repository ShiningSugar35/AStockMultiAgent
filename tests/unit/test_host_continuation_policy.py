from __future__ import annotations

import pytest

from astock.investor_orchestration.host_continuation import (
    AgentHost,
    ChatInvocationPolicy,
    HostContinuationPolicy,
)


@pytest.mark.parametrize("elapsed", [0, 1799, 1800, 2099])
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
@pytest.mark.parametrize("elapsed", [2100, 2399, 2400, 2699, 2700, 10000])
def test_unfinished_chat_checkpoints_without_partial_report(completed: int, elapsed: int) -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=elapsed,
        completed_units=completed,
        total_units=6,
    )
    assert result.continuation_required and result.scheduled_creation_required
    assert result.checkpoint_required
    assert not result.partial_delivery_required
    assert result.binding_verification_required == (elapsed >= 2400)
    assert not result.continuation_binding_verified
    assert result.schedule_cadence_seconds == 3600
    assert result.invocation_stop_required == (elapsed >= 2700)
    assert result.remaining_invocation_seconds == max(0, 2700 - elapsed)


def test_completed_round_does_not_create_child() -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=2700,
        completed_units=6,
        total_units=6,
    )
    assert result.invocation_stop_required
    assert not result.continuation_required
    assert not result.checkpoint_required
    assert not result.binding_verification_required


@pytest.mark.parametrize("verified", [False, True])
def test_existing_child_prevents_duplicate_but_id_alone_does_not_prove_binding(
    verified: bool,
) -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=2400,
        completed_units=5,
        total_units=6,
        existing_platform_task_id="opaque-child",
        platform_binding_verified=verified,
    )
    assert result.continuation_required
    assert not result.scheduled_creation_required
    assert not result.run_immediately
    assert result.binding_verification_required is not verified
    assert result.continuation_binding_verified is verified


@pytest.mark.parametrize("elapsed", [2100, 2400, 2700, 12000, 86400])
def test_other_agents_have_no_overall_deadline(
    elapsed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("other agent must not load ChatGPT budget")

    monkeypatch.setattr(ChatInvocationPolicy, "load", unexpected_load)
    result = HostContinuationPolicy.evaluate(
        host="OTHER_AGENT",
        elapsed_seconds=elapsed,
        completed_units=0,
        total_units=10,
    )
    assert not result.overall_deadline_enforced
    assert not result.activation_deadline_enforced
    assert not result.continuation_required
    assert not result.invocation_stop_required
    assert result.remaining_invocation_seconds is None


def test_earlier_platform_deadline_keeps_handoff_reserve() -> None:
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=1200,
        completed_units=1,
        total_units=10,
        platform_hard_limit_seconds=1800,
    )
    assert result.checkpoint_required
    assert result.remaining_invocation_seconds == 600
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=1500,
        completed_units=1,
        total_units=10,
        platform_hard_limit_seconds=1800,
    )
    assert result.binding_verification_required
    assert not result.invocation_stop_required


@pytest.mark.parametrize("completed,total,elapsed", [(-1, 6, 0), (7, 6, 0), (1, 0, 0), (1, 6, -1)])
def test_invalid_progress_rejected(completed: int, total: int, elapsed: int) -> None:
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(
            host="CHATGPT_CHAT",
            elapsed_seconds=elapsed,
            completed_units=completed,
            total_units=total,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "UNKNOWN"},
        {"existing_platform_task_id": " "},
        {"platform_binding_verified": True},
        {"platform_hard_limit_seconds": 0},
    ],
)
def test_invalid_host_task_or_verification_rejected(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = dict(
        host="CHATGPT_CHAT",
        elapsed_seconds=2500,
        completed_units=1,
        total_units=6,
    )
    values.update(kwargs)
    with pytest.raises(ValueError):
        HostContinuationPolicy.evaluate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "closeout,check,hard,cadence",
    [
        (2400, 2400, 2700, 3600),
        (2100, 2700, 2700, 3600),
        (2100, 2400, 2700, 1800),
        (2100, 2400, 2300, 3600),
    ],
)
def test_policy_order_and_hourly_recurrence(
    closeout: int, check: int, hard: int, cadence: int
) -> None:
    with pytest.raises(ValueError):
        ChatInvocationPolicy(
            closeout_after_seconds=closeout,
            binding_check_after_seconds=check,
            hard_limit_seconds=hard,
            continuation_cadence_seconds=cadence,
        )


def test_policy_thresholds_are_configuration_not_python_constants() -> None:
    policy = ChatInvocationPolicy(
        closeout_after_seconds=900,
        binding_check_after_seconds=1200,
        hard_limit_seconds=1500,
        continuation_cadence_seconds=3600,
    )
    result = HostContinuationPolicy.evaluate(
        host="CHATGPT_CHAT",
        elapsed_seconds=1200,
        completed_units=1,
        total_units=2,
        policy=policy,
    )
    assert result.binding_verification_required
    assert result.remaining_invocation_seconds == 300
