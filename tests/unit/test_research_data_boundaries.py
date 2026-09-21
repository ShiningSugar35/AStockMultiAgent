"""Numeric and company boundaries for current research standardization."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from astock.data_standardization import (
    build_financial_fact_pack,
    normalize_provider_snapshot,
    parse_decimal,
    standardize_financial_facts,
)
from astock.schemas.financial import (
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        "Infinity",
        "-Infinity",
        "sNaN",
    ],
)
def test_nonfinite_numbers_are_missing_instead_of_facts(value: object) -> None:
    assert parse_decimal(value) is None


def test_invalid_primary_quote_alias_uses_finite_fallback() -> None:
    snapshot = normalize_provider_snapshot(
        provider_id="boundary-fixture",
        instrument_id="XSHE:000001",
        payload={"price": float("nan"), "last": "12.30", "pe": float("inf")},
    )
    assert snapshot.price == Decimal("12.30")
    assert snapshot.pe_ttm is None
    assert snapshot.source_fields["price"] == "last"
    assert "pe_ttm" in snapshot.missing_fields


def test_nonfinite_financial_metric_remains_an_explicit_gap() -> None:
    pack = build_financial_fact_pack(
        company_id="000001",
        industry_profile=FinancialIndustryProfile.BANK,
        raw_metrics={"TOTAL_ASSETS": Decimal("Infinity")},
    )
    assert "TOTAL_ASSETS" not in pack.metrics
    assert "TOTAL_ASSETS" in pack.missing_critical_metrics


def _asset(company: str) -> FinancialFact:
    return FinancialFact(
        fact_id=f"asset:{company}",
        company_id=company,
        period_end=date(2025, 12, 31),
        period_type=FinancialPeriodType.ANNUAL,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        field_code=FinancialFieldCode.TOTAL_ASSETS,
        reported_value=Decimal("123"),
        unit=FinancialUnit.TEN_THOUSAND_CNY,
        source_snapshot_id=f"source:{company}",
    )


def test_foreign_company_fact_cannot_enter_the_requested_company_pack() -> None:
    with pytest.raises(ValueError, match="company"):
        standardize_financial_facts(
            company_id="000001",
            industry_profile=FinancialIndustryProfile.BANK,
            facts=[_asset("000001"), _asset("600000")],
        )


def test_matching_company_fact_keeps_its_unit_conversion() -> None:
    pack = standardize_financial_facts(
        company_id="000001",
        industry_profile=FinancialIndustryProfile.BANK,
        facts=[_asset("000001")],
    )
    assert pack.metrics["TOTAL_ASSETS"] == Decimal("1230000")
