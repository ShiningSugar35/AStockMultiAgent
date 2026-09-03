"""Safe SQLite indexes for immutable shadow-evaluation artifacts."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    MarketRegimeSnapshot,
    Phase8AdmissionReport,
    ShadowArmDefinition,
    ShadowDecisionAssignment,
    ShadowEvaluationPolicy,
    ShadowEvaluationReport,
    ShadowExecutionObservation,
    ShadowStudyManifest,
)


class ShadowRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def policy_summary(self, policy_version: str) -> dict[str, object] | None:
        return self._one(
            "SELECT policy_version,policy_id,engine_version,effective_from,object_hash,"
            "config_hash,created_at FROM shadow_policy_index WHERE policy_version=?",
            (policy_version,),
        )

    def get_policy(self, policy_version: str) -> ShadowEvaluationPolicy | None:
        row = self.policy_summary(policy_version)
        return (
            None
            if row is None
            else ShadowEvaluationPolicy.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def register_policy(
        self,
        policy: ShadowEvaluationPolicy,
        *,
        object_hash: str,
        config_hash: str,
    ) -> ShadowEvaluationPolicy:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,config_hash FROM shadow_policy_index "
                "WHERE policy_version=?",
                (policy.policy_version,),
            ).fetchone()
            if row is not None:
                if str(row["config_hash"]) != config_hash:
                    raise ValueError("shadow policy changed without a version bump")
                return ShadowEvaluationPolicy.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO shadow_policy_index("
                "policy_version,policy_id,engine_version,effective_from,object_hash,"
                "config_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    policy.policy_version,
                    policy.policy_id,
                    policy.engine_version,
                    policy.effective_from.astimezone(UTC).isoformat(),
                    object_hash,
                    config_hash,
                    policy.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return policy

    def study_summary(self, study_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT study_id,study_name,study_mode,effective_from,observation_end,"
            "candidate_policy_id,candidate_policy_version,candidate_set_id,policy_version,"
            "engine_version,evidence_status,arm_count,object_hash,request_hash,study_hash,"
            "created_at,registered_at,prospective_eligible "
            "FROM shadow_study_index WHERE study_id=?",
            (study_id,),
        )

    def latest_study_summary(
        self,
        *,
        policy_version: str | None = None,
    ) -> dict[str, object] | None:
        select = (
            "SELECT study_id,study_name,study_mode,effective_from,observation_end,"
            "candidate_policy_id,candidate_policy_version,candidate_set_id,policy_version,"
            "engine_version,evidence_status,arm_count,object_hash,request_hash,study_hash,"
            "created_at,registered_at,prospective_eligible FROM shadow_study_index "
        )
        order = "ORDER BY COALESCE(registered_at,created_at) DESC,study_id DESC LIMIT 1"
        if policy_version is None:
            return self._one(select + order, ())
        return self._one(select + "WHERE policy_version=? " + order, (policy_version,))

    def get_study(self, study_id: str) -> ShadowStudyManifest | None:
        row = self.study_summary(study_id)
        return (
            None
            if row is None
            else ShadowStudyManifest.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def arm_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT arm_id,study_id,arm_key,arm_type,research_status,specialist_skill_id,"
            "specialist_skill_version,benchmark_symbol,object_hash,arm_hash,created_at "
            "FROM shadow_arm_index WHERE study_id=? ORDER BY arm_id",
            (study_id,),
        )

    def get_arm(self, arm_id: str) -> ShadowArmDefinition | None:
        row = self._one(
            "SELECT object_hash FROM shadow_arm_index WHERE arm_id=?",
            (arm_id,),
        )
        return (
            None
            if row is None
            else ShadowArmDefinition.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def get_arms(self, study_id: str) -> list[ShadowArmDefinition]:
        return [
            ShadowArmDefinition.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
            for row in self.arm_summaries(study_id)
        ]

    def register_study(
        self,
        manifest: ShadowStudyManifest,
        arms: list[ShadowArmDefinition],
        *,
        manifest_object_hash: str,
        arm_object_hashes: dict[str, str],
        registered_at: datetime,
        prospective_eligible: bool,
    ) -> ShadowStudyManifest:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash,study_hash FROM shadow_study_index "
                "WHERE study_id=?",
                (manifest.study_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["request_hash"]) != manifest.request_sha256
                    or str(row["study_hash"]) != manifest.study_sha256
                    or str(row["object_hash"]) != manifest_object_hash
                ):
                    raise ValueError(f"shadow study collision: {manifest.study_id}")
                return ShadowStudyManifest.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO shadow_study_index("
                "study_id,study_name,study_mode,effective_from,observation_end,"
                "candidate_policy_id,candidate_policy_version,candidate_set_id,policy_version,"
                "engine_version,evidence_status,arm_count,object_hash,request_hash,study_hash,"
                "created_at,registered_at,prospective_eligible) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.study_id,
                    manifest.study_name,
                    manifest.mode.value,
                    manifest.effective_from.astimezone(UTC).isoformat(),
                    (
                        manifest.observation_end.astimezone(UTC).isoformat()
                        if manifest.observation_end
                        else None
                    ),
                    manifest.candidate_policy_id,
                    manifest.candidate_policy_version,
                    manifest.candidate_set_id,
                    manifest.policy_version,
                    manifest.engine_version,
                    manifest.evidence_status.value,
                    len(arms),
                    manifest_object_hash,
                    manifest.request_sha256,
                    manifest.study_sha256,
                    manifest.created_at.astimezone(UTC).isoformat(),
                    registered_at.astimezone(UTC).isoformat(),
                    int(prospective_eligible),
                ),
            )
            for arm in arms:
                connection.execute(
                    "INSERT INTO shadow_arm_index("
                    "arm_id,study_id,arm_key,arm_type,research_status,specialist_skill_id,"
                    "specialist_skill_version,benchmark_symbol,object_hash,arm_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        arm.arm_id,
                        manifest.study_id,
                        arm.arm_key,
                        arm.arm_type.value,
                        arm.research_status.value,
                        arm.specialist_skill_id,
                        arm.specialist_skill_version,
                        arm.benchmark_symbol,
                        arm_object_hashes[arm.arm_id],
                        arm.arm_sha256,
                        arm.created_at.astimezone(UTC).isoformat(),
                    ),
                )
        return manifest

    def assignment_summary(self, assignment_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT assignment_id,study_id,candidate_set_id,company_id,symbol,market,"
            "signal_time,independence_key,thesis_version,event_id,research_memo_id,"
            "decision_id,trade_protocol_id,arm_signal_count,object_hash,assignment_hash,"
            "created_at,registered_at,prospective_eligible "
            "FROM shadow_assignment_index WHERE assignment_id=?",
            (assignment_id,),
        )

    def assignment_research_identity_conflict(
        self,
        study_id: str,
        *,
        research_memo_id: str,
        decision_id: str,
    ) -> dict[str, object] | None:
        return self._one(
            "SELECT assignment_id,research_memo_id,decision_id "
            "FROM shadow_assignment_index "
            "WHERE study_id=? AND prospective_eligible=1 "
            "AND (research_memo_id=? OR decision_id=?) "
            "ORDER BY assignment_id LIMIT 1",
            (study_id, research_memo_id, decision_id),
        )

    def assignment_inputs(self, assignment_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT artifact_id,artifact_type,object_hash,available_at "
            "FROM shadow_assignment_input_index WHERE assignment_id=? ORDER BY artifact_id",
            (assignment_id,),
        )

    def get_assignment(self, assignment_id: str) -> ShadowDecisionAssignment | None:
        row = self.assignment_summary(assignment_id)
        return (
            None
            if row is None
            else ShadowDecisionAssignment.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def assignments(self, study_id: str) -> list[ShadowDecisionAssignment]:
        rows = self._many(
            "SELECT object_hash FROM shadow_assignment_index WHERE study_id=? "
            "ORDER BY signal_time,assignment_id",
            (study_id,),
        )
        return [
            ShadowDecisionAssignment.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]

    def register_assignment(
        self,
        assignment: ShadowDecisionAssignment,
        *,
        object_hash: str,
        registered_at: datetime,
        prospective_eligible: bool,
    ) -> ShadowDecisionAssignment:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,assignment_hash FROM shadow_assignment_index "
                "WHERE assignment_id=?",
                (assignment.assignment_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["assignment_hash"]) != assignment.assignment_sha256
                    or str(row["object_hash"]) != object_hash
                ):
                    raise ValueError(f"shadow assignment collision: {assignment.assignment_id}")
            else:
                connection.execute(
                    "INSERT INTO shadow_assignment_index("
                    "assignment_id,study_id,candidate_set_id,company_id,symbol,market,"
                    "signal_time,independence_key,thesis_version,event_id,research_memo_id,"
                    "decision_id,trade_protocol_id,arm_signal_count,object_hash,assignment_hash,"
                    "created_at,registered_at,prospective_eligible) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        assignment.assignment_id,
                        assignment.study_id,
                        assignment.candidate_set_id,
                        assignment.company_id,
                        assignment.symbol,
                        assignment.market.value,
                        assignment.signal_time.astimezone(UTC).isoformat(),
                        assignment.independence_key,
                        assignment.thesis_version,
                        assignment.event_id,
                        assignment.research_memo_id,
                        assignment.decision_id,
                        assignment.trade_protocol_id,
                        len(assignment.arm_signals),
                        object_hash,
                        assignment.assignment_sha256,
                        assignment.created_at.astimezone(UTC).isoformat(),
                        registered_at.astimezone(UTC).isoformat(),
                        int(prospective_eligible),
                    ),
                )
            for reference in assignment.artifact_references:
                input_row = connection.execute(
                    "SELECT artifact_type,object_hash,available_at "
                    "FROM shadow_assignment_input_index "
                    "WHERE assignment_id=? AND artifact_id=?",
                    (assignment.assignment_id, reference.artifact_id),
                ).fetchone()
                expected_input = (
                    reference.artifact_type,
                    reference.object_sha256,
                    reference.available_at.astimezone(UTC).isoformat(),
                )
                if input_row is None:
                    connection.execute(
                        "INSERT INTO shadow_assignment_input_index("
                        "assignment_id,artifact_id,artifact_type,object_hash,available_at,"
                        "created_at) VALUES(?,?,?,?,?,?)",
                        (
                            assignment.assignment_id,
                            reference.artifact_id,
                            *expected_input,
                            assignment.created_at.astimezone(UTC).isoformat(),
                        ),
                    )
                elif tuple(str(input_row[key]) for key in input_row.keys()) != expected_input:
                    raise ValueError(
                        f"shadow assignment input collision: {assignment.assignment_id}"
                    )
        return assignment

    def regime_summary(self, regime_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT regime_id,study_id,regime_rule_version,as_of,regime,feature_snapshot_id,"
            "feature_snapshot_hash,object_hash,regime_hash,created_at "
            "FROM market_regime_index WHERE regime_id=?",
            (regime_id,),
        )

    def regime_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT regime_id,study_id,regime_rule_version,as_of,regime,feature_snapshot_id,"
            "feature_snapshot_hash,object_hash,regime_hash,created_at "
            "FROM market_regime_index WHERE study_id=? ORDER BY as_of,regime_id",
            (study_id,),
        )

    def get_regime(self, regime_id: str) -> MarketRegimeSnapshot | None:
        row = self.regime_summary(regime_id)
        return (
            None
            if row is None
            else MarketRegimeSnapshot.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def register_regime(
        self,
        snapshot: MarketRegimeSnapshot,
        *,
        object_hash: str,
    ) -> MarketRegimeSnapshot:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,regime_hash FROM market_regime_index WHERE regime_id=?",
                (snapshot.regime_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["regime_hash"]) != snapshot.regime_sha256
                    or str(row["object_hash"]) != object_hash
                ):
                    raise ValueError(f"market regime collision: {snapshot.regime_id}")
                return MarketRegimeSnapshot.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO market_regime_index("
                "regime_id,study_id,regime_rule_version,as_of,regime,feature_snapshot_id,"
                "feature_snapshot_hash,object_hash,regime_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot.regime_id,
                    snapshot.study_id,
                    snapshot.regime_rule_version,
                    snapshot.features.as_of.astimezone(UTC).isoformat(),
                    snapshot.regime.value,
                    snapshot.features.feature_snapshot_id,
                    snapshot.features.feature_snapshot_sha256,
                    object_hash,
                    snapshot.regime_sha256,
                    snapshot.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return snapshot

    def observation_summary(self, observation_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT observation_id,observation_version,supersedes_observation_id,study_id,"
            "assignment_id,arm_id,regime_id,independence_key,"
            "horizon_days,observation_status,formal_eligible,signal_time,valuation_time,"
            "replay_quality,net_pnl_fen,outcome_data_source,data_available_at,thesis_status,"
            "object_hash,observation_hash,created_at,registered_at,forward_data_eligible "
            "FROM shadow_observation_index WHERE observation_id=?",
            (observation_id,),
        )

    def observation_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT observation_id,observation_version,supersedes_observation_id,study_id,"
            "assignment_id,arm_id,regime_id,independence_key,horizon_days,"
            "observation_status,formal_eligible,signal_time,valuation_time,replay_quality,"
            "net_pnl_fen,outcome_data_source,data_available_at,thesis_status,object_hash,"
            "observation_hash,created_at,registered_at,forward_data_eligible "
            "FROM shadow_observation_index WHERE study_id=? "
            "ORDER BY assignment_id,arm_id,horizon_days,created_at,observation_id",
            (study_id,),
        )

    def latest_observation_summary(
        self,
        *,
        assignment_id: str,
        arm_id: str,
        horizon_days: int,
    ) -> dict[str, object] | None:
        return self._one(
            "SELECT observation_id,observation_version,supersedes_observation_id,study_id,"
            "assignment_id,arm_id,regime_id,independence_key,horizon_days,observation_status,"
            "formal_eligible,signal_time,valuation_time,replay_quality,net_pnl_fen,object_hash,"
            "observation_hash,created_at,registered_at,forward_data_eligible "
            "FROM shadow_observation_index "
            "WHERE assignment_id=? AND arm_id=? AND horizon_days=? "
            "ORDER BY created_at DESC,observation_id DESC LIMIT 1",
            (assignment_id, arm_id, horizon_days),
        )

    def get_observation(self, observation_id: str) -> ShadowExecutionObservation | None:
        row = self.observation_summary(observation_id)
        return (
            None
            if row is None
            else ShadowExecutionObservation.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def observations(
        self,
        study_id: str,
        *,
        as_of: datetime | None = None,
    ) -> list[ShadowExecutionObservation]:
        time_filter = "" if as_of is None else " AND created_at<=?"
        parameters: tuple[object, ...] = (
            (study_id,)
            if as_of is None
            else (study_id, as_of.astimezone(UTC).isoformat())
        )
        rows = self._many(
            "SELECT object_hash FROM ("
            "SELECT object_hash,signal_time,assignment_id,arm_id,horizon_days,"
            "ROW_NUMBER() OVER (PARTITION BY assignment_id,arm_id,horizon_days "
            "ORDER BY created_at DESC,observation_id DESC) AS version_rank "
            f"FROM shadow_observation_index WHERE study_id=?{time_filter}"
            ") WHERE version_rank=1 "
            "ORDER BY signal_time,assignment_id,arm_id,horizon_days",
            parameters,
        )
        return [
            ShadowExecutionObservation.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]

    def observations_for_assignment(
        self,
        assignment_id: str,
        *,
        horizon_days: int,
    ) -> list[ShadowExecutionObservation]:
        rows = self._many(
            "SELECT object_hash FROM ("
            "SELECT object_hash,arm_id,ROW_NUMBER() OVER (PARTITION BY arm_id,horizon_days "
            "ORDER BY created_at DESC,observation_id DESC) AS version_rank "
            "FROM shadow_observation_index WHERE assignment_id=? AND horizon_days=?"
            ") WHERE version_rank=1 ORDER BY arm_id",
            (assignment_id, horizon_days),
        )
        return [
            ShadowExecutionObservation.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]

    def register_observation(
        self,
        observation: ShadowExecutionObservation,
        *,
        object_hash: str,
        registered_at: datetime,
        forward_data_eligible: bool,
    ) -> ShadowExecutionObservation:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,observation_hash FROM shadow_observation_index "
                "WHERE observation_id=?",
                (observation.observation_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["observation_hash"]) != observation.observation_sha256
                    or str(row["object_hash"]) != object_hash
                ):
                    raise ValueError(f"shadow observation collision: {observation.observation_id}")
                return ShadowExecutionObservation.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO shadow_observation_index("
                "observation_id,observation_version,supersedes_observation_id,study_id,"
                "assignment_id,arm_id,regime_id,independence_key,"
                "horizon_days,observation_status,formal_eligible,signal_time,valuation_time,"
                "replay_quality,net_pnl_fen,outcome_data_source,data_available_at,thesis_status,"
                "object_hash,observation_hash,created_at,registered_at,forward_data_eligible) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.observation_version,
                    observation.supersedes_observation_id,
                    observation.study_id,
                    observation.assignment_id,
                    observation.arm_id,
                    observation.regime_id,
                    observation.independence_key,
                    observation.horizon_days,
                    observation.status.value,
                    int(observation.formal_eligible),
                    observation.signal_time.astimezone(UTC).isoformat(),
                    (
                        observation.valuation_time.astimezone(UTC).isoformat()
                        if observation.valuation_time
                        else None
                    ),
                    observation.replay_quality.value,
                    observation.net_pnl_fen,
                    observation.outcome_data_source.value,
                    (
                        observation.data_available_at.astimezone(UTC).isoformat()
                        if observation.data_available_at
                        else None
                    ),
                    observation.thesis_status.value,
                    object_hash,
                    observation.observation_sha256,
                    observation.created_at.astimezone(UTC).isoformat(),
                    registered_at.astimezone(UTC).isoformat(),
                    int(forward_data_eligible),
                ),
            )
        return observation

    def prospective_assignment_ids(self, study_id: str) -> set[str]:
        rows = self._many(
            "SELECT assignment_id FROM shadow_assignment_index "
            "WHERE study_id=? AND prospective_eligible=1 ORDER BY assignment_id",
            (study_id,),
        )
        return {str(row["assignment_id"]) for row in rows}

    def forward_counts(
        self,
        study_id: str,
        *,
        final_horizon_days: int,
    ) -> dict[str, int]:
        with closing(self.state.connect()) as connection:
            prospective_events = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT independence_key) "
                    "FROM shadow_assignment_index "
                    "WHERE study_id=? AND prospective_eligible=1",
                    (study_id,),
                ).fetchone()[0]
            )
            mature_events = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT latest.assignment_id) FROM ("
                    "SELECT assignment_id,arm_id,observation_status,formal_eligible,"
                    "forward_data_eligible,"
                    "ROW_NUMBER() OVER (PARTITION BY assignment_id,arm_id,horizon_days "
                    "ORDER BY created_at DESC,observation_id DESC) AS version_rank "
                    "FROM shadow_observation_index "
                    "WHERE study_id=? AND horizon_days=?"
                    ") AS latest JOIN shadow_arm_index AS arm "
                    "ON arm.arm_id=latest.arm_id "
                    "WHERE latest.version_rank=1 "
                    "AND latest.observation_status='MATURE' "
                    "AND latest.formal_eligible=1 "
                    "AND latest.forward_data_eligible=1 "
                    "AND arm.arm_type='FULL_COMMITTEE'",
                    (study_id, final_horizon_days),
                ).fetchone()[0]
            )
        return {
            "formal_forward_event_count": prospective_events,
            "formal_mature_future_event_count": mature_events,
        }

    def latest_report_summary(self, study_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT report_id,run_id,study_id,evidence_status,assignment_count,"
            "mature_observation_count,independent_decision_count,comparison_count,"
            "object_hash,report_hash,created_at FROM shadow_report_index WHERE study_id=? "
            "ORDER BY created_at DESC,report_id DESC LIMIT 1",
            (study_id,),
        )

    def report_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT report_id,run_id,study_id,evidence_status,assignment_count,"
            "mature_observation_count,independent_decision_count,comparison_count,"
            "object_hash,report_hash,created_at FROM shadow_report_index WHERE study_id=? "
            "ORDER BY created_at,report_id",
            (study_id,),
        )

    def evaluation_run_summary(self, run_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT run_id,study_id,as_of,policy_version,statistics_version,run_status,"
            "input_hash,report_id,report_object_hash,created_at,completed_at "
            "FROM shadow_evaluation_run_index WHERE run_id=?",
            (run_id,),
        )

    def evaluation_run_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT run_id,study_id,as_of,policy_version,statistics_version,run_status,"
            "input_hash,report_id,report_object_hash,created_at,completed_at "
            "FROM shadow_evaluation_run_index WHERE study_id=? ORDER BY as_of,run_id",
            (study_id,),
        )

    def get_report(self, report_id: str) -> ShadowEvaluationReport | None:
        row = self._one(
            "SELECT object_hash FROM shadow_report_index WHERE report_id=?",
            (report_id,),
        )
        return (
            None
            if row is None
            else ShadowEvaluationReport.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def latest_admission_summary(self, study_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT admission_id,study_id,report_id,admission_status,object_hash,"
            "admission_hash,created_at FROM phase8_admission_index WHERE study_id=? "
            "ORDER BY created_at DESC,admission_id DESC LIMIT 1",
            (study_id,),
        )

    def admission_summaries(self, study_id: str) -> list[dict[str, object]]:
        return self._many(
            "SELECT admission_id,study_id,report_id,admission_status,object_hash,"
            "admission_hash,created_at FROM phase8_admission_index WHERE study_id=? "
            "ORDER BY created_at,admission_id",
            (study_id,),
        )

    def get_admission(self, admission_id: str) -> Phase8AdmissionReport | None:
        row = self._one(
            "SELECT object_hash FROM phase8_admission_index WHERE admission_id=?",
            (admission_id,),
        )
        return (
            None
            if row is None
            else Phase8AdmissionReport.model_validate_json(
                self.object_store.get_bytes(str(row["object_hash"]))
            )
        )

    def register_evaluation(
        self,
        *,
        run_id: str,
        input_hash: str,
        report: ShadowEvaluationReport,
        report_object_hash: str,
        admission: Phase8AdmissionReport,
        admission_object_hash: str,
    ) -> None:
        with self.state.transaction() as connection:
            now = report.created_at.astimezone(UTC).isoformat()
            existing = connection.execute(
                "SELECT study_id,as_of,policy_version,statistics_version,run_status,input_hash,"
                "report_id,report_object_hash,created_at,completed_at "
                "FROM shadow_evaluation_run_index WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                expected_run = (
                    report.study_id,
                    report.as_of.astimezone(UTC).isoformat(),
                    report.policy_version,
                    report.statistics_version,
                    "COMPLETED",
                    input_hash,
                    report.report_id,
                    report_object_hash,
                    now,
                    now,
                )
                if tuple(str(existing[key]) for key in existing.keys()) != expected_run:
                    raise ValueError(f"shadow evaluation run collision: {run_id}")
            else:
                connection.execute(
                    "INSERT INTO shadow_evaluation_run_index("
                    "run_id,study_id,as_of,policy_version,statistics_version,run_status,"
                    "input_hash,report_id,report_object_hash,created_at,completed_at) "
                    "VALUES(?,?,?,?,?,'COMPLETED',?,?,?,?,?)",
                    (
                        run_id,
                        report.study_id,
                        report.as_of.astimezone(UTC).isoformat(),
                        report.policy_version,
                        report.statistics_version,
                        input_hash,
                        report.report_id,
                        report_object_hash,
                        now,
                        now,
                    ),
                )
            report_row = connection.execute(
                "SELECT report_id,run_id,study_id,evidence_status,assignment_count,"
                "mature_observation_count,independent_decision_count,comparison_count,"
                "object_hash,report_hash,created_at FROM shadow_report_index "
                "WHERE report_id=? OR run_id=?",
                (report.report_id, run_id),
            ).fetchone()
            expected_report = (
                report.report_id,
                run_id,
                report.study_id,
                report.evidence_status.value,
                str(report.assignment_count),
                str(report.mature_observation_count),
                str(report.independent_decision_count),
                str(len(report.comparisons)),
                report_object_hash,
                report.report_sha256,
                now,
            )
            if report_row is None:
                connection.execute(
                    "INSERT INTO shadow_report_index("
                    "report_id,run_id,study_id,evidence_status,assignment_count,"
                    "mature_observation_count,independent_decision_count,comparison_count,"
                    "object_hash,report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    expected_report,
                )
            elif tuple(str(report_row[key]) for key in report_row.keys()) != expected_report:
                raise ValueError(f"shadow evaluation report collision: {report.report_id}")
            admission_row = connection.execute(
                "SELECT admission_id,study_id,report_id,admission_status,object_hash,"
                "admission_hash,created_at FROM phase8_admission_index "
                "WHERE admission_id=? OR report_id=?",
                (admission.admission_id, admission.shadow_report_id),
            ).fetchone()
            admission_created_at = admission.created_at.astimezone(UTC).isoformat()
            expected_admission = (
                admission.admission_id,
                admission.study_id,
                admission.shadow_report_id,
                admission.status.value,
                admission_object_hash,
                admission.admission_sha256,
                admission_created_at,
            )
            if admission_row is None:
                connection.execute(
                    "INSERT INTO phase8_admission_index("
                    "admission_id,study_id,report_id,admission_status,object_hash,"
                    "admission_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    expected_admission,
                )
            elif (
                tuple(str(admission_row[key]) for key in admission_row.keys())
                != expected_admission
            ):
                raise ValueError(
                    f"Phase 8 admission collision: {admission.admission_id}"
                )
            connection.execute(
                "UPDATE shadow_study_index SET evidence_status=? WHERE study_id=?",
                (report.evidence_status.value, report.study_id),
            )

    def counts(self, study_id: str) -> dict[str, int]:
        with closing(self.state.connect()) as connection:
            arm_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_arm_index WHERE study_id=?", (study_id,)
                ).fetchone()[0]
            )
            assignment_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_assignment_index WHERE study_id=?", (study_id,)
                ).fetchone()[0]
            )
            observation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT ROW_NUMBER() OVER (PARTITION BY assignment_id,arm_id,horizon_days "
                    "ORDER BY created_at DESC,observation_id DESC) AS version_rank "
                    "FROM shadow_observation_index WHERE study_id=?"
                    ") WHERE version_rank=1",
                    (study_id,),
                ).fetchone()[0]
            )
            mature_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT observation_status,ROW_NUMBER() OVER ("
                    "PARTITION BY assignment_id,arm_id,horizon_days "
                    "ORDER BY created_at DESC,observation_id DESC) AS version_rank "
                    "FROM shadow_observation_index WHERE study_id=?"
                    ") WHERE version_rank=1 AND observation_status='MATURE'",
                    (study_id,),
                ).fetchone()[0]
            )
            independent_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT independence_key) FROM shadow_assignment_index "
                    "WHERE study_id=?",
                    (study_id,),
                ).fetchone()[0]
            )
        return {
            "arm_count": arm_count,
            "assignment_count": assignment_count,
            "observation_count": observation_count,
            "mature_observation_count": mature_count,
            "independent_decision_count": independent_count,
        }

    def integrity_counts(self) -> dict[str, int]:
        tables = [
            "shadow_policy_index",
            "shadow_study_index",
            "shadow_arm_index",
            "shadow_assignment_index",
            "shadow_assignment_input_index",
            "market_regime_index",
            "shadow_observation_index",
            "shadow_evaluation_run_index",
            "shadow_report_index",
            "phase8_admission_index",
        ]
        with closing(self.state.connect()) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def _one(self, sql: str, parameters: tuple[object, ...]) -> dict[str, object] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None

    def _many(self, sql: str, parameters: tuple[object, ...]) -> list[dict[str, object]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["ShadowRepository"]
