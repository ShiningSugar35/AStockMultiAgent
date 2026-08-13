from __future__ import annotations

from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.shadow import ShadowEvaluationService, load_shadow_evaluation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_phase7_runtime_status_is_collecting_and_never_invents_events(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    policy = load_shadow_evaluation_policy(
        PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
    )
    service = ShadowEvaluationService(
        state,
        ObjectStore(tmp_path / "objects"),
        policy,
    )

    status = service.status()

    assert status.status == "COLLECTING"
    assert status.formal_forward_event_count == 0
    assert status.formal_mature_future_event_count == 0
    assert status.assignment_count == 0
    assert status.observation_count == 0
    assert status.required_independent_decision_count == 100
    assert status.remaining_independent_decision_count == 100
    assert policy.minimum_independent_decisions == 100
    assert policy.required_horizons == [5, 20, 60]
    assert policy.final_horizon_days == 60
    assert policy.maximum_signal_registration_lag_seconds == 300
