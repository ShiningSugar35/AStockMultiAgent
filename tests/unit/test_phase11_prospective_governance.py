from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.prospective import (
    ProspectiveFunnelOutcome,
    ProspectiveFunnelStage,
    ProspectiveTrialRecordRequest,
    TrialClusterType,
)
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.formal_study import ensure_default_formal_study
from astock.shadow.governance import ProspectiveGovernanceService
from astock.shadow.service import ShadowEvaluationService
from astock.shadow.statistics import (
    deflated_sharpe_probability,
    deterministic_cluster_bootstrap,
    time_fold_probability_of_backtest_overfitting,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


def _services(tmp_path: Path) -> tuple[ShadowEvaluationService, ProspectiveGovernanceService]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    policy = load_shadow_evaluation_policy(PROJECT_ROOT / "configs" / "shadow_evaluation.yaml")
    shadow = ShadowEvaluationService(state, objects, policy, clock=lambda: NOW)
    governance = ProspectiveGovernanceService(
        state,
        objects,
        policy,
        clock=lambda: NOW + timedelta(seconds=30),
    )
    return shadow, governance


def test_phase11_rejected_trial_is_registered_without_inflating_forward_count(
    tmp_path: Path,
) -> None:
    shadow, governance = _services(tmp_path)
    study, _ = ensure_default_formal_study(shadow, now=NOW)
    config = governance.register_default_config(study.study_id)
    assert config.created_at == NOW + timedelta(seconds=30)
    assert config.effective_from == study.effective_from
    assert config.effective_from >= config.created_at
    assert governance.register_default_config(study.study_id) == config
    config_artifact_id = governance.config_artifact_id(config.config_version)
    config_record = governance.state.artifact_record(config_artifact_id)
    assert config_record is not None
    decision_time = config.effective_from + timedelta(minutes=1)

    trial = governance.register_trial(
        ProspectiveTrialRecordRequest(
            study_id=study.study_id,
            governance_config_artifact_id=config_artifact_id,
            governance_config_object_hash=str(config_record["object_hash"]),
            research_trial_id="trial:600001:20260812",
            funnel_event_id="seed-reject:600001:20260812",
            company_id="600001",
            decision_time=decision_time,
            stage=ProspectiveFunnelStage.SEED,
            outcome=ProspectiveFunnelOutcome.SEED_REJECTED,
            independence_unit_id="independent:600001:20260812",
            cluster_ids={
                TrialClusterType.STOCK: ["stock:600001"],
                TrialClusterType.DECISION_DATE: ["decision-date:2026-08-12"],
                TrialClusterType.SHARED_CATALYST: ["catalyst:sector-policy"],
            },
            market_regime_id="regime:neutral-v1",
            market_regime_rule_version=config.market_regime_rule_version,
            reason_codes=["SEED_EVIDENCE_INSUFFICIENT"],
            created_at=decision_time,
        )
    )

    status = shadow.status(study.study_id)
    assert status.formal_forward_event_count == 0
    assert status.assignment_count == 0
    report = governance.all_trials_report(study.study_id)
    assert report.event_count == 1
    assert report.research_trial_count == 1
    assert report.independence_unit_count == 1
    assert report.formal_trade_event_count == 0
    assert not report.prospective_forward_count_mutated
    assert governance.audit(trial.trial_event_id)["status"] == "PASS"
    plan = governance.statistics_plan(study.study_id)
    assert not plan.independence_sample_floor_reached
    assert "INDEPENDENCE_SAMPLE_FLOOR_NOT_REACHED" in plan.finding_codes


def test_phase11_trial_schema_forbids_marking_research_event_as_trade() -> None:
    with pytest.raises(ValidationError):
        ProspectiveTrialRecordRequest.model_validate(
            {
                "study_id": "study",
                "governance_config_artifact_id": "config",
                "governance_config_object_hash": "a" * 64,
                "research_trial_id": "trial",
                "funnel_event_id": "event",
                "decision_time": NOW.isoformat(),
                "stage": "SEED",
                "outcome": "SEED_REJECTED",
                "independence_unit_id": "independent",
                "cluster_ids": {"DECISION_DATE": ["2026-08-12"]},
                "formal_trade_event": True,
                "created_at": NOW.isoformat(),
            }
        )


def test_phase11_cluster_statistics_respect_cluster_unit() -> None:
    interval, p_value = deterministic_cluster_bootstrap(
        [Decimal("0.10"), Decimal("0.12"), Decimal("-0.05"), Decimal("-0.03")],
        ["stock:a", "stock:a", "stock:b", "stock:b"],
        seed="phase11",
        replicates=300,
        confidence_level=Decimal("0.95"),
        metric="cluster-return",
        created_at=NOW,
    )

    assert interval.sample_count == 2
    assert interval.estimate == Decimal("0.035")
    assert p_value is not None
    assert Decimal("0") <= p_value <= Decimal("1")
    assert deflated_sharpe_probability([0.01] * 20, selection_candidate_count=1) is None
    pbo = time_fold_probability_of_backtest_overfitting(
        [
            [0.01, 0.02, 0.03, 0.04, 0.01, -0.01, -0.02, -0.03, 0.01, 0.02],
            [0.02, 0.01, 0.00, -0.01, 0.03, 0.04, 0.01, 0.00, -0.02, -0.01],
        ],
        fold_count=5,
    )
    assert pbo is not None
    assert 0 <= pbo <= 1
