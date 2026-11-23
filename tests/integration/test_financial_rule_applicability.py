"""Period-aware financial rule applicability, with provenance-safe negative cases.

These synthetic documents are recorded fixtures; they do not claim live qualification.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.financial_integrity import FinancialIntegrityService
from astock.financial_integrity.config import load_financial_rule_registry
from astock.schemas import (
    EvidenceGrade,
    FinancialAuditRequest,
    FinancialCoverageStatus,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialRiskLevel,
    RunStatus,
)
from tests.helpers import FINANCIAL_GOLDEN_VALUES, make_financial_facts

ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 11, 23, tzinfo=UTC)


def _audit(state, object_store, *, values=None, period_type=FinancialPeriodType.QUARTERLY,
           requested_rule_ids=None, evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
           industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL):
    facts = make_financial_facts(
        state, object_store,
        source_suffix="period-aware:" + period_type.value,
        period_end=date(2026, 9, 30) if period_type is FinancialPeriodType.QUARTERLY
        else date(2025, 12, 31),
        period_type=period_type,
        published_at=datetime(2026, 11, 1, tzinfo=UTC),
        values=values,
        evidence_grade=evidence_grade,
    )
    service = FinancialIntegrityService(
        state, object_store,
        rule_config_path=ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=ROOT / "configs" / "financial_industry_profiles.yaml",
    )
    return service.run(FinancialAuditRequest(
        company_id="000001", as_of=AS_OF,
        industry_profile=industry_profile,
        facts=facts, requested_rule_ids=requested_rule_ids or [],
    )).pack


def _finding(pack, rule_id):
    return next(x for x in pack.rule_findings if x.rule_id == rule_id)


def test_resource_mining_does_not_skip_core_accounting_equations(state, object_store) -> None:
    complete = _audit(
        state, object_store, industry_profile=FinancialIndustryProfile.RESOURCE_MINING,
    )
    assert _finding(complete, "balance_sheet_identity").status is FinancialFindingStatus.PASS
    assert _finding(complete, "cash_flow_identity").status is FinancialFindingStatus.PASS

    changed = dict(FINANCIAL_GOLDEN_VALUES)
    changed[FinancialFieldCode.TOTAL_ASSETS] = Decimal("1200")
    disputed = _audit(
        state, object_store, values=changed,
        industry_profile=FinancialIndustryProfile.RESOURCE_MINING,
    )
    assert _finding(disputed, "balance_sheet_identity").status is FinancialFindingStatus.FLAG


def test_quarterly_optional_cashflow_note_missing_does_not_block_core_identity(
    state, object_store,
) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.NET_PROFIT_CASH_FLOW}
    pack = _audit(state, object_store, values=values)
    assert pack.status is RunStatus.SUCCEEDED
    assert pack.coverage_status is FinancialCoverageStatus.COMPLETE
    assert pack.evidence_gaps == []
    assert _finding(pack, "balance_sheet_identity").status is FinancialFindingStatus.PASS
    assert _finding(pack, "cash_flow_identity").status is FinancialFindingStatus.PASS
    note = _finding(pack, "net_profit_cross_statement")
    assert note.status is FinancialFindingStatus.NOT_EVALUATED
    assert note.actual_value is None
    assert note.not_evaluated_fields == [FinancialFieldCode.NET_PROFIT_CASH_FLOW]
    assert note.applicability_reason_code == "CONDITIONAL_INPUTS_NOT_VERIFIED"
    assert not note.evidence_gap_ids
    assert not pack.manual_tasks


def test_annual_cross_statement_note_remains_required(state, object_store) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.NET_PROFIT_CASH_FLOW}
    pack = _audit(state, object_store, values=values, period_type=FinancialPeriodType.ANNUAL)
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.PARTIAL
    assert _finding(pack, "net_profit_cross_statement").status is (
        FinancialFindingStatus.INSUFFICIENT_DATA
    )


def test_quarterly_cross_statement_explicit_request_requires_evidence(
    state, object_store,
) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.NET_PROFIT_CASH_FLOW}
    pack = _audit(state, object_store, values=values,
                  requested_rule_ids=["net_profit_cross_statement"])
    assert pack.status is RunStatus.NEEDS_INFO
    assert _finding(pack, "net_profit_cross_statement").evidence_gap_ids


def test_quarterly_present_cashflow_note_conflict_still_flags(
    state, object_store,
) -> None:
    values = dict(FINANCIAL_GOLDEN_VALUES)
    values[FinancialFieldCode.NET_PROFIT_CASH_FLOW] = Decimal("500")
    pack = _audit(state, object_store, values=values)
    assert _finding(pack, "net_profit_cross_statement").status is FinancialFindingStatus.FLAG
    assert pack.risk_level is FinancialRiskLevel.MEDIUM


def test_optional_descriptive_ratio_missing_does_not_block_other_audits(
    state, object_store,
) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.INVENTORY}
    pack = _audit(state, object_store, values=values,
                  period_type=FinancialPeriodType.ANNUAL)
    assert pack.status is RunStatus.SUCCEEDED
    assert pack.coverage_status is FinancialCoverageStatus.COMPLETE
    assert _finding(pack, "inventory_operating_cost_ratio").status is (
        FinancialFindingStatus.NOT_EVALUATED
    )
    assert not pack.evidence_gaps


def test_explicit_descriptive_ratio_is_still_evidence_required(state, object_store) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.INVENTORY}
    pack = _audit(state, object_store, values=values,
                  requested_rule_ids=["inventory_operating_cost_ratio"])
    assert pack.status is RunStatus.NEEDS_INFO
    assert _finding(pack, "inventory_operating_cost_ratio").evidence_gap_ids


def test_core_balance_sheet_missing_remains_blocked(state, object_store) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.TOTAL_ASSETS}
    pack = _audit(state, object_store, values=values)
    assert pack.status is RunStatus.NEEDS_INFO
    assert _finding(pack, "balance_sheet_identity").status is (
        FinancialFindingStatus.INSUFFICIENT_DATA
    )


def test_core_balance_sheet_mismatch_remains_flagged(state, object_store) -> None:
    values = dict(FINANCIAL_GOLDEN_VALUES)
    values[FinancialFieldCode.TOTAL_ASSETS] = Decimal("1200")
    pack = _audit(state, object_store, values=values)
    assert _finding(pack, "balance_sheet_identity").status is FinancialFindingStatus.FLAG
    assert pack.risk_level is FinancialRiskLevel.HIGH


def test_bad_provenance_is_not_silently_cleared_by_optional_rule(
    state, object_store,
) -> None:
    pack = _audit(state, object_store, evidence_grade=EvidenceGrade.COMMUNITY_LEAD)
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.BLOCKED


def test_rule_configuration_declares_exact_conditional_periods() -> None:
    rules = load_financial_rule_registry(ROOT / "configs" / "financial_rules.yaml")
    by_id = {rule.rule_id: rule for rule in rules.rules}
    assert by_id["net_profit_cross_statement"].conditional_missing_periods == [
        FinancialPeriodType.QUARTERLY
    ]
    assert by_id["net_profit_cross_statement"].conditional_missing_fields == [
        FinancialFieldCode.NET_PROFIT_CASH_FLOW
    ]
    assert not by_id["balance_sheet_identity"].conditional_missing_periods
    assert not by_id["cash_flow_identity"].conditional_missing_periods
    assert set(by_id["inventory_operating_cost_ratio"].conditional_missing_periods) == {
        FinancialPeriodType.QUARTERLY, FinancialPeriodType.SEMIANNUAL,
        FinancialPeriodType.ANNUAL,
    }
    with pytest.raises(ValidationError, match="conditional_missing_periods must be unique"):
        duplicate = by_id["net_profit_cross_statement"].model_dump(mode="python")
        duplicate["conditional_missing_periods"] = ["QUARTERLY", "QUARTERLY"]
        type(by_id["net_profit_cross_statement"]).model_validate(duplicate)


def test_quarterly_main_income_field_remains_required_even_when_note_is_conditional(
    state, object_store,
) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.NET_PROFIT_INCOME}
    pack = _audit(state, object_store, values=values)
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.PARTIAL
    assert _finding(pack, "net_profit_cross_statement").status is (
        FinancialFindingStatus.INSUFFICIENT_DATA
    )


def test_quarterly_missing_fx_for_core_cashflow_is_not_assumed_zero(
    state, object_store,
) -> None:
    values = {k: v for k, v in FINANCIAL_GOLDEN_VALUES.items()
              if k is not FinancialFieldCode.EXCHANGE_EFFECT}
    pack = _audit(state, object_store, values=values)
    assert pack.status is RunStatus.NEEDS_INFO
    assert _finding(pack, "cash_flow_identity").status is (
        FinancialFindingStatus.INSUFFICIENT_DATA
    )
    assert not any(
        item.rule_id == "cash_flow_identity" and item.status is FinancialFindingStatus.PASS
        for item in pack.rule_findings
    )


def test_conflicting_official_quarterly_note_is_not_hidden_by_conditional_rule(
    state, object_store,
) -> None:
    first = make_financial_facts(
        state, object_store, source_suffix="first-quarter-note",
        period_end=date(2026, 9, 30), period_type=FinancialPeriodType.QUARTERLY,
        published_at=datetime(2026, 10, 25, tzinfo=UTC),
    )
    changed = dict(FINANCIAL_GOLDEN_VALUES)
    changed[FinancialFieldCode.NET_PROFIT_CASH_FLOW] = Decimal("300")
    second = make_financial_facts(
        state, object_store, source_suffix="second-quarter-note",
        period_end=date(2026, 9, 30), period_type=FinancialPeriodType.QUARTERLY,
        published_at=datetime(2026, 10, 26, tzinfo=UTC), values=changed,
    )
    service = FinancialIntegrityService(
        state, object_store,
        rule_config_path=ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=ROOT / "configs" / "financial_industry_profiles.yaml",
    )
    pack = service.run(FinancialAuditRequest(
        company_id="000001", as_of=AS_OF,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=[*first, *second],
    )).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.document_conflicts
    assert any(
        FinancialFieldCode.NET_PROFIT_CASH_FLOW in gap.field_codes
        for gap in pack.evidence_gaps
    )
    assert _finding(pack, "net_profit_cross_statement").status is not (
        FinancialFindingStatus.PASS
    )


def test_conditional_fields_policy_rejects_unlisted_or_unscoped_inputs() -> None:
    base = load_financial_rule_registry(ROOT / "configs" / "financial_rules.yaml")
    rule = next(item for item in base.rules if item.rule_id == "net_profit_cross_statement")
    def create(**updates):
        payload = rule.model_dump(mode="python")
        payload.update(updates)
        return type(rule).model_validate(payload)

    with pytest.raises(ValidationError, match="conditional fields must be declared"):
        create(conditional_missing_fields=[FinancialFieldCode.TOTAL_ASSETS])
    with pytest.raises(ValidationError, match="conditional fields require conditional periods"):
        create(conditional_missing_periods=[], conditional_missing_fields=[
            FinancialFieldCode.NET_PROFIT_CASH_FLOW
        ])


def test_advanced_rule_cannot_silently_claim_unsupported_conditional_behavior() -> None:
    registry = load_financial_rule_registry(ROOT / "configs" / "financial_rules.yaml")
    advanced = next(rule for rule in registry.rules if rule.rule_id == "beneish_m_score")
    payload = advanced.model_dump(mode="python")
    payload["conditional_missing_periods"] = ["ANNUAL"]
    with pytest.raises(ValidationError, match="only supported by deterministic M3.1"):
        type(advanced).model_validate(payload)


def test_non_evaluated_fields_cannot_be_attached_to_pass_finding() -> None:
    from astock.schemas import FinancialRuleFinding, FinancialSeverity

    with pytest.raises(ValidationError, match="only NOT_EVALUATED"):
        FinancialRuleFinding(
            finding_id="test-positive", rule_id="net_profit_cross_statement",
            formula_version="1.0.0", status=FinancialFindingStatus.PASS,
            severity=FinancialSeverity.INFO, message_code="FROZEN_COMPUTATION_PASSED",
            evidence_ids=["evidence:report"], not_evaluated_fields=[
                FinancialFieldCode.NET_PROFIT_CASH_FLOW,
            ],
        )

