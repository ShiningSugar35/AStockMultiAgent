from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from astock.adaptive import AdaptiveResearchStatusService
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.shadow import ShadowArmType
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.formal_study import (
    build_default_formal_study_request,
    ensure_default_formal_study,
)
from astock.shadow.service import ShadowEvaluationService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def test_phase7_default_formal_study_is_idempotent_and_never_creates_samples(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = ShadowEvaluationService(
        state,
        objects,
        load_shadow_evaluation_policy(PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"),
        clock=lambda: NOW,
    )

    first, first_reused = ensure_default_formal_study(service, now=NOW)
    second, second_reused = ensure_default_formal_study(service, now=NOW)

    assert not first_reused
    assert second_reused
    assert first.study_id == second.study_id
    assert first.mode.value == "FORWARD_FORMAL"
    assert first.policy_version == "shadow-evaluation-policy-v2"
    assert len(first.arm_ids) == 6
    arms = service.repository.get_arms(first.study_id)
    assert {item.arm_type for item in arms} == {
        ShadowArmType.RULE_BASELINE,
        ShadowArmType.BASE_CASE_ONLY,
        ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
        ShadowArmType.FULL_COMMITTEE,
        ShadowArmType.CSI300_BENCHMARK,
        ShadowArmType.EQUAL_WEIGHT_CANDIDATE,
    }
    status = service.status(first.study_id)
    assert status.formal_forward_event_count == 0
    assert status.assignment_count == 0
    assert status.observation_count == 0
    audit = service.audit(first.study_id)
    assert audit["status"] == "PASS"

def test_current_policy_ignores_newer_study_from_other_policy(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    policy = load_shadow_evaluation_policy(PROJECT_ROOT / "configs" / "shadow_evaluation.yaml")
    service = ShadowEvaluationService(state, objects, policy, clock=lambda: NOW)

    current, current_reused = ensure_default_formal_study(service, now=NOW)
    assert not current_reused

    foreign_policy = policy.model_copy(
        update={"policy_version": "shadow-evaluation-policy-foreign-test"}
    )
    foreign_service = ShadowEvaluationService(
        state,
        objects,
        foreign_policy,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    foreign_request = build_default_formal_study_request(
        foreign_policy,
        created_at=NOW + timedelta(minutes=2),
        effective_from=NOW + timedelta(minutes=3),
        candidate_set_id="foreign-policy-candidates",
    )
    foreign = foreign_service.create_study(foreign_request).manifest
    assert foreign.study_id != current.study_id
    latest_any = service.repository.latest_study_summary()
    assert latest_any is not None
    assert latest_any["study_id"] == foreign.study_id

    latest_current = service.repository.latest_study_summary(
        policy_version=policy.policy_version
    )
    assert latest_current is not None
    assert latest_current["study_id"] == current.study_id
    assert service.status().study_id == current.study_id
    assert AdaptiveResearchStatusService(service).status().study_id == current.study_id

    reused, reused_existing = ensure_default_formal_study(
        service,
        now=NOW + timedelta(minutes=10),
    )
    assert reused_existing
    assert reused.study_id == current.study_id
