"""Safe SQLite indexes for frozen committee artifacts."""

from __future__ import annotations

from datetime import UTC

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    CommitteeAssessmentSnapshot,
    CommitteeInputBundle,
    CommitteeInvestigationTask,
    CommitteeRuleConfig,
    CounterCasePack,
    DecisionPack,
    TradeProtocol,
)


class CommitteeRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def rule_summary(self, rules_version: str) -> dict[str, object] | None:
        return self._one(
            "SELECT rules_version,rule_set_id,engine_version,effective_from,object_hash,"
            "config_hash,created_at FROM committee_rule_index WHERE rules_version=?",
            (rules_version,),
        )

    def get_rules(self, rules_version: str) -> CommitteeRuleConfig | None:
        row = self.rule_summary(rules_version)
        if row is None:
            return None
        return CommitteeRuleConfig.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_rules(
        self,
        rules: CommitteeRuleConfig,
        *,
        object_hash: str,
        config_hash: str,
    ) -> CommitteeRuleConfig:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,config_hash FROM committee_rule_index WHERE rules_version=?",
                (rules.rules_version,),
            ).fetchone()
            if row is not None:
                if str(row["config_hash"]) != config_hash:
                    raise ValueError("committee rules changed without a version bump")
                return CommitteeRuleConfig.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_rule_index("
                "rules_version,rule_set_id,engine_version,effective_from,object_hash,"
                "config_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    rules.rules_version,
                    rules.rule_set_id,
                    rules.engine_version,
                    rules.effective_from.astimezone(UTC).isoformat(),
                    object_hash,
                    config_hash,
                    rules.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return rules

    def assessment_summary(self, assessment_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT assessment_id,company_id,decision_scope,as_of,evidence_count,"
            "object_hash,request_hash,created_at FROM committee_assessment_index "
            "WHERE assessment_id=?",
            (assessment_id,),
        )

    def get_assessment(self, assessment_id: str) -> CommitteeAssessmentSnapshot | None:
        row = self.assessment_summary(assessment_id)
        if row is None:
            return None
        return CommitteeAssessmentSnapshot.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_assessment(
        self,
        assessment: CommitteeAssessmentSnapshot,
        *,
        object_hash: str,
    ) -> CommitteeAssessmentSnapshot:
        evidence_ids = _assessment_evidence_ids(assessment)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash FROM committee_assessment_index "
                "WHERE assessment_id=?",
                (assessment.assessment_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != assessment.request_sha256:
                    raise ValueError(f"committee assessment collision: {assessment.assessment_id}")
                return CommitteeAssessmentSnapshot.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_assessment_index("
                "assessment_id,company_id,decision_scope,as_of,evidence_count,object_hash,"
                "request_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    assessment.assessment_id,
                    assessment.company_id,
                    assessment.scope.value,
                    assessment.as_of.astimezone(UTC).isoformat(),
                    len(evidence_ids),
                    object_hash,
                    assessment.request_sha256,
                    assessment.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return assessment

    def counter_case_summary(self, counter_case_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT counter_case_id,assessment_id,company_id,decision_scope,as_of,"
            "trigger_count,evidence_count,object_hash,input_hash,created_at "
            "FROM counter_case_index WHERE counter_case_id=?",
            (counter_case_id,),
        )

    def get_counter_case(self, counter_case_id: str) -> CounterCasePack | None:
        row = self.counter_case_summary(counter_case_id)
        if row is None:
            return None
        return CounterCasePack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_counter_case(
        self,
        pack: CounterCasePack,
        *,
        assessment_id: str,
        object_hash: str,
    ) -> CounterCasePack:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM counter_case_index WHERE counter_case_id=?",
                (pack.counter_case_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != pack.input_sha256:
                    raise ValueError(f"counter-case collision: {pack.counter_case_id}")
                return CounterCasePack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO counter_case_index("
                "counter_case_id,assessment_id,company_id,decision_scope,as_of,trigger_count,"
                "evidence_count,object_hash,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    pack.counter_case_id,
                    assessment_id,
                    pack.company_id,
                    pack.scope.value,
                    pack.as_of.astimezone(UTC).isoformat(),
                    len(pack.trigger_codes),
                    len(pack.evidence_ids),
                    object_hash,
                    pack.input_sha256,
                    pack.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return pack

    def bundle_summary(self, bundle_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT bundle_id,assessment_id,counter_case_id,company_id,decision_scope,as_of,"
            "rules_version,engine_version,input_count,object_hash,bundle_hash,created_at "
            "FROM committee_bundle_index WHERE bundle_id=?",
            (bundle_id,),
        )

    def get_bundle(self, bundle_id: str) -> CommitteeInputBundle | None:
        row = self.bundle_summary(bundle_id)
        if row is None:
            return None
        return CommitteeInputBundle.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_bundle(
        self,
        bundle: CommitteeInputBundle,
        *,
        assessment_id: str,
        counter_case_id: str | None,
        object_hash: str,
    ) -> CommitteeInputBundle:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,bundle_hash FROM committee_bundle_index WHERE bundle_id=?",
                (bundle.bundle_id,),
            ).fetchone()
            if row is not None:
                if str(row["bundle_hash"]) != bundle.bundle_sha256:
                    raise ValueError(f"committee bundle collision: {bundle.bundle_id}")
                return CommitteeInputBundle.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_bundle_index("
                "bundle_id,assessment_id,counter_case_id,company_id,decision_scope,as_of,"
                "rules_version,engine_version,input_count,object_hash,bundle_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bundle.bundle_id,
                    assessment_id,
                    counter_case_id,
                    bundle.company_id,
                    bundle.scope.value,
                    bundle.as_of.astimezone(UTC).isoformat(),
                    bundle.rules_version,
                    bundle.engine_version,
                    len(bundle.artifact_references),
                    object_hash,
                    bundle.bundle_sha256,
                    bundle.created_at.astimezone(UTC).isoformat(),
                ),
            )
            for reference in bundle.artifact_references:
                connection.execute(
                    "INSERT INTO committee_bundle_input_index("
                    "bundle_id,artifact_id,artifact_type,artifact_role,object_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        bundle.bundle_id,
                        reference.artifact_id,
                        reference.artifact_type,
                        reference.role.value,
                        reference.object_sha256,
                        bundle.created_at.astimezone(UTC).isoformat(),
                    ),
                )
        return bundle

    def bundle_inputs(self, bundle_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,artifact_type,artifact_role,object_hash "
                "FROM committee_bundle_input_index WHERE bundle_id=? ORDER BY artifact_id",
                (bundle_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def decision_summary(self, decision_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT decision_id,bundle_id,company_id,decision_scope,as_of,rules_version,"
            "engine_version,verdict,hard_block_count,needs_info_count,counter_case_id,"
            "object_hash,decision_hash,created_at FROM committee_decision_index "
            "WHERE decision_id=?",
            (decision_id,),
        )

    def latest_decision_summary(self, company_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT decision_id,bundle_id,company_id,decision_scope,as_of,rules_version,"
            "engine_version,verdict,hard_block_count,needs_info_count,counter_case_id,"
            "object_hash,decision_hash,created_at FROM committee_decision_index "
            "WHERE company_id=? ORDER BY as_of DESC,created_at DESC,decision_id DESC LIMIT 1",
            (company_id,),
        )

    def get_decision(self, decision_id: str) -> DecisionPack | None:
        row = self.decision_summary(decision_id)
        if row is None:
            return None
        return DecisionPack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_decision(
        self,
        decision: DecisionPack,
        *,
        object_hash: str,
    ) -> DecisionPack:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,decision_hash FROM committee_decision_index "
                "WHERE decision_id=?",
                (decision.decision_id,),
            ).fetchone()
            if row is not None:
                if str(row["decision_hash"]) != decision.decision_sha256:
                    raise ValueError(f"committee decision collision: {decision.decision_id}")
                return DecisionPack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_decision_index("
                "decision_id,bundle_id,company_id,decision_scope,as_of,rules_version,"
                "engine_version,verdict,hard_block_count,needs_info_count,counter_case_id,"
                "object_hash,decision_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.bundle_id,
                    decision.company_id,
                    decision.scope.value,
                    decision.as_of.astimezone(UTC).isoformat(),
                    decision.rules_version,
                    decision.engine_version,
                    decision.verdict.value,
                    len(decision.hard_blocks),
                    len(decision.needs_info_task_ids),
                    decision.counter_case_id,
                    object_hash,
                    decision.decision_sha256,
                    decision.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return decision

    def protocol_summary(self, protocol_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT protocol_id,decision_id,company_id,verdict,protocol_status,strategy_id,"
            "effective_from,requires_user_confirmation,broker_execution_allowed,"
            "ledger_write_allowed,object_hash,input_hash,created_at "
            "FROM committee_trade_protocol_index WHERE protocol_id=?",
            (protocol_id,),
        )

    def protocol_for_decision(self, decision_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT protocol_id,decision_id,company_id,verdict,protocol_status,strategy_id,"
            "effective_from,requires_user_confirmation,broker_execution_allowed,"
            "ledger_write_allowed,object_hash,input_hash,created_at "
            "FROM committee_trade_protocol_index WHERE decision_id=?",
            (decision_id,),
        )

    def get_protocol(self, protocol_id: str) -> TradeProtocol | None:
        row = self.protocol_summary(protocol_id)
        if row is None:
            return None
        return TradeProtocol.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_protocol(
        self,
        protocol: TradeProtocol,
        *,
        object_hash: str,
        input_hash: str,
    ) -> TradeProtocol:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM committee_trade_protocol_index "
                "WHERE protocol_id=?",
                (protocol.protocol_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"trade protocol collision: {protocol.protocol_id}")
                return TradeProtocol.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_trade_protocol_index("
                "protocol_id,decision_id,company_id,verdict,protocol_status,strategy_id,"
                "effective_from,requires_user_confirmation,broker_execution_allowed,"
                "ledger_write_allowed,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    protocol.protocol_id,
                    protocol.decision_id,
                    protocol.company_id,
                    protocol.verdict.value,
                    protocol.protocol_status.value,
                    protocol.strategy_id,
                    protocol.effective_from.astimezone(UTC).isoformat(),
                    int(protocol.requires_user_confirmation),
                    int(protocol.broker_execution_allowed),
                    int(protocol.ledger_write_allowed),
                    object_hash,
                    input_hash,
                    protocol.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return protocol

    def task_summary(self, task_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT task_id,decision_id,bundle_id,reason_code,status,resolution_artifact_id,"
            "resolution_object_hash,object_hash,input_hash,created_at "
            "FROM committee_investigation_task_index WHERE task_id=?",
            (task_id,),
        )

    def task_summaries_for_decision(self, decision_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT task_id,decision_id,bundle_id,reason_code,status,"
                "resolution_artifact_id,resolution_object_hash,object_hash,input_hash,"
                "created_at FROM committee_investigation_task_index WHERE decision_id=? "
                "ORDER BY task_id",
                (decision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: str) -> CommitteeInvestigationTask | None:
        row = self.task_summary(task_id)
        if row is None:
            return None
        return CommitteeInvestigationTask.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_task(
        self,
        task: CommitteeInvestigationTask,
        *,
        object_hash: str,
        input_hash: str,
    ) -> CommitteeInvestigationTask:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM committee_investigation_task_index "
                "WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"committee investigation task collision: {task.task_id}")
                return CommitteeInvestigationTask.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO committee_investigation_task_index("
                "task_id,decision_id,bundle_id,reason_code,status,resolution_artifact_id,"
                "resolution_object_hash,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id,
                    task.decision_id,
                    task.bundle_id,
                    task.reason_code,
                    task.status.value,
                    task.resolution_artifact_id,
                    task.resolution_object_sha256,
                    object_hash,
                    input_hash,
                    task.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return task

    def resolve_task(
        self,
        task_id: str,
        *,
        resolution_artifact_id: str,
        resolution_object_hash: str,
    ) -> dict[str, object]:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT status,resolution_artifact_id,resolution_object_hash "
                "FROM committee_investigation_task_index WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown committee investigation task: {task_id}")
            if str(row["status"]) == "RESOLVED":
                if (
                    str(row["resolution_artifact_id"]) != resolution_artifact_id
                    or str(row["resolution_object_hash"]) != resolution_object_hash
                ):
                    raise ValueError(f"committee task resolution collision: {task_id}")
            elif str(row["status"]) != "OPEN":
                raise ValueError("only open committee tasks can be resolved")
            else:
                connection.execute(
                    "UPDATE committee_investigation_task_index SET status='RESOLVED',"
                    "resolution_artifact_id=?,resolution_object_hash=? WHERE task_id=?",
                    (resolution_artifact_id, resolution_object_hash, task_id),
                )
        resolved = self.task_summary(task_id)
        assert resolved is not None
        return resolved

    def _one(self, sql: str, parameters: tuple[object, ...]) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None


def _assessment_evidence_ids(assessment: CommitteeAssessmentSnapshot) -> list[str]:
    return sorted(
        set(assessment.support_evidence_ids)
        | set(assessment.expected_return_range.evidence_ids)
        | set(assessment.downside_range.evidence_ids)
        | set(assessment.coverage.evidence_ids)
        | set(assessment.portfolio_risk.evidence_ids)
        | set(assessment.protocol.evidence_ids)
        | {
            evidence_id
            for values in assessment.signal_evidence_ids.values()
            for evidence_id in values
        }
    )


__all__ = ["CommitteeRepository"]
