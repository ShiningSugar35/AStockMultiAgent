from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.research import load_position_lifecycle_config
from astock.schemas import (
    DecisionReferenceStatus,
    HoldingReviewRequest,
    LifecycleCondition,
    LifecycleMetricDefinition,
    LifecycleSourceType,
    PositionAction,
    PositionActionProposal,
    PositionPlanCreateRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_lifecycle_config_freezes_action_priority_and_safety_gates() -> None:
    config = load_position_lifecycle_config(
        PROJECT_ROOT / "configs" / "position_lifecycle.yaml"
    )
    assert config.rules_version == "generic-position-lifecycle-v1"
    assert config.action_priority == [
        PositionAction.EXIT,
        PositionAction.REVIEW,
        PositionAction.TRIM,
        PositionAction.ADD,
        PositionAction.HOLD,
    ]
    assert config.requires_user_confirmation
    assert config.add_requires_new_evidence


def test_add_condition_requires_new_evidence() -> None:
    with pytest.raises(ValidationError, match="ADD conditions must require new evidence"):
        LifecycleCondition(
            rule_id="add-without-evidence",
            signal_code="ADD_WITHOUT_EVIDENCE",
            action=PositionAction.ADD,
            source_type=LifecycleSourceType.FUNDAMENTAL,
            description="Synthetic unsafe add rule.",
            requires_new_evidence=False,
        )


def test_monitoring_plan_requires_an_exit_condition() -> None:
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="at least one EXIT"):
        PositionPlanCreateRequest(
            position_id="position:1",
            company_id="company:1",
            decision_id="decision:1",
            decision_reference_status=DecisionReferenceStatus.USER_DECLARED_EXTERNAL,
            base_case_id="base:1",
            route_plan_id="route:1",
            memo_id="memo:1",
            as_of=as_of,
            thesis_summary="Synthetic thesis.",
            entry_assumptions=["Synthetic assumption."],
            holding_horizon="long",
            key_value_drivers=["Synthetic driver."],
            validation_metrics=[
                LifecycleMetricDefinition(
                    metric_id="metric:1",
                    name="Synthetic metric",
                    unit="ratio",
                    evidence_ids=["evidence:1"],
                )
            ],
            monitoring_sources=["official_disclosures"],
            monitoring_cadence={"official_disclosures": "daily"},
            conditions=[
                LifecycleCondition(
                    rule_id="review:1",
                    signal_code="REVIEW_SIGNAL",
                    action=PositionAction.REVIEW,
                    source_type=LifecycleSourceType.EVENT,
                    description="Synthetic review rule.",
                    hard_block=True,
                )
            ],
            manual_information_needs=[],
            next_review_at=as_of + timedelta(days=1),
        )


def test_review_request_sets_are_unique_and_move_forward() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="added evidence ids must be unique"):
        HoldingReviewRequest(
            plan_id="plan:1",
            from_as_of=start,
            to_as_of=start + timedelta(days=1),
            added_evidence_ids=["evidence:1", "evidence:1"],
            changed_claim_ids=[],
            invalidated_evidence_ids=[],
            unresolved_conflict_ids=[],
            signals=[],
        )


def test_position_proposal_cannot_bypass_manual_confirmation() -> None:
    with pytest.raises(ValidationError, match="always require user confirmation"):
        PositionActionProposal(
            proposal_id="proposal:1",
            position_id="position:1",
            action=PositionAction.HOLD,
            reasons=["NO_TRIGGER"],
            evidence_ids=[],
            hard_blocks=[],
            requires_user_confirmation=False,
        )
