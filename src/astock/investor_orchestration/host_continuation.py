"""Host-specific invocation budgets; logical research survives activation boundaries."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from astock.investor_orchestration.models import StrictModel


class AgentHost(StrEnum):
    CHATGPT_CHAT = "CHATGPT_CHAT"
    OTHER_AGENT = "OTHER_AGENT"


class ChatInvocationPolicy(StrictModel):
    schema_version: Literal["chat-invocation-policy-v1"] = "chat-invocation-policy-v1"
    closeout_after_seconds: int = Field(gt=0)
    hard_limit_seconds: int = Field(gt=0, le=2100)
    continuation_cadence_seconds: int = Field(ge=3600)

    @model_validator(mode="after")
    def ordered(self) -> ChatInvocationPolicy:
        if self.closeout_after_seconds >= self.hard_limit_seconds:
            raise ValueError("closeout must precede the hard invocation limit")
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> ChatInvocationPolicy:
        source = path or (
            Path(__file__).resolve().parents[3] / "configs/chat_invocation_policy_v1.yaml"
        )
        return cls.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))


class HostContinuationDecision(StrictModel):
    host: AgentHost
    elapsed_seconds: int
    completed_units: int
    total_units: int
    progress_numerator: int
    progress_denominator: int
    overall_deadline_enforced: bool
    activation_deadline_enforced: bool = False
    continuation_required: bool
    scheduled_creation_required: bool
    schedule_cadence_seconds: int | None = None
    run_immediately: bool = False
    existing_platform_task_id: str | None = None
    reason_code: str
    invocation_stop_required: bool = False
    partial_delivery_required: bool = False
    remaining_invocation_seconds: int | None = None


class HostContinuationPolicy:
    @classmethod
    def evaluate(
        cls,
        *,
        host: AgentHost | str,
        elapsed_seconds: int,
        completed_units: int,
        total_units: int,
        existing_platform_task_id: str | None = None,
        policy: ChatInvocationPolicy | None = None,
    ) -> HostContinuationDecision:
        host = AgentHost(host)
        if elapsed_seconds < 0 or total_units <= 0:
            raise ValueError("elapsed must be non-negative and total units positive")
        if not 0 <= completed_units <= total_units:
            raise ValueError("completed_units must stay within total_units")
        if existing_platform_task_id is not None and not existing_platform_task_id.strip():
            raise ValueError("existing platform task id cannot be blank")
        limits = policy or ChatInvocationPolicy.load()
        chat = host is AgentHost.CHATGPT_CHAT
        incomplete = completed_units < total_units
        handoff = chat and incomplete and elapsed_seconds >= limits.closeout_after_seconds
        stop = chat and elapsed_seconds >= limits.hard_limit_seconds
        create = handoff and existing_platform_task_id is None
        if not chat:
            reason = "OTHER_AGENT_NO_OVERALL_RESEARCH_DEADLINE"
        elif stop:
            reason = "CHATGPT_INVOCATION_HARD_STOP"
        elif not incomplete:
            reason = "CHATGPT_ROUND_COMPLETED"
        elif handoff:
            reason = (
                "CHATGPT_CONTINUATION_ALREADY_BOUND"
                if not create
                else "CHATGPT_CONTINUATION_REQUIRED"
            )
        else:
            reason = "CHATGPT_WITHIN_PRIMARY_TURN_WINDOW"
        remaining_seconds = (
            max(0, limits.hard_limit_seconds - elapsed_seconds) if chat else None
        )
        return HostContinuationDecision(
            host=host,
            elapsed_seconds=elapsed_seconds,
            completed_units=completed_units,
            total_units=total_units,
            progress_numerator=completed_units,
            progress_denominator=total_units,
            # The logical request has no overall deadline; only this activation is bounded.
            overall_deadline_enforced=False,
            activation_deadline_enforced=chat,
            continuation_required=handoff,
            scheduled_creation_required=create,
            schedule_cadence_seconds=(
                limits.continuation_cadence_seconds if handoff else None
            ),
            run_immediately=create,
            existing_platform_task_id=existing_platform_task_id,
            reason_code=reason,
            invocation_stop_required=stop,
            partial_delivery_required=handoff,
            remaining_invocation_seconds=remaining_seconds,
        )


__all__ = [
    "AgentHost",
    "ChatInvocationPolicy",
    "HostContinuationDecision",
    "HostContinuationPolicy",
]
