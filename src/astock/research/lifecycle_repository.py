"""Safe indexes for versioned position monitoring and incremental reviews."""

from __future__ import annotations

from datetime import UTC

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    HoldingEvidenceUpdate,
    HoldingReviewPack,
    PositionActionProposal,
    PositionLifecycleConfig,
    PositionMonitoringPlan,
)


class LifecycleRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def rule_summary(self, rules_version: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT rules_version,action_count,object_hash,config_hash,created_at "
                "FROM position_lifecycle_rule_index WHERE rules_version=?",
                (rules_version,),
            ).fetchone()
        return dict(row) if row else None

    def get_rules(self, rules_version: str) -> PositionLifecycleConfig | None:
        row = self.rule_summary(rules_version)
        if row is None:
            return None
        return PositionLifecycleConfig.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def register_rules(
        self,
        config: PositionLifecycleConfig,
        *,
        object_hash: str,
        config_hash: str,
    ) -> PositionLifecycleConfig:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,config_hash FROM position_lifecycle_rule_index "
                "WHERE rules_version=?",
                (config.rules_version,),
            ).fetchone()
            if row is not None:
                if str(row["config_hash"]) != config_hash:
                    raise ValueError("position lifecycle rules changed without a version bump")
                return PositionLifecycleConfig.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO position_lifecycle_rule_index("
                "rules_version,action_count,object_hash,config_hash,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    config.rules_version,
                    len(config.action_priority),
                    object_hash,
                    config_hash,
                    config.created_at.isoformat(),
                ),
            )
        return config

    def get_plan(self, plan_id: str) -> PositionMonitoringPlan | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM position_monitoring_plan_index WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return PositionMonitoringPlan.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def plan_object_hash(self, plan_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM position_monitoring_plan_index WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_plan(
        self,
        plan: PositionMonitoringPlan,
        *,
        object_hash: str,
        input_hash: str,
        condition_count: int,
    ) -> PositionMonitoringPlan:
        assert plan.plan_id is not None
        assert plan.base_case_id is not None
        assert plan.route_plan_id is not None
        assert plan.memo_id is not None
        assert plan.rules_version is not None
        assert plan.as_of is not None
        assert plan.decision_reference_status is not None
        assert plan.coverage_status is not None
        assert plan.next_review_at is not None
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM position_monitoring_plan_index "
                "WHERE plan_id=?",
                (plan.plan_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"position monitoring plan collision: {plan.plan_id}")
                return PositionMonitoringPlan.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO position_monitoring_plan_index("
                "plan_id,position_id,company_id,decision_id,decision_reference_status,"
                "base_case_id,route_plan_id,memo_id,rules_version,as_of,next_review_at,"
                "coverage_status,condition_count,baseline_evidence_count,object_hash,"
                "input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.plan_id,
                    plan.position_id,
                    plan.company_id,
                    plan.decision_id,
                    plan.decision_reference_status,
                    plan.base_case_id,
                    plan.route_plan_id,
                    plan.memo_id,
                    plan.rules_version,
                    plan.as_of.astimezone(UTC).isoformat(),
                    plan.next_review_at.astimezone(UTC).isoformat(),
                    plan.coverage_status,
                    condition_count,
                    len(plan.baseline_evidence_ids),
                    object_hash,
                    input_hash,
                    plan.created_at.isoformat(),
                ),
            )
        return plan

    def latest_plan_summary(self, position_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT plan_id,position_id,company_id,decision_id,decision_reference_status,"
                "base_case_id,route_plan_id,memo_id,rules_version,as_of,next_review_at,"
                "coverage_status,condition_count,baseline_evidence_count,object_hash,created_at "
                "FROM position_monitoring_plan_index WHERE position_id=? "
                "ORDER BY as_of DESC,created_at DESC,plan_id DESC LIMIT 1",
                (position_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_update(self, update_id: str) -> HoldingEvidenceUpdate | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM holding_evidence_update_index WHERE update_id=?",
                (update_id,),
            ).fetchone()
        if row is None:
            return None
        return HoldingEvidenceUpdate.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def update_object_hash(self, update_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM holding_evidence_update_index WHERE update_id=?",
                (update_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_update(
        self,
        update: HoldingEvidenceUpdate,
        *,
        object_hash: str,
        input_hash: str,
    ) -> HoldingEvidenceUpdate:
        assert update.update_id is not None
        assert update.plan_id is not None
        assert update.rules_version is not None
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM holding_evidence_update_index "
                "WHERE update_id=?",
                (update.update_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"holding evidence update collision: {update.update_id}")
                return HoldingEvidenceUpdate.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO holding_evidence_update_index("
                "update_id,plan_id,position_id,rules_version,from_as_of,to_as_of,"
                "added_evidence_count,changed_claim_count,invalidated_evidence_count,"
                "unresolved_conflict_count,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    update.update_id,
                    update.plan_id,
                    update.position_id,
                    update.rules_version,
                    update.from_as_of.astimezone(UTC).isoformat(),
                    update.to_as_of.astimezone(UTC).isoformat(),
                    len(update.added_evidence_ids),
                    len(update.changed_claim_ids),
                    len(update.invalidated_evidence_ids),
                    len(update.unresolved_conflicts),
                    object_hash,
                    input_hash,
                    update.created_at.isoformat(),
                ),
            )
        return update

    def get_review(self, review_id: str) -> HoldingReviewPack | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM holding_review_index WHERE review_id=?",
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        return HoldingReviewPack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def review_object_hash(self, review_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM holding_review_index WHERE review_id=?",
                (review_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_review(
        self,
        review: HoldingReviewPack,
        *,
        object_hash: str,
        input_hash: str,
        from_as_of: str,
    ) -> HoldingReviewPack:
        assert review.review_id is not None
        assert review.plan_id is not None
        assert review.evidence_update_id is not None
        assert review.rules_version is not None
        assert review.proposal_id is not None
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM holding_review_index WHERE review_id=?",
                (review.review_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"holding review collision: {review.review_id}")
                return HoldingReviewPack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO holding_review_index("
                "review_id,plan_id,update_id,position_id,rules_version,from_as_of,to_as_of,"
                "recommended_action,action_confidence,trigger_count,hard_block_count,"
                "evidence_count,proposal_id,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    review.review_id,
                    review.plan_id,
                    review.evidence_update_id,
                    review.position_id,
                    review.rules_version,
                    from_as_of,
                    review.as_of.astimezone(UTC).isoformat(),
                    review.recommended_action.value,
                    review.action_confidence,
                    len(review.triggered_rules),
                    len(review.hard_blocks),
                    len(review.evidence_ids),
                    review.proposal_id,
                    object_hash,
                    input_hash,
                    review.created_at.isoformat(),
                ),
            )
        return review

    def latest_review_summary(self, position_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT review_id,plan_id,update_id,position_id,rules_version,from_as_of,"
                "to_as_of,recommended_action,action_confidence,trigger_count,hard_block_count,"
                "evidence_count,proposal_id,object_hash,created_at FROM holding_review_index "
                "WHERE position_id=? ORDER BY to_as_of DESC,review_id DESC LIMIT 1",
                (position_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_review_for_plan(self, plan_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT review_id,from_as_of,to_as_of,object_hash FROM holding_review_index "
                "WHERE plan_id=? ORDER BY to_as_of DESC,review_id DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return dict(row) if row else None

    def review_summaries_for_plan(self, plan_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT review_id,plan_id,update_id,position_id,rules_version,from_as_of,"
                "to_as_of,recommended_action,action_confidence,trigger_count,hard_block_count,"
                "evidence_count,proposal_id,object_hash,created_at FROM holding_review_index "
                "WHERE plan_id=? "
                "ORDER BY from_as_of ASC,to_as_of ASC,review_id ASC",
                (plan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_review_summary_for_plan(self, plan_id: str) -> dict[str, object] | None:
        summaries = self.review_summaries_for_plan(plan_id)
        return summaries[-1] if summaries else None

    def get_proposal(self, proposal_id: str) -> PositionActionProposal | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM position_action_proposal_index WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return PositionActionProposal.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def proposal_object_hash(self, proposal_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM position_action_proposal_index WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_proposal(
        self,
        proposal: PositionActionProposal,
        *,
        object_hash: str,
        input_hash: str,
    ) -> PositionActionProposal:
        assert proposal.review_id is not None
        assert proposal.plan_id is not None
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM position_action_proposal_index "
                "WHERE proposal_id=?",
                (proposal.proposal_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"position action proposal collision: {proposal.proposal_id}")
                return PositionActionProposal.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO position_action_proposal_index("
                "proposal_id,review_id,plan_id,position_id,action,requires_user_confirmation,"
                "hard_block_count,evidence_count,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal.proposal_id,
                    proposal.review_id,
                    proposal.plan_id,
                    proposal.position_id,
                    proposal.action.value,
                    int(proposal.requires_user_confirmation),
                    len(proposal.hard_blocks),
                    len(proposal.evidence_ids),
                    object_hash,
                    input_hash,
                    proposal.created_at.isoformat(),
                ),
            )
        return proposal


__all__ = ["LifecycleRepository"]
