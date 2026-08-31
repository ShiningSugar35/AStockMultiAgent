from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from astock.schemas import (
    HoldingEventSeverity,
    HoldingReviewRequest,
    HoldingTargetBandInput,
    PositionAction,
)
from tests.integration.test_position_lifecycle import _ledger_counts, _service_and_plan
from tests.integration.test_research_core import _fixture


def _event_request(
    plan_id: str,
    start,
    end,
    *,
    severity: HoldingEventSeverity,
    evidence_ids: list[str] | None = None,
    portfolio_effect_codes: list[str] | None = None,
    target_band: HoldingTargetBandInput | None = None,
) -> HoldingReviewRequest:
    return HoldingReviewRequest(
        plan_id=plan_id,
        from_as_of=start,
        to_as_of=end,
        added_evidence_ids=evidence_ids or [],
        changed_claim_ids=[],
        invalidated_evidence_ids=[],
        unresolved_conflict_ids=[],
        signals=[],
        event_severity=severity,
        portfolio_effect_codes=portfolio_effect_codes or [],
        target_band=target_band,
    )


def test_unverified_event_lead_cannot_directly_change_position(tmp_path: Path, state) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    ledger_before = _ledger_counts(state)
    result = service.review(
        _event_request(
            plan.plan_id,
            plan.as_of,
            plan.as_of + timedelta(days=1),
            severity=HoldingEventSeverity.UNVERIFIED_LEAD,
        )
    )
    assert result.proposal.action is PositionAction.REVIEW
    assert "UNVERIFIED_LEAD_REQUIRES_EVIDENCE" in result.proposal.hard_blocks
    assert result.proposal.event_severity == HoldingEventSeverity.UNVERIFIED_LEAD.value
    assert _ledger_counts(state) == ledger_before


def test_material_event_without_new_evidence_is_review_only(tmp_path: Path, state) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    result = service.review(
        _event_request(
            plan.plan_id,
            plan.as_of,
            plan.as_of + timedelta(days=1),
            severity=HoldingEventSeverity.THESIS_INVALIDATING,
        )
    )
    assert result.proposal.action is PositionAction.REVIEW
    assert "MATERIAL_EVENT_EVIDENCE_REQUIRED" in result.proposal.hard_blocks


def test_verified_weakening_event_can_trim_and_preserve_typed_target_band(
    tmp_path: Path,
    state,
) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    _, _, _, event_evidence, _ = _fixture(
        tmp_path,
        state,
        suffix="holding-event-weakening",
        available_at=plan.as_of + timedelta(hours=12),
    )
    band = HoldingTargetBandInput(
        current_quantity=1200,
        current_weight=0.12,
        target_weight_lower=0.06,
        target_weight_mid=0.08,
        target_weight_upper=0.10,
        target_quantity_min=600,
        target_quantity_max=1000,
        implementation_cost_fen=500,
        preconditions=["official-event-verified"],
        reversal_conditions=["thesis-restored"],
    )
    result = service.review(
        _event_request(
            plan.plan_id,
            plan.as_of,
            plan.as_of + timedelta(days=1),
            severity=HoldingEventSeverity.THESIS_WEAKENING,
            evidence_ids=[event_evidence.evidence_id],
            target_band=band,
        )
    )
    assert result.proposal.action is PositionAction.TRIM
    assert result.review.thesis_strength_change == "WEAKENED"
    assert result.proposal.target_weight_mid == 0.08
    assert result.proposal.target_quantity_min == 600
    assert result.proposal.target_quantity_max == 1000
    assert result.proposal.implementation_cost_fen == 500
    assert result.proposal.preconditions == ["official-event-verified"]
    assert result.proposal.reversal_conditions == ["thesis-restored"]


def test_portfolio_risk_only_can_trim_without_claiming_company_thesis_weakened(
    tmp_path: Path,
    state,
) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    result = service.review(
        _event_request(
            plan.plan_id,
            plan.as_of,
            plan.as_of + timedelta(days=1),
            severity=HoldingEventSeverity.PORTFOLIO_RISK_ONLY,
            portfolio_effect_codes=["RISK_CONTRIBUTION_ABOVE_BUDGET"],
            target_band=HoldingTargetBandInput(
                current_weight=0.12,
                target_weight_lower=0.06,
                target_weight_mid=0.08,
                target_weight_upper=0.10,
                preconditions=[],
                reversal_conditions=[],
            ),
        )
    )
    assert result.proposal.action is PositionAction.TRIM
    assert result.review.thesis_strength_change == "UNCHANGED"
    assert result.review.risk_change == "HIGHER"
    assert result.proposal.portfolio_effect_codes == ["RISK_CONTRIBUTION_ABOVE_BUDGET"]


def test_strengthening_event_still_requires_new_evidence_for_add(tmp_path: Path, state) -> None:
    service, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    _, _, _, event_evidence, _ = _fixture(
        tmp_path,
        state,
        suffix="holding-event-strengthening",
        available_at=plan.as_of + timedelta(hours=12),
    )
    result = service.review(
        _event_request(
            plan.plan_id,
            plan.as_of,
            plan.as_of + timedelta(days=1),
            severity=HoldingEventSeverity.THESIS_STRENGTHENING,
            evidence_ids=[event_evidence.evidence_id],
            target_band=HoldingTargetBandInput(
                current_weight=0.04,
                target_weight_lower=0.06,
                target_weight_mid=0.08,
                target_weight_upper=0.10,
                preconditions=["valuation-remains-acceptable"],
                reversal_conditions=["new-evidence-invalidated"],
            ),
        )
    )
    assert result.proposal.action is PositionAction.ADD
    assert result.review.thesis_strength_change == "STRENGTHENED"
    assert result.proposal.requires_user_confirmation is True
