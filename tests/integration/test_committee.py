from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from astock.committee import CommitteeService, load_committee_rules
from astock.core.codex_runs import CodexRunService, registered_committee_artifact_types
from astock.financial_integrity import FinancialIntegrityService
from astock.schemas import (
    CommitteeAccessPolicy,
    CommitteeAssessment,
    CommitteeCoverageMetrics,
    CommitteeDecisionRequest,
    CommitteeDecisionScope,
    CommitteeEntryOrderType,
    CommitteeNarrativeMode,
    CommitteeProtocolDraft,
    CommitteeProtocolStatus,
    CommitteeRatioRange,
    CommitteeRuleConfig,
    CommitteeVerdict,
    CounterCaseDraft,
    FinancialAuditRequest,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FrozenEvidencePack,
    HoldingReviewPack,
    PositionMonitoringPlan,
    ResearchMemoArtifact,
    SpecialistRoutePlan,
)
from tests.helpers import FINANCIAL_GOLDEN_VALUES, make_financial_facts
from tests.integration.test_phase4_integration import (
    _complete_chain,
    _draft,
    _strict_run,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_PROTOCOL = "private committee protocol prose that must stay out of SQLite"


def _service_and_request(
    tmp_path: Path,
    state,
    *,
    scope: CommitteeDecisionScope = CommitteeDecisionScope.NEW_CANDIDATE,
    assessment_updates: dict[str, object] | None = None,
    counter_case: CounterCaseDraft | None = None,
    rules: CommitteeRuleConfig | None = None,
    severe_financial: bool = False,
):
    lifecycle, _, artifacts, artifact_ids = _complete_chain(tmp_path, state)
    effective_rules = rules or load_committee_rules(
        PROJECT_ROOT / "configs" / "committee_rules.yaml"
    )
    committee = CommitteeService(
        state,
        lifecycle.object_store,
        effective_rules,
    )
    evidence_pack = artifacts["FrozenEvidencePack"]
    memo = artifacts["ResearchMemoArtifact"]
    route = artifacts["SpecialistRoutePlan"]
    assert isinstance(evidence_pack, FrozenEvidencePack)
    assert isinstance(memo, ResearchMemoArtifact)
    assert isinstance(route, SpecialistRoutePlan)
    evidence_ids = sorted(memo.evidence_ids)
    as_of = max(
        memo.as_of + timedelta(days=1),
        effective_rules.effective_from + timedelta(days=1),
    )
    skill_versions = {
        item.skill_id: item.skill_version for item in route.selected
    }
    skill_versions["ResearchMemoComposer"] = "research-memo-composer-v1"
    if scope is not CommitteeDecisionScope.NEW_CANDIDATE:
        plan = artifacts["PositionMonitoringPlan"]
        review = artifacts["HoldingReviewPack"]
        assert isinstance(plan, PositionMonitoringPlan)
        assert isinstance(review, HoldingReviewPack)
        as_of = max(
            review.as_of,
            effective_rules.effective_from + timedelta(days=1),
        )
        skill_versions.update(plan.skill_versions)
        assert plan.rules_version is not None
        skill_versions["PositionLifecycleRules"] = plan.rules_version
    financial_values = dict(FINANCIAL_GOLDEN_VALUES)
    if severe_financial:
        financial_values[FinancialFieldCode.TOTAL_ASSETS] = Decimal("1100")
    financial = FinancialIntegrityService(
        state,
        lifecycle.object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=(
            PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml"
        ),
    ).run(
        FinancialAuditRequest(
            company_id=memo.company_id,
            as_of=as_of,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            facts=make_financial_facts(
                state,
                lifecycle.object_store,
                company_id=memo.company_id,
                values=financial_values,
            ),
        )
    ).pack
    artifacts["FinancialIntegrityEvidencePack"] = financial
    artifact_ids["FinancialIntegrityEvidencePack"] = (
        f"FinancialIntegrityEvidencePack:{financial.audit_run_id}"
    )
    selected_types = [
        "FrozenEvidencePack",
        "BaseCasePack",
        "SpecialistRoutePlan",
        "SpecialistDelta",
        "SpecialistDiagnosticReport",
        "ResearchMemoArtifact",
        "FinancialIntegrityEvidencePack",
    ]
    if scope is not CommitteeDecisionScope.NEW_CANDIDATE:
        selected_types.extend(
            [
                "PositionMonitoringPlan",
                "HoldingEvidenceUpdate",
                "HoldingReviewPack",
                "PositionActionProposal",
            ]
        )
    references = sorted(
        (committee.resolve_reference(artifact_ids[item]) for item in selected_types),
        key=lambda item: item.artifact_id,
    )
    assessment_payload: dict[str, object] = {
        "schema_version": "1.0",
        "created_at": as_of,
        "company_id": memo.company_id,
        "scope": scope,
        "as_of": as_of,
        "expected_return_range": CommitteeRatioRange(
            lower=Decimal("0.12"),
            upper=Decimal("0.25"),
            evidence_ids=evidence_ids,
            created_at=as_of,
        ),
        "downside_range": CommitteeRatioRange(
            lower=Decimal("-0.20"),
            upper=Decimal("-0.05"),
            evidence_ids=evidence_ids,
            created_at=as_of,
        ),
        "confidence": Decimal("0.80"),
        "coverage": CommitteeCoverageMetrics(
            data_coverage=Decimal("1"),
            evidence_coverage=Decimal("1"),
            specialist_coverage=Decimal("1"),
            pit_coverage=Decimal("1"),
            liquidity_score=Decimal("1"),
            evidence_ids=evidence_ids,
            created_at=as_of,
        ),
        "tradable": True,
        "market_data_quality_pass": True,
        "current_position": (
            Decimal("0")
            if scope is CommitteeDecisionScope.NEW_CANDIDATE
            else Decimal("0.03")
        ),
        "requested_position": Decimal("0.04"),
        "holding_horizon_days": 180,
        "review_at": as_of + timedelta(days=7),
        "support_evidence_ids": evidence_ids,
        "signal_evidence_ids": {},
        "optional_narrative_requested": False,
        "estimated_provider_cost_cny": Decimal("0"),
        "protocol": CommitteeProtocolDraft(
            strategy_id="fixture-value-strategy",
            skill_versions=dict(sorted(skill_versions.items())),
            earliest_executable_time=as_of + timedelta(days=1),
            entry_rule=f"{_PRIVATE_PROTOCOL}: enter only after the signal",
            entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
            position_size_rule=f"{_PRIVATE_PROTOCOL}: obey frozen max position",
            price_stop_rule=f"{_PRIVATE_PROTOCOL}: price review",
            volatility_stop_rule=f"{_PRIVATE_PROTOCOL}: volatility review",
            trailing_stop_rule=f"{_PRIVATE_PROTOCOL}: trailing review",
            time_stop_rule=f"{_PRIVATE_PROTOCOL}: time review",
            thesis_invalidation_rule=f"{_PRIVATE_PROTOCOL}: thesis invalidation",
            take_profit_rule=f"{_PRIVATE_PROTOCOL}: valuation review",
            review_events=["ANNUAL_REPORT", "MATERIAL_DISCLOSURE"],
            max_holding_period_days=730,
            cost_model_version="cn-equity-cost-v1",
            fill_model_version="paper-fill-v1",
            evidence_snapshot_id=evidence_pack.pack_id,
            evidence_ids=evidence_ids,
            created_at=as_of,
        ),
    }
    assessment_payload.update(assessment_updates or {})
    assessment = CommitteeAssessment.model_validate(assessment_payload)
    request = CommitteeDecisionRequest(
        artifact_references=references,
        assessment=assessment,
        counter_case=counter_case,
        access_policy=CommitteeAccessPolicy(
            frozen_artifact_hashes=sorted(item.object_sha256 for item in references),
            created_at=as_of,
        ),
        created_at=as_of,
    )
    return committee, request, artifacts


def _ledger_counts(state) -> tuple[int, ...]:
    with state.connect() as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "ledger_account",
                "ledger_entry",
                "order_record",
                "fill",
                "position",
                "position_settlement",
            )
        )


def test_committee_plan_is_read_only_and_decision_is_deterministic_auditable_and_private(
    tmp_path: Path,
    state,
) -> None:
    committee, request, _ = _service_and_request(tmp_path, state)
    with state.connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM committee_decision_index"
        ).fetchone()[0]
    plan = committee.plan(request)
    assert plan.verdict is CommitteeVerdict.PAPER_ELIGIBLE
    assert not plan.persistent_writes
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM committee_decision_index"
        ).fetchone()[0] == before

    ledger_before = _ledger_counts(state)
    first = committee.decide(request)
    second = committee.decide(request)
    assert first == second
    clone_payload = request.model_dump(mode="python")

    def shift_created_at(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: (
                    request.assessment.as_of + timedelta(hours=6)
                    if key == "created_at"
                    else shift_created_at(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [shift_created_at(child) for child in value]
        return value

    timestamp_variant = CommitteeDecisionRequest.model_validate(
        shift_created_at(clone_payload)
    )
    assert committee.decide(timestamp_variant) == first
    assert first.decision.verdict is CommitteeVerdict.PAPER_ELIGIBLE
    assert first.protocol.protocol_status is CommitteeProtocolStatus.ACTIVE
    assert first.protocol.requires_user_confirmation
    assert not first.protocol.broker_execution_allowed
    assert not first.protocol.ledger_write_allowed
    assert committee.audit(first.decision.decision_id)["status"] == "PASS"
    assert committee.status(decision_id=first.decision.decision_id)["status"] == "AVAILABLE"
    assert _ledger_counts(state) == ledger_before

    with state.connect() as connection:
        safe_sqlite = "\n".join(
            str(value)
            for table in (
                "committee_assessment_index",
                "committee_bundle_index",
                "committee_bundle_input_index",
                "committee_decision_index",
                "committee_trade_protocol_index",
                "committee_investigation_task_index",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert _PRIVATE_PROTOCOL not in safe_sqlite
    assert str(tmp_path) not in safe_sqlite


def test_counter_case_is_selective_and_missing_case_creates_needs_info_tasks(
    tmp_path: Path,
    state,
) -> None:
    committee, request, artifacts = _service_and_request(
        tmp_path,
        state,
        assessment_updates={"requested_position": Decimal("0.06")},
    )
    missing = committee.decide(request)
    assert missing.decision.verdict is CommitteeVerdict.NEEDS_INFO
    assert "COUNTER_CASE_REQUIRED" in missing.decision.hard_blocks
    assert missing.investigation_tasks
    assert missing.protocol.protocol_status is CommitteeProtocolStatus.BLOCKED
    for task in missing.investigation_tasks:
        assert committee.task_status(task.task_id)["status"] == "OPEN"

    evidence_pack = artifacts["FrozenEvidencePack"]
    memo = artifacts["ResearchMemoArtifact"]
    assert isinstance(evidence_pack, FrozenEvidencePack)
    assert isinstance(memo, ResearchMemoArtifact)
    evidence_ids = sorted(memo.evidence_ids)
    counter_case = CounterCaseDraft(
        challenged_claim_ids=sorted(evidence_pack.claim_ids),
        alternative_explanations=["SYNTHETIC_ALTERNATIVE_EXPLANATION"],
        downside_paths=["SYNTHETIC_DOWNSIDE_PATH"],
        missing_evidence_codes=[],
        evidence_ids=evidence_ids,
        estimated_tokens=200,
        estimated_minutes=15,
        estimated_cost_cny=Decimal("0"),
        created_at=request.assessment.as_of,
    )
    no_trigger_assessment = CommitteeAssessment.model_validate(
        {
            **request.assessment.model_dump(mode="python"),
            "requested_position": Decimal("0.04"),
        }
    )
    no_trigger_request = CommitteeDecisionRequest(
        artifact_references=request.artifact_references,
        assessment=no_trigger_assessment,
        counter_case=counter_case,
        access_policy=request.access_policy,
        created_at=request.assessment.as_of,
    )
    with pytest.raises(ValueError, match="without a configured trigger"):
        committee.plan(no_trigger_request)
    completed_request = CommitteeDecisionRequest(
        **request.model_dump(
            mode="python",
            exclude={"counter_case", "schema_version", "created_at"},
        ),
        schema_version=request.schema_version,
        counter_case=counter_case,
        created_at=request.assessment.as_of,
    )
    completed = committee.decide(completed_request)
    assert completed.counter_case is not None
    assert completed.decision.verdict is CommitteeVerdict.PAPER_ELIGIBLE
    assert not completed.investigation_tasks
    assert committee.audit(completed.decision.decision_id)["status"] == "PASS"

    codex = CodexRunService(tmp_path / "codex-committee", committee.object_store, state)
    strict_outputs = {
        "CounterCasePack": completed.counter_case,
        "DecisionPack": completed.decision,
        "TradeProtocol": completed.protocol,
    }
    assert sorted(strict_outputs) == registered_committee_artifact_types()
    strict_ids = {
        "CounterCasePack": f"CounterCasePack:{completed.counter_case.counter_case_id}",
        "DecisionPack": f"DecisionPack:{completed.decision.decision_id}",
        "TradeProtocol": f"TradeProtocol:{completed.protocol.protocol_id}",
    }
    for artifact_type in registered_committee_artifact_types():
        artifact = strict_outputs[artifact_type]
        assert artifact is not None
        run = _strict_run(codex, strict_ids[artifact_type])
        codex.stage_draft(
            run.run_id,
            _draft(
                tmp_path / f"strict-{artifact_type}.json",
                artifact_type,
                artifact,
            ),
        )
        report = codex.import_draft(run.run_id)
        assert report.valid, report.errors
        assert codex.audit(run.run_id)["status"] == "PASS"


def test_hard_blocks_and_budget_degradation_cannot_be_overridden_by_narrative(
    tmp_path: Path,
    state,
) -> None:
    committee, request, _ = _service_and_request(
        tmp_path,
        state,
        assessment_updates={
            "tradable": False,
            "signal_evidence_ids": {
                "tradable": [
                    "evidence:placeholder"
                ]
            },
            "optional_narrative_requested": True,
        },
    )
    evidence_id = request.assessment.support_evidence_ids[0]
    blocked_assessment = CommitteeAssessment.model_validate(
        {
            **request.assessment.model_dump(mode="python"),
            "signal_evidence_ids": {"tradable": [evidence_id]},
        }
    )
    blocked_request = CommitteeDecisionRequest(
        artifact_references=request.artifact_references,
        assessment=blocked_assessment,
        access_policy=request.access_policy,
        created_at=request.assessment.as_of,
    )
    blocked = committee.decide(blocked_request)
    assert blocked.decision.verdict is CommitteeVerdict.REJECT
    assert "NOT_TRADABLE" in blocked.decision.hard_blocks
    assert not blocked.decision.narrative_can_override

    base_rules = load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml")
    tiny_rules = CommitteeRuleConfig.model_validate(
        {
            **base_rules.model_dump(mode="python"),
            "rules_version": "committee-rules-tiny-context-v1",
            "max_context_bytes": 1,
            "max_estimated_text_tokens": 1,
        }
    )
    tiny_committee = CommitteeService(state, committee.object_store, tiny_rules)
    tiny_plan = tiny_committee.plan(blocked_request)
    assert tiny_plan.verdict is CommitteeVerdict.REJECT
    assert tiny_plan.context_budget.narrative_mode is CommitteeNarrativeMode.BUDGET_EXCEEDED
    assert tiny_plan.context_budget.context.expected_api_calls == 0
    assert tiny_plan.context_budget.context.expected_browser_steps == 0
    assert not tiny_plan.context_budget.context.full_documents_to_open


def test_financial_integrity_is_required_and_severe_identity_failure_rejects(
    tmp_path: Path,
    state,
) -> None:
    committee, request, _ = _service_and_request(
        tmp_path,
        state,
        severe_financial=True,
    )
    without_financial_refs = [
        item
        for item in request.artifact_references
        if item.artifact_type != "FinancialIntegrityEvidencePack"
    ]
    without_financial = CommitteeDecisionRequest(
        artifact_references=without_financial_refs,
        assessment=request.assessment,
        access_policy=CommitteeAccessPolicy(
            frozen_artifact_hashes=sorted(
                item.object_sha256 for item in without_financial_refs
            ),
            created_at=request.assessment.as_of,
        ),
        created_at=request.assessment.as_of,
    )
    missing = committee.decide(without_financial)
    assert missing.decision.verdict is CommitteeVerdict.NEEDS_INFO
    assert "FINANCIAL_INTEGRITY_NOT_RUN" in missing.decision.hard_blocks
    financial_task = next(
        task
        for task in missing.investigation_tasks
        if task.reason_code == "FINANCIAL_INTEGRITY_NOT_RUN"
    )
    with pytest.raises(ValueError, match="new frozen resolution"):
        committee.resolve_task(
            financial_task.task_id,
            without_financial_refs[0].artifact_id,
        )
    financial_reference = next(
        item
        for item in request.artifact_references
        if item.artifact_type == "FinancialIntegrityEvidencePack"
    )
    resolved = committee.resolve_task(
        financial_task.task_id,
        financial_reference.artifact_id,
    )
    assert resolved["status"] == "RESOLVED"
    assert committee.resolve_task(
        financial_task.task_id,
        financial_reference.artifact_id,
    ) == resolved
    assert committee.audit(missing.decision.decision_id)["status"] == "PASS"

    severe = committee.decide(request)
    assert severe.decision.verdict is CommitteeVerdict.REJECT
    assert "FINANCIAL_INTEGRITY_SEVERE" in severe.decision.hard_blocks
    assert not severe.decision.narrative_can_override


def test_position_verdicts_recovery_and_audit_detect_index_tampering(
    tmp_path: Path,
    state,
    monkeypatch,
) -> None:
    committee, request, _ = _service_and_request(
        tmp_path,
        state,
        scope=CommitteeDecisionScope.PAPER_POSITION,
        assessment_updates={"requested_position": Decimal("0.03")},
    )
    ledger_before = _ledger_counts(state)
    original_register_protocol = committee.repository.register_protocol

    def crash_before_protocol(*args, **kwargs):
        raise RuntimeError("synthetic crash before protocol index")

    monkeypatch.setattr(committee.repository, "register_protocol", crash_before_protocol)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        committee.decide(request)
    monkeypatch.setattr(committee.repository, "register_protocol", original_register_protocol)
    recovered = committee.recover(request)
    assert recovered["audit_status"] == "PASS"
    decision_id = str(recovered["decision_id"])
    hold = committee.repository.get_decision(decision_id)
    assert hold is not None
    assert hold.verdict is CommitteeVerdict.PAPER_HOLD

    evidence_id = request.assessment.support_evidence_ids[0]
    exit_assessment = CommitteeAssessment.model_validate(
        {
            **request.assessment.model_dump(mode="python"),
            "thesis_invalidated": True,
            "signal_evidence_ids": {"thesis_invalidated": [evidence_id]},
        }
    )
    exit_request = CommitteeDecisionRequest(
        artifact_references=request.artifact_references,
        assessment=exit_assessment,
        access_policy=request.access_policy,
        created_at=request.assessment.as_of,
    )
    exit_execution = committee.decide(exit_request)
    assert exit_execution.decision.verdict is CommitteeVerdict.PAPER_EXIT
    assert exit_execution.decision.max_position == 0
    assert _ledger_counts(state) == ledger_before

    with state.transaction() as connection:
        connection.execute(
            "UPDATE committee_bundle_input_index SET artifact_role='STATE' "
            "WHERE bundle_id=? AND artifact_type='BaseCasePack'",
            (exit_execution.bundle.bundle_id,),
        )
    audit = committee.audit(exit_execution.decision.decision_id)
    assert audit["status"] == "PARTIAL"
    finding_codes = audit["finding_codes"]
    assert isinstance(finding_codes, list)
    assert "BUNDLE_INPUT_INDEX_MISMATCH" in finding_codes
