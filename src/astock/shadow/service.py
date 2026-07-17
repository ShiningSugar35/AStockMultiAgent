"""Deterministic frozen-weight shadow study, assignment, and regime services."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from astock.core.errors import AStockError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    CommitteeVerdict,
    DecisionPack,
    Market,
    MarketRegime,
    MarketRegimeFeatures,
    MarketRegimeSnapshot,
    Phase8AdmissionReport,
    Phase8AdmissionStatus,
    PointInTimeStatus,
    ReplayQuality,
    ResearchSkillStatus,
    ShadowAction,
    ShadowArmDefinition,
    ShadowArmDraft,
    ShadowArmMetrics,
    ShadowArmResearchStatus,
    ShadowArmType,
    ShadowComparisonResult,
    ShadowDecisionAssignment,
    ShadowDecisionAssignmentRequest,
    ShadowEvaluationPolicy,
    ShadowEvaluationReport,
    ShadowEvidenceStatus,
    ShadowExecutionObservation,
    ShadowExecutionObservationDraft,
    ShadowFillStatus,
    ShadowFoldResult,
    ShadowObservationStatus,
    ShadowRegimeResult,
    ShadowStatusReport,
    ShadowStudyCreateRequest,
    ShadowStudyManifest,
    ShadowStudyMode,
    ShadowStudyPlan,
    TradeProtocol,
)
from astock.shadow.repository import ShadowRepository
from astock.shadow.statistics import (
    deterministic_block_bootstrap,
    holm_adjust,
    maximum_drawdown_from_pnl,
    mean,
    wilson_interval,
)
from astock.shadow.storage import ParquetShadowStore


@dataclass(frozen=True, slots=True)
class ShadowStudyExecution:
    manifest: ShadowStudyManifest
    arms: list[ShadowArmDefinition]
    object_sha256_by_id: dict[str, str]


@dataclass(frozen=True, slots=True)
class ShadowEvaluationExecution:
    report: ShadowEvaluationReport
    admission: Phase8AdmissionReport
    report_object_sha256: str
    admission_object_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedStudy:
    policy: ShadowEvaluationPolicy
    policy_object_hash: str
    config_hash: str
    request_hash: str
    manifest: ShadowStudyManifest
    manifest_object_hash: str
    arms: list[ShadowArmDefinition]
    arm_object_hashes: dict[str, str]


class ShadowEvaluationService:
    """No-network, no-broker shadow evaluation over immutable local inputs."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        policy: ShadowEvaluationPolicy,
        parquet_store: ParquetShadowStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.configured_policy = policy
        self.repository = ShadowRepository(state, object_store)
        self.parquet_store = parquet_store or ParquetShadowStore(
            state.path.parent / "data" / "parquet"
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_policy(self) -> tuple[ShadowEvaluationPolicy, str]:
        return self._policy_reference(persist=True)

    def plan_study(self, request: ShadowStudyCreateRequest) -> ShadowStudyPlan:
        prepared = self._prepare_study(request, persist=False)
        return ShadowStudyPlan(
            prospective_study_id=prepared.manifest.study_id,
            prospective_arm_ids=prepared.manifest.arm_ids,
            policy_version=prepared.policy.policy_version,
            engine_version=prepared.policy.engine_version,
            mode=request.mode,
            minimum_independent_decisions=prepared.policy.minimum_independent_decisions,
            minimum_observation_months=(
                prepared.policy.phase8_observation_months
                if request.mode is ShadowStudyMode.FORWARD_FORMAL
                else prepared.policy.provisional_observation_months
            ),
            created_at=request.created_at,
        )

    def create_study(self, request: ShadowStudyCreateRequest) -> ShadowStudyExecution:
        if request.created_at > self._now():
            raise ValueError("shadow studies cannot be created in the future")
        prepared = self._prepare_study(request, persist=True)
        return ShadowStudyExecution(
            manifest=prepared.manifest,
            arms=prepared.arms,
            object_sha256_by_id={
                prepared.manifest.study_id: prepared.manifest_object_hash,
                **prepared.arm_object_hashes,
            },
        )

    def recover_study(self, request: ShadowStudyCreateRequest) -> dict[str, object]:
        execution = self.create_study(request)
        recovered = self._recover_registered_children(execution.manifest.study_id)
        audit = self.audit(execution.manifest.study_id)
        return {
            "status": "RECOVERED_OR_ALREADY_COMPLETE",
            "study_id": execution.manifest.study_id,
            "arm_ids": execution.manifest.arm_ids,
            "recovered_counts": recovered,
            "audit_status": audit["status"],
            "finding_codes": audit["finding_codes"],
        }

    def latest_admission(self, study_id: str) -> dict[str, object]:
        if self.repository.study_summary(study_id) is None:
            return {
                "study_id": study_id,
                "status": "NOT_RUN",
                "online_weight_changes_allowed": False,
                "broker_execution_allowed": False,
            }
        summary = self.repository.latest_admission_summary(study_id)
        if summary is None:
            return {
                "study_id": study_id,
                "status": "NOT_EVALUATED",
                "online_weight_changes_allowed": False,
                "broker_execution_allowed": False,
            }
        report = self.repository.get_admission(str(summary["admission_id"]))
        if report is None:
            return {
                "study_id": study_id,
                "status": "PARTIAL",
                "finding_codes": ["PHASE8_ADMISSION_OBJECT_UNAVAILABLE"],
                "online_weight_changes_allowed": False,
                "broker_execution_allowed": False,
            }
        return report.model_dump(mode="json")

    def status(self, study_id: str | None = None) -> ShadowStatusReport:
        summary = (
            self.repository.study_summary(study_id)
            if study_id is not None
            else self.repository.latest_study_summary()
        )
        if summary is None:
            return ShadowStatusReport(
                study_id=study_id,
                status="NOT_RUN",
                arm_count=0,
                assignment_count=0,
                observation_count=0,
                mature_observation_count=0,
                independent_decision_count=0,
            )
        resolved_id = str(summary["study_id"])
        counts = self.repository.counts(resolved_id)
        report = self.repository.latest_report_summary(resolved_id)
        admission = self.repository.latest_admission_summary(resolved_id)
        return ShadowStatusReport(
            study_id=resolved_id,
            status=str(summary["evidence_status"]),
            arm_count=counts["arm_count"],
            assignment_count=counts["assignment_count"],
            observation_count=counts["observation_count"],
            mature_observation_count=counts["mature_observation_count"],
            independent_decision_count=counts["independent_decision_count"],
            report_id=(str(report["report_id"]) if report else None),
            admission_status=(
                Phase8AdmissionStatus(str(admission["admission_status"]))
                if admission
                else None
            ),
        )

    def classify_regime(
        self,
        study_id: str,
        features: MarketRegimeFeatures,
        *,
        persist: bool = True,
    ) -> MarketRegimeSnapshot:
        study = self._require_study(study_id)
        policy = self._policy_for_study(study)
        now = self._now()
        if features.created_at > now or (
            study.mode is ShadowStudyMode.FORWARD_FORMAL and features.as_of > now
        ):
            raise ValueError("shadow market regimes cannot use future facts")
        if study.mode is ShadowStudyMode.FORWARD_FORMAL and features.as_of < study.effective_from:
            raise ValueError("formal market regimes cannot precede the shadow study")
        if (
            study.mode is ShadowStudyMode.FORWARD_FORMAL
            and features.created_at > features.as_of
        ):
            raise ValueError("formal market regimes must be frozen by their as-of time")
        if not self.object_store.verify(features.feature_snapshot_sha256):
            raise ValueError("market-regime feature snapshot is unavailable or corrupted")
        regime, rationale = self._market_regime(features, policy)
        identity = {
            "study_id": study_id,
            "regime_rule_version": policy.regime_rule_version,
            "features": features,
            "regime": regime.value,
            "rationale_codes": rationale,
        }
        regime_hash = content_hash(identity)
        snapshot = MarketRegimeSnapshot(
            regime_id=f"market-regime:{regime_hash}",
            study_id=study_id,
            regime_rule_version=policy.regime_rule_version,
            features=features,
            regime=regime,
            rationale_codes=rationale,
            regime_sha256=regime_hash,
            created_at=features.as_of,
        )
        payload = canonical_json_bytes(snapshot.model_dump(mode="json"))
        object_hash = sha256_bytes(payload)
        if persist:
            self.object_store.put_bytes(payload)
            self.state.register_artifact(
                artifact_id=f"MarketRegimeSnapshot:{snapshot.regime_id}",
                artifact_type="MarketRegimeSnapshot",
                schema_version=snapshot.schema_version,
                object_hash=object_hash,
                input_hashes=[features.feature_snapshot_sha256],
            )
            self.repository.register_regime(snapshot, object_hash=object_hash)
        return snapshot

    def assign(
        self,
        request: ShadowDecisionAssignmentRequest,
    ) -> ShadowDecisionAssignment:
        study = self._require_study(request.study_id)
        now = self._now()
        if request.created_at > now or request.signal_time > now:
            raise ValueError("shadow assignments cannot be frozen in the future")
        expected_independence_key = self.build_independence_key(
            study.study_id,
            company_id=request.company_id,
            thesis_version=request.thesis_version,
            event_id=request.event_id,
        )
        if request.independence_key != expected_independence_key:
            raise ValueError("shadow independence key does not match the frozen rule")
        if request.candidate_set_id != study.candidate_set_id:
            raise ValueError("shadow assignment candidate set does not match the study")
        if request.signal_time < study.effective_from:
            raise ValueError("shadow assignment cannot precede study effectiveness")
        if study.observation_end and request.signal_time > study.observation_end:
            raise ValueError("shadow assignment exceeds the frozen retrospective window")
        arms = self.repository.get_arms(study.study_id)
        expected_arm_ids = sorted(arm.arm_id for arm in arms)
        actual_arm_ids = [signal.arm_id for signal in request.arm_signals]
        if actual_arm_ids != expected_arm_ids:
            raise ValueError("shadow assignment must retain every frozen study arm")
        registry = self._validate_artifact_references(request)
        protocol_artifact_id = f"TradeProtocol:{request.trade_protocol_id}"
        if protocol_artifact_id not in registry:
            raise ValueError("shadow assignments require the frozen TradeProtocol")
        protocol = TradeProtocol.model_validate_json(
            self.object_store.get_bytes(registry[protocol_artifact_id]["object_hash"])
        )
        if protocol.protocol_id != request.trade_protocol_id:
            raise ValueError("shadow TradeProtocol identity mismatch")
        if protocol.company_id != request.company_id:
            raise ValueError("shadow assignment company does not match TradeProtocol")
        if protocol.signal_time != request.signal_time:
            raise ValueError("shadow arms must share the TradeProtocol signal time")
        for arm in arms:
            if arm.cost_model_version != protocol.cost_model_version:
                raise ValueError("shadow arm cost model differs from TradeProtocol")
            if arm.fill_model_version != protocol.fill_model_version:
                raise ValueError("shadow arm fill model differs from TradeProtocol")
        self._validate_arm_inputs(request, arms, registry)
        self._validate_committee_contract(request, arms, registry, protocol)
        normalized = request.model_copy(update={"created_at": request.signal_time})
        normalized_assignment = normalized.model_dump(
            mode="python",
            exclude={"schema_version", "created_at"},
        )
        assignment_hash = content_hash(normalized_assignment)
        assignment = ShadowDecisionAssignment(
            **normalized_assignment,
            assignment_id=f"shadow-assignment:{assignment_hash}",
            assignment_sha256=assignment_hash,
            schema_version=request.schema_version,
            created_at=request.signal_time,
        )
        payload = canonical_json_bytes(assignment.model_dump(mode="json"))
        object_hash = sha256_bytes(payload)
        self.object_store.put_bytes(payload)
        self.state.register_artifact(
            artifact_id=f"ShadowDecisionAssignment:{assignment.assignment_id}",
            artifact_type="ShadowDecisionAssignment",
            schema_version=assignment.schema_version,
            object_hash=object_hash,
            input_hashes=sorted(
                [
                    study.study_sha256,
                    *(item.object_sha256 for item in assignment.artifact_references),
                ]
            ),
        )
        self.repository.register_assignment(assignment, object_hash=object_hash)
        return assignment

    def build_independence_key(
        self,
        study_id: str,
        *,
        company_id: str,
        thesis_version: str,
        event_id: str,
    ) -> str:
        study = self._require_study(study_id)
        policy = self._policy_for_study(study)
        identity = {
            "study_id": study_id,
            "independence_rule_version": policy.independence_rule_version,
            "company_id": company_id,
            "thesis_version": thesis_version,
            "event_id": event_id,
        }
        return f"shadow-independence:{content_hash(identity)}"

    def record_observation(
        self,
        draft: ShadowExecutionObservationDraft,
    ) -> ShadowExecutionObservation:
        if draft.exclusion_codes:
            raise ValueError(
                "shadow exclusion codes are derived by policy and cannot be submitted"
            )
        study = self._require_study(draft.study_id)
        policy = self._policy_for_study(study)
        now = self._now()
        if draft.created_at > now or (
            draft.valuation_time is not None and draft.valuation_time > now
        ):
            raise ValueError("shadow observations cannot use future outcomes")
        assignment = self.repository.get_assignment(draft.assignment_id)
        if assignment is None or assignment.study_id != study.study_id:
            raise ValueError("shadow observation assignment is unavailable")
        arm = self.repository.get_arm(draft.arm_id)
        if arm is None or arm.study_id != study.study_id:
            raise ValueError("shadow observation arm is unavailable")
        regime = self.repository.get_regime(draft.regime_id)
        if regime is None or regime.study_id != study.study_id:
            raise ValueError("shadow observation regime is unavailable")
        benchmark_arm = arm.arm_type in {
            ShadowArmType.CSI300_BENCHMARK,
            ShadowArmType.CHINA_ALL_BENCHMARK,
        }
        expected_symbol = arm.benchmark_symbol if benchmark_arm else assignment.symbol
        expected_market = Market.INDEX if benchmark_arm else assignment.market
        if (
            draft.independence_key != assignment.independence_key
            or draft.company_id != assignment.company_id
            or draft.symbol != expected_symbol
            or draft.market is not expected_market
            or draft.signal_time != assignment.signal_time
        ):
            raise ValueError("shadow observation does not match its frozen assignment")
        signal = next(
            (item for item in assignment.arm_signals if item.arm_id == arm.arm_id),
            None,
        )
        if signal is None or signal.action is not draft.action:
            raise ValueError("shadow observation action differs from the pre-outcome signal")
        if regime.features.as_of != assignment.signal_time:
            raise ValueError("shadow market regime must be frozen at the signal time")
        if draft.horizon_days not in policy.required_horizons:
            raise ValueError("shadow observation horizon is not configured")
        if draft.valuation_time and draft.valuation_time > draft.created_at:
            raise ValueError("shadow observation cannot be recorded before its valuation")
        if draft.capital_at_risk_fen > study.fixed_notional_fen:
            raise ValueError("shadow observation exceeds the frozen study notional")
        if draft.normalization_notional_fen != study.fixed_notional_fen:
            raise ValueError("shadow return denominator differs from the frozen notional")
        if draft.nav_before_fen != study.initial_capital_fen:
            raise ValueError("shadow observation NAV must start from frozen study capital")
        if draft.participation_rate > policy.maximum_participation_rate:
            raise ValueError("shadow observation exceeds the frozen participation limit")
        frozen_fact_hashes = {
            "market manifest": draft.market_manifest_sha256,
            "trading calendar": draft.trading_calendar_snapshot_sha256,
            "candidate set": draft.candidate_set_snapshot_sha256,
            "corporate action": draft.corporate_action_snapshot_sha256,
            "delisting": draft.delisting_snapshot_sha256,
        }
        for label, object_hash in frozen_fact_hashes.items():
            if not self.object_store.verify(object_hash):
                raise ValueError(f"shadow {label} snapshot is unavailable or corrupted")
        self._validate_candidate_snapshot(
            draft.candidate_set_snapshot_sha256,
            assignment=assignment,
        )
        protocol = self._assignment_protocol(assignment)
        if draft.entry_time and draft.entry_time < protocol.earliest_executable_time:
            raise ValueError("shadow execution precedes the TradeProtocol executable time")
        if draft.cost_model_version != arm.cost_model_version:
            raise ValueError("shadow observation cost model differs from its arm")
        if draft.fill_model_version != arm.fill_model_version:
            raise ValueError("shadow observation fill model differs from its arm")
        if draft.corporate_action_version != arm.corporate_action_version:
            raise ValueError("shadow observation corporate-action model differs from its arm")
        expected_fees = self._execution_fees(draft, policy)
        actual_fees = (
            draft.commission_fen,
            draft.tax_fen,
            draft.transfer_fee_fen,
            draft.slippage_fen,
        )
        if actual_fees != expected_fees:
            raise ValueError("shadow execution fees do not match the frozen cost model")
        for peer in self.repository.observations_for_assignment(
            draft.assignment_id,
            horizon_days=draft.horizon_days,
        ):
            if (
                peer.assignment_id != draft.assignment_id
                or peer.horizon_days != draft.horizon_days
                or peer.arm_id == draft.arm_id
            ):
                continue
            comparable_contract = (
                peer.signal_time,
                peer.valuation_time,
                peer.trading_days_elapsed,
                peer.regime_id,
                peer.trading_calendar_snapshot_sha256,
                peer.candidate_set_snapshot_sha256,
                peer.corporate_action_snapshot_sha256,
                peer.delisting_snapshot_sha256,
                peer.normalization_notional_fen,
                peer.cost_model_version,
                peer.fill_model_version,
                peer.corporate_action_version,
            )
            draft_contract = (
                draft.signal_time,
                draft.valuation_time,
                draft.trading_days_elapsed,
                draft.regime_id,
                draft.trading_calendar_snapshot_sha256,
                draft.candidate_set_snapshot_sha256,
                draft.corporate_action_snapshot_sha256,
                draft.delisting_snapshot_sha256,
                draft.normalization_notional_fen,
                draft.cost_model_version,
                draft.fill_model_version,
                draft.corporate_action_version,
            )
            if comparable_contract != draft_contract:
                raise ValueError("shadow arms do not share one frozen observation contract")

        exclusions = set(draft.exclusion_codes)
        if study.mode is ShadowStudyMode.EXPLORATORY_RETROSPECTIVE:
            exclusions.add("RETROSPECTIVE_EXPLORATORY_ONLY")
        if set(draft.pit_statuses) - set(policy.formal_pit_statuses):
            exclusions.add("FORMAL_PIT_STATUS_FAILED")
        if regime.regime is MarketRegime.UNCLASSIFIED:
            exclusions.add("MARKET_REGIME_UNCLASSIFIED")
        if draft.replay_quality is ReplayQuality.UNREPLAYABLE:
            exclusions.add("UNREPLAYABLE_MARKET_PATH")
        if not signal.comparable:
            exclusions.add("ARM_NOT_COMPARABLE")
        if arm.research_status is ShadowArmResearchStatus.RESEARCH_ISOLATED:
            exclusions.add("RESEARCH_ISOLATED_ARM")
        if not draft.candidate_membership_pit_safe:
            exclusions.add("CANDIDATE_MEMBERSHIP_NOT_PIT_SAFE")
        if not draft.corporate_action_coverage_complete:
            exclusions.add("CORPORATE_ACTION_COVERAGE_INCOMPLETE")
        if not draft.delisting_coverage_complete:
            exclusions.add("DELISTING_COVERAGE_INCOMPLETE")
        if not draft.t_plus_one_compliant:
            exclusions.add("T_PLUS_ONE_VIOLATION")
        if not draft.price_limit_compliant:
            exclusions.add("PRICE_LIMIT_VIOLATION")
        if not draft.suspension_compliant:
            exclusions.add("SUSPENSION_CONSTRAINT_VIOLATION")
        mature = draft.trading_days_elapsed >= draft.horizon_days
        status = (
            ShadowObservationStatus.EXCLUDED
            if exclusions
            else (
                ShadowObservationStatus.MATURE
                if mature
                else ShadowObservationStatus.PENDING_MATURITY
            )
        )
        formal_eligible = (
            status is ShadowObservationStatus.MATURE
            and study.mode is ShadowStudyMode.FORWARD_FORMAL
        )
        normalized_draft = draft.model_copy(
            update={"exclusion_codes": sorted(exclusions)}
        )
        identity = {
            "draft": normalized_draft.model_dump(
                mode="python",
                exclude={"schema_version", "created_at"},
            ),
            "status": status.value,
            "formal_eligible": formal_eligible,
            "policy_version": policy.policy_version,
        }
        observation_hash = content_hash(identity)
        observation = ShadowExecutionObservation(
            **normalized_draft.model_dump(
                mode="python",
                exclude={"schema_version", "created_at"},
            ),
            observation_id=f"shadow-observation:{observation_hash}",
            status=status,
            formal_eligible=formal_eligible,
            observation_sha256=observation_hash,
            schema_version=draft.schema_version,
            created_at=draft.created_at,
        )
        payload = canonical_json_bytes(observation.model_dump(mode="json"))
        object_hash = sha256_bytes(payload)
        existing = self.repository.observation_summary(observation.observation_id)
        if existing is None:
            latest = self.repository.latest_observation_summary(
                assignment_id=assignment.assignment_id,
                arm_id=arm.arm_id,
                horizon_days=draft.horizon_days,
            )
            if latest is None and draft.supersedes_observation_id is not None:
                raise ValueError("first shadow observation version cannot supersede an object")
            if latest is not None:
                if draft.supersedes_observation_id != str(latest["observation_id"]):
                    raise ValueError("new shadow observation must supersede the latest version")
                latest_created_at = datetime.fromisoformat(str(latest["created_at"]))
                if draft.created_at <= latest_created_at:
                    raise ValueError("shadow observation versions must advance in time")
        self.object_store.put_bytes(payload)
        self.parquet_store.write(observation, object_sha256=object_hash)
        parent_hashes: list[str] = []
        if draft.supersedes_observation_id:
            parent = self.repository.observation_summary(draft.supersedes_observation_id)
            if parent is None:
                raise ValueError("superseded shadow observation is unavailable")
            parent_hashes.append(str(parent["object_hash"]))
        self.state.register_artifact(
            artifact_id=f"ShadowExecutionObservation:{observation.observation_id}",
            artifact_type="ShadowExecutionObservation",
            schema_version=observation.schema_version,
            object_hash=object_hash,
            input_hashes=sorted(
                [
                    assignment.assignment_sha256,
                    regime.regime_sha256,
                    *frozen_fact_hashes.values(),
                    *parent_hashes,
                ]
            ),
        )
        self.repository.register_observation(observation, object_hash=object_hash)
        return observation

    def evaluate(
        self,
        study_id: str,
        *,
        as_of: datetime,
    ) -> ShadowEvaluationExecution:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("shadow evaluation as_of must be timezone-aware")
        if as_of > self._now():
            raise ValueError("shadow evaluation cannot use a future as_of")
        study = self._require_study(study_id)
        policy = self._policy_for_study(study)
        if as_of < study.effective_from:
            raise ValueError("shadow evaluation cannot precede study effectiveness")
        assignments = [
            item for item in self.repository.assignments(study_id) if item.signal_time <= as_of
        ]
        observations = self.repository.observations(study_id, as_of=as_of)
        arms = self.repository.get_arms(study_id)
        phase6_contract_integrity = self._phase6_contract_integrity(assignments, arms)
        final_observations = [
            item
            for item in observations
            if item.horizon_days == policy.final_horizon_days
        ]
        formal = [
            item
            for item in final_observations
            if item.status is ShadowObservationStatus.MATURE and item.formal_eligible
        ]
        arm_metrics = [
            self._arm_metrics(
                arm,
                [item for item in formal if item.arm_id == arm.arm_id],
                policy,
                initial_capital_fen=study.initial_capital_fen,
            )
            for arm in arms
        ]
        arm_metrics.sort(key=lambda item: item.arm_id)
        comparisons = self._comparisons(
            assignments,
            arms,
            formal,
            arm_metrics,
            policy,
        )
        comparisons = self._apply_holm(comparisons)

        unique_assignment_observation: dict[str, ShadowExecutionObservation] = {}
        for observation in sorted(formal, key=lambda item: (item.assignment_id, item.arm_id)):
            unique_assignment_observation.setdefault(observation.assignment_id, observation)
        regime_counts: Counter[MarketRegime] = Counter()
        for item in unique_assignment_observation.values():
            regime = self.repository.get_regime(item.regime_id)
            if regime is not None:
                regime_counts[regime.regime] += 1
        pit_counts: Counter[PointInTimeStatus] = Counter()
        for item in unique_assignment_observation.values():
            pit_counts.update(item.pit_statuses)
        exclusion_counts: Counter[str] = Counter()
        for item in final_observations:
            exclusion_counts.update(item.exclusion_codes)
        replay_quality_counts: Counter[ReplayQuality] = Counter(
            item.replay_quality for item in final_observations
        )
        observation_months = self._observation_months(assignments, as_of)
        evidence_status, findings = self._evidence_status(
            study,
            assignments,
            arms,
            final_observations,
            formal,
            comparisons,
            regime_counts,
            observation_months,
            policy,
            phase6_contract_integrity=phase6_contract_integrity,
        )
        assignment_hashes = sorted(
            {item.assignment_sha256 for item in assignments}
        )
        observation_hashes = sorted(
            {item.observation_sha256 for item in observations}
        )
        input_hash = content_hash(
            {
                "study_sha256": study.study_sha256,
                "as_of": as_of,
                "policy_version": policy.policy_version,
                "statistics_version": policy.statistics_version,
                "assignments": assignment_hashes,
                "observations": observation_hashes,
            }
        )
        run_id = f"shadow-run:{input_hash}"
        report_fields = {
            "run_id": run_id,
            "study_id": study_id,
            "policy_version": policy.policy_version,
            "engine_version": policy.engine_version,
            "statistics_version": policy.statistics_version,
            "required_phase8_observation_months": policy.phase8_observation_months,
            "required_independent_decisions": policy.minimum_independent_decisions,
            "required_regime_count": policy.minimum_regime_count,
            "required_decisions_per_regime": policy.minimum_decisions_per_regime,
            "required_walk_forward_folds": policy.minimum_walk_forward_folds,
            "required_decisions_per_fold": policy.minimum_decisions_per_fold,
            "as_of": as_of,
            "evidence_status": evidence_status,
            "observation_months": observation_months,
            "assignment_count": len(assignments),
            "mature_observation_count": len(formal),
            "independent_decision_count": len(
                {item.independence_key for item in assignments}
            ),
            "market_regime_counts": {
                key: regime_counts[key] for key in sorted(regime_counts, key=str)
            },
            "pit_status_counts": {
                key: pit_counts[key] for key in sorted(pit_counts, key=str)
            },
            "exclusion_counts": {
                key: exclusion_counts[key] for key in sorted(exclusion_counts)
            },
            "replay_quality_counts": {
                key: replay_quality_counts[key]
                for key in sorted(replay_quality_counts, key=str)
            },
            "input_assignment_sha256s": assignment_hashes,
            "input_observation_sha256s": observation_hashes,
            "evaluation_input_sha256": input_hash,
            "phase6_contract_integrity": phase6_contract_integrity,
            "arm_metrics": arm_metrics,
            "comparisons": comparisons,
            "finding_codes": findings,
        }
        report_hash = content_hash(_without_created_at(report_fields))
        report = ShadowEvaluationReport(
            report_id=f"shadow-report:{report_hash}",
            **report_fields,
            report_sha256=report_hash,
            created_at=as_of,
        )
        report = ShadowEvaluationReport.model_validate(
            _replace_created_at(report.model_dump(mode="python"), as_of)
        )
        admission = self._admission(report, arms, arm_metrics, policy)
        report_payload = canonical_json_bytes(report.model_dump(mode="json"))
        report_object_hash = sha256_bytes(report_payload)
        admission_payload = canonical_json_bytes(admission.model_dump(mode="json"))
        admission_object_hash = sha256_bytes(admission_payload)
        self.object_store.put_bytes(report_payload)
        self.object_store.put_bytes(admission_payload)
        self.state.register_artifact(
            artifact_id=f"ShadowEvaluationReport:{report.report_id}",
            artifact_type="ShadowEvaluationReport",
            schema_version=report.schema_version,
            object_hash=report_object_hash,
            input_hashes=sorted(
                [
                    study.study_sha256,
                    *(item.assignment_sha256 for item in assignments),
                    *(item.observation_sha256 for item in observations),
                ]
            ),
        )
        self.state.register_artifact(
            artifact_id=f"Phase8AdmissionReport:{admission.admission_id}",
            artifact_type="Phase8AdmissionReport",
            schema_version=admission.schema_version,
            object_hash=admission_object_hash,
            input_hashes=[report_object_hash],
        )
        self.repository.register_evaluation(
            run_id=run_id,
            input_hash=input_hash,
            report=report,
            report_object_hash=report_object_hash,
            admission=admission,
            admission_object_hash=admission_object_hash,
        )
        return ShadowEvaluationExecution(
            report=report,
            admission=admission,
            report_object_sha256=report_object_hash,
            admission_object_sha256=admission_object_hash,
        )

    def audit(self, study_id: str) -> dict[str, object]:
        try:
            return self._audit_study(study_id)
        except (AStockError, OSError, ValueError):
            return {
                "study_id": study_id,
                "status": "PARTIAL",
                "finding_codes": ["SHADOW_AUDIT_INPUT_UNAVAILABLE_OR_INVALID"],
                "counts": self.repository.counts(study_id),
            }

    def _audit_study(self, study_id: str) -> dict[str, object]:
        study_summary = self.repository.study_summary(study_id)
        if study_summary is None:
            return {
                "study_id": study_id,
                "status": "NOT_RUN",
                "finding_codes": ["SHADOW_STUDY_NOT_RUN"],
            }
        findings: set[str] = set()
        study_hash = str(study_summary["object_hash"])
        if not self.object_store.verify(study_hash):
            findings.add("SHADOW_STUDY_OBJECT_INVALID")
            return {
                "study_id": study_id,
                "status": "PARTIAL",
                "finding_codes": sorted(findings),
            }
        study = self.repository.get_study(study_id)
        assert study is not None
        policy_summary = self.repository.policy_summary(study.policy_version)
        if policy_summary is None or not self.object_store.verify(
            str(policy_summary["object_hash"])
        ):
            findings.add("SHADOW_POLICY_INVALID")
        elif not self._artifact_matches(
            f"ShadowEvaluationPolicy:{study.policy_version}",
            "ShadowEvaluationPolicy",
            str(policy_summary["object_hash"]),
            input_hashes=[study.config_sha256],
        ):
            findings.add("SHADOW_POLICY_REGISTRY_MISMATCH")
        arms = self.repository.get_arms(study_id)
        if sorted(arm.arm_id for arm in arms) != study.arm_ids:
            findings.add("SHADOW_ARM_SET_MISMATCH")
        arm_hashes: dict[str, str] = {}
        arm_object_hashes: dict[str, str] = {}
        for arm, row in zip(arms, self.repository.arm_summaries(study_id), strict=True):
            if not self.object_store.verify(str(row["object_hash"])):
                findings.add("SHADOW_ARM_OBJECT_INVALID")
            if content_hash(self._arm_identity(arm, study)) != arm.arm_sha256:
                findings.add("SHADOW_ARM_HASH_MISMATCH")
            if not self._artifact_matches(
                f"ShadowArmDefinition:{arm.arm_id}",
                "ShadowArmDefinition",
                str(row["object_hash"]),
                input_hashes=[study.config_sha256, arm.arm_sha256],
            ):
                findings.add("SHADOW_ARM_REGISTRY_MISMATCH")
            arm_hashes[arm.arm_id] = arm.arm_sha256
            arm_object_hashes[arm.arm_id] = str(row["object_hash"])
        if content_hash(self._study_identity(study, arm_hashes)) != study.study_sha256:
            findings.add("SHADOW_STUDY_HASH_MISMATCH")
        if not self._artifact_matches(
            f"ShadowStudyManifest:{study.study_id}",
            "ShadowStudyManifest",
            study_hash,
            input_hashes=sorted(
                [study.config_sha256, *arm_object_hashes.values()]
            ),
        ):
            findings.add("SHADOW_STUDY_REGISTRY_MISMATCH")
        assignments = self.repository.assignments(study_id)
        for assignment in assignments:
            row = self.repository.assignment_summary(assignment.assignment_id)
            if row is None or not self.object_store.verify(str(row["object_hash"])):
                findings.add("SHADOW_ASSIGNMENT_OBJECT_INVALID")
            if content_hash(
                assignment.model_dump(
                    mode="python",
                    exclude={
                        "schema_version",
                        "created_at",
                        "assignment_id",
                        "assignment_sha256",
                    },
                )
            ) != assignment.assignment_sha256:
                findings.add("SHADOW_ASSIGNMENT_HASH_MISMATCH")
            expected_inputs = [
                (
                    item.artifact_id,
                    item.artifact_type,
                    item.object_sha256,
                    item.available_at.astimezone(UTC).isoformat(),
                )
                for item in assignment.artifact_references
            ]
            actual_inputs = [
                (
                    str(item["artifact_id"]),
                    str(item["artifact_type"]),
                    str(item["object_hash"]),
                    str(item["available_at"]),
                )
                for item in self.repository.assignment_inputs(assignment.assignment_id)
            ]
            if actual_inputs != expected_inputs:
                findings.add("SHADOW_ASSIGNMENT_INPUT_INDEX_MISMATCH")
            if row is not None and not self._artifact_matches(
                f"ShadowDecisionAssignment:{assignment.assignment_id}",
                "ShadowDecisionAssignment",
                str(row["object_hash"]),
                input_hashes=sorted(
                    [
                        study.study_sha256,
                        *(
                            item.object_sha256
                            for item in assignment.artifact_references
                        ),
                    ]
                ),
            ):
                findings.add("SHADOW_ASSIGNMENT_REGISTRY_MISMATCH")
        if not self._phase6_contract_integrity(assignments, arms):
            findings.add("SHADOW_PHASE6_CONTRACT_INVALID")
        for row in self.repository.regime_summaries(study_id):
            snapshot = self.repository.get_regime(str(row["regime_id"]))
            if snapshot is None or not self.object_store.verify(str(row["object_hash"])):
                findings.add("MARKET_REGIME_OBJECT_INVALID")
                continue
            identity = {
                "study_id": study_id,
                "regime_rule_version": snapshot.regime_rule_version,
                "features": snapshot.features,
                "regime": snapshot.regime.value,
                "rationale_codes": snapshot.rationale_codes,
            }
            if content_hash(identity) != snapshot.regime_sha256:
                findings.add("MARKET_REGIME_HASH_MISMATCH")
            if not self.object_store.verify(snapshot.features.feature_snapshot_sha256):
                findings.add("MARKET_REGIME_FEATURE_OBJECT_INVALID")
            if not self._artifact_matches(
                f"MarketRegimeSnapshot:{snapshot.regime_id}",
                "MarketRegimeSnapshot",
                str(row["object_hash"]),
                input_hashes=[snapshot.features.feature_snapshot_sha256],
            ):
                findings.add("MARKET_REGIME_REGISTRY_MISMATCH")
        policy = self._policy_for_study(study)
        previous_by_series: dict[tuple[str, str, int], str] = {}
        for row in self.repository.observation_summaries(study_id):
            observation_id = str(row["observation_id"])
            if not self.object_store.verify(str(row["object_hash"])):
                findings.add("SHADOW_OBSERVATION_OBJECT_INVALID")
                continue
            observation = self.repository.get_observation(observation_id)
            if observation is None:
                findings.add("SHADOW_OBSERVATION_OBJECT_INVALID")
                continue
            identity = {
                "draft": observation.model_dump(
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
                "status": observation.status.value,
                "formal_eligible": observation.formal_eligible,
                "policy_version": policy.policy_version,
            }
            if content_hash(identity) != observation.observation_sha256:
                findings.add("SHADOW_OBSERVATION_HASH_MISMATCH")
            if not self.parquet_store.verify(
                observation,
                object_sha256=str(row["object_hash"]),
            ):
                findings.add("SHADOW_OBSERVATION_PARQUET_INVALID")
            series = (
                observation.assignment_id,
                observation.arm_id,
                observation.horizon_days,
            )
            expected_parent_id = previous_by_series.get(series)
            if observation.supersedes_observation_id != expected_parent_id:
                findings.add("SHADOW_OBSERVATION_VERSION_CHAIN_INVALID")
            previous_by_series[series] = observation.observation_id
            assignment = self.repository.get_assignment(observation.assignment_id)
            regime = self.repository.get_regime(observation.regime_id)
            parent_hashes: list[str] = []
            if observation.supersedes_observation_id is not None:
                parent = self.repository.observation_summary(
                    observation.supersedes_observation_id
                )
                if parent is None:
                    findings.add("SHADOW_OBSERVATION_PARENT_MISSING")
                else:
                    parent_hashes.append(str(parent["object_hash"]))
            fact_hashes = [
                observation.market_manifest_sha256,
                observation.trading_calendar_snapshot_sha256,
                observation.candidate_set_snapshot_sha256,
                observation.corporate_action_snapshot_sha256,
                observation.delisting_snapshot_sha256,
            ]
            if any(not self.object_store.verify(item) for item in fact_hashes):
                findings.add("SHADOW_OBSERVATION_FACT_SNAPSHOT_INVALID")
            expected_inputs = (
                sorted(
                    [
                        assignment.assignment_sha256,
                        regime.regime_sha256,
                        *fact_hashes,
                        *parent_hashes,
                    ]
                )
                if assignment is not None and regime is not None
                else None
            )
            if expected_inputs is None:
                findings.add("SHADOW_OBSERVATION_PARENT_INPUT_INVALID")
            if not self._artifact_matches(
                f"ShadowExecutionObservation:{observation.observation_id}",
                "ShadowExecutionObservation",
                str(row["object_hash"]),
                input_hashes=expected_inputs,
            ):
                findings.add("SHADOW_OBSERVATION_REGISTRY_MISMATCH")
        reports: dict[str, tuple[ShadowEvaluationReport, str]] = {}
        for report_summary in self.repository.report_summaries(study_id):
            report_id = str(report_summary["report_id"])
            report = self.repository.get_report(report_id)
            report_object_hash = str(report_summary["object_hash"])
            if report is None or not self.object_store.verify(report_object_hash):
                findings.add("SHADOW_REPORT_OBJECT_INVALID")
                continue
            reports[report_id] = (report, report_object_hash)
            if report.report_sha256 != str(report_summary["report_hash"]):
                findings.add("SHADOW_REPORT_HASH_MISMATCH")
            if content_hash(
                _without_created_at(
                    report.model_dump(
                        mode="python",
                        exclude={
                            "schema_version",
                            "created_at",
                            "report_id",
                            "report_sha256",
                        },
                    )
                )
            ) != report.report_sha256:
                findings.add("SHADOW_REPORT_CONTENT_HASH_MISMATCH")
            expected_input_hash = content_hash(
                {
                    "study_sha256": study.study_sha256,
                    "as_of": report.as_of,
                    "policy_version": report.policy_version,
                    "statistics_version": report.statistics_version,
                    "assignments": report.input_assignment_sha256s,
                    "observations": report.input_observation_sha256s,
                }
            )
            if (
                expected_input_hash != report.evaluation_input_sha256
                or report.run_id != f"shadow-run:{expected_input_hash}"
            ):
                findings.add("SHADOW_REPORT_INPUT_HASH_MISMATCH")
            run = self.repository.evaluation_run_summary(report.run_id)
            if (
                run is None
                or str(run["study_id"]) != study_id
                or str(run["input_hash"]) != report.evaluation_input_sha256
                or str(run["report_id"]) != report.report_id
                or str(run["report_object_hash"]) != report_object_hash
                or str(run["run_status"]) != "COMPLETED"
            ):
                findings.add("SHADOW_EVALUATION_RUN_INDEX_MISMATCH")
            report_assignments = [
                item
                for item in self.repository.assignments(study_id)
                if item.signal_time <= report.as_of
            ]
            report_observations = self.repository.observations(
                study_id,
                as_of=report.as_of,
            )
            if report.input_assignment_sha256s != sorted(
                {item.assignment_sha256 for item in report_assignments}
            ):
                findings.add("SHADOW_REPORT_ASSIGNMENT_SET_MISMATCH")
            if report.input_observation_sha256s != sorted(
                {item.observation_sha256 for item in report_observations}
            ):
                findings.add("SHADOW_REPORT_OBSERVATION_SET_MISMATCH")
            if report.phase6_contract_integrity != self._phase6_contract_integrity(
                report_assignments,
                arms,
            ):
                findings.add("SHADOW_REPORT_PHASE6_CONTRACT_MISMATCH")
            if not self._report_recalculation_matches(
                report,
                study=study,
                arms=arms,
                assignments=report_assignments,
                observations=report_observations,
                policy=policy,
            ):
                findings.add("SHADOW_REPORT_RECALCULATION_MISMATCH")
            if not self._artifact_matches(
                f"ShadowEvaluationReport:{report.report_id}",
                "ShadowEvaluationReport",
                report_object_hash,
                input_hashes=sorted(
                    [
                        study.study_sha256,
                        *report.input_assignment_sha256s,
                        *report.input_observation_sha256s,
                    ]
                ),
            ):
                findings.add("SHADOW_REPORT_REGISTRY_MISMATCH")
        admission_report_ids: set[str] = set()
        for admission_summary in self.repository.admission_summaries(study_id):
            admission = self.repository.get_admission(
                str(admission_summary["admission_id"])
            )
            if admission is None or not self.object_store.verify(
                str(admission_summary["object_hash"])
            ):
                findings.add("PHASE8_ADMISSION_OBJECT_INVALID")
            else:
                admission_report_ids.add(admission.shadow_report_id)
                identity = {
                    "study_id": admission.study_id,
                    "shadow_report_id": admission.shadow_report_id,
                    "shadow_report_sha256": admission.shadow_report_sha256,
                    "status": admission.status.value,
                    "gate_results": admission.gate_results,
                    "experimental_arm_gate_results": (
                        admission.experimental_arm_gate_results
                    ),
                    "eligible_experimental_arm_ids": (
                        admission.eligible_experimental_arm_ids
                    ),
                    "reason_codes": admission.reason_codes,
                }
                if content_hash(identity) != admission.admission_sha256:
                    findings.add("PHASE8_ADMISSION_HASH_MISMATCH")
                report_entry = reports.get(admission.shadow_report_id)
                if report_entry is None:
                    findings.add("PHASE8_ADMISSION_REPORT_MISSING")
                    report_object_hash = ""
                else:
                    report, report_object_hash = report_entry
                    expected_admission = self._admission(
                        report,
                        arms,
                        report.arm_metrics,
                        policy,
                    )
                    if (
                        expected_admission.model_dump(mode="json")
                        != admission.model_dump(mode="json")
                    ):
                        findings.add("PHASE8_ADMISSION_RECALCULATION_MISMATCH")
                if not self._artifact_matches(
                    f"Phase8AdmissionReport:{admission.admission_id}",
                    "Phase8AdmissionReport",
                    str(admission_summary["object_hash"]),
                    input_hashes=[report_object_hash],
                ):
                    findings.add("PHASE8_ADMISSION_REGISTRY_MISMATCH")
        for run in self.repository.evaluation_run_summaries(study_id):
            report_id = str(run["report_id"])
            report_entry = reports.get(report_id)
            if report_entry is None:
                findings.add("SHADOW_EVALUATION_REPORT_INDEX_MISSING")
                continue
            _, report_object_hash = report_entry
            if (
                str(run["run_status"]) != "COMPLETED"
                or str(run["report_object_hash"]) != report_object_hash
            ):
                findings.add("SHADOW_EVALUATION_RUN_INDEX_MISMATCH")
            if report_id not in admission_report_ids:
                findings.add("PHASE8_ADMISSION_INDEX_MISSING")
        return {
            "study_id": study_id,
            "status": "PASS" if not findings else "PARTIAL",
            "finding_codes": sorted(findings),
            "counts": self.repository.counts(study_id),
        }

    def _report_recalculation_matches(
        self,
        report: ShadowEvaluationReport,
        *,
        study: ShadowStudyManifest,
        arms: list[ShadowArmDefinition],
        assignments: list[ShadowDecisionAssignment],
        observations: list[ShadowExecutionObservation],
        policy: ShadowEvaluationPolicy,
    ) -> bool:
        final_observations = [
            item
            for item in observations
            if item.horizon_days == policy.final_horizon_days
        ]
        formal = [
            item
            for item in final_observations
            if item.status is ShadowObservationStatus.MATURE and item.formal_eligible
        ]
        arm_metrics = [
            self._arm_metrics(
                arm,
                [item for item in formal if item.arm_id == arm.arm_id],
                policy,
                initial_capital_fen=study.initial_capital_fen,
            )
            for arm in arms
        ]
        arm_metrics.sort(key=lambda item: item.arm_id)
        comparisons = self._apply_holm(
            self._comparisons(assignments, arms, formal, arm_metrics, policy)
        )

        unique_assignment_observation: dict[str, ShadowExecutionObservation] = {}
        for observation in sorted(
            formal,
            key=lambda item: (item.assignment_id, item.arm_id),
        ):
            unique_assignment_observation.setdefault(
                observation.assignment_id,
                observation,
            )
        regime_counts: Counter[MarketRegime] = Counter()
        for item in unique_assignment_observation.values():
            regime = self.repository.get_regime(item.regime_id)
            if regime is not None:
                regime_counts[regime.regime] += 1
        pit_counts: Counter[PointInTimeStatus] = Counter()
        for item in unique_assignment_observation.values():
            pit_counts.update(item.pit_statuses)
        exclusion_counts: Counter[str] = Counter()
        for item in final_observations:
            exclusion_counts.update(item.exclusion_codes)
        replay_quality_counts: Counter[ReplayQuality] = Counter(
            item.replay_quality for item in final_observations
        )
        observation_months = self._observation_months(assignments, report.as_of)
        phase6_contract_integrity = self._phase6_contract_integrity(assignments, arms)
        evidence_status, finding_codes = self._evidence_status(
            study,
            assignments,
            arms,
            final_observations,
            formal,
            comparisons,
            regime_counts,
            observation_months,
            policy,
            phase6_contract_integrity=phase6_contract_integrity,
        )
        expected = {
            "policy_version": policy.policy_version,
            "engine_version": policy.engine_version,
            "statistics_version": policy.statistics_version,
            "required_phase8_observation_months": policy.phase8_observation_months,
            "required_independent_decisions": policy.minimum_independent_decisions,
            "required_regime_count": policy.minimum_regime_count,
            "required_decisions_per_regime": policy.minimum_decisions_per_regime,
            "required_walk_forward_folds": policy.minimum_walk_forward_folds,
            "required_decisions_per_fold": policy.minimum_decisions_per_fold,
            "evidence_status": evidence_status,
            "observation_months": observation_months,
            "assignment_count": len(assignments),
            "mature_observation_count": len(formal),
            "independent_decision_count": len(
                {item.independence_key for item in assignments}
            ),
            "market_regime_counts": {
                key: regime_counts[key] for key in sorted(regime_counts, key=str)
            },
            "pit_status_counts": {
                key: pit_counts[key] for key in sorted(pit_counts, key=str)
            },
            "exclusion_counts": {
                key: exclusion_counts[key] for key in sorted(exclusion_counts)
            },
            "replay_quality_counts": {
                key: replay_quality_counts[key]
                for key in sorted(replay_quality_counts, key=str)
            },
            "phase6_contract_integrity": phase6_contract_integrity,
            "arm_metrics": _replace_created_at(
                [item.model_dump(mode="python") for item in arm_metrics],
                report.as_of,
            ),
            "comparisons": _replace_created_at(
                [item.model_dump(mode="python") for item in comparisons],
                report.as_of,
            ),
            "finding_codes": finding_codes,
        }
        actual = {
            key: (
                [item.model_dump(mode="python") for item in report.arm_metrics]
                if key == "arm_metrics"
                else [
                    item.model_dump(mode="python") for item in report.comparisons
                ]
                if key == "comparisons"
                else getattr(report, key)
            )
            for key in expected
        }
        return actual == expected

    def _artifact_matches(
        self,
        artifact_id: str,
        artifact_type: str,
        object_hash: str,
        *,
        input_hashes: list[str] | None = None,
    ) -> bool:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT type,object_hash,input_hashes_json FROM artifact_registry "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return bool(
            row is not None
            and str(row["type"]) == artifact_type
            and str(row["object_hash"]) == object_hash
            and (
                input_hashes is None
                or json.loads(str(row["input_hashes_json"])) == input_hashes
            )
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shadow service clock must be timezone-aware")
        return value.astimezone(UTC)

    def _recover_registered_children(self, study_id: str) -> dict[str, int]:
        supported_types = {
            "ShadowDecisionAssignment",
            "MarketRegimeSnapshot",
            "ShadowExecutionObservation",
            "ShadowEvaluationReport",
            "Phase8AdmissionReport",
        }
        with closing(self.state.connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT artifact_id,type,object_hash FROM artifact_registry "
                    "WHERE type IN (?,?,?,?,?) ORDER BY created_at,artifact_id",
                    tuple(sorted(supported_types)),
                ).fetchall()
            ]
        assignments: list[tuple[ShadowDecisionAssignment, str]] = []
        regimes: list[tuple[MarketRegimeSnapshot, str]] = []
        observations: list[tuple[ShadowExecutionObservation, str]] = []
        reports: dict[str, tuple[ShadowEvaluationReport, str]] = {}
        admissions: dict[str, tuple[Phase8AdmissionReport, str]] = {}
        canonical_prefixes = {
            "ShadowDecisionAssignment": "ShadowDecisionAssignment:shadow-assignment:",
            "MarketRegimeSnapshot": "MarketRegimeSnapshot:market-regime:",
            "ShadowExecutionObservation": (
                "ShadowExecutionObservation:shadow-observation:"
            ),
            "ShadowEvaluationReport": "ShadowEvaluationReport:shadow-report:",
            "Phase8AdmissionReport": "Phase8AdmissionReport:phase8-admission:",
        }
        for row in rows:
            object_hash = str(row["object_hash"])
            if not self.object_store.verify(object_hash):
                continue
            payload = self.object_store.get_bytes(object_hash)
            artifact_type = str(row["type"])
            if not str(row["artifact_id"]).startswith(
                canonical_prefixes[artifact_type]
            ):
                continue
            if artifact_type == "ShadowDecisionAssignment":
                item = ShadowDecisionAssignment.model_validate_json(payload)
                if item.study_id == study_id:
                    assignments.append((item, object_hash))
            elif artifact_type == "MarketRegimeSnapshot":
                item = MarketRegimeSnapshot.model_validate_json(payload)
                if item.study_id == study_id:
                    regimes.append((item, object_hash))
            elif artifact_type == "ShadowExecutionObservation":
                item = ShadowExecutionObservation.model_validate_json(payload)
                if item.study_id == study_id:
                    observations.append((item, object_hash))
            elif artifact_type == "ShadowEvaluationReport":
                item = ShadowEvaluationReport.model_validate_json(payload)
                if item.study_id == study_id:
                    reports[item.report_id] = (item, object_hash)
            elif artifact_type == "Phase8AdmissionReport":
                item = Phase8AdmissionReport.model_validate_json(payload)
                if item.study_id == study_id:
                    admissions[item.shadow_report_id] = (item, object_hash)
        recovered = {
            "assignments": 0,
            "regimes": 0,
            "observations": 0,
            "evaluations": 0,
        }
        for item, object_hash in sorted(
            assignments,
            key=lambda value: (value[0].signal_time, value[0].assignment_id),
        ):
            self.repository.register_assignment(item, object_hash=object_hash)
            recovered["assignments"] += 1
        for item, object_hash in sorted(
            regimes,
            key=lambda value: (value[0].features.as_of, value[0].regime_id),
        ):
            self.repository.register_regime(item, object_hash=object_hash)
            recovered["regimes"] += 1
        for item, object_hash in sorted(
            observations,
            key=lambda value: (
                value[0].assignment_id,
                value[0].arm_id,
                value[0].horizon_days,
                value[0].created_at,
                value[0].observation_id,
            ),
        ):
            self.parquet_store.write(item, object_sha256=object_hash)
            self.repository.register_observation(item, object_hash=object_hash)
            recovered["observations"] += 1
        for report_id, (report, report_hash) in sorted(
            reports.items(),
            key=lambda value: (value[1][0].as_of, value[0]),
        ):
            admission_entry = admissions.get(report_id)
            if admission_entry is None:
                continue
            admission, admission_hash = admission_entry
            self.repository.register_evaluation(
                run_id=report.run_id,
                input_hash=report.evaluation_input_sha256,
                report=report,
                report_object_hash=report_hash,
                admission=admission,
                admission_object_hash=admission_hash,
            )
            recovered["evaluations"] += 1
        return recovered

    def _assignment_protocol(self, assignment: ShadowDecisionAssignment) -> TradeProtocol:
        artifact_id = f"TradeProtocol:{assignment.trade_protocol_id}"
        reference = next(
            (
                item
                for item in assignment.artifact_references
                if item.artifact_id == artifact_id
            ),
            None,
        )
        if reference is None or not self.object_store.verify(reference.object_sha256):
            raise ValueError("shadow assignment TradeProtocol is unavailable")
        protocol = TradeProtocol.model_validate_json(
            self.object_store.get_bytes(reference.object_sha256)
        )
        if protocol.protocol_id != assignment.trade_protocol_id:
            raise ValueError("shadow assignment TradeProtocol identity mismatch")
        return protocol

    def _phase6_contract_integrity(
        self,
        assignments: list[ShadowDecisionAssignment],
        arms: list[ShadowArmDefinition],
    ) -> bool:
        try:
            for assignment in assignments:
                registry = self._validate_artifact_references(assignment)
                protocol = self._assignment_protocol(assignment)
                self._validate_arm_inputs(assignment, arms, registry)
                self._validate_committee_contract(
                    assignment,
                    arms,
                    registry,
                    protocol,
                )
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _observation_months(
        assignments: list[ShadowDecisionAssignment],
        as_of: datetime,
    ) -> Decimal:
        if not assignments:
            return Decimal("0")
        first = min(item.signal_time for item in assignments)
        elapsed_seconds = Decimal(str((as_of - first).total_seconds()))
        return max(Decimal("0"), elapsed_seconds / Decimal("2629800"))

    @staticmethod
    def _arm_metrics(
        arm: ShadowArmDefinition,
        observations: list[ShadowExecutionObservation],
        policy: ShadowEvaluationPolicy,
        *,
        initial_capital_fen: int,
    ) -> ShadowArmMetrics:
        ordered = sorted(
            observations,
            key=lambda item: (item.signal_time, item.assignment_id),
        )
        returns = [item.net_return for item in ordered]
        metric_created_at = ordered[-1].created_at if ordered else arm.created_at
        interval, _ = deterministic_block_bootstrap(
            returns,
            seed=f"arm:{arm.arm_id}",
            replicates=policy.bootstrap_replicates,
            block_length=policy.bootstrap_block_length,
            confidence_level=policy.confidence_level,
            metric="MEAN_NET_RETURN",
            created_at=metric_created_at,
        )
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        mean_win = mean(wins)
        mean_loss = mean(losses)
        payoff = (
            mean_win / abs(mean_loss)
            if mean_win is not None and mean_loss is not None and mean_loss != 0
            else None
        )
        total = len(ordered)
        total_net_pnl_fen = sum(item.net_pnl_fen for item in ordered)
        ending_nav_fen = max(0, initial_capital_fen + total_net_pnl_fen)
        return ShadowArmMetrics(
            arm_id=arm.arm_id,
            independent_decision_count=len({item.independence_key for item in ordered}),
            mature_observation_count=total,
            total_net_pnl_fen=total_net_pnl_fen,
            ending_nav_fen=ending_nav_fen,
            total_net_return_on_initial_capital=(
                max(
                    Decimal("-1"),
                    Decimal(total_net_pnl_fen) / Decimal(initial_capital_fen),
                )
            ),
            mean_net_return=interval,
            win_rate=wilson_interval(
                len(wins),
                total,
                confidence_level=policy.confidence_level,
                created_at=metric_created_at,
            ),
            maximum_drawdown=maximum_drawdown_from_pnl(
                initial_capital_fen,
                [item.net_pnl_fen for item in ordered],
            ),
            mean_mfe=mean([item.mfe for item in ordered]),
            mean_mae=mean([item.mae for item in ordered]),
            payoff_ratio=payoff,
            total_cost_fen=sum(
                item.commission_fen
                + item.tax_fen
                + item.transfer_fee_fen
                + item.slippage_fen
                for item in ordered
            ),
            turnover_fen=sum(item.turnover_fen for item in ordered),
            mean_holding_days=mean(
                [Decimal(item.trading_days_elapsed) for item in ordered]
            ),
            mean_liquidity_score=mean([item.liquidity_score for item in ordered]),
            mean_participation_rate=mean(
                [item.participation_rate for item in ordered]
            ),
            partial_fill_rate=(
                Decimal(
                    sum(item.fill_status is ShadowFillStatus.PARTIAL for item in ordered)
                )
                / Decimal(total)
                if total
                else Decimal("0")
            ),
            unfilled_rate=(
                Decimal(
                    sum(item.fill_status is ShadowFillStatus.UNFILLED for item in ordered)
                )
                / Decimal(total)
                if total
                else Decimal("0")
            ),
            path_uncertainty_rate=(
                Decimal(sum(item.ambiguous_intrabar_path for item in ordered))
                / Decimal(total)
                if total
                else Decimal("0")
            ),
            dual_source_rate=(
                Decimal(
                    sum(
                        item.replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
                        for item in ordered
                    )
                )
                / Decimal(total)
                if total
                else Decimal("0")
            ),
            created_at=(ordered[-1].created_at if ordered else arm.created_at),
        )

    def _comparisons(
        self,
        assignments: list[ShadowDecisionAssignment],
        arms: list[ShadowArmDefinition],
        observations: list[ShadowExecutionObservation],
        arm_metrics: list[ShadowArmMetrics],
        policy: ShadowEvaluationPolicy,
    ) -> list[ShadowComparisonResult]:
        baseline = next(
            (item for item in arms if item.arm_type is ShadowArmType.BASE_CASE_ONLY),
            None,
        )
        if baseline is None:
            return []
        experimental = [
            item
            for item in arms
            if item.arm_type
            in {
                ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
                ShadowArmType.FULL_COMMITTEE,
                ShadowArmType.APPROVED_SKILL,
            }
        ]
        observation_by_arm_key = {
            (item.arm_id, item.independence_key): item for item in observations
        }
        assignment_by_key = {item.independence_key: item for item in assignments}
        metrics = {item.arm_id: item for item in arm_metrics}
        results: list[ShadowComparisonResult] = []
        for arm in sorted(experimental, key=lambda item: item.arm_id):
            all_keys = set(assignment_by_key)
            baseline_keys = {
                key
                for candidate_arm, key in observation_by_arm_key
                if candidate_arm == baseline.arm_id
            }
            experimental_keys = {
                key
                for candidate_arm, key in observation_by_arm_key
                if candidate_arm == arm.arm_id
            }
            keys = sorted(
                baseline_keys & experimental_keys,
                key=lambda key: (assignment_by_key[key].signal_time, key),
            )
            missing_baseline = all_keys - baseline_keys
            missing_experimental = all_keys - experimental_keys
            unpaired = all_keys - set(keys)
            pair_exclusion_counts = {
                code: count
                for code, count in sorted(
                    {
                        "BASELINE_MATURE_RESULT_MISSING": len(missing_baseline),
                        "EXPERIMENTAL_MATURE_RESULT_MISSING": len(
                            missing_experimental
                        ),
                    }.items()
                )
                if count
            }
            pairs = [
                (
                    observation_by_arm_key[(baseline.arm_id, key)],
                    observation_by_arm_key[(arm.arm_id, key)],
                )
                for key in keys
            ]
            deltas = [
                experimental_obs.net_return - base.net_return
                for base, experimental_obs in pairs
            ]
            interval, p_value = deterministic_block_bootstrap(
                deltas,
                seed=f"comparison:{baseline.arm_id}:{arm.arm_id}",
                replicates=policy.bootstrap_replicates,
                block_length=policy.bootstrap_block_length,
                confidence_level=policy.confidence_level,
                metric="PAIRED_NET_RETURN_DELTA",
                created_at=(pairs[-1][1].created_at if pairs else arm.created_at),
            )
            folds: list[ShadowFoldResult] = []
            fold_size = policy.minimum_decisions_per_fold
            for fold_number, fold_pairs, overlap_reassigned_count in (
                self._walk_forward_folds(pairs, fold_size=fold_size)
            ):
                fold_deltas = [
                    experimental_obs.net_return - base.net_return
                    for base, experimental_obs in fold_pairs
                ]
                fold_interval, _ = deterministic_block_bootstrap(
                    fold_deltas,
                    seed=(
                        f"fold:{baseline.arm_id}:{arm.arm_id}:"
                        f"{fold_number}"
                    ),
                    replicates=policy.bootstrap_replicates,
                    block_length=policy.bootstrap_block_length,
                    confidence_level=policy.confidence_level,
                    metric="PAIRED_NET_RETURN_DELTA",
                    created_at=(
                        fold_pairs[-1][1].created_at
                        if fold_pairs
                        else arm.created_at
                    ),
                )
                if not fold_pairs:
                    continue
                estimate = fold_interval.estimate
                folds.append(
                    ShadowFoldResult(
                        fold_number=fold_number,
                        start_at=fold_pairs[0][1].signal_time,
                        end_at=fold_pairs[-1][1].signal_time,
                        independent_decision_count=len(fold_pairs),
                        overlap_reassigned_count=overlap_reassigned_count,
                        paired_net_return_delta=fold_interval,
                        positive_point_estimate=bool(
                            estimate is not None and estimate > 0
                        ),
                        created_at=fold_pairs[-1][1].created_at,
                    )
                )
            regime_groups: dict[
                MarketRegime,
                list[tuple[ShadowExecutionObservation, ShadowExecutionObservation]],
            ] = defaultdict(list)
            for pair in pairs:
                regime = self.repository.get_regime(pair[1].regime_id)
                if regime is not None:
                    regime_groups[regime.regime].append(pair)
            regimes: list[ShadowRegimeResult] = []
            for regime_name in sorted(regime_groups, key=str):
                regime_pairs = regime_groups[regime_name]
                regime_deltas = [
                    experimental_obs.net_return - base.net_return
                    for base, experimental_obs in regime_pairs
                ]
                regime_interval, _ = deterministic_block_bootstrap(
                    regime_deltas,
                    seed=f"regime:{baseline.arm_id}:{arm.arm_id}:{regime_name.value}",
                    replicates=policy.bootstrap_replicates,
                    block_length=policy.bootstrap_block_length,
                    confidence_level=policy.confidence_level,
                    metric="PAIRED_NET_RETURN_DELTA",
                    created_at=regime_pairs[-1][1].created_at,
                )
                regimes.append(
                    ShadowRegimeResult(
                        regime=regime_name,
                        independent_decision_count=len(regime_pairs),
                        paired_net_return_delta=regime_interval,
                        clearly_harmful=bool(
                            regime_interval.upper is not None
                            and regime_interval.upper < 0
                        ),
                        created_at=regime_pairs[-1][1].created_at,
                    )
                )
            single_contribution, regime_contribution = self._profit_concentrations(
                pairs
            )
            results.append(
                ShadowComparisonResult(
                    baseline_arm_id=baseline.arm_id,
                    experimental_arm_id=arm.arm_id,
                    specialist_skill_id=arm.specialist_skill_id,
                    paired_decision_count=len(pairs),
                    unpaired_decision_count=len(unpaired),
                    missing_baseline_count=len(missing_baseline),
                    missing_experimental_count=len(missing_experimental),
                    pair_exclusion_counts=pair_exclusion_counts,
                    paired_net_return_delta=interval,
                    raw_one_sided_p_value=p_value,
                    folds=folds,
                    regimes=regimes,
                    maximum_drawdown_delta=(
                        metrics[arm.arm_id].maximum_drawdown
                        - metrics[baseline.arm_id].maximum_drawdown
                    ),
                    single_profit_contribution=single_contribution,
                    regime_profit_contribution=regime_contribution,
                    created_at=(pairs[-1][1].created_at if pairs else arm.created_at),
                )
            )
        return results

    @staticmethod
    def _walk_forward_folds(
        pairs: list[
            tuple[ShadowExecutionObservation, ShadowExecutionObservation]
        ],
        *,
        fold_size: int,
    ) -> list[
        tuple[
            int,
            list[tuple[ShadowExecutionObservation, ShadowExecutionObservation]],
            int,
        ]
    ]:
        if not pairs:
            return []
        original_fold = [index // fold_size for index in range(len(pairs))]
        assigned_fold = list(original_fold)
        for earlier_index, (_, earlier) in enumerate(pairs):
            if earlier.valuation_time is None:
                continue
            for later_index in range(earlier_index + 1, len(pairs)):
                _, later = pairs[later_index]
                if later.company_id != earlier.company_id:
                    continue
                if later.signal_time > earlier.valuation_time:
                    break
                assigned_fold[earlier_index] = max(
                    assigned_fold[earlier_index],
                    original_fold[later_index],
                )
        grouped: dict[
            int,
            list[tuple[ShadowExecutionObservation, ShadowExecutionObservation]],
        ] = defaultdict(list)
        reassigned: Counter[int] = Counter()
        for index, pair in enumerate(pairs):
            grouped[assigned_fold[index]].append(pair)
            if assigned_fold[index] != original_fold[index]:
                reassigned[assigned_fold[index]] += 1
        return [
            (fold_index + 1, grouped[fold_index], reassigned[fold_index])
            for fold_index in sorted(grouped)
        ]

    def _profit_concentrations(
        self,
        pairs: list[tuple[ShadowExecutionObservation, ShadowExecutionObservation]],
    ) -> tuple[Decimal, Decimal]:
        positives: list[tuple[Decimal, str]] = []
        for base, experimental in pairs:
            delta = experimental.net_return - base.net_return
            if delta > 0:
                positives.append((delta, experimental.regime_id))
        total = sum((item[0] for item in positives), Decimal("0"))
        if total <= 0:
            return Decimal("0"), Decimal("0")
        single = max(item[0] for item in positives) / total
        by_regime: dict[MarketRegime, Decimal] = defaultdict(lambda: Decimal("0"))
        for value, regime_id in positives:
            snapshot = self.repository.get_regime(regime_id)
            regime = snapshot.regime if snapshot is not None else MarketRegime.UNCLASSIFIED
            by_regime[regime] += value
        regime = max(by_regime.values()) / total
        return single, regime

    @staticmethod
    def _apply_holm(
        comparisons: list[ShadowComparisonResult],
    ) -> list[ShadowComparisonResult]:
        p_values = {
            item.experimental_arm_id: item.raw_one_sided_p_value
            for item in comparisons
            if item.raw_one_sided_p_value is not None
        }
        adjusted = holm_adjust(
            {key: value for key, value in p_values.items() if value is not None}
        )
        return [
            item.model_copy(
                update={
                    "holm_adjusted_p_value": adjusted.get(item.experimental_arm_id)
                }
            )
            for item in comparisons
        ]

    def _evidence_status(
        self,
        study: ShadowStudyManifest,
        assignments: list[ShadowDecisionAssignment],
        arms: list[ShadowArmDefinition],
        final_observations: list[ShadowExecutionObservation],
        formal: list[ShadowExecutionObservation],
        comparisons: list[ShadowComparisonResult],
        regime_counts: Counter[MarketRegime],
        observation_months: Decimal,
        policy: ShadowEvaluationPolicy,
        *,
        phase6_contract_integrity: bool,
    ) -> tuple[ShadowEvidenceStatus, list[str]]:
        findings: set[str] = set()
        if not assignments:
            return ShadowEvidenceStatus.COLLECTING, ["NO_SHADOW_ASSIGNMENTS"]
        independent = len({item.independence_key for item in assignments})
        if observation_months < policy.provisional_observation_months:
            findings.add("OBSERVATION_WINDOW_UNDER_SIX_MONTHS")
        if independent < policy.minimum_independent_decisions:
            findings.add("INDEPENDENT_SAMPLE_UNDER_MINIMUM")
        qualifying_regimes = [
            regime
            for regime, count in regime_counts.items()
            if regime is not MarketRegime.UNCLASSIFIED
            and count >= policy.minimum_decisions_per_regime
        ]
        if len(qualifying_regimes) < policy.minimum_regime_count:
            findings.add("MARKET_REGIME_COVERAGE_INSUFFICIENT")
        comparison_ready = any(
            item.paired_decision_count >= policy.minimum_independent_decisions
            and len(
                [
                    fold
                    for fold in item.folds
                    if fold.independent_decision_count
                    >= policy.minimum_decisions_per_fold
                ]
            )
            >= policy.minimum_walk_forward_folds
            for item in comparisons
        )
        if not comparison_ready:
            findings.add("WALK_FORWARD_SAMPLE_INSUFFICIENT")
        formal_arm_ids = {
            arm.arm_id
            for arm in arms
            if arm.research_status is not ShadowArmResearchStatus.RESEARCH_ISOLATED
        }
        expected_final = len(assignments) * len(formal_arm_ids)
        eligible_final_observations = [
            item for item in final_observations if item.arm_id in formal_arm_ids
        ]
        if (
            len(eligible_final_observations) != expected_final
            or len(formal) != expected_final
        ):
            findings.add("FORMAL_FINAL_OBSERVATION_SET_INCOMPLETE")
        if not phase6_contract_integrity:
            findings.add("PHASE6_CONTRACT_INTEGRITY_FAILED")
        metrics = [item for item in formal if item.horizon_days == policy.final_horizon_days]
        if metrics:
            path_rate = Decimal(sum(item.ambiguous_intrabar_path for item in metrics)) / Decimal(
                len(metrics)
            )
            dual_rate = Decimal(
                sum(
                    item.replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
                    for item in metrics
                )
            ) / Decimal(len(metrics))
            if path_rate > policy.maximum_path_uncertainty_rate:
                findings.add("PATH_UNCERTAINTY_RATE_EXCEEDED")
            if dual_rate < policy.minimum_dual_source_rate:
                findings.add("DUAL_SOURCE_RATE_INSUFFICIENT")
        if observation_months < policy.provisional_observation_months:
            return ShadowEvidenceStatus.COLLECTING, sorted(findings)
        sample_findings = {
            "INDEPENDENT_SAMPLE_UNDER_MINIMUM",
            "MARKET_REGIME_COVERAGE_INSUFFICIENT",
            "WALK_FORWARD_SAMPLE_INSUFFICIENT",
        }
        if findings & sample_findings:
            return ShadowEvidenceStatus.INSUFFICIENT_SAMPLE, sorted(findings)
        if observation_months < policy.phase8_observation_months:
            return ShadowEvidenceStatus.PROVISIONAL, sorted(findings)
        integrity_findings = {
            "FORMAL_FINAL_OBSERVATION_SET_INCOMPLETE",
            "PATH_UNCERTAINTY_RATE_EXCEEDED",
            "DUAL_SOURCE_RATE_INSUFFICIENT",
            "PHASE6_CONTRACT_INTEGRITY_FAILED",
        }
        if findings & integrity_findings:
            return ShadowEvidenceStatus.FAILED_INTEGRITY, sorted(findings)
        if study.mode is not ShadowStudyMode.FORWARD_FORMAL:
            findings.add("RETROSPECTIVE_STUDY_CANNOT_BECOME_EVIDENCE_READY")
            return ShadowEvidenceStatus.FAILED_INTEGRITY, sorted(findings)
        findings.add("SHADOW_EVIDENCE_READY")
        return ShadowEvidenceStatus.EVIDENCE_READY, sorted(findings)

    def _admission(
        self,
        report: ShadowEvaluationReport,
        arms: list[ShadowArmDefinition],
        arm_metrics: list[ShadowArmMetrics],
        policy: ShadowEvaluationPolicy,
    ) -> Phase8AdmissionReport:
        metrics = {item.arm_id: item for item in arm_metrics}
        arm_by_id = {item.arm_id: item for item in arms}
        eligible: list[str] = []
        arm_gate_results: dict[str, dict[str, bool]] = {}
        for comparison in report.comparisons:
            if comparison.specialist_skill_id is None:
                continue
            interval = comparison.paired_net_return_delta
            full_folds = [
                item
                for item in comparison.folds
                if item.independent_decision_count >= policy.minimum_decisions_per_fold
            ]
            positive_ratio = (
                Decimal(sum(item.positive_point_estimate for item in full_folds))
                / Decimal(len(full_folds))
                if full_folds
                else Decimal("0")
            )
            qualifying_regimes = [
                item
                for item in comparison.regimes
                if item.regime is not MarketRegime.UNCLASSIFIED
                and item.independent_decision_count >= policy.minimum_decisions_per_regime
            ]
            experimental_metrics = metrics[comparison.experimental_arm_id]
            arm = arm_by_id[comparison.experimental_arm_id]
            gates = {
                "APPROVED_PRODUCTION_CONTRACT": (
                    arm.research_status
                    is ShadowArmResearchStatus.PRODUCTION_CONTRACT
                    and arm.specialist_skill_status
                    is ResearchSkillStatus.ENABLED_CONTRACT
                ),
                "PAIRED_SAMPLE_AT_LEAST_MINIMUM": (
                    comparison.paired_decision_count
                    >= policy.minimum_independent_decisions
                ),
                "BOOTSTRAP_CI_LOWER_POSITIVE": (
                    interval.lower is not None and interval.lower > 0
                ),
                "HOLM_ADJUSTED_SIGNIFICANT": (
                    comparison.holm_adjusted_p_value is not None
                    and comparison.holm_adjusted_p_value <= policy.holm_family_alpha
                ),
                "WALK_FORWARD_FOLDS_COMPLETE": (
                    len(full_folds) >= policy.minimum_walk_forward_folds
                ),
                "POSITIVE_FOLD_RATIO": (
                    positive_ratio >= policy.minimum_positive_fold_ratio
                ),
                "MARKET_REGIME_COVERAGE": (
                    len(qualifying_regimes) >= policy.minimum_regime_count
                ),
                "NO_CLEARLY_HARMFUL_REGIME": not any(
                    item.clearly_harmful for item in qualifying_regimes
                ),
                "MAXIMUM_DRAWDOWN_WITHIN_LIMIT": (
                    experimental_metrics.maximum_drawdown <= policy.maximum_drawdown
                ),
                "DRAWDOWN_WORSENING_WITHIN_LIMIT": (
                    comparison.maximum_drawdown_delta
                    <= policy.maximum_drawdown_worsening
                ),
                "PATH_UNCERTAINTY_WITHIN_LIMIT": (
                    experimental_metrics.path_uncertainty_rate
                    <= policy.maximum_path_uncertainty_rate
                ),
                "DUAL_SOURCE_COVERAGE": (
                    experimental_metrics.dual_source_rate
                    >= policy.minimum_dual_source_rate
                ),
                "SINGLE_PROFIT_CONCENTRATION_WITHIN_LIMIT": (
                    comparison.single_profit_contribution
                    <= policy.maximum_single_profit_contribution
                ),
                "REGIME_PROFIT_CONCENTRATION_WITHIN_LIMIT": (
                    comparison.regime_profit_contribution
                    <= policy.maximum_regime_profit_contribution
                ),
            }
            arm_gate_results[comparison.experimental_arm_id] = gates
            if all(gates.values()):
                eligible.append(comparison.experimental_arm_id)
        evidence_ready = report.evidence_status is ShadowEvidenceStatus.EVIDENCE_READY
        coverage_integrity = not set(report.finding_codes) & {
            "FORMAL_FINAL_OBSERVATION_SET_INCOMPLETE",
            "PATH_UNCERTAINTY_RATE_EXCEEDED",
            "DUAL_SOURCE_RATE_INSUFFICIENT",
        }
        if not evidence_ready or not report.phase6_contract_integrity or not coverage_integrity:
            eligible = []
        gate_results = {
            "EVIDENCE_READY": evidence_ready,
            "FORMAL_PIT_REPLAY_AND_COVERAGE_COMPLETE": coverage_integrity,
            "PHASE6_HARD_RISK_CONTRACT": report.phase6_contract_integrity,
            "STABLE_POSITIVE_SPECIALIST_INCREMENT": bool(eligible),
        }
        if all(gate_results.values()):
            status = Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
            reasons = ["ALL_PHASE8_RESEARCH_ADMISSION_GATES_PASSED"]
        elif report.evidence_status is ShadowEvidenceStatus.FAILED_INTEGRITY:
            status = Phase8AdmissionStatus.NOT_ELIGIBLE_INTEGRITY
            reasons = ["SHADOW_EVIDENCE_INTEGRITY_FAILED"]
        elif not evidence_ready:
            status = Phase8AdmissionStatus.NOT_ELIGIBLE_INSUFFICIENT_SAMPLE
            reasons = ["SHADOW_EVIDENCE_NOT_READY"]
        else:
            status = Phase8AdmissionStatus.NOT_ELIGIBLE_NO_INCREMENT
            reasons = ["NO_STABLE_SPECIALIST_INCREMENT"]
        identity = {
            "study_id": report.study_id,
            "shadow_report_id": report.report_id,
            "shadow_report_sha256": report.report_sha256,
            "status": status.value,
            "gate_results": gate_results,
            "experimental_arm_gate_results": arm_gate_results,
            "eligible_experimental_arm_ids": sorted(eligible),
            "reason_codes": reasons,
        }
        admission_hash = content_hash(identity)
        return Phase8AdmissionReport(
            admission_id=f"phase8-admission:{admission_hash}",
            study_id=report.study_id,
            shadow_report_id=report.report_id,
            shadow_report_sha256=report.report_sha256,
            status=status,
            gate_results=gate_results,
            experimental_arm_gate_results=arm_gate_results,
            eligible_experimental_arm_ids=sorted(eligible),
            reason_codes=reasons,
            admission_sha256=admission_hash,
            created_at=report.as_of,
        )

    def _prepare_study(
        self,
        request: ShadowStudyCreateRequest,
        *,
        persist: bool,
    ) -> _PreparedStudy:
        policy, policy_object_hash = self._policy_reference(persist=persist)
        config_hash = content_hash(policy)
        if policy.effective_from > request.effective_from:
            raise ValueError("shadow policy is not effective for the requested study")
        self._validate_common_arm_contract(request.arms)
        for arm in request.arms:
            if arm.cost_model_version != policy.cost_model_version:
                raise ValueError("shadow arm cost model differs from the policy")
            if arm.fill_model_version != policy.fill_model_version:
                raise ValueError("shadow arm fill model differs from the policy")
            if arm.corporate_action_version != policy.corporate_action_version:
                raise ValueError("shadow arm corporate-action model differs from the policy")
        normalized_request = request.model_copy(update={"created_at": request.created_at})
        request_hash = content_hash(normalized_request)
        study_identity_hash = content_hash(
            {"request_hash": request_hash, "config": config_hash}
        )
        study_id = f"shadow-study:{study_identity_hash}"
        arms: list[ShadowArmDefinition] = []
        arm_hashes: dict[str, str] = {}
        arm_object_hashes: dict[str, str] = {}
        for draft in request.arms:
            normalized_arm = draft.model_dump(
                mode="python",
                exclude={"schema_version", "created_at"},
            )
            identity = {
                "study_id": study_id,
                "arm": normalized_arm,
                "initial_capital_fen": request.initial_capital_fen,
                "fixed_notional_fen": request.fixed_notional_fen,
            }
            arm_hash = content_hash(identity)
            arm_id = f"shadow-arm:{arm_hash}"
            arm = ShadowArmDefinition(
                **draft.model_dump(
                    mode="python",
                    exclude={"schema_version", "created_at"},
                ),
                arm_id=arm_id,
                study_id=study_id,
                arm_sha256=arm_hash,
                schema_version=draft.schema_version,
                created_at=request.created_at,
            )
            payload = canonical_json_bytes(arm.model_dump(mode="json"))
            arms.append(arm)
            arm_hashes[arm_id] = arm_hash
            arm_object_hashes[arm_id] = sha256_bytes(payload)
        arms.sort(key=lambda item: item.arm_id)
        arm_ids = [arm.arm_id for arm in arms]
        study_identity = {
            "study_id": study_id,
            "request_hash": request_hash,
            "policy_version": policy.policy_version,
            "engine_version": policy.engine_version,
            "config_sha256": config_hash,
            "arm_hashes": {arm_id: arm_hashes[arm_id] for arm_id in arm_ids},
        }
        study_hash = content_hash(study_identity)
        manifest = ShadowStudyManifest(
            study_id=study_id,
            study_name=request.study_name,
            mode=request.mode,
            effective_from=request.effective_from,
            observation_end=request.observation_end,
            candidate_policy_id=request.candidate_policy_id,
            candidate_policy_version=request.candidate_policy_version,
            candidate_set_id=request.candidate_set_id,
            initial_capital_fen=request.initial_capital_fen,
            fixed_notional_fen=request.fixed_notional_fen,
            policy_version=policy.policy_version,
            engine_version=policy.engine_version,
            config_sha256=config_hash,
            request_sha256=request_hash,
            arm_ids=arm_ids,
            evidence_status=ShadowEvidenceStatus.COLLECTING,
            study_sha256=study_hash,
            created_at=request.created_at,
        )
        manifest_payload = canonical_json_bytes(manifest.model_dump(mode="json"))
        manifest_object_hash = sha256_bytes(manifest_payload)
        if persist:
            for arm in arms:
                payload = canonical_json_bytes(arm.model_dump(mode="json"))
                self.object_store.put_bytes(payload)
            self.object_store.put_bytes(manifest_payload)
            for arm in arms:
                self.state.register_artifact(
                    artifact_id=f"ShadowArmDefinition:{arm.arm_id}",
                    artifact_type="ShadowArmDefinition",
                    schema_version=arm.schema_version,
                    object_hash=arm_object_hashes[arm.arm_id],
                    input_hashes=[config_hash, arm.arm_sha256],
                )
            self.state.register_artifact(
                artifact_id=f"ShadowStudyManifest:{manifest.study_id}",
                artifact_type="ShadowStudyManifest",
                schema_version=manifest.schema_version,
                object_hash=manifest_object_hash,
                input_hashes=sorted([config_hash, *(arm_object_hashes.values())]),
            )
            self.repository.register_study(
                manifest,
                arms,
                manifest_object_hash=manifest_object_hash,
                arm_object_hashes=arm_object_hashes,
            )
        return _PreparedStudy(
            policy=policy,
            policy_object_hash=policy_object_hash,
            config_hash=config_hash,
            request_hash=request_hash,
            manifest=manifest,
            manifest_object_hash=manifest_object_hash,
            arms=arms,
            arm_object_hashes=arm_object_hashes,
        )

    def _policy_reference(self, *, persist: bool) -> tuple[ShadowEvaluationPolicy, str]:
        policy = self.configured_policy
        config_hash = content_hash(policy)
        payload = canonical_json_bytes(policy.model_dump(mode="json"))
        object_hash = sha256_bytes(payload)
        summary = self.repository.policy_summary(policy.policy_version)
        if summary is not None and str(summary["config_hash"]) != config_hash:
            raise ValueError("shadow policy changed without a version bump")
        if persist:
            self.object_store.put_bytes(payload)
            self.state.register_artifact(
                artifact_id=f"ShadowEvaluationPolicy:{policy.policy_version}",
                artifact_type="ShadowEvaluationPolicy",
                schema_version=policy.schema_version,
                object_hash=object_hash,
                input_hashes=[config_hash],
            )
            self.repository.register_policy(
                policy,
                object_hash=object_hash,
                config_hash=config_hash,
            )
        return policy, object_hash

    def _require_study(self, study_id: str) -> ShadowStudyManifest:
        study = self.repository.get_study(study_id)
        if study is None:
            raise ValueError(f"unknown shadow study: {study_id}")
        return study

    def _policy_for_study(self, study: ShadowStudyManifest) -> ShadowEvaluationPolicy:
        policy = self.repository.get_policy(study.policy_version)
        if policy is None:
            if study.policy_version != self.configured_policy.policy_version:
                raise ValueError("shadow study policy is unavailable")
            policy = self.configured_policy
        if content_hash(policy) != study.config_sha256:
            raise ValueError("shadow study policy hash mismatch")
        return policy

    @staticmethod
    def _validate_common_arm_contract(arms: list[ShadowArmDraft]) -> None:
        for field in (
            "protocol_family_version",
            "cost_model_version",
            "fill_model_version",
            "corporate_action_version",
        ):
            values = {str(getattr(arm, field)) for arm in arms}
            if len(values) != 1:
                raise ValueError(f"all shadow arms must share {field}")

    @staticmethod
    def _execution_fees(
        draft: ShadowExecutionObservationDraft,
        policy: ShadowEvaluationPolicy,
    ) -> tuple[int, int, int, int]:
        if draft.fill_status in {
            ShadowFillStatus.NOT_APPLICABLE,
            ShadowFillStatus.UNFILLED,
        }:
            return (0, 0, 0, 0)
        assert draft.entry_price_fen is not None
        assert draft.valuation_price_fen is not None
        entry_value = draft.entry_price_fen * draft.quantity
        exit_value = draft.valuation_price_fen * draft.quantity

        def rounded(value: Decimal) -> int:
            return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        entry_commission = rounded(Decimal(entry_value) * policy.commission_rate)
        exit_commission = rounded(Decimal(exit_value) * policy.commission_rate)
        if policy.commission_rate > 0:
            entry_commission = max(entry_commission, policy.minimum_commission_fen)
            exit_commission = max(exit_commission, policy.minimum_commission_fen)
        tax = rounded(Decimal(exit_value) * policy.stamp_tax_sell_rate)
        transfer = rounded(
            Decimal(entry_value + exit_value) * policy.transfer_fee_rate
        )
        slippage = rounded(Decimal(entry_value + exit_value) * policy.slippage_rate)
        return entry_commission + exit_commission, tax, transfer, slippage

    def _validate_artifact_references(
        self,
        request: ShadowDecisionAssignmentRequest,
    ) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        with closing(self.state.connect()) as connection:
            for reference in request.artifact_references:
                row = connection.execute(
                    "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                    (reference.artifact_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"unknown registered shadow input artifact: {reference.artifact_id}"
                    )
                if str(row["type"]) != reference.artifact_type:
                    raise ValueError("shadow input artifact type mismatch")
                if str(row["object_hash"]) != reference.object_sha256:
                    raise ValueError("shadow input artifact hash mismatch")
                if not self.object_store.verify(reference.object_sha256):
                    raise ValueError("shadow input artifact object is unavailable")
                try:
                    payload = json.loads(
                        self.object_store.get_bytes(reference.object_sha256)
                    )
                    artifact_created_at = datetime.fromisoformat(
                        str(payload["created_at"])
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "shadow input artifacts require a durable creation time"
                    ) from exc
                if (
                    artifact_created_at.tzinfo is None
                    or artifact_created_at.utcoffset() is None
                    or artifact_created_at > request.signal_time
                ):
                    raise ValueError("shadow input artifact was not frozen by signal time")
                result[reference.artifact_id] = {
                    "type": str(row["type"]),
                    "object_hash": str(row["object_hash"]),
                }
        return result

    def _validate_candidate_snapshot(
        self,
        object_hash: str,
        *,
        assignment: ShadowDecisionAssignment,
    ) -> None:
        try:
            payload = json.loads(self.object_store.get_bytes(object_hash))
            candidate_set_id = str(payload["candidate_set_id"])
            frozen_at = datetime.fromisoformat(str(payload["frozen_at"]))
            members = [str(item) for item in payload["members"]]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "shadow candidate snapshot requires identity, time, and members"
            ) from exc
        if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
            raise ValueError("shadow candidate snapshot time must be timezone-aware")
        if frozen_at > assignment.signal_time:
            raise ValueError("shadow candidate snapshot was not frozen by signal time")
        if candidate_set_id != assignment.candidate_set_id:
            raise ValueError("shadow candidate snapshot identity mismatch")
        if members != sorted(set(members)):
            raise ValueError("shadow candidate snapshot members must be sorted and unique")
        if assignment.symbol not in members:
            raise ValueError("shadow assignment symbol is absent from its candidate snapshot")

    @staticmethod
    def _validate_arm_inputs(
        request: ShadowDecisionAssignmentRequest,
        arms: list[ShadowArmDefinition],
        registry: dict[str, dict[str, str]],
    ) -> None:
        type_by_id = {artifact_id: row["type"] for artifact_id, row in registry.items()}
        signals = {signal.arm_id: signal for signal in request.arm_signals}
        for arm in arms:
            signal = signals[arm.arm_id]
            types = {type_by_id[item] for item in signal.input_artifact_ids}
            if arm.arm_type in {
                ShadowArmType.BASE_CASE_ONLY,
                ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
            } and "BaseCasePack" not in types:
                raise ValueError("BaseCase shadow arms require a frozen BaseCasePack")
            if (
                arm.arm_type is ShadowArmType.BASE_CASE_PLUS_SPECIALIST
                and "SpecialistDelta" not in types
            ):
                raise ValueError("specialist shadow arms require a frozen SpecialistDelta")
            if arm.arm_type is ShadowArmType.FULL_COMMITTEE and not {
                "DecisionPack",
                "TradeProtocol",
            }.issubset(types):
                raise ValueError("full committee shadow arms require decision and protocol")

    def _validate_committee_contract(
        self,
        request: ShadowDecisionAssignmentRequest,
        arms: list[ShadowArmDefinition],
        registry: dict[str, dict[str, str]],
        protocol: TradeProtocol,
    ) -> None:
        full_arm = next(
            (item for item in arms if item.arm_type is ShadowArmType.FULL_COMMITTEE),
            None,
        )
        if full_arm is None:
            raise ValueError("shadow assignments require a full committee arm")
        full_signal = next(
            (item for item in request.arm_signals if item.arm_id == full_arm.arm_id),
            None,
        )
        if full_signal is None:
            raise ValueError("shadow assignment is missing its full committee signal")
        decision_artifact_ids = [
            artifact_id
            for artifact_id in full_signal.input_artifact_ids
            if registry[artifact_id]["type"] == "DecisionPack"
        ]
        protocol_artifact_ids = [
            artifact_id
            for artifact_id in full_signal.input_artifact_ids
            if registry[artifact_id]["type"] == "TradeProtocol"
        ]
        if len(decision_artifact_ids) != 1 or protocol_artifact_ids != [
            f"TradeProtocol:{protocol.protocol_id}"
        ]:
            raise ValueError("full committee arms require one exact decision/protocol pair")
        decision_artifact_id = decision_artifact_ids[0]
        decision = DecisionPack.model_validate_json(
            self.object_store.get_bytes(registry[decision_artifact_id]["object_hash"])
        )
        if decision_artifact_id != f"DecisionPack:{decision.decision_id}":
            raise ValueError("shadow DecisionPack registry identity mismatch")
        if (
            decision.decision_id != protocol.decision_id
            or decision.decision_sha256 != protocol.decision_sha256
            or decision.company_id != request.company_id
            or protocol.company_id != request.company_id
            or decision.verdict is not protocol.verdict
        ):
            raise ValueError("shadow committee decision/protocol lineage mismatch")
        if (
            decision.as_of > request.signal_time
            or decision.created_at > request.signal_time
            or protocol.created_at > request.signal_time
        ):
            raise ValueError("shadow committee contract was not frozen by signal time")
        action_by_verdict = {
            CommitteeVerdict.PAPER_ELIGIBLE: ShadowAction.ENTER,
            CommitteeVerdict.PAPER_HOLD: ShadowAction.HOLD,
            CommitteeVerdict.PAPER_EXIT: ShadowAction.EXIT,
            CommitteeVerdict.REJECT: ShadowAction.NO_ACTION,
            CommitteeVerdict.NEEDS_INFO: ShadowAction.NO_ACTION,
            CommitteeVerdict.WATCH: ShadowAction.NO_ACTION,
        }
        if full_signal.action is not action_by_verdict[protocol.verdict]:
            raise ValueError("full committee signal action differs from its TradeProtocol")

    @staticmethod
    def _market_regime(
        features: MarketRegimeFeatures,
        policy: ShadowEvaluationPolicy,
    ) -> tuple[MarketRegime, list[str]]:
        core = (
            features.daily_trend_score,
            features.hourly_trend_score,
            features.market_breadth,
            features.volatility_percentile,
            features.index_drawdown,
        )
        if any(value is None for value in core) or set(features.pit_statuses) - set(
            policy.formal_pit_statuses
        ):
            return MarketRegime.UNCLASSIFIED, ["REGIME_INPUT_INCOMPLETE_OR_NOT_PIT_SAFE"]
        daily = features.daily_trend_score
        hourly = features.hourly_trend_score
        breadth = features.market_breadth
        volatility = features.volatility_percentile
        drawdown = features.index_drawdown
        assert daily is not None
        assert hourly is not None
        assert breadth is not None
        assert volatility is not None
        assert drawdown is not None
        if drawdown <= policy.panic_drawdown_threshold and (
            volatility >= policy.panic_volatility_percentile
            or breadth <= policy.panic_breadth_threshold
        ):
            return MarketRegime.PANIC, ["PANIC_DRAWDOWN_AND_STRESS"]
        if (
            daily >= policy.bull_daily_trend_threshold
            and hourly >= 0
            and breadth >= policy.bull_breadth_threshold
            and volatility >= policy.high_volatility_percentile
        ):
            return MarketRegime.HIGH_VOL_BULL, ["BULL_TREND_WITH_HIGH_VOLATILITY"]
        if (
            daily >= policy.bull_daily_trend_threshold
            and hourly >= 0
            and breadth >= policy.bull_breadth_threshold
        ):
            return MarketRegime.TREND_BULL, ["BULL_TREND_AND_BREADTH"]
        if (
            daily <= policy.bear_daily_trend_threshold
            and hourly <= 0
            and breadth <= policy.bear_breadth_threshold
        ):
            return MarketRegime.TREND_BEAR, ["BEAR_TREND_AND_WEAK_BREADTH"]
        return MarketRegime.RANGE, ["CORE_FEATURES_VALID_NO_TREND_REGIME"]

    @staticmethod
    def _arm_identity(
        arm: ShadowArmDefinition,
        study: ShadowStudyManifest,
    ) -> dict[str, Any]:
        return {
            "study_id": arm.study_id,
            "arm": arm.model_dump(
                mode="python",
                exclude={
                    "schema_version",
                    "created_at",
                    "arm_id",
                    "study_id",
                    "arm_sha256",
                },
            ),
            "initial_capital_fen": study.initial_capital_fen,
            "fixed_notional_fen": study.fixed_notional_fen,
        }

    @staticmethod
    def _study_identity(
        study: ShadowStudyManifest,
        arm_hashes: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "study_id": study.study_id,
            "request_hash": study.request_sha256,
            "policy_version": study.policy_version,
            "engine_version": study.engine_version,
            "config_sha256": study.config_sha256,
            "arm_hashes": {
                arm_id: arm_hashes[arm_id] for arm_id in sorted(arm_hashes)
            },
        }


__all__ = [
    "ShadowEvaluationExecution",
    "ShadowEvaluationService",
    "ShadowStudyExecution",
]


def _replace_created_at(value: Any, created_at: datetime) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                created_at
                if key == "created_at"
                else _replace_created_at(child, created_at)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_created_at(item, created_at) for item in value]
    return value


def _without_created_at(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _without_created_at(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {
            key: _without_created_at(child)
            for key, child in value.items()
            if key != "created_at"
        }
    if isinstance(value, list):
        return [_without_created_at(child) for child in value]
    return value
