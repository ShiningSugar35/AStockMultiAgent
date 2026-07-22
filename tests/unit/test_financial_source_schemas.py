"""Strict financial-source schema regressions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from astock.schemas import (
    FinancialDurationSemantics,
    FinancialFieldCode,
    FinancialPeriodType,
    FinancialSourceObservation,
    FinancialStatementScope,
    FinancialStatementType,
    FinancialUnit,
    InstrumentType,
    Market,
)


def _observation(**changes: object) -> FinancialSourceObservation:
    values = {
        "observation_id": "a" * 64,
        "company_id": "000001",
        "instrument_id": "XSHE:000001",
        "market": Market.XSHE,
        "instrument_type": InstrumentType.STOCK,
        "instrument_release_id": "b" * 64,
        "instrument_manifest_artifact_id": f"market-reference:{'b' * 64}",
        "instrument_manifest_object_hash": "c" * 64,
        "instrument_content_hash": "d" * 64,
        "period_start": date(2025, 1, 1),
        "period_end": date(2025, 12, 31),
        "period_type": FinancialPeriodType.ANNUAL,
        "duration_semantics": FinancialDurationSemantics.REPORTED_PERIOD,
        "statement_type": FinancialStatementType.INCOME_STATEMENT,
        "statement_scope": FinancialStatementScope.CONSOLIDATED,
        "field_code": FinancialFieldCode.REVENUE,
        "provider_field": "REVENUE",
        "reported_value": Decimal("1000"),
        "unit": FinancialUnit.TEN_THOUSAND_CNY,
        "provider_id": "eastmoney-financial",
        "source_snapshot_id": "snapshot",
        "source_request_hash": "e" * 64,
        "available_to_system_at": datetime(2026, 7, 22, 9, tzinfo=UTC),
    }
    values.update(changes)
    return FinancialSourceObservation.model_validate(values)


def test_financial_observation_period_duration_scope_and_instrument_are_strict() -> None:
    assert _observation().statement_scope is FinancialStatementScope.CONSOLIDATED
    with pytest.raises(ValueError, match="CONSOLIDATED"):
        _observation(statement_scope=FinancialStatementScope.PARENT_COMPANY)
    with pytest.raises(ValueError, match="REPORTED_PERIOD"):
        _observation(duration_semantics=FinancialDurationSemantics.YEAR_TO_DATE)
    with pytest.raises(ValueError, match="December 31"):
        _observation(period_end=date(2025, 9, 30))
    with pytest.raises(ValueError, match="instrument identity"):
        _observation(instrument_id="XSHG:000001")


def test_balance_and_quarterly_semantics_fail_closed() -> None:
    balance = _observation(
        period_start=None,
        duration_semantics=FinancialDurationSemantics.INSTANT,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        field_code=FinancialFieldCode.TOTAL_ASSETS,
        provider_field="TOTAL_ASSETS",
    )
    assert balance.period_start is None
    with pytest.raises(ValueError, match="fiscal-year period_start"):
        _observation(period_start=date(2025, 4, 1))
    with pytest.raises(ValueError, match="YEAR_TO_DATE"):
        _observation(
            period_end=date(2025, 9, 30),
            period_type=FinancialPeriodType.QUARTERLY,
        )
