from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from astock.financial_integrity import FinancialIntegrityService
from astock.schemas import (
    FinancialAuditRequest,
    FinancialDerivationType,
    FinancialDurationSemantics,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialGapType,
    FinancialIndustryProfile,
    FinancialPeerCohort,
    FinancialPeerObservation,
    FinancialPeriodType,
    FinancialSeriesRequest,
    FinancialUnit,
    RunStatus,
)
from tests.helpers import FINANCIAL_GOLDEN_VALUES, make_financial_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(state, object_store) -> FinancialIntegrityService:
    return FinancialIntegrityService(
        state,
        object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    )


def _advanced_values(*, prior: bool) -> dict[FinancialFieldCode, Decimal]:
    values = dict(FINANCIAL_GOLDEN_VALUES)
    values.update(
        {
            FinancialFieldCode.TOTAL_ASSETS: Decimal("1000" if prior else "1200"),
            FinancialFieldCode.TOTAL_LIABILITIES: Decimal("600" if prior else "650"),
            FinancialFieldCode.TOTAL_EQUITY: Decimal("400" if prior else "550"),
            FinancialFieldCode.CASH_BEGINNING: Decimal("80" if prior else "180"),
            FinancialFieldCode.NET_CASH_OPERATING: Decimal("140" if prior else "220"),
            FinancialFieldCode.NET_CASH_INVESTING: Decimal("-50" if prior else "-80"),
            FinancialFieldCode.NET_CASH_FINANCING: Decimal("10" if prior else "30"),
            FinancialFieldCode.CASH_ENDING: Decimal("180" if prior else "350"),
            FinancialFieldCode.NET_PROFIT_INCOME: Decimal("80" if prior else "120"),
            FinancialFieldCode.NET_PROFIT_CASH_FLOW: Decimal("80" if prior else "120"),
            FinancialFieldCode.REVENUE: Decimal("900" if prior else "1100"),
            FinancialFieldCode.OPERATING_COST: Decimal("600" if prior else "680"),
            FinancialFieldCode.ACCOUNTS_RECEIVABLE: Decimal("100" if prior else "110"),
            FinancialFieldCode.CURRENT_ASSETS: Decimal("500" if prior else "620"),
            FinancialFieldCode.CURRENT_LIABILITIES: Decimal("300" if prior else "310"),
            FinancialFieldCode.RETAINED_EARNINGS: Decimal("150" if prior else "220"),
            FinancialFieldCode.EBIT: Decimal("120" if prior else "170"),
            FinancialFieldCode.PROPERTY_PLANT_EQUIPMENT: Decimal("300" if prior else "340"),
            FinancialFieldCode.DEPRECIATION_AMORTIZATION: Decimal("30" if prior else "35"),
            FinancialFieldCode.SELLING_GENERAL_ADMIN_EXPENSE: Decimal("90" if prior else "100"),
            FinancialFieldCode.LONG_TERM_DEBT: Decimal("200" if prior else "190"),
            FinancialFieldCode.MARKET_CAP: Decimal("800" if prior else "1200"),
            FinancialFieldCode.SHARES_OUTSTANDING: Decimal("1000"),
        }
    )
    return values


def _two_period_facts(state, object_store):
    prior = make_financial_facts(
        state,
        object_store,
        source_suffix="advanced-2024",
        period_end=date(2024, 12, 31),
        published_at=datetime(2025, 3, 20, tzinfo=UTC),
        values=_advanced_values(prior=True),
    )
    current = make_financial_facts(
        state,
        object_store,
        source_suffix="advanced-2025",
        period_end=date(2025, 12, 31),
        published_at=datetime(2026, 3, 20, tzinfo=UTC),
        values=_advanced_values(prior=False),
    )
    return [*prior, *current]


def test_all_m3_2_scores_run_with_components_and_no_fabricated_gap(
    state, object_store
) -> None:
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=_two_period_facts(state, object_store),
        requested_rule_ids=[
            "beneish_m_score",
            "altman_z_score",
            "piotroski_f_score",
            "sloan_accrual_ratio",
            "dupont_decomposition",
        ],
    )
    execution = _service(state, object_store).run(request)
    assert execution.pack.status is RunStatus.SUCCEEDED
    advanced = {
        metric.rule_id: metric
        for metric in execution.pack.recalculated_metrics
        if metric.rule_id in request.requested_rule_ids
    }
    assert set(advanced) == set(request.requested_rule_ids)
    assert len(advanced["beneish_m_score"].component_values) == 8
    assert len(advanced["piotroski_f_score"].component_values) == 9
    assert advanced["dupont_decomposition"].component_values["EQUITY_MULTIPLIER"] > 0
    assert execution.pack.capability_status["financial_scores"].startswith("AVAILABLE_M3_2")
    assert not execution.pack.evidence_gaps


def test_real_estate_advanced_score_is_calculation_only_not_generic_alarm(
    state, object_store
) -> None:
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.REAL_ESTATE,
        facts=_two_period_facts(state, object_store),
        requested_rule_ids=["beneish_m_score"],
    )
    pack = _service(state, object_store).run(request).pack
    finding = next(item for item in pack.rule_findings if item.rule_id == "beneish_m_score")
    assert finding.status is FinancialFindingStatus.CALCULATED
    assert finding.threshold_value is None
    assert finding.message_code == "ADVANCED_SCORE_CALCULATED_WITHOUT_INDUSTRY_FLAG_THRESHOLD"


def test_ytd_quarters_derive_ttm_qoq_and_per_share_without_guessing(
    state, object_store
) -> None:
    facts = []
    quarter_values = {
        date(2025, 3, 31): Decimal("100"),
        date(2025, 6, 30): Decimal("210"),
        date(2025, 9, 30): Decimal("330"),
        date(2025, 12, 31): Decimal("460"),
    }
    for index, (period_end, revenue) in enumerate(quarter_values.items(), start=1):
        values = dict(FINANCIAL_GOLDEN_VALUES)
        values[FinancialFieldCode.REVENUE] = revenue
        values[FinancialFieldCode.SHARES_OUTSTANDING] = Decimal("1000")
        facts.extend(
            make_financial_facts(
                state,
                object_store,
                source_suffix=f"quarter-{index}",
                period_end=period_end,
                period_type=FinancialPeriodType.QUARTERLY,
                duration_semantics=FinancialDurationSemantics.YEAR_TO_DATE,
                published_at=datetime(2026, 1, index, tzinfo=UTC),
                values=values,
            )
        )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
        series_requests=[
            FinancialSeriesRequest(
                request_id="ttm-revenue",
                derivation_type=FinancialDerivationType.TTM,
                field_code=FinancialFieldCode.REVENUE,
            ),
            FinancialSeriesRequest(
                request_id="qoq-revenue",
                derivation_type=FinancialDerivationType.QUARTER_OVER_QUARTER,
                field_code=FinancialFieldCode.REVENUE,
            ),
            FinancialSeriesRequest(
                request_id="revenue-per-share",
                derivation_type=FinancialDerivationType.PER_SHARE,
                field_code=FinancialFieldCode.REVENUE,
            ),
        ],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.SUCCEEDED
    by_request = {metric.request_id: metric for metric in pack.derived_metrics}
    assert by_request["ttm-revenue"].value == Decimal("4600000")
    assert by_request["qoq-revenue"].value == Decimal("0.08333333333333333333333333333")
    assert by_request["revenue-per-share"].value == Decimal("4600")


def test_quarterly_duration_without_semantics_is_an_explicit_gap(state, object_store) -> None:
    facts = []
    for index, period_end in enumerate((date(2025, 9, 30), date(2025, 12, 31)), start=1):
        facts.extend(
            make_financial_facts(
                state,
                object_store,
                source_suffix=f"ambiguous-quarter-{index}",
                period_end=period_end,
                period_type=FinancialPeriodType.QUARTERLY,
                published_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
        series_requests=[
            FinancialSeriesRequest(
                request_id="ambiguous-qoq",
                derivation_type=FinancialDerivationType.QUARTER_OVER_QUARTER,
                field_code=FinancialFieldCode.REVENUE,
            )
        ],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert not pack.derived_metrics
    assert any(
        gap.gap_type is FinancialGapType.AMBIGUOUS_PERIOD_SEMANTICS
        for gap in pack.evidence_gaps
    )


def test_peer_percentile_requires_and_persists_audited_cohort(state, object_store) -> None:
    facts = _two_period_facts(state, object_store)
    observations = []
    for index, value in enumerate(("-0.10", "0", "0.10"), start=1):
        peer_facts = make_financial_facts(
            state,
            object_store,
            source_suffix=f"peer-{index}",
            company_id=f"peer-{index}",
            period_end=date(2025, 12, 31),
            published_at=datetime(2026, 3, 18, tzinfo=UTC),
        )
        lineage = peer_facts[0]
        observations.append(
            FinancialPeerObservation(
                observation_id=f"peer-observation-{index}",
                company_id=f"peer-{index}",
                industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
                metric_id="sloan_accrual_ratio",
                formula_version="2.0.0",
                period_end=date(2025, 12, 31),
                available_at=datetime(2026, 3, 18, tzinfo=UTC),
                value=Decimal(value),
                unit=FinancialUnit.RATIO,
                source_snapshot_ids=[lineage.source_snapshot_id or ""],
                pit_ids=[lineage.pit_id or ""],
                evidence_ids=lineage.evidence_ids,
            )
        )
    cohort = FinancialPeerCohort(
        cohort_id="general-industrial-sloan-2025",
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        metric_id="sloan_accrual_ratio",
        formula_version="2.0.0",
        as_of=datetime(2026, 3, 20, tzinfo=UTC),
        minimum_sample_size=3,
        observations=observations,
    )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
        requested_rule_ids=["sloan_accrual_ratio"],
        peer_cohorts=[cohort],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.SUCCEEDED
    assert len(pack.peer_percentiles) == 1
    assert pack.peer_percentiles[0].percentile == Decimal(1) / Decimal(3)
    with state.connect() as connection:
        row = connection.execute(
            "SELECT sample_count,object_hash FROM financial_peer_cohort_manifest "
            "WHERE cohort_id=?",
            (cohort.cohort_id,),
        ).fetchone()
    assert row["sample_count"] == 3
    assert object_store.verify(row["object_hash"])
