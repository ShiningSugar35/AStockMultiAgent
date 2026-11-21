"""Host-aware handoff; elapsed project time is not a platform deadline."""
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
    schema_version: Literal["chat-invocation-policy-v2"] = "chat-invocation-policy-v2"
    handoff_reserve_seconds: int = Field(default=600, ge=0)
    binding_verification_reserve_seconds: int = Field(default=300, ge=0)
    continuation_cadence_seconds: int = Field(default=3600, ge=3600)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_thresholds(cls, value: object) -> object:
        """Read v1 policy snapshots without restoring their assumed hard stop."""
        if not isinstance(value, dict):
            return value
        values = dict(value)
        legacy_keys = (
            "closeout_after_seconds",
            "binding_check_after_seconds",
            "hard_limit_seconds",
        )
        if not any(key in values for key in legacy_keys):
            return values
        legacy = tuple(values.pop(key, None) for key in legacy_keys)
        if any(not isinstance(item, int) or isinstance(item, bool) for item in legacy):
            raise ValueError("legacy invocation thresholds must be complete integers")
        closeout, check, hard = legacy
        if not 0 < closeout < check < hard:
            raise ValueError("legacy closeout < binding check < hard limit required")
        reserves = {
            "handoff_reserve_seconds": hard - closeout,
            "binding_verification_reserve_seconds": hard - check,
        }
        for key, reserve in reserves.items():
            if key in values and values[key] != reserve:
                raise ValueError("legacy invocation thresholds conflict with handoff reserves")
            values[key] = reserve
        values["schema_version"] = "chat-invocation-policy-v2"
        return values

    @model_validator(mode="after")
    def ordered(self) -> ChatInvocationPolicy:
        if self.binding_verification_reserve_seconds > self.handoff_reserve_seconds:
            raise ValueError("binding verification reserve cannot exceed handoff reserve")
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
    # Retained for persisted consumers; handoff never mandates a partial report.
    partial_delivery_required: bool = False
    checkpoint_required: bool = False
    binding_verification_required: bool = False
    continuation_binding_verified: bool = False
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
        platform_binding_verified: bool = False,
        platform_hard_limit_seconds: int | None = None,
        platform_interrupted: bool = False,
        policy: ChatInvocationPolicy | None = None,
    ) -> HostContinuationDecision:
        host = AgentHost(host)
        if elapsed_seconds < 0 or total_units <= 0:
            raise ValueError("elapsed must be non-negative and total units positive")
        if not 0 <= completed_units <= total_units:
            raise ValueError("completed_units must stay within total_units")
        if existing_platform_task_id is not None and not existing_platform_task_id.strip():
            raise ValueError("existing platform task id cannot be blank")
        if platform_binding_verified and existing_platform_task_id is None:
            raise ValueError("verified binding requires a platform task id")
        if platform_hard_limit_seconds is not None and platform_hard_limit_seconds <= 0:
            raise ValueError("platform hard limit must be positive")

        chat = host is AgentHost.CHATGPT_CHAT
        incomplete = completed_units < total_units
        hard = platform_hard_limit_seconds
        # Only an observed platform limit or actual interruption activates handoff.
        # Legacy project thresholds are reserves, never evidence of a host limit.
        limits = (
            policy or ChatInvocationPolicy.load()
            if hard is not None or platform_interrupted
            else None
        )
        remaining = max(0, hard - elapsed_seconds) if hard is not None else None
        stop = platform_interrupted or (remaining is not None and remaining == 0)
        handoff = incomplete and (
            platform_interrupted
            or (
                remaining is not None
                and limits is not None
                and remaining <= limits.handoff_reserve_seconds
            )
        )
        create = chat and handoff and existing_platform_task_id is None
        verify = handoff and not platform_binding_verified and (
            platform_interrupted
            or (
                remaining is not None
                and limits is not None
                and remaining <= limits.binding_verification_reserve_seconds
            )
        )
        if platform_interrupted:
            reason = "PLATFORM_ACTIVATION_INTERRUPTED"
        elif stop:
            reason = "PLATFORM_INVOCATION_HARD_STOP"
        elif not incomplete:
            reason = "RESEARCH_ROUND_COMPLETED"
        elif verify:
            reason = "CONTINUATION_BINDING_VERIFICATION_REQUIRED"
        elif handoff:
            reason = (
                "CONTINUATION_ALREADY_BOUND"
                if platform_binding_verified
                else "CONTINUATION_REQUIRED"
            )
        else:
            reason = "CONTINUE_WITHIN_RESOURCE_BUDGET"
        return HostContinuationDecision(
            host=host,
            elapsed_seconds=elapsed_seconds,
            completed_units=completed_units,
            total_units=total_units,
            progress_numerator=completed_units,
            progress_denominator=total_units,
            overall_deadline_enforced=False,
            activation_deadline_enforced=hard is not None,
            continuation_required=handoff,
            scheduled_creation_required=create,
            schedule_cadence_seconds=(
                limits.continuation_cadence_seconds if chat and handoff and limits else None
            ),
            run_immediately=create,
            existing_platform_task_id=existing_platform_task_id,
            reason_code=reason,
            invocation_stop_required=stop,
            partial_delivery_required=False,
            checkpoint_required=handoff,
            binding_verification_required=verify,
            continuation_binding_verified=platform_binding_verified,
            remaining_invocation_seconds=remaining,
        )


__all__ = [
    "AgentHost",
    "ChatInvocationPolicy",
    "HostContinuationDecision",
    "HostContinuationPolicy",
]
