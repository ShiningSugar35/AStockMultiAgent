from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash
from astock.schemas.full_research import (
    CandidateDecisionNarrative,
    CandidateRankingEntry,
    ChallengerAssessment,
    CompanyFundamentalSnapshot,
    EventEvidenceClass,
    EvidenceConflict,
    FactorSnapshot,
    FinancialQualityAssessment,
    FullResearchNode,
    FullResearchNodeStatus,
    GovernanceAssessment,
    HoldingDecisionSnapshot,
    IndustryResearchOutcome,
    MacroResearchOutcome,
    NewsEvent,
    NewsEventCoverage,
    PortfolioPositionPlan,
    RecommendationValuationSnapshot,
    SourceAuthority,
    SourceFamily,
    SourceLineageEntry,
    ValuationScenario,
)

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)


def _request(
    text: str,
    *,
    intent: RequestIntent | str = RequestIntent.RESEARCH,
    side_effect: SideEffectClass = SideEffectClass.READ,
    request_id: str = "full-research-test",
) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope.model_validate(
        {
            "request_id": request_id,
            "question_time": NOW,
            "user_timezone": "Asia/Shanghai",
            "raw_text": text,
            "normalized_intent": intent,
            "side_effect": side_effect,
            "idempotency_key": f"idem:{request_id}",
        }
    )


def _macro() -> MacroResearchOutcome:
    return MacroResearchOutcome(
        macro_regime="温和增长",
        liquidity_regime="中性偏松",
        risk_appetite="中性",
        policy_bias="结构性支持",
        dimensions={
            "domestic_growth": "温和增长",
            "inflation": "低位",
            "credit": "稳定",
            "liquidity": "中性偏松",
            "interest_rates": "低位",
            "foreign_exchange": "双向波动",
            "fiscal_policy": "积极",
            "industrial_policy": "结构性支持",
            "real_estate": "修复中",
            "exports": "韧性",
            "global_risk_assets": "中性",
        },
        macro_sector_implications=("盈利质量优先",),
        macro_risk_events=("外部风险资产波动",),
        evidence_ids=("evidence-macro",),
        created_at=NOW,
    )


def _industry(industry_id: str = "IND-1") -> IndustryResearchOutcome:
    return IndustryResearchOutcome(
        industry_id=industry_id,
        industry_score=Decimal("70"),
        cycle_phase="MID_CYCLE",
        competitive_intensity="MEDIUM",
        dimensions={
            "demand": "stable",
            "supply_capacity": "balanced",
            "inventory": "normal",
            "pricing": "stable",
            "capital_expenditure": "disciplined",
            "competition": "medium",
            "policy": "neutral",
            "technology": "incremental",
            "cycle_position": "mid",
            "value_chain_bargaining_power": "balanced",
            "valuation_percentile": Decimal("0.45"),
        },
        industry_catalysts=("需求改善",),
        industry_risks=("价格竞争",),
        peer_set=("600101", "600102", "600103", "600104", "600105"),
        evidence_ids=("evidence-industry",),
        created_at=NOW,
    )


def _fundamental(code: str) -> CompanyFundamentalSnapshot:
    return CompanyFundamentalSnapshot(
        instrument_id=code,
        available_complete_years=6,
        analyzed_complete_years=5,
        available_quarters=16,
        analyzed_quarters=12,
        ttm_reconstructed=True,
        metrics={
            "revenue": Decimal("100"),
            "parent_net_profit": Decimal("12"),
            "adjusted_net_profit": Decimal("11"),
            "gross_margin": Decimal("0.35"),
            "operating_margin": Decimal("0.18"),
            "roe": Decimal("0.16"),
            "roic": Decimal("0.14"),
            "operating_cash_flow": Decimal("15"),
            "free_cash_flow": Decimal("9"),
            "capital_expenditure": Decimal("6"),
            "receivables": Decimal("8"),
            "inventory": Decimal("7"),
            "contract_liabilities": Decimal("4"),
            "net_debt": Decimal("-2"),
            "financing_cost": Decimal("0.03"),
            "shares_outstanding": Decimal("10"),
            "dividends": Decimal("3"),
            "buybacks": Decimal("1"),
            "earnings_revision": Decimal("0.02"),
        },
        growth_decomposition={
            "price": Decimal("0.02"),
            "volume": Decimal("0.05"),
            "consolidation": Decimal("0"),
            "foreign_exchange": Decimal("0"),
            "non_recurring": Decimal("0"),
            "subsidy": Decimal("0"),
            "asset_disposal": Decimal("0"),
            "fair_value_change": Decimal("0"),
            "core_business": Decimal("0.07"),
        },
        source_artifact_ids=("artifact-1",),
        source_object_hashes=("1" * 64,),
        created_at=NOW,
    )


def _financial(code: str, *, veto: bool = False) -> FinancialQualityAssessment:
    checks: dict[str, bool | str | Decimal] = {
        "audit_opinion": "STANDARD_UNQUALIFIED",
        "non_standard_opinion": False,
        "revenue_cashflow_divergence": False,
        "receivables_anomaly": False,
        "inventory_anomaly": False,
        "gross_margin_anomaly": False,
        "capitalized_r_and_d": False,
        "goodwill_impairment": False,
        "asset_disposal": False,
        "government_subsidy": False,
        "related_party_transactions": False,
        "controlling_shareholder_fund_occupation": False,
        "share_pledge": False,
        "guarantees": False,
        "short_debt_long_investment": False,
        "cash_and_interest_bearing_debt": False,
        "non_recurring_items": False,
        "minority_interest": False,
        "cash_conversion_quality": "GOOD",
    }
    return FinancialQualityAssessment(
        instrument_id=code,
        accounting_quality_score=Decimal("85" if not veto else "20"),
        checks=checks,
        red_flags=("AUDIT_CRITICAL",) if veto else (),
        critical_veto=veto,
        critical_veto_reasons=("AUDIT_CRITICAL",) if veto else (),
        audit_opinion="STANDARD_UNQUALIFIED" if not veto else "ADVERSE",
        cash_conversion_quality="GOOD" if not veto else "POOR",
        evidence_ids=("evidence-financial",),
        created_at=NOW,
    )


def _governance(code: str, *, veto: bool = False) -> GovernanceAssessment:
    checks: dict[str, bool | str | Decimal] = {
        "controlling_shareholder": "KNOWN",
        "actual_controller": "KNOWN",
        "management_stability": "STABLE",
        "regulatory_penalties": False,
        "formal_investigations": veto,
        "director_executive_changes": False,
        "insider_reductions": False,
        "share_pledges": False,
        "related_party_transactions": False,
        "fund_occupation": False,
        "illegal_guarantees": False,
        "auditor_changes": False,
        "material_litigation": False,
    }
    return GovernanceAssessment(
        instrument_id=code,
        governance_score=Decimal("80" if not veto else "15"),
        checks=checks,
        red_flags=("FORMAL_INVESTIGATION",) if veto else (),
        critical_veto=veto,
        critical_veto_reasons=("FORMAL_INVESTIGATION",) if veto else (),
        controller="controller",
        management_stability="STABLE",
        evidence_ids=("evidence-governance",),
        created_at=NOW,
    )


def _factor(code: str, *, dominant: tuple[str, ...] = ()) -> FactorSnapshot:
    return FactorSnapshot(
        instrument_id=code,
        value=Decimal("0.3"),
        quality=Decimal("0.7"),
        growth=Decimal("0.4"),
        momentum=Decimal("0.2"),
        low_volatility=Decimal("0.4"),
        liquidity=Decimal("0.8"),
        size=Decimal("0.5"),
        earnings_revision=Decimal("0.3"),
        profitability=Decimal("0.7"),
        crowding=Decimal("0.2"),
        dominant_exposures=dominant,
        created_at=NOW,
    )


def _valuation(
    code: str, *, current_price: Decimal = Decimal("10")
) -> RecommendationValuationSnapshot:
    scenarios = (
        ValuationScenario(
            scenario="BEAR",
            per_share_value=Decimal("8"),
            probability=Decimal("0.25"),
            expected_return=Decimal("-0.2"),
            created_at=NOW,
        ),
        ValuationScenario(
            scenario="BASE",
            per_share_value=Decimal("14"),
            probability=Decimal("0.50"),
            expected_return=Decimal("0.4"),
            created_at=NOW,
        ),
        ValuationScenario(
            scenario="BULL",
            per_share_value=Decimal("18"),
            probability=Decimal("0.25"),
            expected_return=Decimal("0.8"),
            created_at=NOW,
        ),
    )
    return RecommendationValuationSnapshot(
        instrument_id=code,
        method_family="GENERAL_CORPORATE",
        methods=("DCF", "PEER_EV_EBITDA", "HISTORICAL_PERCENTILE"),
        current_price=current_price,
        scenarios=scenarios,
        expected_return_mean=Decimal("0.35"),
        expected_return_downside=Decimal("-0.2"),
        margin_of_safety=(Decimal("14") - current_price) / Decimal("14"),
        historical_percentile=Decimal("0.4"),
        peer_relative_percentile=Decimal("0.45"),
        source_artifact_ids=("artifact-1",),
        source_object_hashes=("1" * 64,),
        created_at=NOW,
    )


def _candidate(
    code: str,
    factor: FactorSnapshot,
    *,
    industry_id: str = "IND-1",
    eligible: bool = True,
    reason: str | None = None,
) -> CandidateRankingEntry:
    return CandidateRankingEntry(
        instrument_id=code,
        industry_id=industry_id,
        expected_return=Decimal("0.35"),
        downside=Decimal("0.2"),
        quality=Decimal("0.8"),
        valuation=Decimal("0.7"),
        catalyst=Decimal("0.5"),
        macro_fit=Decimal("0.5"),
        industry_fit=Decimal("0.6"),
        momentum=Decimal("0.2"),
        liquidity=Decimal("0.8"),
        accounting_risk=Decimal("0.1"),
        governance_risk=Decimal("0.1"),
        evidence_confidence=Decimal("0.9"),
        eligible=eligible,
        rejection_reasons=() if eligible else (reason or "VALUATION_TOO_HIGH",),
        factor_snapshot=factor,
        created_at=NOW,
    )


def _narrative(code: str) -> CandidateDecisionNarrative:
    return CandidateDecisionNarrative(
        instrument_id=code,
        investment_thesis="盈利质量、现金流与估值安全边际共同支持中期持有",
        why_now="估值低于基础情景合理价值，行业景气与盈利修正没有恶化",
        catalysts=("盈利兑现", "行业需求改善"),
        primary_risks=("需求低于预期", "估值修复延后"),
        thesis_invalidation_conditions=("核心盈利连续恶化",),
        evidence_ids=("evidence-financial", "evidence-industry"),
        created_at=NOW,
    )


def _challenger(code: str) -> ChallengerAssessment:
    return ChallengerAssessment(
        instrument_id=code,
        independent_context_id=f"challenger:{code}",
        raw_fact_artifact_ids=("artifact-1",),
        primary_final_label_visible=False,
        market_may_be_right_because="盈利改善可能已经部分计价",
        overlooked_bad_news=("需求恢复慢于预期",),
        valuation_fully_priced_risk="若增速回落，当前估值不再便宜",
        thesis_invalidation_conditions=("核心盈利连续恶化",),
        maximum_reasonable_downside=Decimal("0.25"),
        better_alternatives=(),
        material_conflict_with_primary=False,
        confidence_adjustment=Decimal("0"),
        created_at=NOW,
    )


def _news() -> tuple[NewsEvent, NewsEventCoverage]:
    event = NewsEvent(
        event_id="event-1",
        instrument_id="600001.XSHG",
        event_timestamp=NOW - timedelta(days=2),
        source_id="src-news",
        evidence_class=EventEvidenceClass.OFFICIAL_FILING,
        confidence=Decimal("1"),
        expected_direction="NEUTRAL",
        impact_horizon="30D",
        already_priced_probability=Decimal("0.5"),
        summary="近期正式披露未改变基础情景",
        created_at=NOW,
    )
    coverage = NewsEventCoverage(
        as_of=NOW,
        covered_windows_days=(7, 30, 90, 180),
        covered_categories=(
            "earnings",
            "orders",
            "m_and_a",
            "restructuring",
            "buyback",
            "insider_reduction",
            "financing",
            "major_contract",
            "product",
            "policy",
            "litigation",
            "investigation",
            "safety_incident",
            "management",
            "industry_pricing",
            "supply_chain",
            "overseas_sanctions_trade",
            "domestic_upstream_downstream_news",
            "global_upstream_downstream_news",
            "domestic_upstream_downstream_policy",
        ),
        source_classes_seen=(EventEvidenceClass.OFFICIAL_FILING,),
        event_ids=(event.event_id,),
        conflicts_resolved=True,
        created_at=NOW,
    )
    return event, coverage


def _sources(
    service: FullResearchRecommendationService,
    *,
    stale_quote: bool = False,
    missing_family: SourceFamily | None = None,
) -> tuple[SourceLineageEntry, ...]:
    families = (
        SourceFamily.MARKET_PRICE,
        SourceFamily.FINANCIAL_REPORT,
        SourceFamily.OFFICIAL_FILING,
        SourceFamily.MACRO_RELEASE,
        SourceFamily.NEWS_EVENT,
    )
    result: list[SourceLineageEntry] = []
    for index, family in enumerate(families, start=1):
        if family == missing_family:
            continue
        artifact_id = f"artifact-{index}"
        digest = content_hash({"artifact": artifact_id, "family": family.value})
        if service.state is not None and service.objects is not None:
            ref = service.objects.put_json({"artifact": artifact_id, "family": family.value})
            digest = ref.sha256
            service.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="FullResearchTestInput",
                schema_version="v1",
                object_hash=digest,
                input_hashes=[],
            )
        observed = NOW - timedelta(
            seconds=300 if stale_quote and family == SourceFamily.MARKET_PRICE else 60
        )
        captured = observed + timedelta(seconds=10)
        available = captured + timedelta(seconds=10)
        result.append(
            SourceLineageEntry(
                source_id="src-news" if family == SourceFamily.NEWS_EVENT else f"source-{index}",
                family=family,
                authority=(
                    SourceAuthority.PRIMARY_OFFICIAL
                    if family != SourceFamily.MARKET_PRICE
                    else SourceAuthority.SECONDARY_STRUCTURED
                ),
                provider=f"provider-{index}",
                source_identity=f"identity-{index}",
                source_url=f"https://example.invalid/{index}",
                artifact_id=artifact_id,
                evidence_id=f"evidence-{index}",
                object_hash=digest,
                observed_at=observed,
                captured_at=captured,
                available_to_system_at=available,
                ingestion_version="ingestion-v1",
                parser_version="parser-v1",
                created_at=NOW,
            )
        )
    return tuple(result)


def _build_receipt(
    service: FullResearchRecommendationService,
    request: InvestorRequestEnvelope,
    *,
    candidate_count: int = 1,
    stale_quote: bool = False,
    missing_family: SourceFamily | None = None,
    open_conflict: bool = False,
    financial_veto: bool = False,
    governance_veto: bool = False,
    all_ineligible: bool = False,
    dominant_exposure: str | None = None,
    holding_action: Literal["HOLD", "ADD", "TRIM", "EXIT", "REVIEW"] | None = None,
):
    contract = service.request_contract(request)
    sources = _sources(service, stale_quote=stale_quote, missing_family=missing_family)
    conflicts = ()
    if open_conflict:
        conflicts = (
            EvidenceConflict(
                conflict_id="conflict-news",
                subject="event direction",
                source_ids=(sources[0].source_id, sources[-1].source_id),
                status="OPEN",
                created_at=NOW,
            ),
        )
    pit = service.point_in_time_snapshot(request, sources, conflicts=conflicts)

    codes = tuple(f"600{index + 1:03d}.XSHG" for index in range(candidate_count))
    holding_reviews = ()
    if holding_action is not None:
        holding_reviews = (
            HoldingDecisionSnapshot(
                instrument_id=codes[0],
                position_id="position-1",
                portfolio_context_revision="portfolio-context-rev-1",
                recommended_action=holding_action,
                action_confidence=Decimal("0.9"),
                thesis_strength_change="WEAKENED"
                if holding_action in {"TRIM", "EXIT"}
                else "UNCHANGED",
                risk_change="HIGHER" if holding_action in {"TRIM", "EXIT"} else "UNCHANGED",
                current_quantity=Decimal("500"),
                current_weight=Decimal("0.30"),
                target_weight_lower=Decimal("0.10"),
                target_weight_mid=Decimal("0.15"),
                target_weight_upper=Decimal("0.20"),
                target_quantity_min=100,
                target_quantity_max=300,
                preconditions=("确认当前价格与账户可售数量",),
                reversal_conditions=("核心盈利假设重新增强",),
                next_review_conditions=("下一份正式财报发布后复核",),
                evidence_ids=("evidence-holding-1",),
                source_artifact_id=sources[0].artifact_id or "artifact-1",
                source_object_hash=sources[0].object_hash,
                created_at=NOW,
            ),
        )
    factors = tuple(
        _factor(code, dominant=(dominant_exposure,) if dominant_exposure else ()) for code in codes
    )
    candidates = tuple(
        _candidate(
            code,
            factor,
            industry_id=f"IND-{index + 1}",
            eligible=not all_ineligible,
            reason="VALUATION_TOO_HIGH" if all_ineligible else None,
        )
        for index, (code, factor) in enumerate(zip(codes, factors, strict=True))
    )
    financials = tuple(
        _financial(code, veto=financial_veto and index == 0) for index, code in enumerate(codes)
    )
    governance = tuple(
        _governance(code, veto=governance_veto and index == 0) for index, code in enumerate(codes)
    )
    candidates = service.apply_candidate_vetoes(candidates, financials, governance)
    valuations = tuple(_valuation(code) for code in codes)
    portfolio = service.build_portfolio(contract, candidates, valuations)
    risk = service.audit_portfolio_risk(
        portfolio,
        portfolio_beta=Decimal("0.9"),
        expected_volatility=Decimal("0.18"),
        expected_shortfall=Decimal("0.12"),
        max_drawdown_proxy=Decimal("0.20"),
        max_order_adv_fraction=Decimal("0.01"),
        turnover=Decimal("0.15"),
        source_artifact_ids=(sources[0].artifact_id or "artifact-1",),
        source_object_hashes=(sources[0].object_hash,),
    )
    market_source = next(item for item in sources if item.family == SourceFamily.MARKET_PRICE)
    quote_sources = {position.instrument_id: market_source for position in portfolio.positions}
    execution = service.execution_plans(NOW, portfolio, valuations, quote_sources)

    statuses: dict[FullResearchNode | str, FullResearchNodeStatus | str] = {
        node: FullResearchNodeStatus.PASS for node in FullResearchNode
    }
    reasons: dict[FullResearchNode | str, str] = {}
    if pit.status != FullResearchNodeStatus.PASS:
        statuses[FullResearchNode.POINT_IN_TIME_SNAPSHOT] = pit.status
        reasons[FullResearchNode.POINT_IN_TIME_SNAPSHOT] = (
            "PIT evidence is incomplete or conflicted"
        )
    if risk.status != FullResearchNodeStatus.PASS:
        statuses[FullResearchNode.RISK_AUDIT] = risk.status
        reasons[FullResearchNode.RISK_AUDIT] = "portfolio risk constraints failed"
    anchor_artifact = sources[0].artifact_id or "artifact-1"
    dag = service.dag_receipt(
        request,
        statuses,
        artifact_ids={node: (anchor_artifact,) for node in FullResearchNode},
        reasons=reasons,
        at=NOW,
    )
    publication = service.publication_decision(
        dag,
        pit,
        portfolio,
        risk,
        execution,
        candidates,
    )
    event, news_coverage = _news()
    challengers = tuple(_challenger(code) for code in codes)
    fundamentals = tuple(_fundamental(code) for code in codes)
    rejected = {
        item.instrument_id: item.rejection_reasons for item in candidates if not item.eligible
    }
    receipt = service.seal_receipt(
        {
            "request_id": request.request_id,
            "as_of": NOW,
            "request_contract": contract,
            "holding_reviews": holding_reviews,
            "pit_snapshot": pit,
            "dag": dag,
            "source_manifest": sources,
            "skill_executions": {node.value: statuses[node] for node in FullResearchNode},
            "candidate_universe": codes,
            "candidate_rankings": candidates,
            "candidate_narratives": tuple(_narrative(code) for code in codes),
            "rejected_candidates": rejected,
            "fundamentals": fundamentals,
            "financial_quality": financials,
            "governance": governance,
            "valuations": valuations,
            "news_events": (event,),
            "news_coverage": news_coverage,
            "challengers": challengers,
            "factor_scores": factors,
            "macro": _macro(),
            "industries": tuple(_industry(f"IND-{index + 1}") for index in range(candidate_count)),
            "portfolio": portfolio,
            "risk_audit": risk,
            "execution_plans": execution,
            "optimizer_inputs": {"capital_rmb": contract.portfolio_assumptions.capital_rmb},
            "optimizer_outputs": {"position_count": len(portfolio.positions)},
            "publication": publication,
            "model_versions": {"fundamental": "v1", "valuation": "v1", "factor": "v1"},
            "code_version": "test-tree",
            "config_versions": {"full_research": service.policy.policy_id},
            "input_artifact_hashes": {
                item.artifact_id: item.object_hash
                for item in sources
                if item.artifact_id is not None
            },
        }
    )
    return receipt


@pytest.mark.parametrize(
    "text",
    (
        "推荐现在可以买的股票。",
        "我有10万，今天怎么买？",
        "选5只股票。",
        "中国平安现在能买吗？",
        "给我直接说股票代码和买入价。",
        "不要分析，直接告诉我买什么。",
    ),
)
def test_investment_decision_language_always_routes_to_full_research(text: str) -> None:
    request = _request(text, request_id=f"route:{content_hash(text)[:8]}")
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION


@pytest.mark.parametrize(
    "legacy_intent",
    ("BUY_DECISION", "HOLDING_DECISION", "PORTFOLIO_DECISION", "RECOMMENDATION"),
)
def test_old_decision_names_are_input_migration_only(legacy_intent: str) -> None:
    assert legacy_intent not in RequestIntent.__members__
    request = _request("legacy input", intent=legacy_intent, request_id=f"legacy:{legacy_intent}")
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    assert request.metadata["full_research_routed_from"] == legacy_intent


def test_semantic_holding_request_uses_full_research_holding_context() -> None:
    request = _request("我已经买了这只股票，持有500股，现在持仓怎么办？")
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    assert request.metadata["full_research_holding_context"] is True
    contract = FullResearchRecommendationService().request_contract(request)
    assert contract.decision_context == "EXISTING_HOLDING"


def test_existing_holding_receipt_requires_and_preserves_holding_action() -> None:
    service = FullResearchRecommendationService()
    request = _request("我持有这只股票500股，现在要不要减仓？", request_id="holding-receipt")
    with pytest.raises(ValidationError, match="existing-holding Full Research"):
        _build_receipt(service, request, all_ineligible=True)

    receipt = _build_receipt(
        service,
        request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    assert receipt.holding_reviews[0].recommended_action == "TRIM"
    projected = VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (receipt,)}))
    )
    assert "现有持仓处置" in projected["conclusion"]
    assert any("减仓" in item for item in projected["actions"])
    assert any("下一份正式财报" in item for item in projected["change_conditions"])


def test_holding_add_cannot_bypass_candidate_admission() -> None:
    service = FullResearchRecommendationService()
    request = _request("我持有这只股票500股，现在要不要加仓？", request_id="holding-add-veto")
    with pytest.raises(ValidationError, match="holding ADD cannot bypass"):
        _build_receipt(service, request, all_ineligible=True, holding_action="ADD")


def test_explicit_buy_share_quantity_still_routes_to_full_research() -> None:
    request = _request("我有10万元，这只股票应该买多少股？")
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION


def test_shareholder_ownership_fact_does_not_route_to_full_research() -> None:
    request = _request("某股东目前持有多少股份？", intent=RequestIntent.RESEARCH)
    assert request.normalized_intent is RequestIntent.RESEARCH
    assert "full_research_holding_context" not in request.metadata


def test_non_decision_research_does_not_force_full_research() -> None:
    request = _request("解释一下市盈率是什么意思")
    assert request.normalized_intent is RequestIntent.RESEARCH


def test_default_model_portfolio_is_explicit_and_does_not_ask_for_missing_constraints() -> None:
    service = FullResearchRecommendationService()
    contract = service.request_contract(_request("推荐现在可以买的股票"))
    assert contract.portfolio_assumptions.source.value == "MODEL_PORTFOLIO"
    assert contract.portfolio_assumptions.capital_rmb == Decimal("100000")
    assert contract.portfolio_assumptions.risk_profile == "MEDIUM"
    assert (
        contract.portfolio_assumptions.horizon_min_months,
        contract.portfolio_assumptions.horizon_max_months,
    ) == (3, 12)
    assert contract.portfolio_assumptions.cash_allowed


def test_mandatory_dag_rejects_silent_skip_and_fake_pass() -> None:
    service = FullResearchRecommendationService()
    request = _request("推荐现在可以买的股票")
    with pytest.raises(ValueError, match="Mandatory Research DAG must be explicit"):
        service.dag_receipt(
            request, {FullResearchNode.REQUEST_CONTRACT: FullResearchNodeStatus.PASS}
        )
    with pytest.raises(ValidationError, match="auditable evidence"):
        service.dag_receipt(
            request,
            {node: FullResearchNodeStatus.PASS for node in FullResearchNode},
            at=NOW,
        )


def test_realtime_price_unavailable_allows_only_conditional_plan() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("推荐现在可以买的股票"),
        stale_quote=True,
    )
    assert receipt.publication.status == "CONDITIONAL_ONLY"
    assert receipt.publication.formal_recommendation_allowed
    assert not receipt.publication.instant_trade_parameters_allowed
    assert all(not item.instant_quantity_allowed for item in receipt.execution_plans)
    assert all(
        item.initial_shares is None and item.target_shares is None
        for item in receipt.execution_plans
    )


@pytest.mark.parametrize(
    "missing_family",
    (SourceFamily.FINANCIAL_REPORT, SourceFamily.OFFICIAL_FILING),
)
def test_missing_latest_financial_or_exchange_filing_blocks_publication(
    missing_family: SourceFamily,
) -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("推荐现在可以买的股票"),
        missing_family=missing_family,
    )
    assert receipt.publication.status == "BLOCKED"
    assert not receipt.publication.formal_recommendation_allowed
    assert receipt.pit_snapshot.status == FullResearchNodeStatus.BLOCKED


def test_conflicting_news_is_not_silently_selected() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("推荐现在可以买的股票"),
        open_conflict=True,
    )
    assert receipt.pit_snapshot.status == FullResearchNodeStatus.BLOCKED
    assert receipt.publication.status == "BLOCKED"


@pytest.mark.parametrize("financial_veto,governance_veto", ((True, False), (False, True)))
def test_critical_financial_or_governance_red_flag_cancels_buy_eligibility(
    financial_veto: bool,
    governance_veto: bool,
) -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("推荐现在可以买的股票"),
        financial_veto=financial_veto,
        governance_veto=governance_veto,
    )
    assert not receipt.candidate_rankings[0].eligible
    assert receipt.portfolio.positions == ()
    assert any("CRITICAL_VETO" in reason for reason in receipt.rejected_candidates["600001.XSHG"])


def test_all_candidates_overvalued_can_legitimately_produce_no_buy() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("选5只股票"),
        candidate_count=5,
        all_ineligible=True,
    )
    assert receipt.portfolio.positions == ()
    assert len(receipt.rejected_candidates) == 5
    assert receipt.publication.formal_recommendation_allowed
    assert not receipt.publication.instant_trade_parameters_allowed


def test_only_three_qualified_candidates_does_not_force_fill_to_five() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("选5只股票"),
        candidate_count=3,
    )
    assert len(receipt.portfolio.positions) == 3
    assert receipt.portfolio.cash > 0
    assert all(position.target_shares % 100 == 0 for position in receipt.portfolio.positions)


def test_same_factor_concentration_blocks_nominally_diversified_portfolio() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request("选5只股票"),
        candidate_count=5,
        dominant_exposure="HIGH_BETA",
    )
    assert any(
        reason.startswith("DOMINANT_FACTOR_CONCENTRATION:HIGH_BETA")
        for reason in receipt.portfolio.constraint_violations
    )
    assert receipt.risk_audit.status == FullResearchNodeStatus.FAIL
    assert receipt.publication.status == "BLOCKED"


def test_valuation_requires_multi_model_and_bear_base_bull_distribution() -> None:
    with pytest.raises(ValidationError, match="at least (?:2|two)"):
        RecommendationValuationSnapshot(
            **{
                **_valuation("600001.XSHG").model_dump(),
                "methods": ("PE",),
            }
        )


def test_company_history_and_news_window_contracts_are_hard_gates() -> None:
    with pytest.raises(ValidationError, match="required complete years"):
        CompanyFundamentalSnapshot(
            **{
                **_fundamental("600001.XSHG").model_dump(),
                "analyzed_complete_years": 4,
            }
        )
    with pytest.raises(ValidationError, match="7/30/90/180"):
        NewsEventCoverage(
            **{
                **_news()[1].model_dump(),
                "covered_windows_days": (7, 30, 90),
            }
        )


def test_missing_factor_dimension_cannot_be_zero_filled_or_remain_buy_eligible() -> None:
    service = FullResearchRecommendationService()
    incomplete = _factor("600001.XSHG").model_copy(update={"earnings_revision": None})
    assert incomplete.missing_dimensions() == ("earnings_revision",)
    assert not incomplete.recommendation_ready()
    with pytest.raises(ValidationError, match="ten-factor"):
        _candidate("600001.XSHG", incomplete)

    rejected = _candidate(
        "600001.XSHG",
        incomplete,
        eligible=False,
        reason="QUANT_FACTOR_INCOMPLETE:earnings_revision",
    )
    position = PortfolioPositionPlan(
        instrument_id="600001.XSHG",
        industry_id="IND-1",
        target_weight=Decimal("0.2"),
        target_amount=Decimal("20000"),
        target_shares=2000,
        reference_price=Decimal("10"),
        lot_size=100,
        estimated_cost=Decimal("24"),
        estimated_slippage=Decimal("16"),
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="lacks required factor exposure"):
        service._portfolio_factor_exposures((position,), (rejected,))


def test_formal_candidate_requires_real_history_metrics_growth_and_peer_coverage() -> None:
    fundamental = _fundamental("600001.XSHG")
    assert fundamental.recommendation_ready()
    assert not fundamental.model_copy(update={"available_complete_years": 4}).recommendation_ready()
    metrics = dict(fundamental.metrics)
    metrics["roic"] = None
    assert not fundamental.model_copy(update={"metrics": metrics}).recommendation_ready()
    growth = dict(fundamental.growth_decomposition)
    growth["volume"] = None
    assert not fundamental.model_copy(
        update={"growth_decomposition": growth}
    ).recommendation_ready()

    proper_industry = _industry()
    assert proper_industry.recommendation_peer_coverage_complete()
    data_gap = proper_industry.model_copy(
        update={
            "peer_set": (),
            "peer_set_exception_reason": "canonical profile lacks peer ids",
        }
    )
    assert not data_gap.recommendation_peer_coverage_complete()
    structural = proper_industry.model_copy(
        update={
            "peer_set": (),
            "peer_set_exception_reason": (
                "STRUCTURAL_NOT_APPLICABLE: no economically comparable listed peers"
            ),
        }
    )
    assert structural.recommendation_peer_coverage_complete()


def test_challenger_cannot_see_primary_label_and_material_conflict_must_reduce_confidence() -> None:
    with pytest.raises(ValidationError):
        ChallengerAssessment(
            **{
                **_challenger("600001.XSHG").model_dump(),
                "primary_final_label_visible": True,
            }
        )
    with pytest.raises(ValidationError, match="reduce recommendation confidence"):
        ChallengerAssessment(
            **{
                **_challenger("600001.XSHG").model_dump(),
                "material_conflict_with_primary": True,
                "confidence_adjustment": Decimal("0"),
            }
        )


def test_receipt_can_be_persisted_replayed_offline_and_auto_enrolled_for_tracking(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = FullResearchRecommendationService(store)
    receipt = _build_receipt(service, _request("推荐现在可以买的股票"))
    loaded, replay = service.load_and_replay(receipt.receipt_id)
    assert loaded.receipt_id == receipt.receipt_id
    assert loaded.receipt_hash == receipt.receipt_hash
    assert replay["status"] == "PASS"
    assert replay["receipt_hash"] == receipt.receipt_hash
    assert replay["position_count"] == 1
    watchlist = store.subject_events(event_types=("MONITOR_ENROLLED",))
    assert any(item.artifact_id == receipt.receipt_id for item in watchlist)


def test_receipt_tamper_is_detected() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(service, _request("推荐现在可以买的股票"))
    tampered = receipt.model_copy(update={"code_version": "tampered"})
    with pytest.raises(ValueError, match="semantic hash mismatch"):
        service.verify_receipt(tampered)


def test_regime_change_creates_new_revision_without_mutating_historical_receipt() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(service, _request("推荐现在可以买的股票"))
    original_hash = receipt.receipt_hash
    revision = service.freeze_reevaluation(
        original=receipt,
        instrument_id="600001.XSHG",
        trigger="MACRO_REGIME_CHANGE",
        previous_view="BUY_CANDIDATE",
        new_view="REVIEW",
        change_reason="市场状态切换",
        recommendation_change="降低风险预算并重新研究",
        observed_at=NOW + timedelta(days=1),
    )
    assert revision.original_receipt_hash == original_hash
    assert receipt.receipt_hash == original_hash
    assert revision.revision_hash != original_hash
