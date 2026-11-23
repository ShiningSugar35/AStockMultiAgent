from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.financial_integrity.config import (
    load_financial_industry_profiles,
    load_financial_rule_registry,
    validate_financial_config,
)
from astock.schemas import (
    FinancialAuditRequest,
    FinancialCoverageStatus,
    FinancialFindingStatus,
    FinancialIndustryProfile,
    FinancialIntegrityEvidencePack,
    FinancialRiskLevel,
    FinancialRuleFinding,
    FinancialSeverity,
    RunStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_financial_rule_registry_has_required_audit_metadata() -> None:
    rules = load_financial_rule_registry(PROJECT_ROOT / "configs" / "financial_rules.yaml")
    profiles = load_financial_industry_profiles(
        PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml"
    )
    validate_financial_config(rules, profiles)
    assert rules.registry_version == "financial-rules-m3.2-v3"
    assert rules.compatible_engine_version == "financial-deterministic-m3.2.1"
    assert len(rules.rules) == 13
    for rule in rules.rules:
        assert rule.formula_version
        assert rule.source_reference
        assert rule.threshold_source
        assert rule.false_positive_modes
        assert rule.tests
    bank = next(profile for profile in profiles.profiles if profile.profile_id == "BANK")
    assert {"beneish_m_score", "altman_z_score"}.issubset(bank.excluded_rule_ids)
    implemented = {
        rule.rule_id
        for rule in rules.rules
        if rule.implementation_status == "IMPLEMENTED_M3_2"
    }
    assert implemented == {
        "beneish_m_score",
        "altman_z_score",
        "piotroski_f_score",
        "sloan_accrual_ratio",
        "dupont_decomposition",
    }


def test_high_severity_accounting_identites_apply_to_every_registered_industry() -> None:
    rules = load_financial_rule_registry(PROJECT_ROOT / "configs" / "financial_rules.yaml")
    profiles = load_financial_industry_profiles(
        PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml"
    )
    all_industries = {profile.profile_id for profile in profiles.profiles}
    high_identities = [
        rule for rule in rules.rules
        if rule.default_enabled and rule.output_type.value == "IDENTITY"
        and rule.severity is FinancialSeverity.HIGH
    ]
    assert len(high_identities) >= 2
    for rule in high_identities:
        assert set(rule.applicable_industries) == all_industries
        assert not rule.excluded_industries
        assert not rule.conditional_missing_periods


def test_high_severity_identity_cannot_become_optional_through_rule_config() -> None:
    rules = load_financial_rule_registry(PROJECT_ROOT / "configs" / "financial_rules.yaml")
    identity = next(rule for rule in rules.rules if rule.rule_id == "balance_sheet_identity")
    payload = identity.model_dump(mode="python")
    payload["conditional_missing_periods"] = ["QUARTERLY"]
    payload["conditional_missing_fields"] = ["TOTAL_EQUITY"]
    with pytest.raises(ValidationError, match="high-severity accounting identity"):
        type(identity).model_validate(payload)


def test_industry_profile_cannot_silently_exclude_accounting_identity() -> None:
    rules = load_financial_rule_registry(PROJECT_ROOT / "configs" / "financial_rules.yaml")
    profiles = load_financial_industry_profiles(
        PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml"
    )
    altered = profiles.model_copy(deep=True)
    bank = next(profile for profile in altered.profiles if profile.profile_id.value == "BANK")
    bank.excluded_rule_ids.append("balance_sheet_identity")
    with pytest.raises(ValueError, match="core accounting identity"):
        validate_financial_config(rules, altered)


def test_financial_audit_request_accepts_empty_facts_for_explicit_needs_info() -> None:
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=[],
    )
    assert not request.facts


def test_financial_pack_cannot_create_trading_hard_blocks() -> None:
    with pytest.raises(ValidationError, match="cannot create risk hard blocks"):
        FinancialIntegrityEvidencePack(
            audit_run_id="financial-audit:test",
            request_hash="a" * 64,
            status=RunStatus.SUCCEEDED,
            coverage_status=FinancialCoverageStatus.COMPLETE,
            company_id="000001",
            as_of=datetime(2026, 3, 21, tzinfo=UTC),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            periods=[],
            input_fact_ids=[],
            source_snapshot_ids=[],

            verified_numbers=[],
            recalculated_metrics=[],
            rule_findings=[],
            risk_level=FinancialRiskLevel.LOW,
            hard_blocks=["block-paper-account"],
            rule_versions={},
            model_versions={},
            capability_status={},
        )


def test_financial_finding_schema_rejects_fraud_assertion() -> None:
    with pytest.raises(ValidationError, match="cannot assert fraud"):
        FinancialRuleFinding(
            finding_id="finding:test",
            rule_id="rule:test",
            formula_version="1.0",
            status=FinancialFindingStatus.FLAG,
            severity=FinancialSeverity.HIGH,
            message_code="FRAUD_CONFIRMED",
            evidence_ids=["evidence:test"],
        )
