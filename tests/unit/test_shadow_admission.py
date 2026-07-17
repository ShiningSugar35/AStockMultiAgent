from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    FrozenWeightProfile,
    MarketRegime,
    Phase8AdmissionStatus,
    PointInTimeStatus,
    ReplayQuality,
    ResearchSkillStatus,
    ShadowArmDefinition,
    ShadowArmMetrics,
    ShadowArmResearchStatus,
    ShadowArmType,
    ShadowComparisonResult,
    ShadowEvaluationReport,
    ShadowEvidenceStatus,
    ShadowFoldResult,
    ShadowMetricInterval,
    ShadowRegimeResult,
)
from astock.shadow import ShadowEvaluationService, load_shadow_evaluation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2027, 7, 18, tzinfo=UTC)


def _interval(
    *,
    count: int,
    estimate: str,
    lower: str,
    upper: str,
) -> ShadowMetricInterval:
    return ShadowMetricInterval(
        metric="PAIRED_NET_RETURN_DELTA",
        sample_count=count,
        estimate=Decimal(estimate),
        lower=Decimal(lower),
        upper=Decimal(upper),
        created_at=AS_OF,
    )


def _arm(arm_id: str, drawdown: str) -> ShadowArmMetrics:
    return ShadowArmMetrics(
        arm_id=arm_id,
        independent_decision_count=100,
        mature_observation_count=100,
        total_net_pnl_fen=1_000_000,
        ending_nav_fen=101_000_000,
        total_net_return_on_initial_capital=Decimal("0.01"),
        mean_net_return=_interval(
            count=100,
            estimate="0.02",
            lower="0.01",
            upper="0.03",
        ),
        win_rate=ShadowMetricInterval(
            metric="WIN_RATE",
            sample_count=100,
            estimate=Decimal("0.6"),
            lower=Decimal("0.5"),
            upper=Decimal("0.7"),
            created_at=AS_OF,
        ),
        maximum_drawdown=Decimal(drawdown),
        mean_mfe=Decimal("0.08"),
        mean_mae=Decimal("-0.03"),
        payoff_ratio=Decimal("1.8"),
        total_cost_fen=10_000,
        turnover_fen=10_000_000,
        mean_holding_days=Decimal("60"),
        mean_liquidity_score=Decimal("0.8"),
        mean_participation_rate=Decimal("0.01"),
        partial_fill_rate=Decimal("0"),
        unfilled_rate=Decimal("0"),
        path_uncertainty_rate=Decimal("0.01"),
        dual_source_rate=Decimal("0.95"),
        created_at=AS_OF,
    )


def _definition(
    arm_id: str,
    *,
    specialist: bool,
    isolated: bool = False,
) -> ShadowArmDefinition:
    return ShadowArmDefinition(
        arm_id=arm_id,
        study_id="study:ready",
        arm_key="b-specialist" if specialist else "a-base",
        arm_type=(
            ShadowArmType.BASE_CASE_PLUS_SPECIALIST
            if specialist
            else ShadowArmType.BASE_CASE_ONLY
        ),
        weight_profile=FrozenWeightProfile(
            profile_id=f"weight:{arm_id}",
            profile_version="v1",
            component_weights={"specialist" if specialist else "base": Decimal("1")},
            created_at=AS_OF,
        ),
        research_status=(
            ShadowArmResearchStatus.RESEARCH_ISOLATED
            if isolated
            else ShadowArmResearchStatus.PRODUCTION_CONTRACT
        ),
        protocol_family_version="protocol-v1",
        cost_model_version="shadow-round-trip-cost-v1",
        fill_model_version="shadow-conservative-5m-v1",
        corporate_action_version="shadow-corporate-actions-v1",
        specialist_skill_id="industry-bottleneck" if specialist else None,
        specialist_skill_version="v1" if specialist else None,
        specialist_skill_status=(
            ResearchSkillStatus.ENABLED_CONTRACT if specialist else None
        ),
        arm_sha256=("b" if specialist else "a") * 64,
        created_at=AS_OF,
    )


def _comparison(lower: str = "0.001") -> ShadowComparisonResult:
    folds = [
        ShadowFoldResult(
            fold_number=index + 1,
            start_at=AS_OF - timedelta(days=(5 - index) * 30),
            end_at=AS_OF - timedelta(days=(4 - index) * 30),
            independent_decision_count=20,
            paired_net_return_delta=_interval(
                count=20,
                estimate="0.01",
                lower="0.001",
                upper="0.02",
            ),
            positive_point_estimate=True,
            created_at=AS_OF,
        )
        for index in range(5)
    ]
    regimes = [
        ShadowRegimeResult(
            regime=regime,
            independent_decision_count=30,
            paired_net_return_delta=_interval(
                count=30,
                estimate="0.01",
                lower="-0.001",
                upper="0.02",
            ),
            clearly_harmful=False,
            created_at=AS_OF,
        )
        for regime in (
            MarketRegime.HIGH_VOL_BULL,
            MarketRegime.PANIC,
            MarketRegime.RANGE,
        )
    ]
    return ShadowComparisonResult(
        baseline_arm_id="arm:base",
        experimental_arm_id="arm:specialist",
        specialist_skill_id="industry-bottleneck",
        paired_decision_count=100,
        unpaired_decision_count=0,
        missing_baseline_count=0,
        missing_experimental_count=0,
        pair_exclusion_counts={},
        paired_net_return_delta=_interval(
            count=100,
            estimate="0.01",
            lower=lower,
            upper="0.02",
        ),
        raw_one_sided_p_value=Decimal("0.001"),
        holm_adjusted_p_value=Decimal("0.002"),
        folds=folds,
        regimes=regimes,
        maximum_drawdown_delta=Decimal("0.01"),
        single_profit_contribution=Decimal("0.10"),
        regime_profit_contribution=Decimal("0.50"),
        created_at=AS_OF,
    )


def _report(comparison: ShadowComparisonResult) -> ShadowEvaluationReport:
    return ShadowEvaluationReport(
        report_id="report:ready",
        run_id="run:ready",
        study_id="study:ready",
        policy_version="shadow-evaluation-policy-v1",
        engine_version="shadow-evaluation-engine-v1",
        statistics_version="shadow-statistics-v1",
        required_phase8_observation_months=12,
        required_independent_decisions=100,
        required_regime_count=3,
        required_decisions_per_regime=30,
        required_walk_forward_folds=5,
        required_decisions_per_fold=20,
        as_of=AS_OF,
        evidence_status=ShadowEvidenceStatus.EVIDENCE_READY,
        observation_months=Decimal("12.1"),
        assignment_count=100,
        mature_observation_count=600,
        independent_decision_count=100,
        market_regime_counts={
            MarketRegime.HIGH_VOL_BULL: 30,
            MarketRegime.PANIC: 30,
            MarketRegime.RANGE: 40,
        },
        pit_status_counts={PointInTimeStatus.CERTIFIED: 100},
        exclusion_counts={},
        replay_quality_counts={ReplayQuality.DUAL_SOURCE_5M_VERIFIED: 600},
        input_assignment_sha256s=["a" * 64],
        input_observation_sha256s=["b" * 64],
        evaluation_input_sha256="c" * 64,
        phase6_contract_integrity=True,
        arm_metrics=[_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        comparisons=[comparison],
        finding_codes=["SHADOW_EVIDENCE_READY"],
        report_sha256="a" * 64,
        created_at=AS_OF,
    )


def test_phase8_admission_requires_ci_stability_and_all_hard_gates(tmp_path: Path) -> None:
    policy = load_shadow_evaluation_policy(
        PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
    )
    service = ShadowEvaluationService(
        StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations"),
        ObjectStore(tmp_path / "objects"),
        policy,
    )
    ready = service._admission(  # noqa: SLF001 - direct deterministic gate test
        _report(_comparison()),
        [_definition("arm:base", specialist=False), _definition("arm:specialist", specialist=True)],
        [_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        policy,
    )
    assert ready.status is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
    assert ready.eligible_experimental_arm_ids == ["arm:specialist"]
    assert all(
        ready.experimental_arm_gate_results["arm:specialist"].values()
    )
    assert not ready.online_weight_changes_allowed
    assert not ready.broker_execution_allowed

    uncertain = service._admission(  # noqa: SLF001 - direct deterministic gate test
        _report(_comparison(lower="0")),
        [_definition("arm:base", specialist=False), _definition("arm:specialist", specialist=True)],
        [_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        policy,
    )
    assert uncertain.status is Phase8AdmissionStatus.NOT_ELIGIBLE_NO_INCREMENT
    assert not uncertain.eligible_experimental_arm_ids

    four_positive_comparison = _comparison().model_copy(
        update={
            "folds": [
                fold.model_copy(update={"positive_point_estimate": index != 0})
                for index, fold in enumerate(_comparison().folds)
            ]
        }
    )
    four_positive = service._admission(  # noqa: SLF001
        _report(four_positive_comparison),
        [_definition("arm:base", specialist=False), _definition("arm:specialist", specialist=True)],
        [_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        policy,
    )
    assert four_positive.status is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH

    three_positive_comparison = _comparison().model_copy(
        update={
            "folds": [
                fold.model_copy(update={"positive_point_estimate": index >= 2})
                for index, fold in enumerate(_comparison().folds)
            ]
        }
    )
    three_positive = service._admission(  # noqa: SLF001
        _report(three_positive_comparison),
        [_definition("arm:base", specialist=False), _definition("arm:specialist", specialist=True)],
        [_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        policy,
    )
    assert three_positive.status is Phase8AdmissionStatus.NOT_ELIGIBLE_NO_INCREMENT

    isolated = service._admission(  # noqa: SLF001
        _report(_comparison()),
        [
            _definition("arm:base", specialist=False),
            _definition("arm:specialist", specialist=True, isolated=True),
        ],
        [_arm("arm:base", "0.14"), _arm("arm:specialist", "0.15")],
        policy,
    )
    assert isolated.status is Phase8AdmissionStatus.NOT_ELIGIBLE_NO_INCREMENT
    assert not isolated.experimental_arm_gate_results["arm:specialist"][
        "APPROVED_PRODUCTION_CONTRACT"
    ]

    with pytest.raises(ValidationError, match="full observation window"):
        ShadowEvaluationReport.model_validate(
            {
                **_report(_comparison()).model_dump(mode="python"),
                "observation_months": Decimal("11.99"),
            }
        )
    with pytest.raises(ValidationError, match="market-regime coverage"):
        ShadowEvaluationReport.model_validate(
            {
                **_report(_comparison()).model_dump(mode="python"),
                "market_regime_counts": {
                    MarketRegime.HIGH_VOL_BULL: 29,
                    MarketRegime.PANIC: 30,
                    MarketRegime.RANGE: 41,
                },
            }
        )
    comparison_99 = _comparison().model_copy(
        update={
            "paired_decision_count": 99,
            "paired_net_return_delta": _interval(
                count=99,
                estimate="0.01",
                lower="0.001",
                upper="0.02",
            ),
        }
    )
    with pytest.raises(ValidationError, match="complete walk-forward folds"):
        ShadowEvaluationReport.model_validate(
            {
                **_report(_comparison()).model_dump(mode="python"),
                "comparisons": [comparison_99.model_dump(mode="python")],
            }
        )
