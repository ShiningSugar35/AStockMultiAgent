from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from astock.research import (
    ResearchDiagnosticsService,
    ResearchSkillService,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.schemas import (
    AdjustmentDirection,
    DailyTrendDiagnosticRequest,
    DiagnosticDirection,
    DiagnosticStatus,
    EventToAlphaDiagnosticRequest,
    GrowthProbabilityDiagnosticRequest,
    GrowthScenario,
    GrowthValuationDiagnosticRequest,
    HourlySwingDiagnosticRequest,
    IndustryBottleneckDiagnosticRequest,
    QualityStatus,
    ResearchMemoComposeRequest,
    SpecialistCoverageStatus,
    SpecialistRouteRequest,
)
from tests.integration.test_research_core import _specialist_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _diagnostics(state, skills: ResearchSkillService) -> ResearchDiagnosticsService:
    return ResearchDiagnosticsService(
        state,
        skills.object_store,
        load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml"),
        load_research_diagnostic_config(
            PROJECT_ROOT / "configs" / "research_diagnostics.yaml"
        ),
    )


def _route(
    skills: ResearchSkillService,
    base_case_id: str,
    *,
    skill_ids: list[str],
    inputs: list[str],
    frequencies: list[str] | None = None,
    horizon: str = "medium",
):
    return skills.route(
        SpecialistRouteRequest(
            base_case_id=base_case_id,
            thesis_tags=[],
            industry_tags=[],
            event_tags=[],
            horizon=horizon,
            available_inputs=inputs,
            available_frequencies=frequencies or [],
            explicit_skill_ids=skill_ids,
        )
    ).plan


def test_six_diagnostics_apply_distinct_rules_and_explicit_degradation(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="six-diagnostics",
    )
    service = _diagnostics(state, skills)
    evidence_ids = [evidence.evidence_id]

    industry_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["IndustryBottleneckSkill"],
        inputs=["industry_evidence"],
        horizon="long",
    )
    industry_request = IndustryBottleneckDiagnosticRequest(
        base_case_id=base_case.base_case_id,
        route_plan_id=industry_route.route_plan_id,
        system_change_verified=True,
        system_change_evidence_ids=evidence_ids,
        necessary_link_verified=True,
        necessary_link_evidence_ids=evidence_ids,
        scarcity_verified=True,
        scarcity_evidence_ids=evidence_ids,
        substitutability_ratio=Decimal("0.20"),
        substitutability_evidence_ids=evidence_ids,
        value_capture_verified=True,
        value_capture_evidence_ids=evidence_ids,
    )
    industry = service.diagnose(industry_request)
    assert industry.report.status is DiagnosticStatus.PASS
    assert industry.report.signal_codes == ["BOTTLENECK_CHAIN_VERIFIED"]
    assert service.diagnose(industry_request) == industry
    modified_config = service.config.model_copy(
        update={
            "industry": service.config.industry.model_copy(
                update={"max_substitutability_ratio": Decimal("0.40")}
            )
        }
    )
    with pytest.raises(ValueError, match="without a version bump"):
        ResearchDiagnosticsService(
            state,
            skills.object_store,
            service.registry,
            modified_config,
        ).diagnose(industry_request)

    broken_industry = service.diagnose(
        industry_request.model_copy(update={"value_capture_verified": False})
    )
    assert broken_industry.report.status is DiagnosticStatus.INSUFFICIENT
    assert "INSUFFICIENT_EVIDENCE" in broken_industry.report.degradation_codes
    assert broken_industry.delta.incremental_findings == []

    event_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["EventToAlphaSkill"],
        inputs=["event_evidence"],
    )
    headline = service.diagnose(
        EventToAlphaDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=event_route.route_plan_id,
            event_verified=True,
            headline_only=True,
            event_evidence_ids=evidence_ids,
            transmission_evidence_ids=[],
            falsifier_evidence_ids=[],
        )
    )
    assert headline.report.status is DiagnosticStatus.INSUFFICIENT
    assert "HEADLINE_ONLY" in headline.report.degradation_codes
    assert headline.delta.incremental_findings == []

    growth_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthProbabilitySkill"],
        inputs=["financial_evidence"],
        horizon="long",
    )
    growth = service.diagnose(
        GrowthProbabilityDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=growth_route.route_plan_id,
            scenarios=[
                GrowthScenario(
                    scenario_id="base",
                    probability=Decimal("0.7"),
                    annual_growth_rate=Decimal("0.10"),
                    duration_years=3,
                    driver="synthetic base driver",
                    failure_condition="synthetic base failure",
                    evidence_ids=evidence_ids,
                ),
                GrowthScenario(
                    scenario_id="upside",
                    probability=Decimal("0.3"),
                    annual_growth_rate=Decimal("0.30"),
                    duration_years=5,
                    driver="synthetic upside driver",
                    failure_condition="synthetic upside failure",
                    evidence_ids=evidence_ids,
                ),
            ],
            consensus_available=False,
            consensus_evidence_ids=[],
        )
    )
    assert growth.report.status is DiagnosticStatus.PARTIAL
    assert "CONSENSUS_UNAVAILABLE" in growth.report.degradation_codes
    weighted = next(
        item
        for item in growth.delta.industry_specific_metrics
        if item.metric_name == "probability_weighted_annual_growth"
    )
    assert weighted.value == "0.160"

    valuation_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthValuationLens"],
        inputs=["valuation_inputs"],
        horizon="long",
    )
    valuation = service.diagnose(
        GrowthValuationDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            market_implied_growth_rate=Decimal("0.08"),
            research_growth_rate=Decimal("0.15"),
            dilution_rate=Decimal("0.01"),
            reinvestment_rate=Decimal("0.40"),
            valuation_evidence_ids=evidence_ids,
            consensus_available=False,
            consensus_evidence_ids=[],
        )
    )
    assert valuation.report.status is DiagnosticStatus.PARTIAL
    assert valuation.delta.valuation_adjustments[0].direction is AdjustmentDirection.INCREASE
    assert valuation.delta.confidence_delta <= 0
    neutral = service.diagnose(
        GrowthValuationDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            market_implied_growth_rate=Decimal("0.08"),
            research_growth_rate=Decimal("0.08"),
            dilution_rate=Decimal("0"),
            reinvestment_rate=Decimal("0.40"),
            valuation_evidence_ids=evidence_ids,
            consensus_available=False,
            consensus_evidence_ids=[],
        )
    )
    assert neutral.delta.valuation_adjustments[0].direction is AdjustmentDirection.NEUTRAL
    decrease = service.diagnose(
        GrowthValuationDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            market_implied_growth_rate=Decimal("0.08"),
            research_growth_rate=Decimal("0.03"),
            dilution_rate=Decimal("0"),
            reinvestment_rate=Decimal("0.40"),
            valuation_evidence_ids=evidence_ids,
            consensus_available=False,
            consensus_evidence_ids=[],
        )
    )
    assert decrease.delta.valuation_adjustments[0].direction is AdjustmentDirection.DECREASE

    daily_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["DailyTrendHealthSkill"],
        inputs=["daily_market_quality"],
        frequencies=["1d"],
        horizon="short",
    )
    daily = service.diagnose(
        DailyTrendDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            quality_report_id="quality:daily:synthetic",
            quality_status=QualityStatus.PASS,
            bar_count=80,
            close_vs_ma20=Decimal("0.02"),
            ma20_slope=Decimal("0.01"),
            ma60_slope=Decimal("0.005"),
            drawdown_from_60d_high=Decimal("-0.03"),
            volume_ratio_20d=Decimal("1.10"),
            evidence_ids=evidence_ids,
        )
    )
    assert daily.report.signal_codes == ["DAILY_TREND_HEALTHY"]
    assert "not a buy signal" in daily.delta.incremental_findings[0].statement

    hourly_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["HourlySwingSkill"],
        inputs=["hourly_market_quality"],
        frequencies=["60m"],
        horizon="short",
    )
    hourly = service.diagnose(
        HourlySwingDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=hourly_route.route_plan_id,
            quality_report_id="quality:hourly:synthetic",
            quality_status=QualityStatus.PASS,
            bar_count=50,
            close_vs_vwap_20h=Decimal("0.01"),
            ema12_slope=Decimal("0.01"),
            realized_volatility_20h=Decimal("0.02"),
            drawdown_10h=Decimal("-0.02"),
            volume_ratio_20h=Decimal("1.2"),
            evidence_ids=evidence_ids,
        )
    )
    assert hourly.report.signal_codes == ["HOURLY_SWING_POSITIVE"]
    assert "not an order" in hourly.delta.incremental_findings[0].statement
    hourly_gate_failure = service.diagnose(
        HourlySwingDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=hourly_route.route_plan_id,
            quality_report_id="quality:hourly:failed",
            quality_status=QualityStatus.FAIL,
            bar_count=5,
            close_vs_vwap_20h=Decimal("0"),
            ema12_slope=Decimal("0"),
            realized_volatility_20h=Decimal("0"),
            drawdown_10h=Decimal("0"),
            volume_ratio_20h=Decimal("1"),
            evidence_ids=evidence_ids,
        )
    )
    assert hourly_gate_failure.report.status is DiagnosticStatus.INSUFFICIENT
    assert "INSUFFICIENT_HOURLY_BARS" in hourly_gate_failure.report.degradation_codes

    daily_gate_failure = service.diagnose(
        DailyTrendDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            quality_report_id="quality:daily:failed",
            quality_status=QualityStatus.FAIL,
            bar_count=10,
            close_vs_ma20=Decimal("0"),
            ma20_slope=Decimal("0"),
            ma60_slope=Decimal("0"),
            drawdown_from_60d_high=Decimal("0"),
            volume_ratio_20d=Decimal("1"),
            evidence_ids=evidence_ids,
        )
    )
    assert daily_gate_failure.report.status is DiagnosticStatus.INSUFFICIENT
    assert set(daily_gate_failure.report.degradation_codes) == {
        "QUALITY_GATE_FAILED",
        "INSUFFICIENT_DAILY_BARS",
    }
    assert service.audit(base_case.base_case_id)["status"] == "PASS"


def test_research_memo_preserves_reference_union_and_reports_missing_specialist(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="memo",
    )
    service = _diagnostics(state, skills)
    evidence_ids = [evidence.evidence_id]
    route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["IndustryBottleneckSkill", "EventToAlphaSkill"],
        inputs=["industry_evidence", "event_evidence"],
    )
    industry = service.diagnose(
        IndustryBottleneckDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            system_change_verified=True,
            system_change_evidence_ids=evidence_ids,
            necessary_link_verified=True,
            necessary_link_evidence_ids=evidence_ids,
            scarcity_verified=True,
            scarcity_evidence_ids=evidence_ids,
            substitutability_ratio=Decimal("0.1"),
            substitutability_evidence_ids=evidence_ids,
            value_capture_verified=True,
            value_capture_evidence_ids=evidence_ids,
        )
    )
    partial = service.compose_memo(
        ResearchMemoComposeRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            delta_ids=[industry.delta.delta_id],
        )
    )
    assert partial.memo.coverage_status is SpecialistCoverageStatus.PARTIAL
    assert partial.memo.missing_selected_skill_ids == ["EventToAlphaSkill"]
    assert partial.memo.evidence_ids == evidence_ids

    event = service.diagnose(
        EventToAlphaDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            event_verified=True,
            event_evidence_ids=evidence_ids,
            operating_metric="synthetic units",
            operating_direction=DiagnosticDirection.INCREASE,
            financial_metric="synthetic revenue",
            financial_direction=DiagnosticDirection.INCREASE,
            window_start=base_case.as_of,
            window_end=base_case.as_of + timedelta(days=30),
            transmission_evidence_ids=evidence_ids,
            falsifier="synthetic units do not increase",
            falsifier_evidence_ids=evidence_ids,
        )
    )
    complete_request = ResearchMemoComposeRequest(
        base_case_id=base_case.base_case_id,
        route_plan_id=route.route_plan_id,
        delta_ids=[industry.delta.delta_id, event.delta.delta_id],
    )
    complete = service.compose_memo(complete_request)
    assert service.compose_memo(complete_request) == complete
    assert complete.memo.coverage_status is SpecialistCoverageStatus.SUFFICIENT
    assert complete.memo.missing_selected_skill_ids == []
    assert complete.memo.evidence_ids == evidence_ids
    assert len(complete.memo.base_sections) == 12
    assert service.audit(base_case.base_case_id)["status"] == "PASS"

    other_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthValuationLens"],
        inputs=["valuation_inputs"],
    )
    other_delta = service.diagnose(
        GrowthValuationDiagnosticRequest(
            base_case_id=base_case.base_case_id,
            route_plan_id=other_route.route_plan_id,
            market_implied_growth_rate=Decimal("0.1"),
            research_growth_rate=Decimal("0.1"),
            dilution_rate=Decimal("0"),
            reinvestment_rate=Decimal("0.2"),
            valuation_evidence_ids=evidence_ids,
            consensus_available=False,
            consensus_evidence_ids=[],
        )
    )
    with pytest.raises(ValueError, match="another frozen scope"):
        service.compose_memo(
            complete_request.model_copy(update={"delta_ids": [other_delta.delta.delta_id]})
        )

    with state.connect() as connection:
        safe_metadata = "\n".join(
            str(value)
            for table in ("specialist_diagnostic_index", "research_memo_index")
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert "synthetic units" not in safe_metadata
    assert "falsifier" not in safe_metadata
