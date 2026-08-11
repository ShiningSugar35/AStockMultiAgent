from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.shadow import ShadowArmType
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.formal_study import ensure_default_formal_study
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
