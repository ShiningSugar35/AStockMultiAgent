from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from astock.investor_orchestration.closure import (
    InvestmentClosureState,
    InvestmentRequestClosurePolicy,
)
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.models import (
    CapabilityRunStatus,
    recommendation_quantity_targets,
    requested_recommendation_count,
)
from astock.investor_orchestration.recommendation_fulfillment import (
    RecommendationFulfillmentAssessment,
    RecommendationFulfillmentService,
    RecommendationFulfillmentState,
)
from tests.unit.test_full_research_recommendation import _build_receipt, _request
from tests.unit.test_investment_request_closure import _coverage, _plan


def test_broad_recommendation_defaults_to_three_minimum_five_target() -> None:
    request = _request("请向我推荐现在可买的股票", request_id="breadth-default")
    assert recommendation_quantity_targets(request.raw_text) == (3, 5)
    assert request.metadata["recommendation_minimum_actionable"] == 3
    assert request.metadata["recommendation_target_actionable"] == 5
    contract = FullResearchRecommendationService().request_contract(request)
    assert contract.requested_count == 5


def test_explicit_counts_override_defaults_and_single_stock_has_no_quota() -> None:
    explicit = _request("请推荐三只股票", request_id="breadth-explicit")
    assert requested_recommendation_count(explicit.raw_text) == 3
    assert explicit.metadata["recommendation_minimum_actionable"] == 3
    assert explicit.metadata["recommendation_target_actionable"] == 3
    chinese = _request("请推荐五只股票", request_id="breadth-five")
    assert chinese.metadata["recommendation_target_actionable"] == 5

    single = _request("湖南黄金现在能买吗？", request_id="single-stock")
    assert recommendation_quantity_targets(single.raw_text) is None
    assert "recommendation_minimum_actionable" not in single.metadata
    assert FullResearchRecommendationService().request_contract(single).requested_count is None


def test_only_current_immediate_formal_positions_count_as_actionable() -> None:
    service = FullResearchRecommendationService()
    broad = _request("请推荐现在可买的股票", request_id="actionable-five")
    receipt = _build_receipt(service, broad, candidate_count=3)
    assert RecommendationFulfillmentService._actionable(receipt) == tuple(
        sorted(receipt.portfolio.positions[index].instrument_id for index in range(3))
    )
    immediate, conditional = RecommendationFulfillmentService._actionability(receipt)
    assert len(immediate) == 3
    assert conditional == ()

    watch = _build_receipt(
        service,
        _request("请推荐现在可买的股票", request_id="watch-only"),
        candidate_count=5,
        all_ineligible=True,
        ineligible_reason="COMMITTEE_WATCH",
    )
    assert RecommendationFulfillmentService._actionable(watch) == ()

    stale = _build_receipt(
        service,
        _request("请推荐现在可买的股票", request_id="stale-quote"),
        candidate_count=3,
        stale_quote=True,
    )
    immediate, conditional = RecommendationFulfillmentService._actionability(stale)
    assert immediate == ()
    assert len(conditional) == 3
    assert RecommendationFulfillmentService._actionable(stale) == ()

    fresh_but_outside_range = _build_receipt(
        service,
        _request("请推荐现在可买的股票", request_id="fresh-outside-entry-range"),
        candidate_count=3,
    )
    first = fresh_but_outside_range.execution_plans[0]
    assert first.buy_range_high is not None
    shifted = first.model_copy(
        update={"reference_price": first.buy_range_high + Decimal("1")}
    )
    fresh_but_outside_range = fresh_but_outside_range.model_copy(
        update={
            "execution_plans": (shifted, *fresh_but_outside_range.execution_plans[1:])
        }
    )
    immediate, conditional = RecommendationFulfillmentService._actionability(
        fresh_but_outside_range
    )
    assert fresh_but_outside_range.execution_plans[0].instrument_id not in immediate
    assert fresh_but_outside_range.execution_plans[0].instrument_id in conditional


def _fulfillment(
    request_id: str, *, satisfied: bool, actionable: tuple[str, ...] = ()
) -> RecommendationFulfillmentAssessment:
    return RecommendationFulfillmentAssessment(
        request_id=request_id,
        minimum_actionable_count=3,
        target_actionable_count=5,
        actionable_instruments=actionable,
        state=(
            RecommendationFulfillmentState.SATISFIED
            if satisfied
            else RecommendationFulfillmentState.GENERATE_NEXT_SEED_BATCH
        ),
        satisfied=satisfied,
        next_action=(
            "PUBLISH_VERIFIED_RECOMMENDATION"
            if satisfied
            else "GENERATE_NEXT_SEED_BATCH_WITH_EXCLUSIONS"
        ),
    )


def test_terminal_capabilities_cannot_publish_broad_result_below_minimum() -> None:
    request = _request("请向我推荐现在可买的股票", request_id="closure-breadth")
    plan = _plan(request)
    coverage = _coverage(request, plan)
    decision = InvestmentRequestClosurePolicy.evaluate(
        request,
        plan,
        coverage,
        recommendation_fulfillment=_fulfillment(request.request_id, satisfied=False),
    )
    assert decision.state is InvestmentClosureState.CONTINUE_AUTOMATICALLY
    assert decision.same_request_continuation_required
    assert decision.investment_conclusion_blocked
    assert not decision.investor_view_allowed

    satisfied = InvestmentRequestClosurePolicy.evaluate(
        request,
        plan,
        coverage,
        recommendation_fulfillment=_fulfillment(
            request.request_id,
            satisfied=True,
            actionable=("600001.XSHG", "600002.XSHG", "600003.XSHG"),
        ),
    )
    assert satisfied.state is InvestmentClosureState.READY_FOR_INVESTOR_VIEW
    assert satisfied.investor_view_allowed
    assert satisfied.recommendation_fulfillment is not None

    exhausted = InvestmentRequestClosurePolicy.evaluate(
        request,
        plan,
        coverage,
        automatic_resolution_exhausted=True,
        recommendation_fulfillment=_fulfillment(request.request_id, satisfied=False),
    )
    assert exhausted.state is InvestmentClosureState.PUBLIC_DATA_UNAVAILABLE
    assert not exhausted.same_request_continuation_required
    assert exhausted.investment_conclusion_blocked
    assert not exhausted.investor_view_allowed


def test_three_actionable_names_still_require_a_formal_full_market_universe(
    monkeypatch,
) -> None:
    request = _request("请向我推荐现在可买的股票", request_id="formal-universe-required")
    plan = _plan(request)
    coverage = _coverage(request, plan)
    records = []
    for record in coverage.records:
        if record.capability_id == "FULL_RESEARCH_GATE":
            record = record.model_copy(
                update={
                    "status": CapabilityRunStatus.COMPLETED,
                    "artifact_ids": ("RecommendationResearchReceipt:test",),
                }
            )
        records.append(record)
    coverage = coverage.model_copy(update={"records": tuple(records)})
    receipt = _build_receipt(
        FullResearchRecommendationService(),
        request,
        candidate_count=3,
    )
    fulfillment = RecommendationFulfillmentService(cast(Any, None), cast(Any, None))
    monkeypatch.setattr(fulfillment, "_receipt", lambda *_: receipt)

    partial = SimpleNamespace(
        report_id="partial-seeds",
        formal_full_market_coverage_allowed=False,
        seeds=(),
    )
    monkeypatch.setattr(fulfillment, "_seed_reports", lambda *_: (partial,))
    blocked = fulfillment.assess(request, coverage)
    assert blocked is not None
    assert blocked.state is RecommendationFulfillmentState.RECOVER_FORMAL_UNIVERSE
    assert not blocked.satisfied
    assert len(blocked.actionable_instruments) == 3

    formal = SimpleNamespace(
        report_id="formal-seeds",
        formal_full_market_coverage_allowed=True,
        seeds=(),
    )
    monkeypatch.setattr(fulfillment, "_seed_reports", lambda *_: (formal,))
    ready = fulfillment.assess(request, coverage)
    assert ready is not None
    assert ready.state is RecommendationFulfillmentState.SATISFIED
    assert ready.satisfied
    assert len(ready.actionable_instruments) == 3


def test_fulfillment_distinguishes_universe_recovery_from_more_candidates(monkeypatch) -> None:
    request = _request("请向我推荐现在可买的股票", request_id="funnel-state")
    plan = _plan(request)
    coverage = _coverage(request, plan)
    records = []
    for record in coverage.records:
        if record.capability_id == "FULL_RESEARCH_GATE":
            record = record.model_copy(
                update={
                    "status": CapabilityRunStatus.COMPLETED,
                    "artifact_ids": ("RecommendationResearchReceipt:test",),
                }
            )
        records.append(record)
    coverage = coverage.model_copy(update={"records": tuple(records)})
    receipt = _build_receipt(
        FullResearchRecommendationService(),
        request,
        candidate_count=2,
        all_ineligible=True,
        ineligible_reason="COMMITTEE_WATCH",
    )
    fulfillment = RecommendationFulfillmentService(cast(Any, None), cast(Any, None))
    monkeypatch.setattr(fulfillment, "_receipt", lambda *_: receipt)

    partial_report = SimpleNamespace(
        report_id="partial-seeds",
        formal_full_market_coverage_allowed=False,
        seeds=(),
    )
    monkeypatch.setattr(fulfillment, "_seed_reports", lambda *_: (partial_report,))
    partial = fulfillment.assess(request, coverage)
    assert partial is not None
    assert partial.state is RecommendationFulfillmentState.RECOVER_FORMAL_UNIVERSE
    assert partial.actionable_instruments == ()
    assert set(partial.watch_instruments) == set(receipt.candidate_universe)

    duplicate_seed = SimpleNamespace(company_id="600001", research_priority_score=0.99)
    next_seed = SimpleNamespace(company_id="600099", research_priority_score=0.91)
    formal_report = SimpleNamespace(
        report_id="formal-seeds",
        formal_full_market_coverage_allowed=True,
        seeds=(duplicate_seed, next_seed),
    )
    monkeypatch.setattr(fulfillment, "_seed_reports", lambda *_: (formal_report,))
    more = fulfillment.assess(request, coverage)
    assert more is not None
    assert more.state is RecommendationFulfillmentState.EXPAND_EXISTING_SEEDS
    assert more.next_candidate_ids == ("600099",)
    assert "600001" in more.next_seed_exclusions
    assert "600002" in more.next_seed_exclusions
    assert all(len(value) == 6 and value.isdigit() for value in more.next_seed_exclusions)
