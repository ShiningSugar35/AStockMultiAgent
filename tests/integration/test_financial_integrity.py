from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.financial_integrity import FinancialIntegrityService
from astock.schemas import (
    EvidenceGrade,
    FinancialAuditRequest,
    FinancialCoverageStatus,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialGapType,
    FinancialIndustryProfile,
    FinancialRiskLevel,
    FinancialUnit,
    RunStatus,
)
from tests.helpers import FINANCIAL_GOLDEN_VALUES, make_financial_facts, make_financial_request

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(state, object_store) -> FinancialIntegrityService:
    return FinancialIntegrityService(
        state,
        object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    )


def test_recorded_golden_case_reconciles_and_recalculates(state, object_store) -> None:
    fixture = json.loads(
        (PROJECT_ROOT / "tests" / "fixtures" / "financial" / "industrial_annual_2025.json")
        .read_text(encoding="utf-8")
    )
    values = {
        FinancialFieldCode(code): Decimal(value) for code, value in fixture["values"].items()
    }
    request = FinancialAuditRequest(
        company_id=fixture["company_id"],
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=make_financial_facts(state, object_store, values=values),
    )
    service = _service(state, object_store)
    first = service.run(request)
    assert first.pack.status is RunStatus.SUCCEEDED
    assert first.pack.coverage_status is FinancialCoverageStatus.COMPLETE
    assert first.pack.risk_level is FinancialRiskLevel.LOW
    assert len(first.pack.verified_numbers) == len(values)
    assert not first.pack.evidence_gaps
    actual = {metric.rule_id: metric.value for metric in first.pack.recalculated_metrics}
    assert actual == {
        rule_id: Decimal(expected)
        for rule_id, expected in fixture["expected_metrics"].items()
    }
    assert all(finding.evidence_ids for finding in first.pack.rule_findings)
    assert first.pack.capability_status["pyod"] == "DISABLED_UNTIL_M3_3"

    repeated = service.run(request)
    assert repeated.reused_existing
    assert repeated.artifact_hash == first.artifact_hash
    assert repeated.pack == first.pack
    assert service.repository.attempt_count(first.pack.audit_run_id) == 1
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM financial_audit_run").fetchone()[0] == 1


def test_identity_mismatch_is_flagged_without_accusing_fraud(state, object_store) -> None:
    values = dict(FINANCIAL_GOLDEN_VALUES)
    values[FinancialFieldCode.TOTAL_ASSETS] = Decimal("1100")
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=make_financial_facts(state, object_store, values=values),
    )
    pack = _service(state, object_store).run(request).pack
    balance = next(
        finding for finding in pack.rule_findings if finding.rule_id == "balance_sheet_identity"
    )
    assert balance.status is FinancialFindingStatus.FLAG
    assert pack.risk_level is FinancialRiskLevel.HIGH
    assert pack.status is RunStatus.SUCCEEDED
    serialized = json.dumps(pack.model_dump(mode="json"), ensure_ascii=False).lower()
    assert "fraud" not in serialized
    assert "造假" not in serialized


def test_missing_fact_is_not_fabricated_and_creates_manual_task(state, object_store) -> None:
    request = make_financial_request(state, object_store)
    request = request.model_copy(
        update={
            "facts": [
                fact
                for fact in request.facts
                if fact.field_code is not FinancialFieldCode.INVENTORY
            ]
        }
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.PARTIAL
    assert all(
        number.field_code is not FinancialFieldCode.INVENTORY
        for number in pack.verified_numbers
    )
    missing = [gap for gap in pack.evidence_gaps if gap.gap_type is FinancialGapType.MISSING_FACT]
    assert any(FinancialFieldCode.INVENTORY in gap.field_codes for gap in missing)
    assert any(task.reason_code == "MISSING_FACT" for task in pack.manual_tasks)
    assert not any(
        metric.rule_id == "inventory_operating_cost_ratio"
        for metric in pack.recalculated_metrics
    )


def test_empty_request_returns_auditable_needs_info_pack(state, object_store) -> None:
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=[],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.BLOCKED
    assert not pack.verified_numbers
    assert pack.evidence_gaps
    assert pack.manual_tasks


def test_equivalent_currency_units_normalize_without_false_conflict(state, object_store) -> None:
    ten_thousand = make_financial_facts(
        state, object_store, source_suffix="ten-thousand"
    )
    cny_values = {
        field: value * Decimal("10000")
        for field, value in FINANCIAL_GOLDEN_VALUES.items()
    }
    cny = make_financial_facts(
        state,
        object_store,
        source_suffix="cny",
        values=cny_values,
        unit=FinancialUnit.CNY,
    )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=[*ten_thousand, *cny],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.SUCCEEDED
    assert not pack.document_conflicts
    assert len(pack.verified_numbers) == len(FINANCIAL_GOLDEN_VALUES)
    assets = next(
        number
        for number in pack.verified_numbers
        if number.field_code is FinancialFieldCode.TOTAL_ASSETS
    )
    assert assets.value_cny == Decimal("10000000")
    assert len(assets.fact_ids) == 2


def test_zero_denominator_is_an_evidence_backed_limitation_not_a_manual_gap(
    state, object_store
) -> None:
    values = dict(FINANCIAL_GOLDEN_VALUES)
    values[FinancialFieldCode.NET_PROFIT_INCOME] = Decimal("0")
    values[FinancialFieldCode.NET_PROFIT_CASH_FLOW] = Decimal("0")
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=make_financial_facts(state, object_store, values=values),
    )
    pack = _service(state, object_store).run(request).pack
    finding = next(
        item for item in pack.rule_findings if item.rule_id == "cfo_net_profit_ratio"
    )
    assert finding.status is FinancialFindingStatus.INSUFFICIENT_DATA
    assert finding.evidence_ids
    assert not finding.evidence_gap_ids
    assert pack.status is RunStatus.SUCCEEDED
    assert not pack.manual_tasks


def test_bank_profile_explicitly_excludes_industrial_scores(state, object_store) -> None:
    request = make_financial_request(
        state, object_store, industry_profile=FinancialIndustryProfile.BANK
    )
    pack = _service(state, object_store).run(request).pack
    findings = {finding.rule_id: finding for finding in pack.rule_findings}
    assert findings["beneish_m_score"].status is FinancialFindingStatus.NOT_APPLICABLE
    assert findings["altman_z_score"].status is FinancialFindingStatus.NOT_APPLICABLE
    assert not pack.evidence_gaps
    assert pack.status is RunStatus.SUCCEEDED


def test_future_snapshot_pit_and_evidence_are_excluded(state, object_store) -> None:
    future = datetime(2026, 4, 20, tzinfo=UTC)
    facts = make_financial_facts(
        state,
        object_store,
        source_suffix="future",
        published_at=future,
    )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert pack.coverage_status is FinancialCoverageStatus.BLOCKED
    assert not pack.verified_numbers
    assert not pack.source_snapshot_ids
    assert not pack.pit_ids
    gap_types = {gap.gap_type for gap in pack.evidence_gaps}
    assert FinancialGapType.SNAPSHOT_NOT_AVAILABLE in gap_types
    assert FinancialGapType.PIT_NOT_USABLE in gap_types
    assert FinancialGapType.EVIDENCE_NOT_USABLE in gap_types
    assert all(not finding.evidence_ids for finding in pack.rule_findings)


def test_community_lead_cannot_be_used_as_a_reported_financial_fact(
    state, object_store
) -> None:
    facts = make_financial_facts(
        state,
        object_store,
        source_suffix="community-lead",
        evidence_grade=EvidenceGrade.COMMUNITY_LEAD,
    )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert not pack.verified_numbers
    assert any(
        gap.gap_type is FinancialGapType.UNSUITABLE_EVIDENCE_GRADE
        for gap in pack.evidence_gaps
    )


def test_advanced_score_requires_real_period_history_without_fake_output(
    state, object_store
) -> None:
    request = make_financial_request(state, object_store).model_copy(
        update={"requested_rule_ids": ["beneish_m_score"]}
    )
    pack = _service(state, object_store).run(request).pack
    beneish = next(
        finding for finding in pack.rule_findings if finding.rule_id == "beneish_m_score"
    )
    assert beneish.status is FinancialFindingStatus.INSUFFICIENT_DATA
    assert beneish.actual_value is None
    assert any(
        gap.gap_type is FinancialGapType.INSUFFICIENT_PERIODS
        and "beneish_m_score" in gap.related_rule_ids
        for gap in pack.evidence_gaps
    )
    assert not any(task.reason_code == "CAPABILITY_DISABLED" for task in pack.manual_tasks)


def test_conflicting_official_values_are_escalated_not_selected(state, object_store) -> None:
    first = make_financial_facts(state, object_store, source_suffix="original")
    revised_values = dict(FINANCIAL_GOLDEN_VALUES)
    revised_values[FinancialFieldCode.TOTAL_ASSETS] = Decimal("1100")
    second = make_financial_facts(
        state,
        object_store,
        source_suffix="conflict",
        values=revised_values,
    )
    request = FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=[*first, *second],
    )
    pack = _service(state, object_store).run(request).pack
    assert pack.status is RunStatus.NEEDS_INFO
    assert len(pack.document_conflicts) == 1
    conflict = pack.document_conflicts[0]
    assert conflict.field_code is FinancialFieldCode.TOTAL_ASSETS
    assert set(conflict.normalized_values_cny) == {
        Decimal("10000000"),
        Decimal("11000000"),
    }
    assert not any(
        number.field_code is FinancialFieldCode.TOTAL_ASSETS
        for number in pack.verified_numbers
    )


def test_interrupted_run_is_recovered_with_same_identity(state, object_store) -> None:
    service = _service(state, object_store)
    request = make_financial_request(state, object_store)
    semantic = service._semantic_request(request)
    request_hash = sha256_bytes(canonical_json_bytes(semantic))
    request_object = object_store.put_json(semantic)
    audit_run_id = service._audit_run_id(request_hash)
    service.repository.ensure_run(
        audit_run_id=audit_run_id,
        request_hash=request_hash,
        company_id=request.company_id,
        as_of=request.as_of.isoformat(),
        industry_profile=request.industry_profile.value,
        rule_registry_version=service.rule_registry.registry_version,
        industry_profile_version=service.profile_registry.registry_version,
        request_object_hash=request_object.sha256,
    )
    abandoned_attempt = service.repository.start_attempt(audit_run_id)

    execution = service.run(request)
    assert execution.pack.audit_run_id == audit_run_id
    assert service.repository.attempt_count(audit_run_id) == 2
    with state.connect() as connection:
        abandoned = connection.execute(
            "SELECT ended_at,error_class,retryable FROM financial_audit_attempt "
            "WHERE attempt_id=?",
            (abandoned_attempt,),
        ).fetchone()
    assert abandoned["ended_at"] is not None
    assert abandoned["error_class"] == "INTERRUPTED_RECOVERED"
    assert abandoned["retryable"] == 1
