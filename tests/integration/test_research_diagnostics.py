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
    AdjustmentMode,
    DailyTrendDiagnosticRequestV2,
    DataQualityReport,
    DiagnosticStatus,
    EventToAlphaDiagnosticRequestV2,
    EvidenceGrade,
    Frequency,
    GrowthHypothesisId,
    GrowthProbabilityDiagnosticRequestV2,
    GrowthValuationDiagnosticRequestV2,
    HourlySwingDiagnosticRequest,
    IndustryBottleneckDiagnosticRequestV2,
    PointInTimeStatus,
    ProviderStatus,
    QualityStatus,
    ReplayQuality,
    ResearchMemoComposeRequestV2,
    SpecialistCoverageStatus,
    SpecialistRouteRequest,
    TimestampSemantics,
    VolumeUnit,
)
from astock.schemas.serenity_v2 import (
    BusinessPurityV2,
    CandidateUniverseV2,
    Comparator,
    CurrencyScale,
    DailySeriesV2,
    DailyTrendHealthContractV2,
    EstimateRevisionV2,
    EventFactV2,
    EventToAlphaContractV2,
    FalsifierV2,
    FinancialMetricV2,
    FundamentalGrowthV2,
    GrowthConsensusV2,
    GrowthHypothesisV2,
    GrowthLikelihoodUpdateV2,
    GrowthPosteriorStepV2,
    GrowthPriorBasisV2,
    GrowthProbabilityContractV2,
    GrowthProbabilityInputV2,
    GrowthValuationContractV2,
    IndustryBottleneckContractV2,
    IndustryChainNodeV2,
    MarketMisclassificationV2,
    MemoCatalystV2,
    MemoControversyV2,
    MemoInvalidationV2,
    MemoMonitoringItemV2,
    MemoScenarioCase,
    MemoScenarioV2,
    MetricDirection,
    MovingAverageV2,
    NecessaryLinkV2,
    ObservableInvalidationV2,
    PegInputV2,
    QualityCalibrationStatus,
    QualityFactorV2,
    ScaleElasticityV2,
    ScarcityMetricV2,
    StructuredResearchMemoV2,
    SubstitutionAlternativeV2,
    SystemChangeV2,
    TamRunwayV2,
    TransmissionStepV2,
    ValidationCheckpointV2,
    ValuationApplicability,
    ValueCaptureV2,
)
from tests.integration.test_research_core import _specialist_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _industry_contract(base_case, evidence_ids: list[str], ratio: str = "0.2"):
    return IndustryBottleneckContractV2(
        target_company_id=base_case.company_id,
        as_of=base_case.as_of,
        system_change=SystemChangeV2(
            node_id="change:1",
            statement="Synthetic change",
            effective_at=base_case.as_of,
            evidence_ids=evidence_ids,
        ),
        chain_nodes=[
            IndustryChainNodeV2(
                node_id="layer:1",
                level=0,
                name="Synthetic root",
                evidence_ids=evidence_ids,
            )
        ],
        candidate_universe=CandidateUniverseV2(
            universe_id="universe:1",
            inclusion_rule="Synthetic frozen rule",
            member_company_ids=[base_case.company_id],
            evidence_ids=evidence_ids,
        ),
        necessary_link=NecessaryLinkV2(
            node_id="layer:1",
            rationale="Synthetic necessary link",
            evidence_ids=evidence_ids,
        ),
        scarcity=[
            ScarcityMetricV2(
                metric="synthetic scarcity",
                value=Decimal("1"),
                unit="ratio",
                measurement_at=base_case.as_of,
                evidence_ids=evidence_ids,
            )
        ],
        substitutions=[
            SubstitutionAlternativeV2(
                alternative_id="alternative:1",
                feasibility=False,
                evidence_ids=evidence_ids,
            )
        ],
        aggregate_substitutability_ratio=Decimal(ratio),
        value_capture=[
            ValueCaptureV2(
                company_id=base_case.company_id,
                mechanism="Synthetic capture",
                evidence_ids=evidence_ids,
            )
        ],
        invalidation_conditions=[
            ObservableInvalidationV2(
                invalidation_id="industry-invalidation:1",
                observable="substitutability_ratio",
                comparator=Comparator.GT,
                threshold=Decimal("0.5"),
                unit="ratio",
                deadline=base_case.as_of + timedelta(days=365),
                evidence_ids=evidence_ids,
            )
        ],
        evidence_ids=evidence_ids,
    )


def _event_contract(base_case, evidence_ids: list[str], *, complete: bool):
    return EventToAlphaContractV2(
        target_company_id=base_case.company_id,
        as_of=base_case.as_of,
        event=EventFactV2(
            event_id="event:1",
            event_type="synthetic",
            announced_at=base_case.as_of,
            demand_metric="demand",
            direction=MetricDirection.INCREASE,
            evidence_ids=evidence_ids,
        ),
        business_purity=BusinessPurityV2(
            metric="revenue_share",
            value=Decimal("0.8"),
            period="2026Q2",
            evidence_ids=evidence_ids,
        ),
        transmission_steps=[
            TransmissionStepV2(
                step_no=1,
                from_metric="demand",
                to_metric="revenue",
                direction=MetricDirection.INCREASE,
                lag_quarters=2,
                evidence_ids=evidence_ids,
            )
        ],
        financial_endpoint=FinancialMetricV2.REVENUE,
        scale_elasticity=ScaleElasticityV2(
            input_metric="demand",
            output_metric="revenue",
            value=Decimal("1.2") if complete else None,
            evidence_ids=evidence_ids,
        ),
        market_misclassification=(
            MarketMisclassificationV2(
                market_implied_metric="growth",
                market_implied_value=Decimal("0.1"),
                research_metric="growth",
                research_value=Decimal("0.2"),
                unit="ratio",
                observed_at=base_case.as_of,
                evidence_ids=evidence_ids,
            )
            if complete
            else None
        ),
        validation_checkpoints=[
            ValidationCheckpointV2(
                quarter_offset=2,
                observable="revenue",
                comparator=Comparator.GT,
                threshold=Decimal("0"),
                unit="ratio",
                evidence_ids=evidence_ids,
            )
        ],
        falsifier=FalsifierV2(
            observable="revenue",
            comparator=Comparator.LTE,
            threshold=Decimal("0"),
            unit="ratio",
            deadline=base_case.as_of + timedelta(days=180),
            evidence_ids=evidence_ids,
        ),
        evidence_ids=evidence_ids,
    )


def _growth_input(base_case, evidence_ids: list[str]):
    bounds = [(-1, 0), (0, 5), (5, 10), (10, 20), (20, 30), (30, 100)]
    hypotheses = [
        GrowthHypothesisV2(
            hypothesis_id=hypothesis_id,
            definition=f"Synthetic {hypothesis_id.value}",
            growth_lower=Decimal(str(lower)) / 100,
            growth_upper=Decimal(str(upper)) / 100,
            duration_years=3,
            drivers=["driver"],
            failure_conditions=["failure"],
            evidence_ids=evidence_ids,
        )
        for hypothesis_id, (lower, upper) in zip(GrowthHypothesisId, bounds, strict=True)
    ]
    prior_values = [
        Decimal("0.1"),
        Decimal("0.1"),
        Decimal("0.2"),
        Decimal("0.3"),
        Decimal("0.2"),
        Decimal("0.1"),
    ]
    prior = dict(zip(GrowthHypothesisId, prior_values, strict=True))
    return GrowthProbabilityInputV2(
        target_company_id=base_case.company_id,
        as_of=base_case.as_of,
        hypotheses=hypotheses,
        prior_by_hypothesis=prior,
        prior_basis=GrowthPriorBasisV2(
            population="Synthetic A-share cohort",
            window="2016-2025",
            evidence_ids=evidence_ids,
        ),
        likelihood_updates=[
            GrowthLikelihoodUpdateV2(
                update_id="update:1",
                sequence=1,
                correlation_group="group:1",
                likelihood_by_hypothesis={item: Decimal("1") for item in GrowthHypothesisId},
                evidence_ids=evidence_ids,
            )
        ],
        consensus=None,
        evidence_ids=evidence_ids,
    )


def _valuation_contract(base_case, evidence_ids: list[str]):
    return GrowthValuationContractV2(
        target_company_id=base_case.company_id,
        as_of=base_case.as_of,
        market_implied_growth_rate=Decimal("0.08"),
        research_growth_rate=Decimal("0.15"),
        dilution_rate=Decimal("0.01"),
        reinvestment_rate=Decimal("0.4"),
        tam_runway=TamRunwayV2(
            tam_value=Decimal("1000"),
            current_revenue=Decimal("100"),
            addressable_share=Decimal("0.5"),
            currency="CNY",
            scale=CurrencyScale.MILLIONS,
            measurement_at=base_case.as_of,
            target_year=2030,
            evidence_ids=evidence_ids,
        ),
        quality_factors=[
            QualityFactorV2(
                factor_id=factor_id,
                raw_value=Decimal("1"),
                unit="ratio",
                direction=MetricDirection.INCREASE,
                calibration_status=QualityCalibrationStatus.REPORT_ONLY_UNCALIBRATED,
                evidence_ids=evidence_ids,
            )
            for factor_id in (
                "durability",
                "cash_conversion",
                "concentration",
                "capital_intensity",
                "dilution",
            )
        ],
        peg=PegInputV2(
            pe_multiple=Decimal("30"),
            earnings_basis="forward",
            earnings_period="2027",
            growth_value=Decimal("15"),
            growth_period="2027",
            evidence_ids=evidence_ids,
        ),
        applicability=ValuationApplicability.REPORT_ONLY,
        applicability_reasons=["Uncalibrated A-share method"],
        consensus=GrowthConsensusV2(
            growth_rate=Decimal("0.15"),
            duration_years=3,
            available_at=base_case.as_of,
            evidence_ids=evidence_ids,
        ),
        evidence_ids=evidence_ids,
    )


def _daily_contract(
    base_case,
    evidence_ids: list[str],
    report_hash: str,
    *,
    full: bool,
    report_id: str = "quality:daily:v2",
    bar_count: int = 220,
):
    windows = (20, 50, 100, 200) if full else (20,)
    return DailyTrendHealthContractV2(
        target_company_id=base_case.company_id,
        as_of=base_case.as_of,
        daily_series=DailySeriesV2(
            symbol="000001",
            as_of=base_case.as_of,
            quality_report_id=report_id,
            bar_count=bar_count,
            adjustment_mode=AdjustmentMode.NONE,
            dataset_version=report_hash,
            evidence_ids=evidence_ids,
        ),
        moving_averages=[
            MovingAverageV2(
                window=window,
                value=Decimal("9"),
                close=Decimal("10"),
                currency="CNY",
                calculated_at=base_case.as_of,
                bars_used=bar_count,
                dataset_version=report_hash,
                evidence_ids=evidence_ids,
            )
            for window in windows
        ],
        fundamental_growth=(
            [
                FundamentalGrowthV2(
                    metric=metric,
                    current=Decimal("110"),
                    prior=Decimal("100"),
                    unit="CNY_million",
                    current_period="2026Q2",
                    prior_period="2025Q2",
                    evidence_ids=evidence_ids,
                )
                for metric in ("REVENUE", "EARNINGS")
            ]
            if full
            else []
        ),
        estimate_revisions=(
            [
                EstimateRevisionV2(
                    metric="earnings",
                    forecast_period="2027",
                    prior_estimate=Decimal("1"),
                    current_estimate=Decimal("1.1"),
                    unit="CNY_per_share",
                    prior_available_at=base_case.as_of - timedelta(days=30),
                    current_available_at=base_case.as_of,
                    evidence_ids=evidence_ids,
                )
            ]
            if full
            else []
        ),
        evidence_ids=evidence_ids,
    )


def _register_daily_quality(
    state,
    objects,
    base_case,
    *,
    report_id: str = "quality:daily:v2",
    bar_count: int = 220,
    actual_end=None,
) -> str:
    frozen_end = actual_end or base_case.as_of
    report = DataQualityReport(
        report_id=report_id,
        batch_ids=[f"batch:{report_id}"],
        symbol="000001",
        frequency=Frequency.D1,
        requested_start=base_case.as_of - timedelta(days=300),
        requested_end=frozen_end,
        actual_start=base_case.as_of - timedelta(days=300),
        actual_end=frozen_end,
        bar_count=bar_count,
        volume_unit=VolumeUnit.SHARE,
        adjustment_mode=AdjustmentMode.NONE,
        timestamp_semantics=TimestampSemantics.BAR_END,
        provider_status=ProviderStatus.AVAILABLE,
        quality_status=QualityStatus.PASS,
        replay_quality=ReplayQuality.DAILY_CONSERVATIVE,
    )
    object_ref = objects.put_json(report.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"DataQualityReport:{report.report_id}",
        artifact_type="DataQualityReport",
        schema_version=report.schema_version,
        object_hash=object_ref.sha256,
        input_hashes=report.batch_ids,
    )
    return object_ref.sha256


def _structured_memo(delta, base_case) -> StructuredResearchMemoV2:
    finding_id = delta.incremental_findings[0].finding_id
    metric_id = delta.industry_specific_metrics[0].metric_id
    return StructuredResearchMemoV2(
        controversies=[
            MemoControversyV2(
                controversy_id="controversy:1",
                question="Synthetic controversy?",
                supporting_source_refs=[finding_id],
                opposing_source_refs=[finding_id],
                open_gap_codes=[],
            )
        ],
        scenarios=[
            MemoScenarioV2(
                case=case,
                thesis=f"Synthetic {case.value}",
                assumption_source_refs=[finding_id],
                growth_hypothesis_refs=[],
            )
            for case in MemoScenarioCase
        ],
        catalysts=[
            MemoCatalystV2(
                catalyst_id="catalyst:1",
                statement="Synthetic catalyst",
                expected_start=base_case.as_of,
                expected_end=base_case.as_of + timedelta(days=30),
                observable="metric",
                source_refs=[finding_id],
            )
        ],
        invalidations=[
            MemoInvalidationV2(
                invalidation_id="invalidation:1",
                statement="Synthetic invalidation",
                observable="metric",
                comparator=Comparator.LT,
                threshold=Decimal("0"),
                unit="ratio",
                deadline=base_case.as_of + timedelta(days=90),
                source_refs=[finding_id],
            )
        ],
        monitoring_items=[
            MemoMonitoringItemV2(
                item_id="monitor:1",
                metric="Synthetic metric",
                source_ref=metric_id,
                cadence="quarterly",
                next_review_at=base_case.as_of + timedelta(days=90),
            )
        ],
    )


def _diagnostics(state, skills: ResearchSkillService) -> ResearchDiagnosticsService:
    return ResearchDiagnosticsService(
        state,
        skills.object_store,
        load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml"),
        load_research_diagnostic_config(PROJECT_ROOT / "configs" / "research_diagnostics.yaml"),
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


def test_serenity_v2_contracts_reject_scope_pit_and_false_precision(
    tmp_path: Path,
    state,
) -> None:
    _, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="serenity-v2-negative",
    )
    evidence_ids = [evidence.evidence_id]

    industry_payload = _industry_contract(base_case, evidence_ids).model_dump(mode="python")
    industry_payload["candidate_universe"]["member_company_ids"] = ["company:other"]
    with pytest.raises(ValueError, match="target company"):
        IndustryBottleneckContractV2.model_validate(industry_payload)

    event_payload = _event_contract(base_case, evidence_ids, complete=True).model_dump(
        mode="python"
    )
    event_payload["event"]["announced_at"] = base_case.as_of + timedelta(seconds=1)
    with pytest.raises(ValueError, match="future"):
        EventToAlphaContractV2.model_validate(event_payload)

    growth = _growth_input(base_case, evidence_ids)
    overlapping = growth.model_dump(mode="python")
    overlapping["hypotheses"][1]["growth_lower"] = Decimal("-0.005")
    with pytest.raises(ValueError, match="continuous and non-overlapping"):
        GrowthProbabilityInputV2.model_validate(overlapping)

    correlated = growth.model_dump(mode="python")
    repeated_update = dict(correlated["likelihood_updates"][0])
    repeated_update.update(
        {"update_id": "update:2", "sequence": 2, "correlation_group": "group:2"}
    )
    correlated["likelihood_updates"].append(repeated_update)
    with pytest.raises(ValueError, match="evidence sets must be unique"):
        GrowthProbabilityInputV2.model_validate(correlated)

    update = growth.likelihood_updates[0]
    forged_step = GrowthPosteriorStepV2(
        sequence=update.sequence,
        update_id=update.update_id,
        prior=growth.prior_by_hypothesis,
        likelihood=update.likelihood_by_hypothesis,
        posterior=dict(
            zip(
                GrowthHypothesisId,
                map(Decimal, ("0.2", "0.2", "0.2", "0.2", "0.1", "0.1")),
                strict=True,
            )
        ),
    )
    with pytest.raises(ValueError, match="normalized prior times likelihood"):
        GrowthProbabilityContractV2(
            input=growth,
            update_trajectory=[forged_step],
            final_posterior=forged_step.posterior,
            evidence_ids=growth.evidence_ids,
        )

    valuation_payload = _valuation_contract(base_case, evidence_ids).model_dump(mode="python")
    valuation_payload["target_price"] = Decimal("100")
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        GrowthValuationContractV2.model_validate(valuation_payload)
    valuation_payload.pop("target_price")
    valuation_payload["peg"]["growth_period"] = "2028"
    with pytest.raises(ValueError, match="periods must match"):
        GrowthValuationContractV2.model_validate(valuation_payload)

    daily_payload = _daily_contract(base_case, evidence_ids, "0" * 64, full=True).model_dump(
        mode="python"
    )
    daily_payload["estimate_revisions"][0]["current_available_at"] = base_case.as_of + timedelta(
        seconds=1
    )
    with pytest.raises(ValueError, match="future"):
        DailyTrendHealthContractV2.model_validate(daily_payload)
    daily_payload = _daily_contract(
        base_case, evidence_ids, "0" * 64, full=True
    ).model_dump(mode="python")
    daily_payload["moving_averages"][0]["dataset_version"] = "1" * 64
    with pytest.raises(ValueError, match="dataset version"):
        DailyTrendHealthContractV2.model_validate(daily_payload)
    daily_payload["moving_averages"][0]["dataset_version"] = "0" * 64
    daily_payload["moving_averages"][0]["bars_used"] = 221
    with pytest.raises(ValueError, match="cannot exceed"):
        DailyTrendHealthContractV2.model_validate(daily_payload)


@pytest.mark.parametrize(
    ("suffix", "pit_status", "conflict", "evidence_grade", "error"),
    [
        (
            "serenity-v2-approximated",
            PointInTimeStatus.APPROXIMATED,
            False,
            EvidenceGrade.PRIMARY_OFFICIAL,
            "certified or reconstructed PIT",
        ),
        (
            "serenity-v2-open-conflict",
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            True,
            EvidenceGrade.PRIMARY_OFFICIAL,
            "open conflict",
        ),
        (
            "serenity-v2-weak-grade",
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            False,
            EvidenceGrade.SECONDARY,
            "requires PRIMARY_OFFICIAL",
        ),
    ],
)
def test_serenity_v2_rejects_node_evidence_that_fails_frozen_gates(
    tmp_path: Path,
    state,
    suffix: str,
    pit_status: PointInTimeStatus,
    conflict: bool,
    evidence_grade: EvidenceGrade,
    error: str,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix=suffix,
        pit_status=pit_status,
        conflict=conflict,
        evidence_grade=evidence_grade,
    )
    route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["IndustryBottleneckSkill"],
        inputs=["industry_evidence"],
        horizon="long",
    )
    with pytest.raises(ValueError, match=error):
        _diagnostics(state, skills).diagnose(
            IndustryBottleneckDiagnosticRequestV2(
                base_case_id=base_case.base_case_id,
                route_plan_id=route.route_plan_id,
                method_contract=_industry_contract(
                    base_case,
                    [evidence.evidence_id],
                ),
            )
        )


def test_serenity_v2_applies_method_node_grade_policy(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, official_evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="serenity-v2-role-grades",
        additional_evidence_grade=EvidenceGrade.SECONDARY,
    )
    evidence_pack = skills.repository.get_evidence_pack(base_case.evidence_pack_id)
    assert evidence_pack is not None
    official_ids = [official_evidence.evidence_id]
    secondary_ids = [
        evidence_id
        for evidence_id, grade in evidence_pack.evidence_grade_by_id.items()
        if grade is EvidenceGrade.SECONDARY
    ]
    assert len(secondary_ids) == 1
    evidence_union = sorted({*official_ids, *secondary_ids})
    service = _diagnostics(state, skills)

    event_payload = _event_contract(
        base_case,
        official_ids,
        complete=True,
    ).model_dump(mode="python")
    event_payload["market_misclassification"]["evidence_ids"] = secondary_ids
    event_payload["evidence_ids"] = evidence_union
    event_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["EventToAlphaSkill"],
        inputs=["event_evidence"],
    )
    event = service.diagnose(
        EventToAlphaDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=event_route.route_plan_id,
            method_contract=EventToAlphaContractV2.model_validate(event_payload),
        )
    )
    assert event.report.status is DiagnosticStatus.PASS

    growth_payload = _growth_input(base_case, official_ids).model_dump(mode="python")
    growth_payload["prior_basis"]["evidence_ids"] = secondary_ids
    growth_payload["consensus"] = GrowthConsensusV2(
        growth_rate=Decimal("0.15"),
        duration_years=3,
        available_at=base_case.as_of,
        evidence_ids=secondary_ids,
    ).model_dump(mode="python")
    growth_payload["evidence_ids"] = evidence_union
    growth_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthProbabilitySkill"],
        inputs=["financial_evidence", "consensus_estimates"],
        horizon="long",
    )
    growth = service.diagnose(
        GrowthProbabilityDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=growth_route.route_plan_id,
            method_input=GrowthProbabilityInputV2.model_validate(growth_payload),
        )
    )
    assert growth.report.status is DiagnosticStatus.PASS

    valuation_payload = _valuation_contract(base_case, official_ids).model_dump(
        mode="python"
    )
    valuation_payload["peg"]["evidence_ids"] = secondary_ids
    valuation_payload["consensus"]["evidence_ids"] = secondary_ids
    valuation_payload["evidence_ids"] = evidence_union
    valuation_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthValuationLens"],
        inputs=["valuation_inputs", "consensus_estimates"],
    )
    valuation = service.diagnose(
        GrowthValuationDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            method_contract=GrowthValuationContractV2.model_validate(valuation_payload),
        )
    )
    assert valuation.report.status is DiagnosticStatus.PASS

    daily_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["DailyTrendHealthSkill"],
        inputs=[
            "daily_market_quality",
            "fundamental_evidence",
            "estimate_revision_evidence",
        ],
        frequencies=[Frequency.D1],
    )
    report_hash = _register_daily_quality(state, skills.object_store, base_case)
    daily_payload = _daily_contract(
        base_case,
        secondary_ids,
        report_hash,
        full=True,
    ).model_dump(mode="python")
    for node in daily_payload["fundamental_growth"]:
        node["evidence_ids"] = official_ids
    daily_payload["evidence_ids"] = evidence_union
    daily_contract = DailyTrendHealthContractV2.model_validate(daily_payload)
    daily = service.diagnose(
        DailyTrendDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            method_contract=daily_contract,
        )
    )
    assert daily.report.status is DiagnosticStatus.PARTIAL
    assert "DMA_CALLER_SUPPLIED_UNVERIFIED" in daily.report.degradation_codes

    invalid_daily_payload = daily_contract.model_dump(mode="python")
    for node in invalid_daily_payload["fundamental_growth"]:
        node["evidence_ids"] = secondary_ids
    invalid_daily_payload["evidence_ids"] = secondary_ids
    with pytest.raises(ValueError, match="daily fundamental growth requires PRIMARY_OFFICIAL"):
        service.diagnose(
            DailyTrendDiagnosticRequestV2(
                base_case_id=base_case.base_case_id,
                route_plan_id=daily_route.route_plan_id,
                method_contract=DailyTrendHealthContractV2.model_validate(
                    invalid_daily_payload
                ),
            )
        )


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
    industry_request = IndustryBottleneckDiagnosticRequestV2(
        base_case_id=base_case.base_case_id,
        route_plan_id=industry_route.route_plan_id,
        method_contract=_industry_contract(base_case, evidence_ids),
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
        industry_request.model_copy(
            update={"method_contract": _industry_contract(base_case, evidence_ids, "0.9")}
        )
    )
    assert broken_industry.report.status is DiagnosticStatus.INSUFFICIENT
    assert "SUBSTITUTION_UNRESOLVED" in broken_industry.report.degradation_codes
    assert broken_industry.delta.incremental_findings == []

    event_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["EventToAlphaSkill"],
        inputs=["event_evidence"],
    )
    headline = service.diagnose(
        EventToAlphaDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=event_route.route_plan_id,
            headline_only=True,
            method_contract=_event_contract(base_case, evidence_ids, complete=False),
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
        GrowthProbabilityDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=growth_route.route_plan_id,
            method_input=_growth_input(base_case, evidence_ids),
        )
    )
    assert growth.report.status is DiagnosticStatus.PARTIAL
    assert "CONSENSUS_UNAVAILABLE" in growth.report.degradation_codes
    weighted = next(
        item
        for item in growth.delta.industry_specific_metrics
        if item.metric_name == "posterior_H3"
    )
    assert weighted.value == "0.300000000000"

    valuation_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthValuationLens"],
        inputs=["valuation_inputs"],
        horizon="long",
    )
    valuation = service.diagnose(
        GrowthValuationDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            method_contract=_valuation_contract(base_case, evidence_ids),
        )
    )
    assert valuation.report.status is DiagnosticStatus.PASS
    assert valuation.report.signal_codes == ["REPORT_ONLY_UNCALIBRATED"]
    assert valuation.delta.valuation_adjustments == []
    assert valuation.delta.confidence_delta == 0
    peg = next(
        item for item in valuation.delta.industry_specific_metrics if item.metric_name == "peg"
    )
    assert peg.value == "2"

    not_applicable_contract = _valuation_contract(base_case, evidence_ids).model_copy(
        update={
            "applicability": ValuationApplicability.NOT_APPLICABLE,
            "applicability_reasons": ["Method is not applicable to this business"],
        }
    )
    not_applicable = service.diagnose(
        GrowthValuationDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            method_contract=not_applicable_contract,
        )
    )
    assert not_applicable.report.status is DiagnosticStatus.PARTIAL
    assert "VALUATION_NOT_APPLICABLE" in not_applicable.report.degradation_codes
    assert not_applicable.delta.industry_specific_metrics == []

    valuation_template = _valuation_contract(base_case, evidence_ids)
    assert valuation_template.tam_runway is not None
    zero_revenue = service.diagnose(
        GrowthValuationDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=valuation_route.route_plan_id,
            method_contract=valuation_template.model_copy(
                update={
                    "tam_runway": valuation_template.tam_runway.model_copy(
                        update={"current_revenue": Decimal("0")}
                    )
                }
            ),
        )
    )
    assert "TAM_DENOMINATOR_ZERO" in zero_revenue.report.degradation_codes
    assert all(
        item.metric_name != "tam_revenue_multiple"
        for item in zero_revenue.delta.industry_specific_metrics
    )

    daily_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["DailyTrendHealthSkill"],
        inputs=["daily_market_quality"],
        frequencies=["1d"],
        horizon="short",
    )
    daily_report_hash = _register_daily_quality(state, skills.object_store, base_case)
    daily = service.diagnose(
        DailyTrendDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            method_contract=_daily_contract(base_case, evidence_ids, daily_report_hash, full=True),
        )
    )
    assert daily.report.status is DiagnosticStatus.PARTIAL
    assert "DMA_CALLER_SUPPLIED_UNVERIFIED" in daily.report.degradation_codes
    assert "GF_DMA_REPORT_ONLY_UNCALIBRATED" in daily.report.signal_codes
    assert "not a buy or sell signal" in daily.delta.incremental_findings[0].statement
    assert "not recomputed from bars" in daily.delta.incremental_findings[0].statement

    short_report_id = "quality:daily:short:v2"
    short_report_hash = _register_daily_quality(
        state,
        skills.object_store,
        base_case,
        report_id=short_report_id,
        bar_count=50,
    )
    short_daily = service.diagnose(
        DailyTrendDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            method_contract=_daily_contract(
                base_case,
                evidence_ids,
                short_report_hash,
                full=False,
                report_id=short_report_id,
                bar_count=50,
            ),
        )
    )
    assert short_daily.report.status is DiagnosticStatus.INSUFFICIENT
    assert "INSUFFICIENT_DAILY_BARS" in short_daily.report.degradation_codes

    future_report_id = "quality:daily:future:v2"
    future_report_hash = _register_daily_quality(
        state,
        skills.object_store,
        base_case,
        report_id=future_report_id,
        actual_end=base_case.as_of + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="end no later"):
        service.diagnose(
            DailyTrendDiagnosticRequestV2(
                base_case_id=base_case.base_case_id,
                route_plan_id=daily_route.route_plan_id,
                method_contract=_daily_contract(
                    base_case,
                    evidence_ids,
                    future_report_hash,
                    full=True,
                    report_id=future_report_id,
                ),
            )
        )
    with pytest.raises(ValueError, match="registered DataQualityReport"):
        service.diagnose(
            DailyTrendDiagnosticRequestV2(
                base_case_id=base_case.base_case_id,
                route_plan_id=daily_route.route_plan_id,
                method_contract=_daily_contract(
                    base_case,
                    evidence_ids,
                    "f" * 64,
                    full=True,
                ).model_copy(
                    update={
                        "daily_series": _daily_contract(
                            base_case,
                            evidence_ids,
                            "f" * 64,
                            full=True,
                        ).daily_series.model_copy(
                            update={"quality_report_id": "quality:missing:v2"}
                        )
                    }
                ),
            )
        )

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

    daily_degraded = service.diagnose(
        DailyTrendDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=daily_route.route_plan_id,
            method_contract=_daily_contract(base_case, evidence_ids, daily_report_hash, full=False),
        )
    )
    assert daily_degraded.report.status is DiagnosticStatus.PARTIAL
    assert set(daily_degraded.report.degradation_codes) == {
        "DMA_CALLER_SUPPLIED_UNVERIFIED",
        "DMA50_UNAVAILABLE",
        "DMA100_UNAVAILABLE",
        "DMA200_UNAVAILABLE",
        "FUNDAMENTALS_UNAVAILABLE",
        "REVISIONS_UNAVAILABLE",
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
        IndustryBottleneckDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            method_contract=_industry_contract(base_case, evidence_ids),
        )
    )
    partial = service.compose_memo(
        ResearchMemoComposeRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            delta_ids=[industry.delta.delta_id],
            structured_memo=_structured_memo(industry.delta, base_case),
        )
    )
    assert partial.memo.coverage_status is SpecialistCoverageStatus.PARTIAL
    assert partial.memo.missing_selected_skill_ids == ["EventToAlphaSkill"]
    assert partial.memo.evidence_ids == evidence_ids

    event = service.diagnose(
        EventToAlphaDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            method_contract=_event_contract(base_case, evidence_ids, complete=True),
        )
    )
    structured = _structured_memo(industry.delta, base_case)
    structured = structured.model_copy(
        update={
            "controversies": [
                structured.controversies[0].model_copy(
                    update={"supporting_source_refs": ["change:1"]}
                )
            ]
        }
    )
    complete_request = ResearchMemoComposeRequestV2(
        base_case_id=base_case.base_case_id,
        route_plan_id=route.route_plan_id,
        delta_ids=[industry.delta.delta_id, event.delta.delta_id],
        structured_memo=structured,
    )
    complete = service.compose_memo(complete_request)
    assert service.compose_memo(complete_request) == complete
    assert complete.memo.coverage_status is SpecialistCoverageStatus.SUFFICIENT
    assert complete.memo.missing_selected_skill_ids == []
    assert complete.memo.evidence_ids == evidence_ids
    assert len(complete.memo.base_sections) == 12
    assert service.audit(base_case.base_case_id)["status"] == "PASS"

    bad_structured = structured.model_copy(
        update={
            "controversies": [
                structured.controversies[0].model_copy(
                    update={"supporting_source_refs": ["method:foreign"]}
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="outside frozen inputs"):
        service.compose_memo(
            complete_request.model_copy(update={"structured_memo": bad_structured})
        )

    no_growth_hypothesis = structured.model_copy(
        update={
            "scenarios": [
                scenario.model_copy(
                    update={
                        "growth_hypothesis_refs": [
                            industry.delta.incremental_findings[0].finding_id
                        ]
                    }
                )
                for scenario in structured.scenarios
            ]
        }
    )
    with pytest.raises(ValueError, match="growth_hypothesis_refs require a hypothesis id"):
        service.compose_memo(
            complete_request.model_copy(update={"structured_memo": no_growth_hypothesis})
        )

    no_growth_probability = structured.model_copy(
        update={
            "scenarios": [
                scenario.model_copy(
                    update={"probability_ref": industry.delta.incremental_findings[0].finding_id}
                )
                for scenario in structured.scenarios
            ]
        }
    )
    with pytest.raises(ValueError, match="probability_ref requires a posterior id"):
        service.compose_memo(
            complete_request.model_copy(update={"structured_memo": no_growth_probability})
        )

    growth_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthProbabilitySkill"],
        inputs=["financial_evidence"],
        horizon="long",
    )
    growth = service.diagnose(
        GrowthProbabilityDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=growth_route.route_plan_id,
            method_input=_growth_input(base_case, evidence_ids),
        )
    )
    growth_memo = _structured_memo(growth.delta, base_case)
    growth_memo = growth_memo.model_copy(
        update={
            "scenarios": [
                scenario.model_copy(
                    update={
                        "growth_hypothesis_refs": ["H0"],
                        "probability_ref": "posterior:H3",
                    }
                )
                for scenario in growth_memo.scenarios
            ]
        }
    )
    growth_composed = service.compose_memo(
        ResearchMemoComposeRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=growth_route.route_plan_id,
            delta_ids=[growth.delta.delta_id],
            structured_memo=growth_memo,
        )
    )
    assert growth_composed.memo.coverage_status is SpecialistCoverageStatus.PARTIAL
    assert "CONSENSUS_UNAVAILABLE" in growth_composed.memo.degradation_codes

    wrong_growth_domain = growth_memo.model_copy(
        update={
            "scenarios": [
                scenario.model_copy(
                    update={
                        "growth_hypothesis_refs": ["posterior:H0"],
                        "probability_ref": "H0",
                    }
                )
                for scenario in growth_memo.scenarios
            ]
        }
    )
    with pytest.raises(ValueError, match="growth_hypothesis_refs require a hypothesis id"):
        service.compose_memo(
            ResearchMemoComposeRequestV2(
                base_case_id=base_case.base_case_id,
                route_plan_id=growth_route.route_plan_id,
                delta_ids=[growth.delta.delta_id],
                structured_memo=wrong_growth_domain,
            )
        )

    other_route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["GrowthValuationLens"],
        inputs=["valuation_inputs"],
    )
    other_delta = service.diagnose(
        GrowthValuationDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=other_route.route_plan_id,
            method_contract=_valuation_contract(base_case, evidence_ids),
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
