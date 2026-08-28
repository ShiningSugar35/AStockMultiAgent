from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from astock.candidates.financial_policy import financial_pack_is_candidate_eligible
from astock.schemas import (
    FinancialCoverageStatus,
    FinancialDocumentConflict,
    FinancialDurationSemantics,
    FinancialEvidenceGap,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialGapType,
    FinancialIndustryProfile,
    FinancialIntegrityEvidencePack,
    FinancialPeriodType,
    FinancialRiskLevel,
    FinancialRuleFinding,
    FinancialSeverity,
    FinancialStatementType,
    VerifiedFinancialNumber,
)
from astock.schemas.runs import RunStatus

NOW = datetime(2026, 8, 25, 4, 0, tzinfo=UTC)
PERIOD_END = date(2025, 12, 31)


def _number(field_code: FinancialFieldCode) -> VerifiedFinancialNumber:
    statement = {
        FinancialFieldCode.TOTAL_ASSETS: FinancialStatementType.BALANCE_SHEET,
        FinancialFieldCode.TOTAL_LIABILITIES: FinancialStatementType.BALANCE_SHEET,
        FinancialFieldCode.TOTAL_EQUITY: FinancialStatementType.BALANCE_SHEET,
        FinancialFieldCode.REVENUE: FinancialStatementType.INCOME_STATEMENT,
        FinancialFieldCode.NET_PROFIT_INCOME: FinancialStatementType.INCOME_STATEMENT,
        FinancialFieldCode.NET_CASH_OPERATING: FinancialStatementType.CASH_FLOW_STATEMENT,
    }[field_code]
    duration = (
        FinancialDurationSemantics.INSTANT
        if statement is FinancialStatementType.BALANCE_SHEET
        else FinancialDurationSemantics.REPORTED_PERIOD
    )
    return VerifiedFinancialNumber(
        field_code=field_code,
        statement_type=statement,
        period_start=None if duration is FinancialDurationSemantics.INSTANT else date(2025, 1, 1),
        period_end=PERIOD_END,
        period_type=FinancialPeriodType.ANNUAL,
        duration_semantics=duration,
        value_cny=Decimal("1"),
        reporting_quantum_cny=Decimal("0.01"),
        fact_ids=[f"fact:{field_code.value}"],
        source_snapshot_ids=["snapshot:official"],
        pit_ids=["pit:official"],
        evidence_ids=[f"evidence:{field_code.value}"],
    )


def _pack(
    *,
    omit: FinancialFieldCode | None = None,
    risk: FinancialRiskLevel = FinancialRiskLevel.LOW,
) -> FinancialIntegrityEvidencePack:
    core = [
        FinancialFieldCode.TOTAL_ASSETS,
        FinancialFieldCode.TOTAL_LIABILITIES,
        FinancialFieldCode.TOTAL_EQUITY,
        FinancialFieldCode.REVENUE,
        FinancialFieldCode.NET_PROFIT_INCOME,
        FinancialFieldCode.NET_CASH_OPERATING,
    ]
    numbers = [_number(item) for item in core if item is not omit]
    gap = FinancialEvidenceGap(
        gap_id="gap:auxiliary",
        gap_type=FinancialGapType.MISSING_FACT,
        detail_code="RULE_REQUIRED_FIELDS_UNAVAILABLE",
        period_end=PERIOD_END,
        field_codes=[FinancialFieldCode.NET_PROFIT_CASH_FLOW],
        related_rule_ids=["net_profit_cross_statement"],
    )
    return FinancialIntegrityEvidencePack(
        audit_run_id="financial-audit:test",
        request_hash="a" * 64,
        status=RunStatus.NEEDS_INFO,
        coverage_status=FinancialCoverageStatus.PARTIAL,
        company_id="603986",
        as_of=NOW,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        periods=[PERIOD_END],
        input_fact_ids=[item.fact_ids[0] for item in numbers],
        source_snapshot_ids=["snapshot:official"],
        pit_ids=["pit:official"],
        verified_numbers=numbers,
        recalculated_metrics=[],
        rule_findings=[],
        evidence_gaps=[gap],
        risk_level=risk,
        rule_versions={},
        model_versions={},
        capability_status={},
        created_at=NOW,
    )


def test_low_risk_partial_with_core_official_fields_can_enter_candidate_research() -> None:
    pack = _pack()

    assert financial_pack_is_candidate_eligible(pack)
    assert pack.coverage_status is FinancialCoverageStatus.PARTIAL
    assert pack.status is RunStatus.NEEDS_INFO


def test_partial_financial_pack_missing_core_field_remains_blocked() -> None:
    assert not financial_pack_is_candidate_eligible(
        _pack(omit=FinancialFieldCode.NET_CASH_OPERATING)
    )


def test_partial_financial_pack_with_nonlow_risk_remains_blocked() -> None:
    assert not financial_pack_is_candidate_eligible(_pack(risk=FinancialRiskLevel.MEDIUM))


def test_partial_financial_pack_with_open_document_conflict_remains_blocked() -> None:
    conflict = FinancialDocumentConflict(
        conflict_id="financial-conflict:revenue",
        period_end=PERIOD_END,
        period_type=FinancialPeriodType.ANNUAL,
        field_code=FinancialFieldCode.REVENUE,
        fact_ids=["fact:revenue:a", "fact:revenue:b"],
        normalized_values_cny=[Decimal("1"), Decimal("2")],
        evidence_ids=["evidence:revenue:conflict"],
    )

    assert not financial_pack_is_candidate_eligible(
        _pack().model_copy(update={"document_conflicts": [conflict]})
    )


def test_partial_financial_pack_with_conflicted_finding_remains_blocked() -> None:
    finding = FinancialRuleFinding(
        finding_id="finding:conflicted",
        rule_id="financial-conflict-rule",
        formula_version="v1",
        period_end=PERIOD_END,
        status=FinancialFindingStatus.CONFLICTED,
        severity=FinancialSeverity.LOW,
        message_code="OFFICIAL_VALUE_CONFLICT",
        evidence_ids=["evidence:conflicted"],
    )

    assert not financial_pack_is_candidate_eligible(
        _pack().model_copy(update={"rule_findings": [finding]})
    )
