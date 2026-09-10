from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.investor_orchestration.capabilities import _INTENT_REQUIREMENTS
from astock.investor_orchestration.closure import (
    InvestmentClosureState,
    InvestmentRequestClosurePolicy,
)
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityNode,
    CapabilityRequirement,
    CapabilityRunRecord,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)

NOW = datetime(2026, 9, 10, 5, 30, tzinfo=UTC)


def _request(intent: RequestIntent = RequestIntent.RECOMMENDATION) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope(
        request_id="request-closure-1",
        question_time=NOW,
        user_timezone="Asia/Shanghai",
        raw_text="请向我推荐现在可买的股票",
        normalized_intent=intent,
        side_effect=SideEffectClass.READ,
        idempotency_key="closure-1",
    )


def _plan(request: InvestorRequestEnvelope) -> CapabilityExecutionPlan:
    required = sorted(
        _INTENT_REQUIREMENTS[request.normalized_intent]
        | {
            "REQUEST_TIME",
            "ENTITY_IDENTITY",
            "SESSION_PREFLIGHT",
            "SUBJECT_REGISTRY",
            "RESPONSE_GATEWAY",
        }
    )
    nodes = tuple(
        CapabilityNode(
            capability_id=capability_id,
            requirement=CapabilityRequirement.REQUIRED,
            output_schema=f"{capability_id}Output",
        )
        for capability_id in required
    )
    return CapabilityExecutionPlan(
        plan_id="plan-closure-1",
        request_id=request.request_id,
        policy_version="investor-capability-policy-v1",
        nodes=nodes,
        planned_at=NOW,
        plan_hash="plan-hash",
    )


def _coverage(
    request: InvestorRequestEnvelope,
    plan: CapabilityExecutionPlan,
    *,
    missing: set[str] | None = None,
) -> CapabilityCoverageReceipt:
    missing = missing or set()
    records = tuple(
        CapabilityRunRecord(
            capability_id=node.capability_id,
            status=(
                CapabilityRunStatus.FAILED
                if node.capability_id in missing
                else CapabilityRunStatus.COMPLETED
            ),
            artifact_ids=(
                ()
                if node.capability_id in missing
                else (f"artifact:{node.capability_id}",)
            ),
        )
        for node in plan.nodes
    )
    complete = not missing
    return CapabilityCoverageReceipt(
        receipt_id="coverage-closure-1",
        request_id=request.request_id,
        plan_id=plan.plan_id,
        policy_version=plan.policy_version,
        records=records,
        required_capability_coverage=1.0 if complete else 0.95,
        prohibited_call_count=0,
        coverage_complete=complete,
        outputs_verified=complete,
        preflight_receipt_id="preflight-1",
        request_fingerprint="request-fingerprint",
        unresolved_conflicts=() if complete else tuple(f"{item}:FAILED" for item in missing),
        receipt_hash="coverage-hash",
    )


def test_recommendation_canonical_plan_contains_full_closed_loop_families() -> None:
    required = _INTENT_REQUIREMENTS[RequestIntent.RECOMMENDATION]

    assert {
        "CURRENT_MARKET",
        "MARKET_REGIME",
        "FULL_MARKET",
        "COMPANY_RESEARCH",
        "INDUSTRY",
        "FINANCIAL_INTEGRITY",
        "GOVERNANCE",
        "EVENT_RESEARCH",
        "FORECAST_VALUATION",
        "RED_TEAM",
        "COMMITTEE",
        "PORTFOLIO",
    } <= required


def test_material_recommendation_cannot_stop_at_intermediate_capability_state() -> None:
    request = _request()
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"COMMITTEE", "PORTFOLIO"})

    decision = InvestmentRequestClosurePolicy.evaluate(request, plan, coverage)

    assert decision.state is InvestmentClosureState.CONTINUE_AUTOMATICALLY
    assert decision.same_request_continuation_required
    assert decision.investment_conclusion_blocked
    assert not decision.investor_view_allowed
    assert set(decision.missing_capabilities) == {"COMMITTEE", "PORTFOLIO"}
    assert not decision.broker_execution_allowed


def test_material_recommendation_is_terminal_only_after_verified_full_plan() -> None:
    request = _request()
    plan = _plan(request)
    coverage = _coverage(request, plan)

    decision = InvestmentRequestClosurePolicy.evaluate(request, plan, coverage)

    assert decision.state is InvestmentClosureState.READY_FOR_INVESTOR_VIEW
    assert not decision.same_request_continuation_required
    assert not decision.investment_conclusion_blocked
    assert decision.investor_view_allowed
    assert decision.missing_capabilities == ()


def test_user_input_can_only_be_requested_after_public_automatic_resolution_is_exhausted() -> None:
    request = _request(RequestIntent.BUY_DECISION)
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"FINANCIAL_INTEGRITY"})

    with pytest.raises(ValueError, match="public-source resolution must be exhausted"):
        InvestmentRequestClosurePolicy.evaluate(
            request,
            plan,
            coverage,
            private_user_input_required=True,
        )

    decision = InvestmentRequestClosurePolicy.evaluate(
        request,
        plan,
        coverage,
        automatic_resolution_exhausted=True,
        private_user_input_required=True,
    )
    assert decision.state is InvestmentClosureState.NEEDS_USER_INPUT
    assert not decision.same_request_continuation_required
    assert decision.investment_conclusion_blocked
    assert not decision.investor_view_allowed


def test_non_material_monitor_request_does_not_inherit_investment_terminal_block() -> None:
    request = _request(RequestIntent.MONITOR)
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"EVENT_RESEARCH"})

    decision = InvestmentRequestClosurePolicy.evaluate(request, plan, coverage)

    assert decision.state is InvestmentClosureState.NOT_REQUIRED
    assert not decision.same_request_continuation_required
    assert not decision.investment_conclusion_blocked
    assert decision.investor_view_allowed
