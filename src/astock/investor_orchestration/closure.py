"""Machine-verifiable terminal gate for material investment requests."""

from __future__ import annotations

from enum import StrEnum

from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityRequirement,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    RequestIntent,
    StrictModel,
)
from astock.investor_orchestration.recommendation_fulfillment import (
    RecommendationFulfillmentAssessment,
)
from astock.schemas.research_acquisition import AcquisitionCapability
from astock.schemas.research_continuation import PublicRecoveryCoverage


class InvestmentRequestNotTerminalError(RuntimeError):
    """Raised when a material investment request still has automatic work to do."""


class InvestmentClosureState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    CONTINUE_AUTOMATICALLY = "CONTINUE_AUTOMATICALLY"
    READY_FOR_INVESTOR_VIEW = "READY_FOR_INVESTOR_VIEW"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    PUBLIC_DATA_UNAVAILABLE = "PUBLIC_DATA_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


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
    public_recovery_evidence: tuple[PublicRecoveryCoverage, ...] = ()
    private_user_input_required: bool = False
    automatic_budget_exhausted: bool = False
    private_input_capabilities: tuple[str, ...] = ()
    remaining_automatic_capabilities: tuple[str, ...] = ()
    recommendation_fulfillment: RecommendationFulfillmentAssessment | None = None


_MATERIAL_INVESTMENT_INTENTS = frozenset(
    {
        RequestIntent.RESEARCH,
        RequestIntent.FULL_RESEARCH_RECOMMENDATION,
    }
)
_SUCCESS = {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}


_RECOVERY_CAPABILITIES = {
    AcquisitionCapability.INSTRUMENT_IDENTITY: "ENTITY_IDENTITY",
    AcquisitionCapability.DAILY_MARKET: "CURRENT_MARKET",
    AcquisitionCapability.CORPORATE_ACTIONS: "EVENT_RESEARCH",
    AcquisitionCapability.FINANCIAL_ANNUAL: "FINANCIAL_INTEGRITY",
    AcquisitionCapability.FINANCIAL_LATEST_INTERIM: "FINANCIAL_INTEGRITY",
}


def _public_routes_exhausted(
    request: InvestorRequestEnvelope, plan: CapabilityExecutionPlan,
    remaining: tuple[str, ...], evidence: tuple[PublicRecoveryCoverage, ...],
) -> bool:
    if not request.entity_ids or not remaining or not evidence:
        return False
    by_entity: dict[str, list[PublicRecoveryCoverage]] = {}
    for proof in evidence:
        if proof.request_id != request.request_id:
            raise ValueError("public recovery evidence belongs to another request")
        by_entity.setdefault(f"{proof.market.value}:{proof.company_id}", []).append(proof)
    if set(request.entity_ids) != set(by_entity):
        return False
    exhausted_by_entity = []
    for proofs in by_entity.values():
        # Multiple continuations for one issuer must each finish their declared gaps.
        exhausted = set()
        for proof in proofs:
            empty = proof.exhausted_capabilities()
            for capability in empty:
                target = _RECOVERY_CAPABILITIES[capability]
                related = {item for item in proof.missing_capabilities
                           if _RECOVERY_CAPABILITIES[item] == target}
                if related.issubset(empty):
                    exhausted.add(target)
        incomplete_targets = {
            _RECOVERY_CAPABILITIES[item] for proof in proofs
            for item in set(proof.missing_capabilities) - proof.exhausted_capabilities()
        }
        exhausted_by_entity.append(exhausted - incomplete_targets)
    blocked = set.intersection(*exhausted_by_entity)
    while True:
        descendants = {
            node.capability_id for node in plan.nodes if set(node.dependencies) & blocked
        }
        if descendants.issubset(blocked):
            break
        blocked.update(descendants)
    return set(remaining).issubset(blocked)


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
        public_recovery_evidence: tuple[PublicRecoveryCoverage, ...] = (),
        private_user_input_required: bool = False,
        private_input_capabilities: tuple[str, ...] = (),
        automatic_budget_exhausted: bool = False,
        recommendation_fulfillment: RecommendationFulfillmentAssessment | None = None,
    ) -> InvestmentClosureDecision:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        plan = CapabilityExecutionPlan.model_validate(plan.model_dump())
        coverage = CapabilityCoverageReceipt.model_validate(coverage.model_dump())
        if request.request_id != plan.request_id or request.request_id != coverage.request_id:
            raise ValueError("investment closure identities differ")
        if coverage.plan_id != plan.plan_id or coverage.policy_version != plan.policy_version:
            raise ValueError("investment closure does not cover the active capability plan")

        if (
            recommendation_fulfillment is not None
            and recommendation_fulfillment.request_id != request.request_id
        ):
            raise ValueError("recommendation fulfillment belongs to another request")

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

        # Private input blocks only its dependency descendants. A legacy boolean
        # identifies a single gap, but cannot classify a whole multi-gap plan.
        private_scope = set(private_input_capabilities)
        if not private_scope.issubset(required):
            raise ValueError("private input must identify required plan capabilities")
        private_scope.intersection_update(missing)
        if private_user_input_required and not private_input_capabilities and len(missing) == 1:
            private_scope.update(missing)
        blocked = set(private_scope)
        while True:
            descendants = {
                node.capability_id for node in plan.nodes
                if set(node.dependencies) & blocked
            }
            if descendants.issubset(blocked):
                break
            blocked.update(descendants)
        remaining_automatic = tuple(sorted(set(missing) - blocked))
        private_needed = bool(private_scope)
        # A caller boolean is retained only for API compatibility, never as evidence.
        automatic_resolution_exhausted = _public_routes_exhausted(
            request, plan, remaining_automatic, public_recovery_evidence,
        )

        terminal_coverage = (
            not missing
            and coverage.coverage_complete
            and coverage.outputs_verified
            and coverage.prohibited_call_count == 0
            and not coverage.unresolved_conflicts
        )
        fulfillment_satisfied = (
            recommendation_fulfillment is None or recommendation_fulfillment.satisfied
        )
        if request.normalized_intent not in _MATERIAL_INVESTMENT_INTENTS:
            state = InvestmentClosureState.NOT_REQUIRED
        elif terminal_coverage and fulfillment_satisfied:
            state = InvestmentClosureState.READY_FOR_INVESTOR_VIEW
        elif private_needed and (
            not remaining_automatic
            or automatic_resolution_exhausted
            or automatic_budget_exhausted
        ):
            state = InvestmentClosureState.NEEDS_USER_INPUT
        elif automatic_budget_exhausted:
            state = InvestmentClosureState.BUDGET_EXHAUSTED
        elif automatic_resolution_exhausted:
            state = InvestmentClosureState.PUBLIC_DATA_UNAVAILABLE
        else:
            state = InvestmentClosureState.CONTINUE_AUTOMATICALLY
        ready = state in {
            InvestmentClosureState.NOT_REQUIRED,
            InvestmentClosureState.READY_FOR_INVESTOR_VIEW,
        }
        return InvestmentClosureDecision(
            request_id=request.request_id,
            intent=request.normalized_intent,
            state=state,
            required_capabilities=required,
            completed_capabilities=completed,
            missing_capabilities=missing,
            same_request_continuation_required=(
                state is InvestmentClosureState.CONTINUE_AUTOMATICALLY
            ),
            investment_conclusion_blocked=not ready,
            investor_view_allowed=ready,
            automatic_resolution_exhausted=automatic_resolution_exhausted,
            public_recovery_evidence=public_recovery_evidence,
            automatic_budget_exhausted=automatic_budget_exhausted,
            private_user_input_required=private_needed and not ready,
            private_input_capabilities=tuple(sorted(private_scope)),
            remaining_automatic_capabilities=remaining_automatic,
            recommendation_fulfillment=recommendation_fulfillment,
        )


__all__ = [
    "InvestmentClosureDecision",
    "InvestmentClosureState",
    "InvestmentRequestClosurePolicy",
    "InvestmentRequestNotTerminalError",
]
