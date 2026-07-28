from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
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
    ShadowFillStatus,
    ShadowObservationStatus,
    ShadowOutcomeDataSource,
    ShadowPerformanceStatus,
    ShadowResearchQuality,
    ShadowStudyCreateRequest,
    ShadowStudyMode,
    ShadowThesisStatus,
)
from astock.shadow import load_shadow_evaluation_policy
from astock.shadow.storage import ParquetShadowStore

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
        "cost_model_version": "shadow-round-trip-cost-v1",
        "fill_model_version": "shadow-conservative-5m-v1",
        "corporate_action_version": "shadow-corporate-actions-v1",
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
    specialist = _arms()[2]
    with pytest.raises(ValidationError, match="production specialist"):
        ShadowArmDraft.model_validate(
            {
                **specialist.model_dump(mode="python"),
                "specialist_skill_status": ResearchSkillStatus.PENDING,
            }
        )
    isolated = ShadowArmDraft.model_validate(
        {
            **specialist.model_dump(mode="python"),
            "research_status": ShadowArmResearchStatus.RESEARCH_ISOLATED,
            "specialist_skill_status": ResearchSkillStatus.PENDING,
        }
    )
    assert isolated.research_status is ShadowArmResearchStatus.RESEARCH_ISOLATED


def test_shadow_observation_recalculates_execution_and_maturity(
    tmp_path: Path,
) -> None:
    draft = ShadowExecutionObservationDraft(
        observation_version="observation-v1",
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
        fill_status=ShadowFillStatus.FULL,
        requested_quantity=100,
        quantity=100,
        entry_price_fen=1000,
        valuation_price_fen=1100,
        highest_price_fen=1150,
        lowest_price_fen=960,
        gross_pnl_fen=10_000,
        commission_fen=50,
        tax_fen=25,
        transfer_fee_fen=5,
        slippage_fen=20,
        net_pnl_fen=9_900,
        capital_at_risk_fen=100_000,
        normalization_notional_fen=100_000,
        net_return=Decimal("0.099"),
        nav_before_fen=1_000_000,
        nav_after_fen=1_009_900,
        mfe=Decimal("0.15"),
        mae=Decimal("-0.04"),
        turnover_fen=210_000,
        liquidity_score=Decimal("0.9"),
        market_volume_shares=10_000,
        participation_rate=Decimal("0.01"),
        replay_quality=ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
        cost_model_version="shadow-round-trip-cost-v1",
        fill_model_version="shadow-conservative-5m-v1",
        corporate_action_version="shadow-corporate-actions-v1",
        market_manifest_sha256="a" * 64,
        trading_calendar_snapshot_sha256="b" * 64,
        candidate_set_snapshot_sha256="c" * 64,
        corporate_action_snapshot_sha256="d" * 64,
        delisting_snapshot_sha256="e" * 64,
        market_observation_ids=["bar:1", "bar:2"],
        pit_statuses=[
            PointInTimeStatus.CERTIFIED,
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        ],
        candidate_membership_pit_safe=True,
        corporate_action_coverage_complete=True,
        delisting_coverage_complete=True,
        t_plus_one_compliant=True,
        price_limit_compliant=True,
        suspension_compliant=True,
        optimistic_net_pnl_fen=9_900,
        created_at=AS_OF,
    )
    with pytest.raises(ValidationError, match="market snapshots and availability"):
        ShadowExecutionObservationDraft.model_validate(
            {
                **draft.model_dump(mode="python"),
                "outcome_data_source": (
                    ShadowOutcomeDataSource.LIVE_FORWARD_MARKET
                ),
            }
        )
    with pytest.raises(ValidationError, match="explicit reason codes"):
        ShadowExecutionObservationDraft.model_validate(
            {
                **draft.model_dump(mode="python"),
                "thesis_status": ShadowThesisStatus.INVALIDATED,
            }
        )
    live = ShadowExecutionObservationDraft.model_validate(
        {
            **draft.model_dump(mode="python"),
            "outcome_data_source": ShadowOutcomeDataSource.LIVE_FORWARD_MARKET,
            "data_available_at": AS_OF + timedelta(days=90),
            "market_snapshot_ids": ["snapshot:eastmoney", "snapshot:sina"],
            "thesis_status": ShadowThesisStatus.INVALIDATED,
            "invalidation_reason_codes": ["DEMAND_ASSUMPTION_FAILED"],
            "created_at": AS_OF + timedelta(days=90),
        }
    )
    assert live.thesis_status is ShadowThesisStatus.INVALIDATED
    observation = ShadowExecutionObservation(
        **draft.model_dump(mode="python", exclude={"schema_version", "created_at"}),
        observation_id="observation:1",
        status=ShadowObservationStatus.MATURE,
        formal_eligible=True,
        observation_sha256="b" * 64,
        created_at=AS_OF,
    )
    assert observation.net_return == Decimal("0.099")
    parquet = ParquetShadowStore(tmp_path / "parquet")
    parquet_path = parquet.write(observation, object_sha256="c" * 64)
    current_table = pq.ParquetFile(parquet_path).read()
    legacy_table = current_table.select(
        [
            name
            for name in current_table.column_names
            if name
            not in {
                "outcome_data_source",
                "data_available_at",
                "market_snapshot_ids",
                "thesis_status",
                "invalidation_reason_codes",
            }
        ]
    )
    pq.write_table(legacy_table, parquet_path)
    assert parquet.verify(observation, object_sha256="c" * 64)

    with pytest.raises(ValidationError, match="net PnL"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(update={"net_pnl_fen": 9_901}).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="MFE/MAE"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(update={"mfe": Decimal("0.16")}).model_dump(
                mode="python"
            )
        )
    with pytest.raises(ValidationError, match="participation rate"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(
                update={"participation_rate": Decimal("0.02")}
            ).model_dump(mode="python")
        )
    partial = ShadowExecutionObservationDraft.model_validate(
        draft.model_copy(
            update={
                "fill_status": ShadowFillStatus.PARTIAL,
                "requested_quantity": 200,
            }
        ).model_dump(mode="python")
    )
    assert partial.fill_status is ShadowFillStatus.PARTIAL
    with pytest.raises(ValidationError, match="100-share lots"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(
                update={"requested_quantity": 50, "quantity": 50}
            ).model_dump(mode="python")
        )
    corporate_action = ShadowExecutionObservationDraft.model_validate(
        draft.model_copy(
            update={
                "corporate_action_cash_fen": 100,
                "gross_pnl_fen": 10_100,
                "net_pnl_fen": 10_000,
                "net_return": Decimal("0.1"),
                "nav_after_fen": 1_010_000,
                "optimistic_net_pnl_fen": 10_000,
            }
        ).model_dump(mode="python")
    )
    assert corporate_action.gross_pnl_fen == 10_100
    with pytest.raises(ValidationError, match="path sensitivity"):
        ShadowExecutionObservationDraft.model_validate(
            draft.model_copy(update={"optimistic_net_pnl_fen": 9_901}).model_dump(
                mode="python"
            )
        )
    path_sensitive = ShadowExecutionObservationDraft.model_validate(
        draft.model_copy(
            update={
                "ambiguous_intrabar_path": True,
                "optimistic_net_pnl_fen": 9_901,
            }
        ).model_dump(mode="python")
    )
    assert path_sensitive.conservative_path_used
    non_execution = {
        "entry_time": None,
        "entry_price_fen": None,
        "valuation_price_fen": None,
        "highest_price_fen": None,
        "lowest_price_fen": None,
        "quantity": 0,
        "gross_pnl_fen": 0,
        "commission_fen": 0,
        "tax_fen": 0,
        "transfer_fee_fen": 0,
        "slippage_fen": 0,
        "net_pnl_fen": 0,
        "capital_at_risk_fen": 0,
        "net_return": Decimal("0"),
        "nav_after_fen": draft.nav_before_fen,
        "mfe": Decimal("0"),
        "mae": Decimal("0"),
        "turnover_fen": 0,
        "participation_rate": Decimal("0"),
        "optimistic_net_pnl_fen": 0,
    }
    unfilled = ShadowExecutionObservationDraft.model_validate(
        draft.model_copy(
            update={
                **non_execution,
                "fill_status": ShadowFillStatus.UNFILLED,
            }
        ).model_dump(mode="python")
    )
    assert unfilled.requested_quantity == 100
    no_action = ShadowExecutionObservationDraft.model_validate(
        draft.model_copy(
            update={
                **non_execution,
                "action": ShadowAction.NO_ACTION,
                "fill_status": ShadowFillStatus.NOT_APPLICABLE,
                "requested_quantity": 0,
                "market_volume_shares": 0,
            }
        ).model_dump(mode="python")
    )
    assert no_action.action is ShadowAction.NO_ACTION
    for horizon in (5, 20, 60):
        exact = ShadowExecutionObservation.model_validate(
            observation.model_copy(
                update={"horizon_days": horizon, "trading_days_elapsed": horizon}
            ).model_dump(mode="python")
        )
        pending = ShadowExecutionObservation.model_validate(
            observation.model_copy(
                update={
                    "horizon_days": horizon,
                    "trading_days_elapsed": horizon - 1,
                    "status": ShadowObservationStatus.PENDING_MATURITY,
                    "formal_eligible": False,
                }
            ).model_dump(mode="python")
        )
        assert exact.status is ShadowObservationStatus.MATURE
        assert pending.status is ShadowObservationStatus.PENDING_MATURITY
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
        "experimental_arm_gate_results": {"arm:1": {"increment": True}},
        "eligible_experimental_arm_ids": ["arm:1"],
        "reason_codes": ["INCREMENT_FAILED"],
        "admission_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError, match="must match all"):
        Phase8AdmissionReport.model_validate(payload)


def test_research_quality_treats_maturity_as_subset_of_forward_events() -> None:
    quality = ShadowResearchQuality(
        status=ShadowPerformanceStatus.COLLECTING,
        independent_event_count=1,
        research_memo_count=1,
        shadow_decision_count=1,
        complete_chain_count=0,
        mature_future_event_count=0,
        formal_forward_event_count=1,
        thesis_status_counts={},
        invalidation_reason_counts={},
        memo_coverage_counts={},
        memo_open_gap_event_count=0,
        memo_degradation_event_count=0,
        finding_codes=["MATURE_FUTURE_EVENTS_UNDER_MINIMUM"],
        created_at=AS_OF,
    )
    assert quality.formal_forward_event_count == 1
    assert quality.mature_future_event_count == 0
    with pytest.raises(
        ValidationError,
        match="mature future events cannot exceed formal forward",
    ):
        ShadowResearchQuality.model_validate(
            quality.model_copy(
                update={
                    "formal_forward_event_count": 0,
                    "mature_future_event_count": 1,
                }
            ).model_dump(mode="python")
        )
