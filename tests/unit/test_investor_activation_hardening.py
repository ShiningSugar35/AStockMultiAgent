"""Negative release-admission tests; no network or production activation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.investor_orchestration.activation import ActivationGateService, shadow_observation
from astock.investor_orchestration.models import AdmissionStatus, ControlledLiveCheck
from astock.investor_orchestration.store import InvestorOrchestrationStore


@pytest.fixture
def activation(tmp_path: Path) -> ActivationGateService:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    return ActivationGateService(store)


def test_caller_cannot_flip_eligible_to_bypass_admission(activation: ActivationGateService) -> None:
    assessment = activation.assess("scheduled_research", recorded_scenario_count=0)
    assert not assessment.eligible
    forged = assessment.model_copy(
        update={
            "eligible": True,
            "blockers": (),
            "owner_approval_id": "unregistered-approval",
        }
    )
    with pytest.raises(ValueError):
        activation.activate(forged, feature_flags={"scheduled_research_enabled": True})
    assert activation.status("scheduled_research") != AdmissionStatus.ACTIVE


def test_claimed_scenario_total_does_not_replace_recorded_results(
    activation: ActivationGateService,
) -> None:
    assessment = activation.assess("scheduled_research", recorded_scenario_count=68)
    assert assessment.gate_results["recorded_scenarios"] is False
    assert assessment.recorded_scenario_count == 0


def test_nonempty_approval_string_is_not_owner_authorization(
    activation: ActivationGateService,
) -> None:
    assessment = activation.assess(
        "scheduled_research",
        recorded_scenario_count=68,
        owner_approval_id="invented-approval",
    )
    assert assessment.gate_results["owner_approval"] is False


def test_empty_controlled_check_is_not_live_evidence(activation: ActivationGateService) -> None:
    check = ControlledLiveCheck(
        check_id="empty-smoke",
        check_type="SCHEDULED_RESEARCH",
        checked_at=datetime.now(UTC),
        status="PASS",
        evidence_ids=(),
        details="not an actual end-to-end run",
    )
    try:
        activation.record_controlled_live(check)
    except ValueError:
        return
    assessment = activation.assess("scheduled_research", recorded_scenario_count=0)
    assert not assessment.gate_results["scheduled_research_controlled_live"]


def test_synthetic_shadow_counts_do_not_satisfy_prospective_gate(
    activation: ActivationGateService,
) -> None:
    observation = shadow_observation(
        feature_id="scheduled_research",
        request_or_run_id="no-real-run",
        required_capability_coverage=1.0,
        notes=("synthetic smoke, not real coverage",),
    )
    try:
        activation.record_shadow(observation)
    except ValueError:
        return
    assessment = activation.assess("scheduled_research", recorded_scenario_count=0)
    assert assessment.shadow_observation_count == 0
    assert not assessment.gate_results["required_capability_coverage"]


def test_future_assessment_time_is_rejected(activation: ActivationGateService) -> None:
    with pytest.raises(ValueError):
        activation.assess(
            "scheduled_research",
            recorded_scenario_count=0,
            assessed_at=datetime.now(UTC) + timedelta(days=1),
        )
