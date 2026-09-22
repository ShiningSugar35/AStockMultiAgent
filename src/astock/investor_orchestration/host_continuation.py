"""Deterministic host policy for long-running investment-research continuation."""

from __future__ import annotations

from enum import StrEnum

from astock.investor_orchestration.models import StrictModel


class AgentHost(StrEnum):
    CHATGPT_CHAT = "CHATGPT_CHAT"
    OTHER_AGENT = "OTHER_AGENT"


class HostContinuationDecision(StrictModel):
    host: AgentHost
    elapsed_seconds: int
    completed_units: int
    total_units: int
    progress_numerator: int
    progress_denominator: int
    overall_deadline_enforced: bool
    continuation_required: bool
    scheduled_creation_required: bool
    schedule_cadence_seconds: int | None = None
    run_immediately: bool = False
    existing_platform_task_id: str | None = None
    reason_code: str


class HostContinuationPolicy:
    CHATGPT_HANDOFF_AFTER_SECONDS = 2400
    SCHEDULE_CADENCE_SECONDS = 3600

    @classmethod
    def evaluate(
        cls,
        *,
        host: AgentHost | str,
        elapsed_seconds: int,
        completed_units: int,
        total_units: int,
        existing_platform_task_id: str | None = None,
    ) -> HostContinuationDecision:
        host = AgentHost(host)
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        if total_units <= 0:
            raise ValueError("total_units must be positive")
        if completed_units < 0 or completed_units > total_units:
            raise ValueError("completed_units must stay within total_units")
        if existing_platform_task_id is not None and not existing_platform_task_id.strip():
            raise ValueError("existing platform task id cannot be blank")

        below_two_thirds = completed_units * 3 < total_units * 2
        chat_trigger = (
            host is AgentHost.CHATGPT_CHAT
            and elapsed_seconds > cls.CHATGPT_HANDOFF_AFTER_SECONDS
            and below_two_thirds
        )
        schedule_create = chat_trigger and existing_platform_task_id is None
        if host is AgentHost.OTHER_AGENT:
            reason = "OTHER_AGENT_NO_OVERALL_RESEARCH_DEADLINE"
        elif elapsed_seconds <= cls.CHATGPT_HANDOFF_AFTER_SECONDS:
            reason = "CHATGPT_WITHIN_PRIMARY_TURN_WINDOW"
        elif not below_two_thirds:
            reason = "CHATGPT_PROGRESS_AT_OR_ABOVE_TWO_THIRDS"
        elif existing_platform_task_id is not None:
            reason = "CHATGPT_CONTINUATION_ALREADY_BOUND"
        else:
            reason = "CHATGPT_HOURLY_CONTINUATION_REQUIRED"

        return HostContinuationDecision(
            host=host,
            elapsed_seconds=elapsed_seconds,
            completed_units=completed_units,
            total_units=total_units,
            progress_numerator=completed_units,
            progress_denominator=total_units,
            overall_deadline_enforced=False,
            continuation_required=chat_trigger,
            scheduled_creation_required=schedule_create,
            schedule_cadence_seconds=(
                cls.SCHEDULE_CADENCE_SECONDS if chat_trigger else None
            ),
            run_immediately=schedule_create,
            existing_platform_task_id=existing_platform_task_id,
            reason_code=reason,
        )


__all__ = [
    "AgentHost",
    "HostContinuationDecision",
    "HostContinuationPolicy",
]
