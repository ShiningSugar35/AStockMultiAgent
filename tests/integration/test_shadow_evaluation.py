from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from astock.core.codex_runs import CodexRunService
from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    CodexRunInputManifest,
    CommitteeBudgetReport,
    CommitteeDecisionScope,
    CommitteeEntryOrderType,
    CommitteeNarrativeMode,
    CommitteeProtocolStatus,
    CommitteeRatioRange,
    CommitteeVerdict,
    ContextBudgetReport,
    DecisionPack,
    FrozenWeightProfile,
    Market,
    MarketRegime,
    MarketRegimeFeatures,
    Phase8AdmissionStatus,
    PointInTimeStatus,
    ReplayQuality,
    ResearchSkillStatus,
    ShadowAction,
    ShadowArmDraft,
    ShadowArmResearchStatus,
    ShadowArmSignal,
    ShadowArmType,
    ShadowArtifactReference,
    ShadowDecisionAssignmentRequest,
    ShadowEvidenceStatus,
    ShadowExecutionObservationDraft,
    ShadowFillStatus,
    ShadowObservationStatus,
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
TEST_NOW = SIGNAL + timedelta(days=500)


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
        "cost_model_version": "shadow-round-trip-cost-v1",
        "fill_model_version": "shadow-conservative-5m-v1",
        "corporate_action_version": "shadow-corporate-actions-v1",
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
        clock=lambda: TEST_NOW,
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
        cost_model_version="shadow-round-trip-cost-v1",
        fill_model_version="shadow-conservative-5m-v1",
        evidence_snapshot_id="snapshot:1",
        evidence_ids=["evidence:1"],
        effective_from=SIGNAL + timedelta(minutes=5),
        created_at=SIGNAL,
    )


def _decision() -> DecisionPack:
    return DecisionPack(
        decision_id="decision:1",
        bundle_id="bundle:1",
        company_id="company:1",
        scope=CommitteeDecisionScope.NEW_CANDIDATE,
        as_of=SIGNAL,
        rules_version="committee-rules-v1",
        engine_version="committee-engine-v1",
        frozen_input_hashes=["a" * 64, "b" * 64],
        verdict=CommitteeVerdict.PAPER_ELIGIBLE,
        expected_return_range=CommitteeRatioRange(
            lower=Decimal("0.10"),
            upper=Decimal("0.20"),
            evidence_ids=["evidence:1"],
            created_at=SIGNAL,
        ),
        downside_range=CommitteeRatioRange(
            lower=Decimal("-0.10"),
            upper=Decimal("-0.05"),
            evidence_ids=["evidence:1"],
            created_at=SIGNAL,
        ),
        confidence=Decimal("0.80"),
        hard_blocks=[],
        needs_info_task_ids=[],
        counter_case_trigger_codes=[],
        current_position=Decimal("0"),
        max_position=Decimal("0.10"),
        review_at=SIGNAL + timedelta(days=30),
        rationale_codes=["ALL_HARD_GATES_PASSED"],
        evidence_ids=["evidence:1"],
        context_budget=CommitteeBudgetReport(
            context=ContextBudgetReport(created_at=SIGNAL),
            within_limit=True,
            narrative_mode=CommitteeNarrativeMode.DETERMINISTIC_ONLY,
            provider_estimated_cost_cny=Decimal("0"),
            provider_cost_ceiling_cny=Decimal("0"),
            degradation_codes=[],
            created_at=SIGNAL,
        ),
        decision_sha256="d" * 64,
        created_at=SIGNAL,
    )


def _execution_fees(
    entry_price_fen: int,
    valuation_price_fen: int,
    quantity: int,
) -> tuple[int, int, int, int]:
    entry_value = entry_price_fen * quantity
    exit_value = valuation_price_fen * quantity

    def rounded(value: Decimal) -> int:
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    commission = max(500, rounded(Decimal(entry_value) * Decimal("0.0003")))
    commission += max(500, rounded(Decimal(exit_value) * Decimal("0.0003")))
    tax = rounded(Decimal(exit_value) * Decimal("0.0005"))
    transfer = rounded(Decimal(entry_value + exit_value) * Decimal("0.00001"))
    slippage = rounded(Decimal(entry_value + exit_value) * Decimal("0.0005"))
    return commission, tax, transfer, slippage


def _assignment_request(
    execution: ShadowStudyExecution,
    state: StateStore,
    objects: ObjectStore,
    service: ShadowEvaluationService,
) -> ShadowDecisionAssignmentRequest:
    manifest = execution.manifest
    arms = execution.arms
    references = [
        _register_input(
            state,
            objects,
            artifact_id="BaseCasePack:base:1",
            artifact_type="BaseCasePack",
            payload={"base_case_id": "base:1", "created_at": SIGNAL.isoformat()},
        ),
        _register_input(
            state,
            objects,
            artifact_id="DecisionPack:decision:1",
            artifact_type="DecisionPack",
            payload=_decision(),
        ),
        _register_input(
            state,
            objects,
            artifact_id="SpecialistDelta:delta:1",
            artifact_type="SpecialistDelta",
            payload={"delta_id": "delta:1", "created_at": SIGNAL.isoformat()},
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
        independence_key=service.build_independence_key(
            manifest.study_id,
            company_id="company:1",
            thesis_version="thesis-v1",
            event_id="event:1",
        ),
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
    arm_summary = service.repository.arm_summaries(first.manifest.study_id)[0]
    arm_path = objects.path_for(str(arm_summary["object_hash"]))
    original_arm_bytes = arm_path.read_bytes()
    arm_path.write_bytes(b"corrupted-shadow-arm")
    corrupted_audit = service.audit(first.manifest.study_id)
    assert corrupted_audit["status"] == "PARTIAL"
    assert corrupted_audit["finding_codes"] == [
        "SHADOW_AUDIT_INPUT_UNAVAILABLE_OR_INVALID"
    ]
    arm_path.write_bytes(original_arm_bytes)
    assert service.audit(first.manifest.study_id)["status"] == "PASS"
    with pytest.raises(ValueError, match="future as_of"):
        service.evaluate(first.manifest.study_id, as_of=TEST_NOW + timedelta(seconds=1))

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
    with pytest.raises(ValueError, match="frozen by their as-of time"):
        service.classify_regime(
            first.manifest.study_id,
            features.model_copy(update={"created_at": SIGNAL + timedelta(seconds=1)}),
        )
    regime = service.classify_regime(first.manifest.study_id, features)
    assert regime.regime is MarketRegime.PANIC
    assert (
        service.classify_regime(first.manifest.study_id, features).regime_id
        == regime.regime_id
    )
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM market_regime_index WHERE regime_id=?",
            (regime.regime_id,),
        )
    recovered_regime = service.recover_study(request)
    assert recovered_regime["audit_status"] == "PASS"
    assert service.repository.get_regime(regime.regime_id) == regime

    assignment_request = _assignment_request(first, state, objects, service)
    with pytest.raises(ValueError, match="frozen no later than signal time"):
        ShadowDecisionAssignmentRequest.model_validate(
            {
                **assignment_request.model_dump(mode="python"),
                "created_at": SIGNAL + timedelta(seconds=1),
            }
        )
    future_base_reference = _register_input(
        state,
        objects,
        artifact_id="BaseCasePack:base:future",
        artifact_type="BaseCasePack",
        payload={
            "base_case_id": "base:future",
            "created_at": (SIGNAL + timedelta(days=1)).isoformat(),
        },
    )
    future_signals = [
        signal.model_copy(
            update={
                "input_artifact_ids": [
                    (
                        future_base_reference.artifact_id
                        if artifact_id == "BaseCasePack:base:1"
                        else artifact_id
                    )
                    for artifact_id in signal.input_artifact_ids
                ]
            }
        )
        for signal in assignment_request.arm_signals
    ]
    future_request = assignment_request.model_copy(
        update={
            "artifact_references": sorted(
                [
                    future_base_reference
                    if item.artifact_id == "BaseCasePack:base:1"
                    else item
                    for item in assignment_request.artifact_references
                ],
                key=lambda item: item.artifact_id,
            ),
            "arm_signals": future_signals,
        }
    )
    with pytest.raises(ValueError, match="not frozen by signal time"):
        service.assign(future_request)
    assignment = service.assign(assignment_request)
    assert service.assign(assignment_request).assignment_id == assignment.assignment_id
    status = service.status(first.manifest.study_id)
    assert status.assignment_count == 1
    assert status.independent_decision_count == 1
    assert status.status == "COLLECTING"
    assert service.audit(first.manifest.study_id)["status"] == "PASS"
    missing_input_id = assignment.artifact_references[0].artifact_id
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM shadow_assignment_input_index "
            "WHERE assignment_id=? AND artifact_id=?",
            (assignment.assignment_id, missing_input_id),
        )
    missing_input_audit = service.audit(first.manifest.study_id)
    missing_input_codes = missing_input_audit["finding_codes"]
    assert isinstance(missing_input_codes, list)
    assert "SHADOW_ASSIGNMENT_INPUT_INDEX_MISMATCH" in missing_input_codes
    recovered_input = service.recover_study(request)
    assert recovered_input["audit_status"] == "PASS"
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM shadow_assignment_input_index WHERE assignment_id=?",
            (assignment.assignment_id,),
        )
        connection.execute(
            "DELETE FROM shadow_assignment_index WHERE assignment_id=?",
            (assignment.assignment_id,),
        )
    recovered_assignment = service.recover_study(request)
    assert recovered_assignment["audit_status"] == "PASS"
    assert service.repository.get_assignment(assignment.assignment_id) == assignment

    with pytest.raises(ValueError, match="independence key"):
        service.assign(
            assignment_request.model_copy(update={"independence_key": "user-picked"})
        )

    with pytest.raises(ValueError, match="retain every"):
        service.assign(
            assignment_request.model_copy(
                update={
                    "arm_signals": assignment_request.arm_signals[:-1],
                }
            )
        )

    paper_counts_before = _paper_counts(state)
    market_manifest = objects.put_json({"canonical": "dual-source-5m"})
    trading_calendar = objects.put_json({"trading_calendar": "xshg-v1"})
    candidate_snapshot = objects.put_json(
        {
            "candidate_set_id": "candidate-set:1",
            "frozen_at": SIGNAL.isoformat(),
            "members": ["600000"],
        }
    )
    corporate_actions = objects.put_json({"corporate_actions": "complete-v1"})
    delisting = objects.put_json({"delisting": "checked-v1"})
    return_by_type = {
        ShadowArmType.RULE_BASELINE: Decimal("0.03"),
        ShadowArmType.BASE_CASE_ONLY: Decimal("0.05"),
        ShadowArmType.BASE_CASE_PLUS_SPECIALIST: Decimal("0.06"),
        ShadowArmType.FULL_COMMITTEE: Decimal("0.055"),
        ShadowArmType.CSI300_BENCHMARK: Decimal("0.04"),
        ShadowArmType.EQUAL_WEIGHT_CANDIDATE: Decimal("0.045"),
    }
    recorded = []
    for arm in first.arms:
        result = return_by_type[arm.arm_type]
        valuation_price = 1000 + int(result * Decimal("1000"))
        gross_pnl = (valuation_price - 1000) * 100
        commission, tax, transfer, slippage = _execution_fees(
            1000,
            valuation_price,
            100,
        )
        net_pnl = gross_pnl - commission - tax - transfer - slippage
        draft = ShadowExecutionObservationDraft(
            observation_version="outcome-v1",
            study_id=first.manifest.study_id,
            assignment_id=assignment.assignment_id,
            arm_id=arm.arm_id,
            independence_key=assignment.independence_key,
            regime_id=regime.regime_id,
            company_id=assignment.company_id,
            symbol=(arm.benchmark_symbol or assignment.symbol),
            market=(
                Market.INDEX
                if arm.arm_type
                in {
                    ShadowArmType.CSI300_BENCHMARK,
                    ShadowArmType.CHINA_ALL_BENCHMARK,
                }
                else assignment.market
            ),
            horizon_days=60,
            trading_days_elapsed=60,
            action=ShadowAction.ENTER,
            signal_time=SIGNAL,
            entry_time=SIGNAL + timedelta(minutes=5),
            valuation_time=SIGNAL + timedelta(days=90),
            fill_status=ShadowFillStatus.FULL,
            requested_quantity=100,
            quantity=100,
            entry_price_fen=1000,
            valuation_price_fen=valuation_price,
            highest_price_fen=1000 + int(
                (result + Decimal("0.02")) * Decimal("1000")
            ),
            lowest_price_fen=980,
            gross_pnl_fen=gross_pnl,
            commission_fen=commission,
            tax_fen=tax,
            transfer_fee_fen=transfer,
            slippage_fen=slippage,
            net_pnl_fen=net_pnl,
            capital_at_risk_fen=100_000,
            normalization_notional_fen=first.manifest.fixed_notional_fen,
            net_return=Decimal(net_pnl) / Decimal(first.manifest.fixed_notional_fen),
            nav_before_fen=first.manifest.initial_capital_fen,
            nav_after_fen=first.manifest.initial_capital_fen + net_pnl,
            mfe=result + Decimal("0.02"),
            mae=Decimal("-0.02"),
            turnover_fen=(1000 + valuation_price) * 100,
            liquidity_score=Decimal("0.90"),
            market_volume_shares=10_000,
            participation_rate=Decimal("0.01"),
            replay_quality=ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
            cost_model_version=arm.cost_model_version,
            fill_model_version=arm.fill_model_version,
            corporate_action_version=arm.corporate_action_version,
            market_manifest_sha256=market_manifest.sha256,
            trading_calendar_snapshot_sha256=trading_calendar.sha256,
            candidate_set_snapshot_sha256=candidate_snapshot.sha256,
            corporate_action_snapshot_sha256=corporate_actions.sha256,
            delisting_snapshot_sha256=delisting.sha256,
            market_observation_ids=["bar:entry", "bar:valuation"],
            pit_statuses=[PointInTimeStatus.CERTIFIED],
            candidate_membership_pit_safe=True,
            corporate_action_coverage_complete=True,
            delisting_coverage_complete=True,
            t_plus_one_compliant=True,
            price_limit_compliant=True,
            suspension_compliant=True,
            optimistic_net_pnl_fen=net_pnl,
            created_at=SIGNAL + timedelta(days=90),
        )
        if not recorded:
            with pytest.raises(ValueError, match="derived by policy"):
                service.record_observation(
                    draft.model_copy(update={"exclusion_codes": ["USER_PICKED"]})
                )
            with pytest.raises(ValueError, match="future outcomes"):
                service.record_observation(
                    draft.model_copy(
                        update={"created_at": TEST_NOW + timedelta(seconds=1)}
                    )
                )
            with pytest.raises(ValueError, match="frozen cost model"):
                service.record_observation(
                    draft.model_copy(
                        update={"commission_fen": draft.commission_fen - 1}
                    )
                )
            with pytest.raises(ValueError, match="frozen study capital"):
                service.record_observation(
                    draft.model_copy(
                        update={
                            "nav_before_fen": draft.nav_before_fen - 1,
                            "nav_after_fen": draft.nav_after_fen - 1,
                        }
                    )
                )
            excessive_participation = draft.model_copy(
                update={
                    "market_volume_shares": 500,
                    "participation_rate": Decimal("0.2"),
                }
            )
            with pytest.raises(ValueError, match="participation limit"):
                service.record_observation(excessive_participation)
            future_candidate = objects.put_json(
                {
                    "candidate_set_id": "candidate-set:1",
                    "frozen_at": (SIGNAL + timedelta(seconds=1)).isoformat(),
                    "members": ["600000"],
                }
            )
            with pytest.raises(ValueError, match="frozen by signal time"):
                service.record_observation(
                    draft.model_copy(
                        update={
                            "candidate_set_snapshot_sha256": future_candidate.sha256
                        }
                    )
                )
        elif len(recorded) == 1:
            mismatched_candidate = objects.put_json(
                {
                    "candidate_set_id": "candidate-set:1",
                    "frozen_at": SIGNAL.isoformat(),
                    "members": ["600000"],
                    "revision": 2,
                }
            )
            with pytest.raises(ValueError, match="one frozen observation contract"):
                service.record_observation(
                    draft.model_copy(
                        update={
                            "candidate_set_snapshot_sha256": (
                                mismatched_candidate.sha256
                            )
                        }
                    )
                )
        observation = service.record_observation(draft)
        assert observation.status is ShadowObservationStatus.MATURE
        assert observation.formal_eligible
        assert service.record_observation(draft).observation_id == observation.observation_id
        recorded.append(observation)
    assert len(recorded) == 6
    recorded_by_type = {
        arm.arm_type: observation
        for arm, observation in zip(first.arms, recorded, strict=True)
    }
    assert all(service.parquet_store.path_for(item).is_file() for item in recorded)
    assert _paper_counts(state) == paper_counts_before

    evaluation = service.evaluate(
        first.manifest.study_id,
        as_of=SIGNAL + timedelta(days=90),
    )
    repeated = service.evaluate(
        first.manifest.study_id,
        as_of=SIGNAL + timedelta(days=90),
    )
    assert evaluation.report.report_id == repeated.report.report_id
    assert evaluation.report.evidence_status is ShadowEvidenceStatus.COLLECTING
    assert evaluation.report.independent_decision_count == 1
    assert evaluation.report.mature_observation_count == 6
    assert len(evaluation.report.comparisons) == 2
    assert all(
        item.unpaired_decision_count == 0
        and not item.pair_exclusion_counts
        for item in evaluation.report.comparisons
    )
    forged_metric = evaluation.report.arm_metrics[0].model_copy(
        update={"total_cost_fen": evaluation.report.arm_metrics[0].total_cost_fen + 1}
    )
    forged_report = evaluation.report.model_copy(
        update={"arm_metrics": [forged_metric, *evaluation.report.arm_metrics[1:]]}
    )
    assert not service._report_recalculation_matches(  # noqa: SLF001
        forged_report,
        study=first.manifest,
        arms=first.arms,
        assignments=[assignment],
        observations=recorded,
        policy=service.configured_policy,
    )
    assert (
        evaluation.admission.status
        is Phase8AdmissionStatus.NOT_ELIGIBLE_INSUFFICIENT_SAMPLE
    )
    assert not evaluation.admission.eligible_experimental_arm_ids
    assert _paper_counts(state) == paper_counts_before
    assert service.audit(first.manifest.study_id)["status"] == "PASS"
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM phase8_admission_index WHERE admission_id=?",
            (evaluation.admission.admission_id,),
        )
        connection.execute(
            "DELETE FROM shadow_report_index WHERE report_id=?",
            (evaluation.report.report_id,),
        )
    missing_evaluation_indexes = service.audit(first.manifest.study_id)
    missing_evaluation_codes = missing_evaluation_indexes["finding_codes"]
    assert isinstance(missing_evaluation_codes, list)
    assert "SHADOW_EVALUATION_REPORT_INDEX_MISSING" in missing_evaluation_codes
    recovered_evaluation = service.recover_study(request)
    assert recovered_evaluation["audit_status"] == "PASS"
    with state.transaction() as connection:
        connection.execute(
            "UPDATE shadow_report_index SET report_hash=? WHERE report_id=?",
            ("0" * 64, evaluation.report.report_id),
        )
    tampered_report = service.audit(first.manifest.study_id)
    assert tampered_report["status"] == "PARTIAL"
    tampered_codes = tampered_report["finding_codes"]
    assert isinstance(tampered_codes, list)
    assert "SHADOW_REPORT_HASH_MISMATCH" in tampered_codes
    with state.transaction() as connection:
        connection.execute(
            "UPDATE shadow_report_index SET report_hash=? WHERE report_id=?",
            (evaluation.report.report_sha256, evaluation.report.report_id),
        )
    assert service.audit(first.manifest.study_id)["status"] == "PASS"

    codex = CodexRunService(tmp_path / "codex-shadow", objects, state)
    for artifact_type, artifact_id, artifact in (
        (
            "ShadowEvaluationReport",
            f"ShadowEvaluationReport:{evaluation.report.report_id}",
            evaluation.report,
        ),
        (
            "Phase8AdmissionReport",
            f"Phase8AdmissionReport:{evaluation.admission.admission_id}",
            evaluation.admission,
        ),
    ):
        reference = codex.resolve_artifact_reference(artifact_id)
        run = codex.initialize(
            {"request": "explain frozen shadow result"},
            context_budget=ContextBudgetReport(
                selected_skills=["astock-research-orchestrator"],
                selected_artifacts=[artifact_id],
                created_at=SIGNAL,
            ),
            input_manifest=CodexRunInputManifest(
                selected_skills=["astock-research-orchestrator"],
                artifact_references=[reference],
                require_registered_output=True,
                created_at=SIGNAL,
            ),
        )
        draft_path = tmp_path / f"strict-{artifact_type}.json"
        draft_path.write_text(
            json.dumps(
                {
                    "artifact_type": artifact_type,
                    "payload": artifact.model_dump(mode="json"),
                    "citations": {},
                    "requested_commands": [],
                }
            ),
            encoding="utf-8",
        )
        codex.stage_draft(run.run_id, draft_path)
        imported = codex.import_draft(run.run_id)
        assert imported.valid, imported.errors
        assert codex.audit(run.run_id)["status"] == "PASS"

    original = recorded_by_type[ShadowArmType.RULE_BASELINE]
    assert original.valuation_price_fen is not None
    corrected_valuation_price = original.valuation_price_fen + 1
    corrected_gross_pnl = (corrected_valuation_price - 1000) * original.quantity
    corrected_commission, corrected_tax, corrected_transfer, corrected_slippage = (
        _execution_fees(1000, corrected_valuation_price, original.quantity)
    )
    corrected_pnl = (
        corrected_gross_pnl
        - corrected_commission
        - corrected_tax
        - corrected_transfer
        - corrected_slippage
    )
    corrected = ShadowExecutionObservationDraft.model_validate(
        {
            **original.model_dump(
                mode="python",
                exclude={
                    "schema_version",
                    "created_at",
                    "observation_id",
                    "status",
                    "formal_eligible",
                    "observation_sha256",
                },
            ),
            "observation_version": "outcome-v2",
            "supersedes_observation_id": original.observation_id,
            "valuation_price_fen": corrected_valuation_price,
            "turnover_fen": original.turnover_fen + original.quantity,
            "gross_pnl_fen": corrected_gross_pnl,
            "commission_fen": corrected_commission,
            "tax_fen": corrected_tax,
            "transfer_fee_fen": corrected_transfer,
            "slippage_fen": corrected_slippage,
            "net_pnl_fen": corrected_pnl,
            "net_return": Decimal(corrected_pnl)
            / Decimal(first.manifest.fixed_notional_fen),
            "nav_after_fen": original.nav_before_fen + corrected_pnl,
            "optimistic_net_pnl_fen": corrected_pnl,
            "created_at": original.created_at + timedelta(days=1),
        }
    )
    corrected_observation = service.record_observation(corrected)
    assert corrected_observation.supersedes_observation_id == original.observation_id
    corrected_parquet = service.parquet_store.path_for(corrected_observation)
    corrected_parquet.unlink()
    missing_parquet = service.audit(first.manifest.study_id)
    missing_parquet_codes = missing_parquet["finding_codes"]
    assert isinstance(missing_parquet_codes, list)
    assert "SHADOW_OBSERVATION_PARQUET_INVALID" in missing_parquet_codes
    recovered_parquet = service.recover_study(request)
    assert recovered_parquet["audit_status"] == "PASS"
    assert corrected_parquet.is_file()
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM shadow_observation_index WHERE observation_id=?",
            (corrected_observation.observation_id,),
        )
        connection.execute(
            "DELETE FROM shadow_observation_index WHERE observation_id=?",
            (original.observation_id,),
        )
    recovered_observations = service.recover_study(request)
    assert recovered_observations["audit_status"] == "PASS"
    before_correction = service.repository.observations(
        first.manifest.study_id,
        as_of=original.created_at,
    )
    after_correction = service.repository.observations(
        first.manifest.study_id,
        as_of=corrected.created_at,
    )
    assert original.observation_id in {item.observation_id for item in before_correction}
    assert corrected_observation.observation_id in {
        item.observation_id for item in after_correction
    }
    excluded_draft = ShadowExecutionObservationDraft.model_validate(
        {
            **corrected_observation.model_dump(
                mode="python",
                exclude={
                    "schema_version",
                    "created_at",
                    "observation_id",
                    "status",
                    "formal_eligible",
                    "observation_sha256",
                },
            ),
            "observation_version": "outcome-v3",
            "supersedes_observation_id": corrected_observation.observation_id,
            "candidate_membership_pit_safe": False,
            "created_at": corrected_observation.created_at + timedelta(days=1),
        }
    )
    excluded = service.record_observation(excluded_draft)
    assert excluded.status is ShadowObservationStatus.EXCLUDED
    assert not excluded.formal_eligible
    assert "CANDIDATE_MEMBERSHIP_NOT_PIT_SAFE" in excluded.exclusion_codes
    for source, field, code in (
        (
            recorded_by_type[ShadowArmType.EQUAL_WEIGHT_CANDIDATE],
            "t_plus_one_compliant",
            "T_PLUS_ONE_VIOLATION",
        ),
        (
            recorded_by_type[ShadowArmType.BASE_CASE_PLUS_SPECIALIST],
            "price_limit_compliant",
            "PRICE_LIMIT_VIOLATION",
        ),
        (
            recorded_by_type[ShadowArmType.FULL_COMMITTEE],
            "suspension_compliant",
            "SUSPENSION_CONSTRAINT_VIOLATION",
        ),
    ):
        constraint_draft = ShadowExecutionObservationDraft.model_validate(
            {
                **source.model_dump(
                    mode="python",
                    exclude={
                        "schema_version",
                        "created_at",
                        "observation_id",
                        "status",
                        "formal_eligible",
                        "observation_sha256",
                    },
                ),
                "observation_version": "outcome-v2",
                "supersedes_observation_id": source.observation_id,
                field: False,
                "created_at": source.created_at + timedelta(days=1),
            }
        )
        constraint_observation = service.record_observation(constraint_draft)
        assert constraint_observation.status is ShadowObservationStatus.EXCLUDED
        assert code in constraint_observation.exclusion_codes
    benchmark = recorded_by_type[ShadowArmType.CSI300_BENCHMARK]
    benchmark_updates = (
        (
            "outcome-v2",
            {"corporate_action_coverage_complete": False},
            "CORPORATE_ACTION_COVERAGE_INCOMPLETE",
        ),
        (
            "outcome-v3",
            {
                "corporate_action_coverage_complete": True,
                "delisting_coverage_complete": False,
            },
            "DELISTING_COVERAGE_INCOMPLETE",
        ),
        (
            "outcome-v4",
            {
                "delisting_coverage_complete": True,
                "pit_statuses": [PointInTimeStatus.APPROXIMATED],
            },
            "FORMAL_PIT_STATUS_FAILED",
        ),
    )
    for offset, (version, updates, code) in enumerate(benchmark_updates, start=1):
        benchmark_draft = ShadowExecutionObservationDraft.model_validate(
            {
                **benchmark.model_dump(
                    mode="python",
                    exclude={
                        "schema_version",
                        "created_at",
                        "observation_id",
                        "status",
                        "formal_eligible",
                        "observation_sha256",
                    },
                ),
                "observation_version": version,
                "supersedes_observation_id": benchmark.observation_id,
                "exclusion_codes": [],
                **updates,
                "created_at": benchmark.created_at + timedelta(days=offset),
            }
        )
        benchmark = service.record_observation(benchmark_draft)
        assert benchmark.status is ShadowObservationStatus.EXCLUDED
        assert code in benchmark.exclusion_codes
    post_exclusion = service.evaluate(
        first.manifest.study_id,
        as_of=SIGNAL + timedelta(days=92),
    )
    compared = {
        item.specialist_skill_id: item for item in post_exclusion.report.comparisons
    }
    missing_specialist = compared["industry-bottleneck"]
    assert missing_specialist.unpaired_decision_count == 1
    assert missing_specialist.missing_baseline_count == 0
    assert missing_specialist.missing_experimental_count == 1
    assert missing_specialist.pair_exclusion_counts == {
        "EXPERIMENTAL_MATURE_RESULT_MISSING": 1
    }
    assert service.audit(first.manifest.study_id)["status"] == "PASS"


def _paper_counts(state: StateStore) -> dict[str, int]:
    with state.connect() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "paper_account",
                "journal",
                "order_record",
                "fill",
                "position",
            )
        }
