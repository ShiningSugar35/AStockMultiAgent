"""Deterministic frozen-weight shadow study, assignment, and regime services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    MarketRegime,
    MarketRegimeFeatures,
    MarketRegimeSnapshot,
    Phase8AdmissionStatus,
    ShadowArmDefinition,
    ShadowArmDraft,
    ShadowArmType,
    ShadowDecisionAssignment,
    ShadowDecisionAssignmentRequest,
    ShadowEvaluationPolicy,
    ShadowEvidenceStatus,
    ShadowStatusReport,
    ShadowStudyCreateRequest,
    ShadowStudyManifest,
    ShadowStudyMode,
    ShadowStudyPlan,
    TradeProtocol,
)
from astock.shadow.repository import ShadowRepository


@dataclass(frozen=True, slots=True)
class ShadowStudyExecution:
    manifest: ShadowStudyManifest
    arms: list[ShadowArmDefinition]
    object_sha256_by_id: dict[str, str]


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
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.configured_policy = policy
        self.repository = ShadowRepository(state, object_store)

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
        prepared = self._prepare_study(request, persist=True)
        return ShadowStudyExecution(
            manifest=prepared.manifest,
            arms=prepared.arms,
            object_sha256_by_id={
                prepared.manifest.study_id: prepared.manifest_object_hash,
                **prepared.arm_object_hashes,
            },
        )

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
        if study.mode is ShadowStudyMode.FORWARD_FORMAL and features.as_of < study.effective_from:
            raise ValueError("formal market regimes cannot precede the shadow study")
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
            self.repository.register_regime(snapshot, object_hash=object_hash)
            self.state.register_artifact(
                artifact_id=f"MarketRegimeSnapshot:{snapshot.regime_id}",
                artifact_type="MarketRegimeSnapshot",
                schema_version=snapshot.schema_version,
                object_hash=object_hash,
                input_hashes=[features.feature_snapshot_sha256],
            )
        return snapshot

    def assign(
        self,
        request: ShadowDecisionAssignmentRequest,
    ) -> ShadowDecisionAssignment:
        study = self._require_study(request.study_id)
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
        self.repository.register_assignment(assignment, object_hash=object_hash)
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
        return assignment

    def audit(self, study_id: str) -> dict[str, object]:
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
        arms = self.repository.get_arms(study_id)
        if sorted(arm.arm_id for arm in arms) != study.arm_ids:
            findings.add("SHADOW_ARM_SET_MISMATCH")
        arm_hashes: dict[str, str] = {}
        for arm, row in zip(arms, self.repository.arm_summaries(study_id), strict=True):
            if not self.object_store.verify(str(row["object_hash"])):
                findings.add("SHADOW_ARM_OBJECT_INVALID")
            if content_hash(self._arm_identity(arm, study)) != arm.arm_sha256:
                findings.add("SHADOW_ARM_HASH_MISMATCH")
            arm_hashes[arm.arm_id] = arm.arm_sha256
        if content_hash(self._study_identity(study, arm_hashes)) != study.study_sha256:
            findings.add("SHADOW_STUDY_HASH_MISMATCH")
        for assignment in self.repository.assignments(study_id):
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
        return {
            "study_id": study_id,
            "status": "PASS" if not findings else "PARTIAL",
            "finding_codes": sorted(findings),
            "counts": self.repository.counts(study_id),
        }

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
            self.repository.register_study(
                manifest,
                arms,
                manifest_object_hash=manifest_object_hash,
                arm_object_hashes=arm_object_hashes,
            )
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
            self.repository.register_policy(
                policy,
                object_hash=object_hash,
                config_hash=config_hash,
            )
            self.state.register_artifact(
                artifact_id=f"ShadowEvaluationPolicy:{policy.policy_version}",
                artifact_type="ShadowEvaluationPolicy",
                schema_version=policy.schema_version,
                object_hash=object_hash,
                input_hashes=[config_hash],
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

    def _validate_artifact_references(
        self,
        request: ShadowDecisionAssignmentRequest,
    ) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        with self.state.connect() as connection:
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
                result[reference.artifact_id] = {
                    "type": str(row["type"]),
                    "object_hash": str(row["object_hash"]),
                }
        return result

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


__all__ = ["ShadowEvaluationService", "ShadowStudyExecution"]
