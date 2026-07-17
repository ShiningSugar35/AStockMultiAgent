from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    CommitteeEntryOrderType,
    CommitteeProtocolStatus,
    CommitteeVerdict,
    FrozenWeightProfile,
    Market,
    MarketRegime,
    MarketRegimeFeatures,
    PointInTimeStatus,
    ResearchSkillStatus,
    ShadowAction,
    ShadowArmDraft,
    ShadowArmResearchStatus,
    ShadowArmSignal,
    ShadowArmType,
    ShadowArtifactReference,
    ShadowDecisionAssignmentRequest,
    ShadowStudyCreateRequest,
    ShadowStudyMode,
    TradeProtocol,
)
from astock.shadow import (
    ShadowEvaluationService,
    ShadowStudyExecution,
    load_shadow_evaluation_policy,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 7, 17, tzinfo=UTC)
SIGNAL = AS_OF + timedelta(days=1)


def _weight(name: str) -> FrozenWeightProfile:
    return FrozenWeightProfile(
        profile_id=f"profile:{name}",
        profile_version="weights-v1",
        component_weights={name: Decimal("1")},
        created_at=AS_OF,
    )


def _study_request() -> ShadowStudyCreateRequest:
    common = {
        "protocol_family_version": "protocol-family-v1",
        "cost_model_version": "cost-v1",
        "fill_model_version": "fill-v1",
        "corporate_action_version": "actions-v1",
        "created_at": AS_OF,
    }
    arms = [
        ShadowArmDraft(
            arm_key="a-rule",
            arm_type=ShadowArmType.RULE_BASELINE,
            weight_profile=_weight("rule"),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="b-base",
            arm_type=ShadowArmType.BASE_CASE_ONLY,
            weight_profile=_weight("base"),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="c-specialist",
            arm_type=ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
            weight_profile=_weight("specialist"),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            specialist_skill_id="industry-bottleneck",
            specialist_skill_version="v1",
            specialist_skill_status=ResearchSkillStatus.ENABLED_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="d-committee",
            arm_type=ShadowArmType.FULL_COMMITTEE,
            weight_profile=_weight("committee"),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="e-csi300",
            arm_type=ShadowArmType.CSI300_BENCHMARK,
            weight_profile=_weight("csi300"),
            research_status=ShadowArmResearchStatus.BENCHMARK,
            benchmark_symbol="000300",
            **common,
        ),
        ShadowArmDraft(
            arm_key="f-equal",
            arm_type=ShadowArmType.EQUAL_WEIGHT_CANDIDATE,
            weight_profile=_weight("equal"),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
    ]
    return ShadowStudyCreateRequest(
        study_name="forward-v1",
        mode=ShadowStudyMode.FORWARD_FORMAL,
        effective_from=AS_OF,
        candidate_policy_id="candidate-policy",
        candidate_policy_version="v1",
        candidate_set_id="candidate-set:1",
        initial_capital_fen=100_000_000,
        fixed_notional_fen=1_000_000,
        arms=arms,
        created_at=AS_OF,
    )


def _service(tmp_path: Path) -> tuple[ShadowEvaluationService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = ShadowEvaluationService(
        state,
        objects,
        load_shadow_evaluation_policy(
            PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
        ),
    )
    return service, state, objects


def _register_input(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str,
    artifact_type: str,
    payload: object,
) -> ShadowArtifactReference:
    reference = objects.put_bytes(canonical_json_bytes(payload))
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        object_hash=reference.sha256,
        input_hashes=[],
    )
    return ShadowArtifactReference(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        object_sha256=reference.sha256,
        available_at=SIGNAL,
        created_at=SIGNAL,
    )


def _protocol() -> TradeProtocol:
    return TradeProtocol(
        protocol_id="protocol:1",
        decision_id="decision:1",
        decision_sha256="d" * 64,
        company_id="company:1",
        verdict=CommitteeVerdict.PAPER_ELIGIBLE,
        protocol_status=CommitteeProtocolStatus.ACTIVE,
        blocking_codes=[],
        strategy_id="strategy-v1",
        skill_versions={"industry-bottleneck": "v1"},
        signal_time=SIGNAL,
        earliest_executable_time=SIGNAL + timedelta(minutes=5),
        holding_horizon_days=60,
        entry_rule="next replayable 5m limit",
        entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
        position_size_rule="fixed study notional",
        price_stop_rule="versioned price stop",
        volatility_stop_rule="versioned volatility stop",
        trailing_stop_rule="versioned trailing stop",
        time_stop_rule="60 trading days",
        thesis_invalidation_rule="frozen thesis invalidation",
        take_profit_rule="versioned take profit",
        review_events=["ANNOUNCEMENT"],
        max_holding_period_days=60,
        cost_model_version="cost-v1",
        fill_model_version="fill-v1",
        evidence_snapshot_id="snapshot:1",
        evidence_ids=["evidence:1"],
        effective_from=SIGNAL + timedelta(minutes=5),
        created_at=SIGNAL,
    )


def _assignment_request(
    execution: ShadowStudyExecution,
    state: StateStore,
    objects: ObjectStore,
) -> ShadowDecisionAssignmentRequest:
    manifest = execution.manifest
    arms = execution.arms
    references = [
        _register_input(
            state,
            objects,
            artifact_id="BaseCasePack:base:1",
            artifact_type="BaseCasePack",
            payload={"base_case_id": "base:1"},
        ),
        _register_input(
            state,
            objects,
            artifact_id="DecisionPack:decision:1",
            artifact_type="DecisionPack",
            payload={"decision_id": "decision:1"},
        ),
        _register_input(
            state,
            objects,
            artifact_id="SpecialistDelta:delta:1",
            artifact_type="SpecialistDelta",
            payload={"delta_id": "delta:1"},
        ),
        _register_input(
            state,
            objects,
            artifact_id="TradeProtocol:protocol:1",
            artifact_type="TradeProtocol",
            payload=_protocol(),
        ),
    ]
    type_inputs = {
        ShadowArmType.RULE_BASELINE: [],
        ShadowArmType.BASE_CASE_ONLY: ["BaseCasePack:base:1"],
        ShadowArmType.BASE_CASE_PLUS_SPECIALIST: [
            "BaseCasePack:base:1",
            "SpecialistDelta:delta:1",
        ],
        ShadowArmType.FULL_COMMITTEE: [
            "DecisionPack:decision:1",
            "TradeProtocol:protocol:1",
        ],
        ShadowArmType.CSI300_BENCHMARK: [],
        ShadowArmType.EQUAL_WEIGHT_CANDIDATE: [],
    }
    signals = sorted(
        [
            ShadowArmSignal(
                arm_id=arm.arm_id,
                action=ShadowAction.ENTER,
                input_artifact_ids=type_inputs[arm.arm_type],
                reason_codes=["FROZEN_BEFORE_OUTCOME"],
                created_at=SIGNAL,
            )
            for arm in arms
        ],
        key=lambda item: item.arm_id,
    )
    return ShadowDecisionAssignmentRequest(
        study_id=manifest.study_id,
        candidate_set_id=manifest.candidate_set_id,
        company_id="company:1",
        symbol="600000",
        market=Market.XSHG,
        signal_time=SIGNAL,
        independence_key="company:1:episode:1",
        thesis_version="thesis-v1",
        event_id="event:1",
        trade_protocol_id="protocol:1",
        artifact_references=references,
        arm_signals=signals,
        created_at=SIGNAL,
    )


def test_shadow_study_regime_assignment_and_audit_are_frozen(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    assert service.status("study:not-run").status == "NOT_RUN"

    request = _study_request()
    plan = service.plan_study(request)
    assert not plan.persistent_writes
    assert service.repository.integrity_counts()["shadow_policy_index"] == 0

    first = service.create_study(request)
    second = service.create_study(request)
    assert first.manifest.study_id == second.manifest.study_id
    assert first.object_sha256_by_id == second.object_sha256_by_id
    counts = service.repository.integrity_counts()
    assert counts["shadow_policy_index"] == 1
    assert counts["shadow_study_index"] == 1
    assert counts["shadow_arm_index"] == 6
    assert service.audit(first.manifest.study_id)["status"] == "PASS"

    feature_ref = objects.put_json({"market_features": "panic-v1"})
    features = MarketRegimeFeatures(
        feature_snapshot_id="features:1",
        feature_snapshot_sha256=feature_ref.sha256,
        as_of=SIGNAL,
        daily_trend_score=Decimal("-0.5"),
        hourly_trend_score=Decimal("-0.4"),
        market_breadth=Decimal("0.15"),
        new_high_low_balance=Decimal("-0.8"),
        turnover_ratio=Decimal("1.4"),
        industry_diffusion=Decimal("0.10"),
        volatility_percentile=Decimal("0.90"),
        index_drawdown=Decimal("-0.15"),
        style_relative_performance=Decimal("-0.2"),
        strategy_performance=Decimal("-0.1"),
        evidence_ids=["evidence:market:1"],
        pit_statuses=[PointInTimeStatus.CERTIFIED],
        created_at=SIGNAL,
    )
    regime = service.classify_regime(first.manifest.study_id, features)
    assert regime.regime is MarketRegime.PANIC
    assert (
        service.classify_regime(first.manifest.study_id, features).regime_id
        == regime.regime_id
    )

    assignment_request = _assignment_request(first, state, objects)
    assignment = service.assign(assignment_request)
    assert service.assign(assignment_request).assignment_id == assignment.assignment_id
    status = service.status(first.manifest.study_id)
    assert status.assignment_count == 1
    assert status.independent_decision_count == 1
    assert status.status == "COLLECTING"
    assert service.audit(first.manifest.study_id)["status"] == "PASS"

    with pytest.raises(ValueError, match="retain every"):
        service.assign(
            assignment_request.model_copy(
                update={
                    "independence_key": "company:1:episode:2",
                    "arm_signals": assignment_request.arm_signals[:-1],
                }
            )
        )
