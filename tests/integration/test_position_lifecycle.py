from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from astock.evidence import EvidenceRepository
from astock.research import PositionLifecycleService, load_position_lifecycle_config
from astock.schemas import (
    DecisionReferenceStatus,
    HoldingReviewRequest,
    HoldingRuleSignal,
    IndustryBottleneckDiagnosticRequestV2,
    LifecycleCondition,
    LifecycleMetricDefinition,
    LifecycleSourceType,
    PositionAction,
    PositionPlanCreateRequest,
    ResearchMemoComposeRequestV2,
)
from tests.integration.test_research_core import _fixture, _specialist_fixture
from tests.integration.test_research_diagnostics import (
    _diagnostics,
    _industry_contract,
    _route,
    _structured_memo,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_THESIS = "Private synthetic lifecycle thesis that must stay out of SQLite."
_PRIVATE_RULE = "Private synthetic condition prose that must stay out of SQLite."


def _service_and_plan(tmp_path: Path, state):
    skills, base_case, baseline_evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-base",
    )
    diagnostics = _diagnostics(state, skills)
    route = _route(
        skills,
        base_case.base_case_id,
        skill_ids=["IndustryBottleneckSkill"],
        inputs=["industry_evidence"],
        horizon="long",
    )
    delta = diagnostics.diagnose(
        IndustryBottleneckDiagnosticRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            method_contract=_industry_contract(base_case, [baseline_evidence.evidence_id]),
        )
    )
    memo = diagnostics.compose_memo(
        ResearchMemoComposeRequestV2(
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            delta_ids=[delta.delta.delta_id],
            structured_memo=_structured_memo(delta.delta, base_case),
        )
    ).memo
    service = PositionLifecycleService(
        state,
        skills.object_store,
        load_position_lifecycle_config(PROJECT_ROOT / "configs" / "position_lifecycle.yaml"),
    )
    plan_request = PositionPlanCreateRequest(
        position_id="monitor:company:000001",
        company_id="company:000001",
        decision_id="user-decision:synthetic",
        decision_reference_status=DecisionReferenceStatus.USER_DECLARED_EXTERNAL,
        base_case_id=base_case.base_case_id,
        route_plan_id=route.route_plan_id,
        memo_id=memo.memo_id,
        as_of=base_case.as_of,
        thesis_summary=_PRIVATE_THESIS,
        entry_assumptions=["Private synthetic entry assumption."],
        holding_horizon="long",
        key_value_drivers=["Private synthetic value driver."],
        validation_metrics=[
            LifecycleMetricDefinition(
                metric_id="metric:baseline",
                name="Synthetic baseline metric",
                unit="ratio",
                evidence_ids=[baseline_evidence.evidence_id],
            )
        ],
        monitoring_sources=["price", "fundamental", "event", "manual"],
        monitoring_cadence={
            "price": "daily",
            "fundamental": "on_disclosure",
            "event": "daily",
            "manual": "on_request",
        },
        conditions=[
            LifecycleCondition(
                rule_id="exit-risk",
                signal_code="THESIS_INVALIDATED",
                action=PositionAction.EXIT,
                source_type=LifecycleSourceType.FUNDAMENTAL,
                description=f"{_PRIVATE_RULE} exit",
                hard_block=True,
            ),
            LifecycleCondition(
                rule_id="review-gap",
                signal_code="EVIDENCE_GAP_OPEN",
                action=PositionAction.REVIEW,
                source_type=LifecycleSourceType.MANUAL,
                description=f"{_PRIVATE_RULE} review",
                hard_block=True,
            ),
            LifecycleCondition(
                rule_id="trim-risk",
                signal_code="RISK_RISING",
                action=PositionAction.TRIM,
                source_type=LifecycleSourceType.PRICE,
                description=f"{_PRIVATE_RULE} trim",
            ),
            LifecycleCondition(
                rule_id="add-strength",
                signal_code="FUNDAMENTALS_STRENGTHENED",
                action=PositionAction.ADD,
                source_type=LifecycleSourceType.EVENT,
                description=f"{_PRIVATE_RULE} add",
            ),
        ],
        manual_information_needs=["Private synthetic manual information need."],
        next_review_at=base_case.as_of + timedelta(days=7),
    )
    execution = service.create_plan(plan_request)
    assert service.create_plan(plan_request) == execution
    return service, execution.plan, baseline_evidence


def _review(
    plan_id: str,
    start,
    end,
    *,
    evidence_ids: list[str] | None = None,
    changed_claim_ids: list[str] | None = None,
    invalidated_evidence_ids: list[str] | None = None,
    conflict_ids: list[str] | None = None,
    signals: list[HoldingRuleSignal] | None = None,
) -> HoldingReviewRequest:
    return HoldingReviewRequest(
        plan_id=plan_id,
        from_as_of=start,
        to_as_of=end,
        added_evidence_ids=evidence_ids or [],
        changed_claim_ids=changed_claim_ids or [],
        invalidated_evidence_ids=invalidated_evidence_ids or [],
        unresolved_conflict_ids=conflict_ids or [],
        signals=signals or [],
    )


def _signal(rule_id: str, occurred_at, evidence_ids: list[str]) -> HoldingRuleSignal:
    return HoldingRuleSignal(
        rule_id=rule_id,
        observed_value="synthetic observation",
        occurred_at=occurred_at,
        evidence_ids=evidence_ids,
    )


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


def test_incremental_lifecycle_is_contiguous_prioritized_private_and_ledger_safe(
    tmp_path: Path,
    state,
) -> None:
    service, plan, baseline_evidence = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    ledger_before = _ledger_counts(state)
    t0 = plan.as_of
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)
    t3 = t0 + timedelta(days=3)
    t4 = t0 + timedelta(days=4)
    t5 = t0 + timedelta(days=5)

    with pytest.raises(ValueError, match="not contiguous"):
        service.review(_review(plan.plan_id, t0 + timedelta(hours=1), t1))
    with pytest.raises(ValueError, match="unknown monitoring rule"):
        service.review(
            _review(
                plan.plan_id,
                t0,
                t1,
                signals=[_signal("unknown-rule", t0 + timedelta(hours=1), [])],
            )
        )

    _, _, _, future_evidence, _ = _fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-future",
        available_at=t1 + timedelta(hours=12),
    )
    with pytest.raises(ValueError, match="outside the incremental window"):
        service.review(_review(plan.plan_id, t0, t1, evidence_ids=[future_evidence.evidence_id]))
    foreign_evidence = future_evidence.model_copy(
        update={
            "evidence_id": "evidence:position-lifecycle-foreign-company",
            "available_to_system_at": t0 + timedelta(hours=12),
            "entity_ids": ["company:999999"],
        }
    )
    EvidenceRepository(state).register_evidence(foreign_evidence)
    with pytest.raises(ValueError, match="another company"):
        service.review(_review(plan.plan_id, t0, t1, evidence_ids=[foreign_evidence.evidence_id]))

    hold = service.review(_review(plan.plan_id, t0, t1))
    assert hold.proposal.action is PositionAction.HOLD
    assert hold.review.thesis_strength_change == "UNCHANGED"
    assert hold.proposal.requires_user_confirmation

    _, _, add_bundle, add_evidence, add_available = _fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-add",
        available_at=t1 + timedelta(hours=12),
    )
    add = service.review(
        _review(
            plan.plan_id,
            t1,
            t2,
            evidence_ids=[add_evidence.evidence_id],
            changed_claim_ids=[add_bundle.claim.claim_id],
            signals=[
                _signal(
                    "add-strength",
                    add_available + timedelta(seconds=1),
                    [add_evidence.evidence_id],
                )
            ],
        )
    )
    assert add.proposal.action is PositionAction.ADD
    assert add.review.thesis_strength_change == "STRENGTHENED"

    _, _, _, trim_evidence, trim_available = _fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-trim",
        available_at=t2 + timedelta(hours=12),
    )
    trim = service.review(
        _review(
            plan.plan_id,
            t2,
            t3,
            evidence_ids=[trim_evidence.evidence_id],
            signals=[
                _signal(
                    "add-strength",
                    trim_available,
                    [trim_evidence.evidence_id],
                ),
                _signal(
                    "trim-risk",
                    trim_available,
                    [trim_evidence.evidence_id],
                ),
            ],
        )
    )
    assert trim.proposal.action is PositionAction.TRIM

    _, _, conflict_bundle, _, conflict_available = _fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-conflict",
        conflict=True,
        available_at=t3 + timedelta(hours=12),
    )
    assert conflict_bundle.conflict is not None
    conflict_evidence_ids = sorted(link.evidence_id for link in conflict_bundle.links)
    review = service.review(
        _review(
            plan.plan_id,
            t3,
            t4,
            evidence_ids=conflict_evidence_ids,
            changed_claim_ids=[conflict_bundle.claim.claim_id],
            invalidated_evidence_ids=[baseline_evidence.evidence_id],
            conflict_ids=[conflict_bundle.conflict.conflict_id],
            signals=[_signal("add-strength", conflict_available, [])],
        )
    )
    assert review.proposal.action is PositionAction.REVIEW
    assert set(review.review.hard_blocks) == {
        "ADD_SUPPORTING_EVIDENCE_MISSING",
        "BASELINE_EVIDENCE_INVALIDATED",
        "EVIDENCE_CONFLICT_REQUIRES_REVIEW",
    }

    _, _, _, exit_evidence, exit_available = _fixture(
        tmp_path,
        state,
        suffix="position-lifecycle-exit",
        available_at=t4 + timedelta(hours=12),
    )
    exit_request = _review(
        plan.plan_id,
        t4,
        t5,
        evidence_ids=[exit_evidence.evidence_id],
        signals=[
            _signal(rule_id, exit_available, [exit_evidence.evidence_id])
            for rule_id in ("add-strength", "trim-risk", "review-gap", "exit-risk")
        ],
    )
    exit_execution = service.review(exit_request)
    assert exit_execution.proposal.action is PositionAction.EXIT
    assert service.review(exit_request) == exit_execution
    assert exit_execution.proposal.requires_user_confirmation
    assert _ledger_counts(state) == ledger_before
    assert service.audit(plan.position_id)["status"] == "PASS"

    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM artifact_registry WHERE artifact_id=?",
            (f"PositionActionProposal:{hold.proposal.proposal_id}",),
        )
    damaged_audit = service.audit(plan.position_id)
    assert damaged_audit["status"] == "PARTIAL"
    assert damaged_audit["finding_codes"] == ["REVIEW_ARTIFACT_MISMATCH"]

    with state.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        safe_metadata = "\n".join(
            str(value)
            for table in (
                "position_lifecycle_rule_index",
                "position_monitoring_plan_index",
                "holding_evidence_update_index",
                "holding_review_index",
                "position_action_proposal_index",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert _PRIVATE_THESIS not in safe_metadata
    assert _PRIVATE_RULE not in safe_metadata


def test_holding_review_recovers_when_review_index_precedes_proposal(
    tmp_path: Path,
    state,
    monkeypatch,
) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    request = _review(
        plan.plan_id,
        plan.as_of,
        plan.as_of + timedelta(days=1),
    )
    original_register = service.repository.register_proposal

    def simulate_crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before proposal index")

    monkeypatch.setattr(service.repository, "register_proposal", simulate_crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        service.review(request)
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM holding_review_index").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM position_action_proposal_index").fetchone()[0]
            == 0
        )

    monkeypatch.setattr(service.repository, "register_proposal", original_register)
    recovered = service.review(request)
    assert recovered.proposal.action is PositionAction.HOLD
    assert recovered.proposal.requires_user_confirmation
    assert service.audit(plan.position_id)["status"] == "PASS"
