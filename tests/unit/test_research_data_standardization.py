from __future__ import annotations

from decimal import Decimal

from astock.data_standardization import (
    build_financial_fact_pack,
    normalize_provider_snapshot,
    parse_decimal,
)
from astock.schemas.financial import FinancialIndustryProfile


def test_decimal_parser_handles_common_provider_and_accounting_forms() -> None:
    assert parse_decimal("1,234.50") == Decimal("1234.50")
    assert parse_decimal("(125.5)") == Decimal("-125.5")
    assert parse_decimal("12.5%", percent_as_ratio=True) == Decimal("0.125")
    assert parse_decimal("--") is None
    assert parse_decimal(None) is None
    assert parse_decimal(True) is None


def test_market_snapshot_uses_explicit_aliases_scales_and_never_fills_missing_with_zero() -> None:
    result = normalize_provider_snapshot(
        provider_id="fixture-provider",
        instrument_id="XSHE:002155",
        payload={
            "last_px": "25.41",
            "native_pe": "18.2",
            "native_pb": "2.10",
            "mv_wan": "1,250,000",
            "amount": "98000000",
        },
        aliases={
            "price": ["last_px"],
            "pe_ttm": ["native_pe"],
            "pb_mrq": ["native_pb"],
            "market_cap_cny": ["mv_wan"],
            "turnover_cny": ["amount"],
        },
        scales={"market_cap_cny": Decimal("10000")},
    )
    assert result.price == Decimal("25.41")
    assert result.pe_ttm == Decimal("18.2")
    assert result.pb_mrq == Decimal("2.10")
    assert result.market_cap_cny == Decimal("12500000000")
    assert result.turnover_cny == Decimal("98000000")
    assert result.missing_fields == []

    missing = normalize_provider_snapshot(
        provider_id="fixture-provider",
        instrument_id="XSHE:002155",
        payload={"last": "25.41", "pe": "-"},
    )
    assert missing.pe_ttm is None
    assert missing.market_cap_cny is None
    assert "pe_ttm" in missing.missing_fields
    assert "market_cap_cny" in missing.missing_fields


def test_general_company_pack_normalizes_numbers_and_reports_only_real_gaps() -> None:
    result = build_financial_fact_pack(
        company_id="002636",
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        raw_metrics={
            "assets_wan": "120,000",
            "liabilities_wan": "35,000",
            "revenue_wan": "80,000",
            "profit_wan": "5,500",
            "cfo_wan": "6,200",
            "optional_bad": "--",
        },
        metric_aliases={
            "TOTAL_ASSETS": ["assets_wan"],
            "TOTAL_LIABILITIES": ["liabilities_wan"],
            "REVENUE": ["revenue_wan"],
            "NET_PROFIT": ["profit_wan"],
            "NET_CASH_OPERATING": ["cfo_wan"],
            "OPTIONAL_METRIC": ["optional_bad"],
        },
        metric_scales={
            key: Decimal("10000")
            for key in (
                "TOTAL_ASSETS",
                "TOTAL_LIABILITIES",
                "REVENUE",
                "NET_PROFIT",
                "NET_CASH_OPERATING",
            )
        },
    )
    assert result.missing_critical_metrics == []
    assert result.metrics["NET_PROFIT"] == Decimal("55000000")
    assert result.metrics["NET_CASH_OPERATING"] == Decimal("62000000")
    assert result.ignored_non_numeric_fields == ["OPTIONAL_METRIC"]


def test_bank_pack_requires_bank_metrics_instead_of_industrial_inventory_logic() -> None:
    result = build_financial_fact_pack(
        company_id="000001",
        industry_profile=FinancialIndustryProfile.BANK,
        raw_metrics={
            "TOTAL_ASSETS": "6.2e12",
            "TOTAL_LIABILITIES": "5.7e12",
            "NET_PROFIT": "49.8e9",
            "NET_INTEREST_MARGIN": "1.85%",
            "NON_PERFORMING_LOAN_RATIO": "1.05%",
            "PROVISION_COVERAGE_RATIO": "245%",
        },
        percent_metrics=[
            "NET_INTEREST_MARGIN",
            "NON_PERFORMING_LOAN_RATIO",
            "PROVISION_COVERAGE_RATIO",
        ],
    )
    assert result.missing_critical_metrics == []
    assert result.metrics["NET_INTEREST_MARGIN"] == Decimal("0.0185")
    assert "INVENTORY" not in result.metrics

    incomplete = build_financial_fact_pack(
        company_id="000001",
        industry_profile=FinancialIndustryProfile.BANK,
        raw_metrics={"TOTAL_ASSETS": "6.2e12", "TOTAL_LIABILITIES": "5.7e12"},
    )
    assert "NET_INTEREST_MARGIN" in incomplete.missing_critical_metrics
    assert "NON_PERFORMING_LOAN_RATIO" in incomplete.missing_critical_metrics


def test_resource_mining_pack_uses_production_and_unit_cost_drivers() -> None:
    result = build_financial_fact_pack(
        company_id="002155",
        industry_profile=FinancialIndustryProfile.RESOURCE_MINING,
        raw_metrics={
            "REVENUE": "32029000000",
            "NET_PROFIT": "957000000",
            "NET_CASH_OPERATING": "-106000000",
            "PRODUCTION_VOLUME": "24800",
            "UNIT_COST": "312.50",
        },
    )
    assert result.missing_critical_metrics == []
    assert result.metrics["PRODUCTION_VOLUME"] == Decimal("24800")
    assert result.metrics["UNIT_COST"] == Decimal("312.50")
