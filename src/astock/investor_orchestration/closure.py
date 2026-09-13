"""Machine-verifiable terminal gate for material investment requests."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityRequirement,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    RequestIntent,
    StrictModel,
)


class InvestmentRequestNotTerminalError(RuntimeError):
    """Raised when a material investment request still has automatic work to do."""


class InvestmentClosureState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    CONTINUE_AUTOMATICALLY = "CONTINUE_AUTOMATICALLY"
    READY_FOR_INVESTOR_VIEW = "READY_FOR_INVESTOR_VIEW"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"


class InvestmentClosureDecision(StrictModel):
    request_id: str
    intent: RequestIntent
    state: InvestmentClosureState
    required_capabilities: tuple[str, ...]
    completed_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    same_request_continuation_required: bool
    investment_conclusion_blocked: bool
    investor_view_allowed: bool
    automatic_resolution_exhausted: bool = False
    private_user_input_required: bool = False
    broker_execution_allowed: Literal[False] = False


_MATERIAL_INVESTMENT_INTENTS = frozenset(
    {
        RequestIntent.RESEARCH,
        RequestIntent.FULL_RESEARCH_RECOMMENDATION,
    }
)
_SUCCESS = {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}


class InvestmentRequestClosurePolicy:
    """Prevent an intermediate research state from becoming the investor answer.

    This policy intentionally consumes the canonical CapabilityExecutionPlan instead
    of maintaining a second list of research requirements. The plan is intent-aware,
    dependency-closed and already contains the mandatory capability graph.
    """

    @staticmethod
    def evaluate(
        request: InvestorRequestEnvelope,
        plan: CapabilityExecutionPlan,
        coverage: CapabilityCoverageReceipt,
        *,
        automatic_resolution_exhausted: bool = False,
        private_user_input_required: bool = False,
    ) -> InvestmentClosureDecision:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        plan = CapabilityExecutionPlan.model_validate(plan.model_dump())
        coverage = CapabilityCoverageReceipt.model_validate(coverage.model_dump())
        if request.request_id != plan.request_id or request.request_id != coverage.request_id:
            raise ValueError("investment closure identities differ")
        if coverage.plan_id != plan.plan_id or coverage.policy_version != plan.policy_version:
            raise ValueError("investment closure does not cover the active capability plan")
        if private_user_input_required and not automatic_resolution_exhausted:
            raise ValueError(
                "public-source resolution must be exhausted before requesting user input"
            )

        required = tuple(
            sorted(
                node.capability_id
                for node in plan.nodes
                if node.requirement is CapabilityRequirement.REQUIRED
            )
        )
        records = {record.capability_id: record for record in coverage.records}
        completed = tuple(
            sorted(
                capability_id
                for capability_id in required
                if capability_id in records and records[capability_id].status in _SUCCESS
            )
        )
        missing = tuple(sorted(set(required) - set(completed)))

        if request.normalized_intent not in _MATERIAL_INVESTMENT_INTENTS:
            return InvestmentClosureDecision(
                request_id=request.request_id,
                intent=request.normalized_intent,
                state=InvestmentClosureState.NOT_REQUIRED,
                required_capabilities=required,
                completed_capabilities=completed,
                missing_capabilities=missing,
                same_request_continuation_required=False,
                investment_conclusion_blocked=False,
                investor_view_allowed=True,
                automatic_resolution_exhausted=automatic_resolution_exhausted,
                private_user_input_required=private_user_input_required,
            )

        terminal_coverage = (
            not missing
            and coverage.coverage_complete
            and coverage.outputs_verified
            and coverage.prohibited_call_count == 0
            and not coverage.unresolved_conflicts
        )
        if terminal_coverage:
            return InvestmentClosureDecision(
                request_id=request.request_id,
                intent=request.normalized_intent,
                state=InvestmentClosureState.READY_FOR_INVESTOR_VIEW,
                required_capabilities=required,
                completed_capabilities=completed,
                missing_capabilities=(),
                same_request_continuation_required=False,
                investment_conclusion_blocked=False,
                investor_view_allowed=True,
                automatic_resolution_exhausted=automatic_resolution_exhausted,
                private_user_input_required=False,
            )
        if private_user_input_required:
            return InvestmentClosureDecision(
                request_id=request.request_id,
                intent=request.normalized_intent,
                state=InvestmentClosureState.NEEDS_USER_INPUT,
                required_capabilities=required,
                completed_capabilities=completed,
                missing_capabilities=missing,
                same_request_continuation_required=False,
                investment_conclusion_blocked=True,
                investor_view_allowed=False,
                automatic_resolution_exhausted=True,
                private_user_input_required=True,
            )
        return InvestmentClosureDecision(
            request_id=request.request_id,
            intent=request.normalized_intent,
            state=InvestmentClosureState.CONTINUE_AUTOMATICALLY,
            required_capabilities=required,
            completed_capabilities=completed,
            missing_capabilities=missing,
            same_request_continuation_required=True,
            investment_conclusion_blocked=True,
            investor_view_allowed=False,
            automatic_resolution_exhausted=automatic_resolution_exhausted,
            private_user_input_required=False,
        )


__all__ = [
    "InvestmentClosureDecision",
    "InvestmentClosureState",
    "InvestmentRequestClosurePolicy",
    "InvestmentRequestNotTerminalError",
]
