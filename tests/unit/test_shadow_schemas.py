from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.schemas import (
    FrozenWeightProfile,
    Market,
    Phase8AdmissionReport,
    Phase8AdmissionStatus,
    PointInTimeStatus,
    ReplayQuality,
    ResearchSkillStatus,
    ShadowAction,
    ShadowArmDraft,
    ShadowArmResearchStatus,
    ShadowArmType,
    ShadowExecutionObservation,
    ShadowExecutionObservationDraft,
    ShadowObservationStatus,
    ShadowStudyCreateRequest,
    ShadowStudyMode,
)
from astock.shadow import load_shadow_evaluation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 7, 17, tzinfo=UTC)


def _weight(name: str) -> FrozenWeightProfile:
    return FrozenWeightProfile(
        profile_id=f"profile:{name}",
        profile_version="weights-v1",
        component_weights={name: Decimal("1")},
        created_at=AS_OF,
    )


def _arms() -> list[ShadowArmDraft]:
    common = {
        "protocol_family_version": "protocol-family-v1",
        "cost_model_version": "cost-v1",
        "fill_model_version": "fill-v1",
        "corporate_action_version": "actions-v1",
        "created_at": AS_OF,
    }
    return [
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


def _study() -> ShadowStudyCreateRequest:
    return ShadowStudyCreateRequest(
        study_name="forward-v1",
        mode=ShadowStudyMode.FORWARD_FORMAL,
        effective_from=AS_OF,
        candidate_policy_id="candidate-policy",
        candidate_policy_version="v1",
        candidate_set_id="candidate-set:1",
        initial_capital_fen=100_000_000,
        fixed_notional_fen=1_000_000,
        arms=_arms(),
        created_at=AS_OF,
    )


def test_shadow_policy_and_frozen_study_contracts() -> None:
    policy = load_shadow_evaluation_policy(
        PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
    )
    assert policy.required_horizons == [5, 20, 60]
    assert policy.minimum_independent_decisions == 100
    assert policy.phase8_observation_months == 12
    assert _study().arms[-1].arm_type is ShadowArmType.EQUAL_WEIGHT_CANDIDATE

    with pytest.raises(ValidationError, match="sum exactly to one"):
        FrozenWeightProfile(
            profile_id="bad",
            profile_version="v1",
            component_weights={"a": Decimal("0.6"), "b": Decimal("0.3")},
        )
    with pytest.raises(ValidationError, match="missing required arms"):
        ShadowStudyCreateRequest.model_validate(
            _study().model_copy(update={"arms": _arms()[1:]}).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="frozen before"):
        ShadowStudyCreateRequest.model_validate(
            _study()
            .model_copy(update={"created_at": AS_OF + timedelta(seconds=1)})
            .model_dump(mode="python")
        )


def test_shadow_observation_recalculates_execution_and_maturity() -> None:
    draft = ShadowExecutionObservationDraft(
        study_id="study:1",
        assignment_id="assignment:1",
        arm_id="arm:1",
        independence_key="episode:1",
        regime_id="regime:1",
        company_id="company:1",
        symbol="600000",
        market=Market.XSHG,
        horizon_days=60,
        trading_days_elapsed=60,
        action=ShadowAction.ENTER,
        signal_time=AS_OF,
        entry_time=AS_OF + timedelta(minutes=5),
        valuation_time=AS_OF + timedelta(days=90),
        quantity=100,
        entry_price_fen=1000,
        valuation_price_fen=1100,
        gross_pnl_fen=10_000,
        commission_fen=50,
        tax_fen=25,
        transfer_fee_fen=5,
        slippage_fen=20,
        net_pnl_fen=9_900,
        capital_at_risk_fen=100_000,
        net_return=Decimal("0.099"),
        nav_before_fen=1_000_000,
        nav_after_fen=1_009_900,
        mfe=Decimal("0.15"),
        mae=Decimal("-0.04"),
        turnover_fen=210_000,
        liquidity_score=Decimal("0.9"),
        participation_rate=Decimal("0.01"),
        replay_quality=ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
        market_manifest_sha256="a" * 64,
        market_observation_ids=["bar:1", "bar:2"],
        pit_statuses=[
            PointInTimeStatus.CERTIFIED,
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        ],
        optimistic_net_pnl_fen=9_900,
        created_at=AS_OF,
    )
    observation = ShadowExecutionObservation(
        **draft.model_dump(mode="python", exclude={"schema_version", "created_at"}),
        observation_id="observation:1",
        status=ShadowObservationStatus.MATURE,
        formal_eligible=True,
        observation_sha256="b" * 64,
        created_at=AS_OF,
    )
    assert observation.net_return == Decimal("0.099")

    with pytest.raises(ValidationError, match="net PnL"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(update={"net_pnl_fen": 9_901}).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="completed horizon"):
        ShadowExecutionObservation.model_validate(
            observation
            .model_copy(update={"trading_days_elapsed": 59})
            .model_dump(mode="python")
        )


def test_phase8_admission_cannot_disagree_with_deterministic_gates() -> None:
    payload = {
        "admission_id": "admission:1",
        "study_id": "study:1",
        "shadow_report_id": "report:1",
        "shadow_report_sha256": "a" * 64,
        "status": Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH,
        "gate_results": {"sample": True, "increment": False},
        "reason_codes": ["INCREMENT_FAILED"],
        "admission_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError, match="must match all"):
        Phase8AdmissionReport.model_validate(payload)
