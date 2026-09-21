"""WP39 fixed arithmetic/industry counterexamples; no network or production writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal, Inexact, localcontext
from typing import cast

import pytest

from astock.candidates.discovery_features import build_discovery_features, peer_percentile
from astock.schemas.financial import (
    FinancialDurationSemantics as Duration,
)
from astock.schemas.financial import (
    FinancialFact,
)
from astock.schemas.financial import (
    FinancialFieldCode as Field,
)
from astock.schemas.financial import (
    FinancialFindingStatus as Status,
)
from astock.schemas.financial import (
    FinancialIndustryProfile as Industry,
)
from astock.schemas.financial import (
    FinancialPeriodType as Period,
)
from astock.schemas.financial import (
    FinancialStatementType as Statement,
)
from astock.schemas.financial import (
    FinancialUnit as Unit,
)
from astock.schemas.financial_sources import FinancialStatementScope as Scope

END = date(2026, 6, 30)


def fact(
    field: Field,
    value: str | int,
    year: int = 2026,
    month: int = 6,
    *,
    company: str = "600001",
    unit: Unit = Unit.CNY,
    start: date | None = None,
    standalone: bool = False,
    suffix: str = "",
) -> FinancialFact:
    day = 31 if month in (3, 12) else 30
    instant = field in {
        Field.TOTAL_ASSETS,
        Field.TOTAL_LIABILITIES,
        Field.TOTAL_EQUITY,
        Field.ACCOUNTS_RECEIVABLE,
        Field.INVENTORY,
    }
    statement = (
        Statement.BALANCE_SHEET
        if instant
        else Statement.CASH_FLOW_STATEMENT
        if field in {Field.NET_CASH_OPERATING, Field.NET_CASH_FINANCING}
        else Statement.INCOME_STATEMENT
    )
    semantics = (
        Duration.INSTANT
        if instant
        else Duration.STANDALONE_PERIOD
        if standalone
        else Duration.REPORTED_PERIOD
        if month == 12
        else Duration.YEAR_TO_DATE
    )
    period = Period.ANNUAL if month == 12 else Period.SEMIANNUAL if month == 6 else Period.QUARTERLY
    return FinancialFact(
        fact_id=f"{company}:{field}:{year}:{month}:{suffix}",
        company_id=company,
        period_start=None if instant else (start or date(year, 1, 1)),
        period_end=date(year, month, day),
        period_type=period,
        duration_semantics=semantics,
        statement_type=statement,
        field_code=field,
        reported_value=Decimal(value),
        unit=unit,
        source_snapshot_id=f"snapshot:{company}:{year}:{month}",
        evidence_ids=[f"evidence:{company}:{field}:{year}:{month}"],
    )


def corpus(company: str = "600001") -> list[FinancialFact]:
    rows: list[FinancialFact] = []
    for code, values in (
        (Field.REVENUE, (1000, 400, 600, 250)),
        (Field.NET_PROFIT_INCOME, (100, 40, 90, 30)),
        (Field.NET_CASH_OPERATING, (120, 50, 100, 40)),
    ):
        for (year, month), value in zip(
            ((2025, 12), (2025, 6), (2026, 6), (2026, 3)), values, strict=True
        ):
            rows.append(fact(code, value, year, month, company=company))
    rows.extend(
        [
            fact(Field.TOTAL_ASSETS, 2000, company=company),
            fact(Field.TOTAL_LIABILITIES, 800, company=company),
            fact(Field.ACCOUNTS_RECEIVABLE, 100, company=company),
            fact(Field.ACCOUNTS_RECEIVABLE, 80, 2025, 6, company=company),
            fact(Field.INVENTORY, 120, company=company),
            fact(Field.INVENTORY, 100, 2025, 6, company=company),
            fact(Field.NET_PROFIT_INCOME, 80, 2024, 12, company=company),
            fact(Field.NET_PROFIT_INCOME, 60, 2023, 12, company=company),
        ]
    )
    return rows


def packet(
    rows: list[FinancialFact] | None = None,
    *,
    company: str = "600001",
    industry: Industry = Industry.GENERAL_INDUSTRIAL,
    group: str = "industrial-pumps",
    version: str = "facts-v1",
):
    supplied = corpus(company) if rows is None else rows
    return build_discovery_features(
        company_id=company,
        industry_profile=industry,
        business_group_id=group,
        period_end=END,
        fact_state_version=version,
        facts=supplied,
        scope_by_snapshot_id={
            row.source_snapshot_id: Scope.CONSOLIDATED
            for row in supplied
            if row.source_snapshot_id is not None
        },
    )


def test_ttm_and_quarter_are_distinct_and_exact():
    result = packet()
    assert result.feature("REVENUE_TTM").value == Decimal(1200)
    assert result.feature("REVENUE_QUARTER").value == Decimal(350)
    assert result.feature("NET_PROFIT_TTM").value == Decimal(150)
    assert result.feature("NET_PROFIT_QUARTER").value == Decimal(60)
    assert result.feature("CASH_CONVERSION").value == Decimal(170) / Decimal(150)
    assert result.feature("REVENUE_YOY").value == Decimal("0.5")
    assert result.feature("NET_PROFIT_YOY").value == Decimal("1.25")
    assert result.feature("PROFITABLE_ANNUAL_PERIODS").value == Decimal(3)
    assert result.feature("LIABILITIES_TO_ASSETS").value == Decimal("0.4")


def test_source_and_unknown_never_become_zero():
    empty = packet([])
    assert empty.feature("REVENUE_TTM").status is Status.INSUFFICIENT_DATA
    assert empty.feature("REVENUE_TTM").value is None
    rows = corpus()
    rows[0] = rows[0].model_copy(update={"evidence_ids": []})
    assert packet(rows).feature("REVENUE_TTM").value is None


@pytest.mark.parametrize("bad_unit", [Unit.SHARES, Unit.RATIO, Unit.PERCENT, Unit.SCORE])
def test_money_dimension_is_not_guessed(bad_unit):
    rows = corpus()
    rows[0] = rows[0].model_copy(update={"unit": bad_unit})
    value = packet(rows).feature("REVENUE_TTM")
    assert value.value is None
    assert "UNIT_MISMATCH" in value.reason_codes


def test_monetary_scale_normalizes_without_double_counting():
    rows = corpus()
    original = rows[0]
    rows[0] = original.model_copy(
        update={"reported_value": Decimal("0.1"), "unit": Unit.TEN_THOUSAND_CNY}
    )
    converted = packet(rows)
    assert converted.feature("REVENUE_TTM").value == Decimal(1200)
    rows.append(original.model_copy(update={"fact_id": "alternate-wrapper"}))
    assert packet(rows).semantic_fingerprint == converted.semantic_fingerprint


def test_contradictory_same_period_is_not_last_write_wins():
    rows = corpus()
    rows.append(
        rows[0].model_copy(
            update={"fact_id": "correction-not-resolved", "reported_value": Decimal(2000)}
        )
    )
    first = packet(rows)
    second = packet(list(reversed(rows)))
    assert first.feature("REVENUE_TTM").status is Status.CONFLICTED
    assert first.feature("REVENUE_TTM").value is None
    assert first.semantic_fingerprint == second.semantic_fingerprint


def test_explicit_standalone_quarter_cross_checks_cumulative():
    rows = corpus()
    rows.append(fact(Field.REVENUE, 350, start=date(2026, 4, 1), standalone=True, suffix="quarter"))
    assert packet(rows).feature("REVENUE_QUARTER").value == Decimal(350)
    rows[-1] = rows[-1].model_copy(update={"reported_value": Decimal(600)})
    assert packet(rows).feature("REVENUE_QUARTER").status is Status.CONFLICTED


def test_missing_annual_or_prior_ytd_is_not_annualized():
    rows = [
        row
        for row in corpus()
        if not (row.field_code is Field.REVENUE and row.period_end == date(2025, 6, 30))
    ]
    assert packet(rows).feature("REVENUE_TTM").value is None
    assert packet(rows).feature("REVENUE_QUARTER").value == Decimal(350)


def test_wrong_duration_not_accepted_as_cumulative():
    rows = corpus()
    rows[2] = rows[2].model_copy(update={"duration_semantics": Duration.REPORTED_PERIOD})
    assert packet(rows).feature("REVENUE_TTM").value is None


@pytest.mark.parametrize("prior", ["0", "-40"])
def test_turnaround_is_not_inflated_normal_growth(prior):
    rows = corpus()
    rows = [
        row.model_copy(update={"reported_value": Decimal(prior)})
        if row.field_code is Field.NET_PROFIT_INCOME and row.period_end == date(2025, 6, 30)
        else row
        for row in rows
    ]
    result = packet(rows)
    assert result.feature("NET_PROFIT_YOY").status is Status.NOT_APPLICABLE
    assert result.feature("NET_PROFIT_CHANGE").value == Decimal(90) - Decimal(prior)


@pytest.mark.parametrize("industry", [Industry.BANK, Industry.INSURANCE, Industry.SECURITIES])
def test_financial_firms_do_not_use_industrial_cash_and_leverage(industry):
    result = packet(industry=industry, group=industry.value)
    for name in ("CASH_CONVERSION", "LIABILITIES_TO_ASSETS", "INVENTORY_YOY"):
        assert result.feature(name).status is Status.NOT_APPLICABLE
    assert result.feature("NET_PROFIT_TTM").value == Decimal(150)
    assert result.feature("SPECIALIST_FINANCIAL_METRICS").status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    "industry", [Industry.GENERAL_INDUSTRIAL, Industry.RESOURCE_MINING, Industry.REAL_ESTATE]
)
def test_nonfinancial_profiles_keep_observations_without_investment_verdict(industry):
    result = packet(industry=industry)
    assert result.feature("CASH_CONVERSION").status is Status.CALCULATED
    assert result.feature("LIABILITIES_TO_ASSETS").value == Decimal("0.4")
    assert result.feature("DIVIDEND_CASH_COVERAGE").value is None
    assert result.feature("SPECIALIST_FINANCIAL_METRICS").value is None


def test_financing_cashflow_is_never_used_as_dividend():
    result = packet(corpus() + [fact(Field.NET_CASH_FINANCING, -100)])
    assert result.feature("DIVIDEND_CASH_COVERAGE").status is Status.INSUFFICIENT_DATA
    assert result.feature("DIVIDEND_CASH_COVERAGE").value is None


def test_identity_and_scope_fail_before_cross_company_mixing():
    with pytest.raises(ValueError, match="company"):
        packet(corpus() + [fact(Field.REVENUE, 5, company="000001")])
    rows = corpus()
    with pytest.raises(ValueError, match="scope"):
        build_discovery_features(
            company_id="600001",
            industry_profile=Industry.GENERAL_INDUSTRIAL,
            business_group_id="pumps",
            period_end=END,
            fact_state_version="v1",
            facts=rows,
            scope_by_snapshot_id={
                row.source_snapshot_id: Scope.PARENT_COMPANY
                for row in rows
                if row.source_snapshot_id
            },
        )


def test_fact_wrapper_and_order_do_not_change_semantic_fingerprint():
    rows = corpus()
    duplicate = rows[0].model_copy(update={"fact_id": "wrapper-copy"})
    assert (
        packet(rows).semantic_fingerprint
        == packet([duplicate, *reversed(rows)]).semantic_fingerprint
    )
    assert (
        packet(rows).semantic_fingerprint
        != packet(rows, version="correction-v2").semantic_fingerprint
    )
    changed = rows[0].model_copy(update={"reported_value": Decimal(1001)})
    assert packet(rows).semantic_fingerprint != packet([changed, *rows[1:]]).semantic_fingerprint


def test_cash_conversion_loss_denominator_and_zero_assets_are_not_safe_ratios():
    rows = corpus()
    rows = [
        row.model_copy(update={"reported_value": Decimal(-100)})
        if row.field_code is Field.NET_PROFIT_INCOME
        else row
        for row in rows
    ]
    rows = [
        row.model_copy(update={"reported_value": Decimal(0)})
        if row.field_code is Field.TOTAL_ASSETS
        else row
        for row in rows
    ]
    result = packet(rows)
    assert result.feature("CASH_CONVERSION").status is Status.NOT_APPLICABLE
    assert result.feature("LIABILITIES_TO_ASSETS").value is None


def test_peer_percentile_requires_ten_unique_business_comparables():
    target = packet()
    peers = [packet(company=f"600{i:03}") for i in range(1, 11)]
    assert peer_percentile(target, peers[:9], "NET_MARGIN").status is Status.INSUFFICIENT_DATA
    valid = peer_percentile(target, peers, "NET_MARGIN")
    assert valid.value == Decimal("0.5")
    assert valid.sample_size == 10
    assert peer_percentile(target, [*peers, peers[0]], "NET_MARGIN") == valid
    other = [
        packet(company=f"601{i:03}", industry=Industry.BANK, group="banks") for i in range(1, 11)
    ]
    assert (
        peer_percentile(target, [*peers[:9], *other], "NET_MARGIN").status
        is Status.INSUFFICIENT_DATA
    )


def test_peer_conflicting_company_observations_never_vote_twice():
    peers = [packet(company=f"600{i:03}") for i in range(1, 11)]
    altered = list(peers[1].features)
    index = next(i for i, feature in enumerate(altered) if feature.metric_id == "NET_MARGIN")
    altered[index] = replace(altered[index], value=Decimal("0.99"))
    conflicting = replace(peers[1], features=tuple(altered))
    result = peer_percentile(peers[0], [*peers, conflicting], "NET_MARGIN")
    assert result.status is Status.CONFLICTED
    assert result.value is None


def test_partial_annual_history_cannot_fake_three_profitable_years():
    rows = [row for row in corpus() if row.period_end != date(2024, 12, 31)]
    result = packet(rows)
    assert result.feature("PROFITABLE_ANNUAL_PERIODS").value is None
    assert result.feature("PROFITABLE_ANNUAL_PERIODS").status is Status.INSUFFICIENT_DATA


# Adversarial main-review regressions: first reproduced as eight failures.
def test_conflicting_quarter_propagates_to_dependent_ttm():
    rows = corpus()
    rows.append(
        fact(
            Field.REVENUE,
            999,
            start=date(2026, 4, 1),
            standalone=True,
            suffix="conflicting-quarter",
        )
    )
    assert packet(rows).feature("REVENUE_TTM").status is Status.CONFLICTED


def test_negative_liability_is_not_low_leverage():
    rows = [
        row.model_copy(update={"reported_value": Decimal(-1)})
        if row.field_code is Field.TOTAL_LIABILITIES
        else row
        for row in corpus()
    ]
    assert packet(rows).feature("LIABILITIES_TO_ASSETS").value is None


@pytest.mark.parametrize("field", [Field.REVENUE, Field.TOTAL_ASSETS])
def test_mislabelled_annual_june_report_is_rejected(field):
    rows = [
        row.model_copy(update={"period_type": Period.ANNUAL})
        if row.field_code is field and row.period_end == date(2026, 6, 30)
        else row
        for row in corpus()
    ]
    name = "REVENUE_TTM" if field is Field.REVENUE else "LIABILITIES_TO_ASSETS"
    assert packet(rows).feature(name).value is None


def test_three_year_history_stops_at_actual_last_annual_end():
    feature = packet().feature("PROFITABLE_ANNUAL_PERIODS")
    assert feature.period_start == date(2023, 1, 1)
    assert feature.period_end == date(2025, 12, 31)


def test_float_sample_minimum_is_not_silently_rounded():
    with pytest.raises(ValueError):
        peer_percentile(packet(), [], "NET_MARGIN", minimum_sample_size=cast(int, 10.1))


def test_peer_unknown_coverage_is_visible():
    peers = [packet(company=f"600{i:03}") for i in range(1, 11)]
    missing = packet([], company="600011")
    result = peer_percentile(peers[0], [*peers, missing], "NET_MARGIN")
    assert result.sample_size == 10
    assert len(result.eligible_company_ids) == 11
    assert result.unavailable_company_ids == ("600011",)


def test_unrelated_correction_preserves_unchanged_feature_dependency_key():
    before = packet()
    rows = [
        row.model_copy(update={"reported_value": Decimal(990)})
        if row.field_code is Field.INVENTORY
        else row
        for row in corpus()
    ]
    after = packet(rows, version="facts-v2")
    assert after.semantic_fingerprint != before.semantic_fingerprint
    assert (
        after.feature("NET_MARGIN").dependency_fingerprint
        == before.feature("NET_MARGIN").dependency_fingerprint
    )
    assert (
        after.feature("INVENTORY_YOY").dependency_fingerprint
        != before.feature("INVENTORY_YOY").dependency_fingerprint
    )


# Reproduced numerical/lineage regression cases.
@pytest.mark.parametrize("number", ["1e1000000", "1e-1000100"])
def test_extreme_finite_currency_is_unknown_not_zero_or_pipeline_exception(number):
    rows = corpus()
    rows[0] = rows[0].model_copy(update={"reported_value": Decimal(number)})
    assert packet(rows).feature("REVENUE_TTM").value is None


def test_unrepresentable_exact_currency_is_not_silently_rounded():
    rows = corpus()
    rows[0] = rows[0].model_copy(update={"reported_value": Decimal("1234567890" * 7)})
    result = packet(rows).feature("REVENUE_TTM")
    with localcontext() as context:
        context.prec = 100
        exact = rows[0].reported_value + Decimal(200)
    assert result.value is None or result.value == exact


@pytest.mark.parametrize("update", [{"source_snapshot_id": "   "}, {"evidence_ids": ["   "]}])
def test_whitespace_lineage_is_not_certified_source_identity(update):
    rows = corpus()
    rows[0] = rows[0].model_copy(update=update)
    assert packet(rows).feature("REVENUE_TTM").value is None


def test_numeric_context_is_local_and_invalid_raw_inputs_invalidate_keys():
    expected = packet()
    with localcontext() as ctx:
        ctx.prec = 5
        ctx.traps[Inexact] = True
        actual = packet()
    assert actual == expected
    rows = corpus()
    rows[0] = rows[0].model_copy(update={"reported_value": Decimal("1e1000000")})
    first = packet(rows)
    rows[0] = rows[0].model_copy(update={"reported_value": Decimal("2e1000000")})
    assert packet(rows).semantic_fingerprint != first.semantic_fingerprint
