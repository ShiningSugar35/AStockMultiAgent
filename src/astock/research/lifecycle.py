"""Versioned position monitoring plans and deterministic incremental reviews."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.research.lifecycle_repository import LifecycleRepository
from astock.research.repository import ResearchRepository
from astock.schemas import (
    ConflictResolutionStatus,
    DecisionReferenceStatus,
    EvidenceConflict,
    HoldingEvidenceUpdate,
    HoldingReviewPack,
    HoldingReviewRequest,
    LifecycleCondition,
    LifecycleSourceType,
    PositionAction,
    PositionActionProposal,
    PositionLifecycleConfig,
    PositionMonitoringPlan,
    PositionPlanCreateRequest,
    SpecialistCoverageStatus,
)


@dataclass(frozen=True, slots=True)
class PositionPlanExecution:
    plan: PositionMonitoringPlan
    object_sha256: str


@dataclass(frozen=True, slots=True)
class HoldingReviewExecution:
    update: HoldingEvidenceUpdate
    review: HoldingReviewPack
    proposal: PositionActionProposal
    update_object_sha256: str
    review_object_sha256: str
    proposal_object_sha256: str


class PositionLifecycleService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        config: PositionLifecycleConfig,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.configured_rules = config
        self.repository = LifecycleRepository(state, object_store)
        self.research_repository = ResearchRepository(state, object_store)
        self.evidence_repository = EvidenceRepository(state)

    def register_rules(self) -> tuple[PositionLifecycleConfig, str]:
        config_hash = content_hash(self.configured_rules)
        summary = self.repository.rule_summary(self.configured_rules.rules_version)
        if summary is not None:
            if str(summary["config_hash"]) != config_hash:
                raise ValueError("position lifecycle rules changed without a version bump")
            rules = self.repository.get_rules(self.configured_rules.rules_version)
            assert rules is not None
            return rules, str(summary["object_hash"])
        object_ref = self.object_store.put_json(
            self.configured_rules.model_dump(mode="json")
        )
        rules = self.repository.register_rules(
            self.configured_rules,
            object_hash=object_ref.sha256,
            config_hash=config_hash,
        )
        self.state.register_artifact(
            artifact_id=f"PositionLifecycleConfig:{rules.rules_version}",
            artifact_type="PositionLifecycleConfig",
            schema_version=rules.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[config_hash],
        )
        return rules, object_ref.sha256

    def create_plan(self, request: PositionPlanCreateRequest) -> PositionPlanExecution:
        rules, rules_hash = self.register_rules()
        base_case = self.research_repository.get_base_case(request.base_case_id)
        route = self.research_repository.get_route_plan(request.route_plan_id)
        memo = self.research_repository.get_research_memo(request.memo_id)
        if base_case is None or route is None or memo is None:
            raise ValueError("position plan requires registered BaseCase, route, and memo")
        if (
            base_case.company_id != request.company_id
            or base_case.base_case_id != route.base_case_id
            or base_case.base_case_id != memo.base_case_id
            or route.route_plan_id != memo.route_plan_id
            or base_case.as_of != request.as_of
            or memo.as_of != request.as_of
        ):
            raise ValueError("position plan research lineage does not match its company/as_of")
        if request.decision_reference_status is DecisionReferenceStatus.REGISTERED_ARTIFACT:
            with self.state.connect() as connection:
                decision = connection.execute(
                    "SELECT 1 FROM artifact_registry WHERE artifact_id IN (?,?) LIMIT 1",
                    (request.decision_id, f"DecisionPack:{request.decision_id}"),
                ).fetchone()
            if decision is None:
                raise ValueError("registered position decision artifact does not exist")
        baseline_scope = set(memo.evidence_ids)
        metric_evidence = {
            evidence_id
            for metric in request.validation_metrics
            for evidence_id in metric.evidence_ids
        }
        if not metric_evidence.issubset(baseline_scope):
            raise ValueError("position plan metrics must cite evidence in the research memo")
        evidence_pack = self.research_repository.get_evidence_pack(
            base_case.evidence_pack_id
        )
        if evidence_pack is None or not baseline_scope.issubset(evidence_pack.evidence_ids):
            raise ValueError("position plan memo evidence is outside the frozen evidence pack")

        input_hash = content_hash(request)
        identity = {
            "request_hash": input_hash,
            "rules_hash": rules_hash,
            "memo_hash": self.research_repository.research_memo_object_hash(memo.memo_id),
        }
        plan_id = f"position-plan:{content_hash(identity)}"
        existing = self.repository.get_plan(plan_id)
        if existing is not None:
            object_hash = self.repository.plan_object_hash(plan_id)
            assert object_hash is not None
            return PositionPlanExecution(plan=existing, object_sha256=object_hash)

        conditions_by_source: dict[LifecycleSourceType, list[dict[str, object]]] = {
            source: [] for source in LifecycleSourceType
        }
        for condition in request.conditions:
            conditions_by_source[condition.source_type].append(
                condition.model_dump(mode="json")
            )
        skill_versions = {
            item.skill_id: item.skill_version for item in route.selected
        }
        skill_versions["ResearchMemoComposer"] = (
            memo.composer_version or "research-memo-composer-v1"
        )
        plan = PositionMonitoringPlan(
            plan_id=plan_id,
            position_id=request.position_id,
            company_id=request.company_id,
            decision_id=request.decision_id,
            decision_reference_status=request.decision_reference_status.value,
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            memo_id=memo.memo_id,
            as_of=request.as_of.astimezone(UTC),
            rules_version=rules.rules_version,
            thesis_summary=request.thesis_summary,
            entry_assumptions=request.entry_assumptions,
            holding_horizon=request.holding_horizon,
            key_value_drivers=request.key_value_drivers,
            validation_metrics=[
                item.model_dump(mode="json") for item in request.validation_metrics
            ],
            monitoring_sources=request.monitoring_sources,
            monitoring_cadence=request.monitoring_cadence,
            price_rules=conditions_by_source[LifecycleSourceType.PRICE],
            fundamental_rules=conditions_by_source[LifecycleSourceType.FUNDAMENTAL],
            event_rules=[
                *conditions_by_source[LifecycleSourceType.EVENT],
                *conditions_by_source[LifecycleSourceType.MANUAL],
            ],
            add_conditions=[
                item.description
                for item in request.conditions
                if item.action is PositionAction.ADD
            ],
            trim_conditions=[
                item.description
                for item in request.conditions
                if item.action is PositionAction.TRIM
            ],
            exit_conditions=[
                item.description
                for item in request.conditions
                if item.action is PositionAction.EXIT
            ],
            invalidation_conditions=[
                item.description
                for item in request.conditions
                if item.action is PositionAction.EXIT
            ],
            manual_information_needs=request.manual_information_needs,
            last_review_at=request.as_of.astimezone(UTC),
            next_review_at=request.next_review_at.astimezone(UTC),
            skill_versions=skill_versions,
            evidence_snapshot_id=base_case.evidence_pack_id,
            baseline_evidence_ids=sorted(baseline_scope),
            coverage_status=memo.coverage_status.value,
            created_at=memo.created_at,
        )
        object_ref = self.object_store.put_json(plan.model_dump(mode="json"))
        stored = self.repository.register_plan(
            plan,
            object_hash=object_ref.sha256,
            input_hash=input_hash,
            condition_count=len(request.conditions),
        )
        stored_hash = self.repository.plan_object_hash(plan_id)
        assert stored_hash is not None
        memo_hash = self.research_repository.research_memo_object_hash(memo.memo_id)
        assert memo_hash is not None
        self.state.register_artifact(
            artifact_id=f"PositionMonitoringPlan:{plan_id}",
            artifact_type="PositionMonitoringPlan",
            schema_version=stored.schema_version,
            object_hash=stored_hash,
            input_hashes=[memo_hash, rules_hash, input_hash],
        )
        self.state.set_checkpoint(
            scope_type="position-monitoring-plan",
            scope_key=plan_id,
            cursor={
                "as_of": request.as_of.astimezone(UTC).isoformat(),
                "condition_count": len(request.conditions),
                "baseline_evidence_count": len(stored.baseline_evidence_ids),
            },
            status="SUCCEEDED",
            object_hash=stored_hash,
        )
        return PositionPlanExecution(plan=stored, object_sha256=stored_hash)

    def review(self, request: HoldingReviewRequest) -> HoldingReviewExecution:
        rules, rules_hash = self.register_rules()
        plan = self.repository.get_plan(request.plan_id)
        if plan is None:
            raise ValueError(f"unknown position monitoring plan: {request.plan_id}")
        if plan.rules_version != rules.rules_version or plan.plan_id is None:
            raise ValueError("position review rules do not match the frozen monitoring plan")
        assert plan.as_of is not None
        request_hash = content_hash(request)
        plan_hash = self.repository.plan_object_hash(plan.plan_id)
        assert plan_hash is not None
        update_identity = {
            "plan_hash": plan_hash,
            "rules_hash": rules_hash,
            "request_hash": request_hash,
        }
        update_id = f"holding-update:{content_hash(update_identity)}"
        review_identity = {
            "update_id": update_id,
            "rules_version": rules.rules_version,
        }
        review_id = f"holding-review:{content_hash(review_identity)}"
        proposal_identity = {"review_id": review_id, "manual_confirmation": True}
        proposal_id = f"position-proposal:{content_hash(proposal_identity)}"
        existing_review = self.repository.get_review(review_id)
        if existing_review is not None:
            existing_update = self.repository.get_update(update_id)
            existing_proposal = self.repository.get_proposal(proposal_id)
            if existing_update is not None and existing_proposal is not None:
                update_hash = self.repository.update_object_hash(update_id)
                review_hash = self.repository.review_object_hash(review_id)
                proposal_hash = self.repository.proposal_object_hash(proposal_id)
                assert update_hash and review_hash and proposal_hash
                return HoldingReviewExecution(
                    update=existing_update,
                    review=existing_review,
                    proposal=existing_proposal,
                    update_object_sha256=update_hash,
                    review_object_sha256=review_hash,
                    proposal_object_sha256=proposal_hash,
                )

        if existing_review is None:
            latest = self.repository.latest_review_for_plan(plan.plan_id)
            expected_from = (
                plan.as_of
                if latest is None
                else _parse_utc_text(str(latest["to_as_of"]))
            )
            if request.from_as_of.astimezone(UTC) != expected_from.astimezone(UTC):
                raise ValueError(
                    "holding review window is not contiguous with the last checkpoint"
                )
        conditions = self._conditions(plan)
        condition_by_rule = {item.rule_id: item for item in conditions}
        self._validate_incremental_inputs(request, plan, condition_by_rule)

        update = HoldingEvidenceUpdate(
            update_id=update_id,
            plan_id=plan.plan_id,
            rules_version=rules.rules_version,
            position_id=plan.position_id,
            from_as_of=request.from_as_of.astimezone(UTC),
            to_as_of=request.to_as_of.astimezone(UTC),
            added_evidence_ids=sorted(request.added_evidence_ids),
            changed_claim_ids=sorted(request.changed_claim_ids),
            invalidated_evidence_ids=sorted(request.invalidated_evidence_ids),
            unresolved_conflicts=sorted(request.unresolved_conflict_ids),
            update_hash=request_hash,
            created_at=request.to_as_of.astimezone(UTC),
        )
        update_ref = self.object_store.put_json(update.model_dump(mode="json"))
        stored_update = self.repository.register_update(
            update,
            object_hash=update_ref.sha256,
            input_hash=request_hash,
        )
        stored_update_hash = self.repository.update_object_hash(update_id)
        assert stored_update_hash is not None

        triggered_conditions = [condition_by_rule[item.rule_id] for item in request.signals]
        hard_blocks: set[str] = set()
        candidates = {item.action for item in triggered_conditions}
        if request.unresolved_conflict_ids:
            candidates.add(PositionAction.REVIEW)
            hard_blocks.add(rules.conflict_hard_block_code)
        if request.invalidated_evidence_ids:
            candidates.add(PositionAction.REVIEW)
            hard_blocks.add(rules.invalidated_evidence_hard_block_code)
        for condition in triggered_conditions:
            if condition.hard_block:
                hard_blocks.add(condition.signal_code)
        add_conditions = [
            condition
            for condition in triggered_conditions
            if condition.action is PositionAction.ADD
        ]
        add_signal_evidence = {
            evidence_id
            for signal in request.signals
            if condition_by_rule[signal.rule_id].action is PositionAction.ADD
            for evidence_id in signal.evidence_ids
        }
        if add_conditions and not add_signal_evidence:
            candidates.discard(PositionAction.ADD)
            candidates.add(PositionAction.REVIEW)
            hard_blocks.add(rules.add_support_missing_code)
        action = next(
            item
            for item in rules.action_priority
            if item in candidates or item is PositionAction.HOLD
        )
        coverage = SpecialistCoverageStatus(plan.coverage_status)
        confidence = min(
            rules.base_action_confidence[action],
            rules.coverage_confidence_caps[coverage],
        )
        if hard_blocks:
            confidence = max(0.0, confidence - 0.1)
        evidence_ids = sorted(set(request.added_evidence_ids))
        triggered_rules = sorted(item.rule_id for item in triggered_conditions)
        degradation_codes = set(hard_blocks)
        if not request.added_evidence_ids:
            degradation_codes.add("NO_NEW_EVIDENCE")
        if plan.decision_reference_status == DecisionReferenceStatus.USER_DECLARED_EXTERNAL.value:
            degradation_codes.add("EXTERNAL_DECISION_REFERENCE")
        signal_rows = [
            {
                "rule_id": signal.rule_id,
                "signal_code": condition_by_rule[signal.rule_id].signal_code,
                "observed_value": signal.observed_value,
                "occurred_at": signal.occurred_at.isoformat(),
                "evidence_ids": signal.evidence_ids,
            }
            for signal in request.signals
        ]
        review = HoldingReviewPack(
            review_id=review_id,
            plan_id=plan.plan_id,
            evidence_update_id=update_id,
            rules_version=rules.rules_version,
            position_id=plan.position_id,
            as_of=request.to_as_of.astimezone(UTC),
            new_market_data=self._signal_rows(
                signal_rows, conditions, LifecycleSourceType.PRICE
            ),
            new_disclosures=self._signal_rows(
                signal_rows, conditions, LifecycleSourceType.FUNDAMENTAL
            ),
            new_regulatory_events=self._signal_rows(
                signal_rows, conditions, LifecycleSourceType.EVENT
            ),
            new_industry_data=[],
            new_news_leads=[],
            manual_evidence_updates=[
                {"evidence_id": evidence_id}
                for evidence_id in request.added_evidence_ids
                if not any(evidence_id in signal.evidence_ids for signal in request.signals)
            ],
            thesis_strength_change=(
                "STRENGTHENED"
                if action is PositionAction.ADD
                else "WEAKENED"
                if action in {PositionAction.TRIM, PositionAction.EXIT}
                else "UNRESOLVED"
                if action is PositionAction.REVIEW
                else "UNCHANGED"
            ),
            risk_change=(
                "HIGHER"
                if action in {PositionAction.TRIM, PositionAction.EXIT}
                else "UNKNOWN"
                if action is PositionAction.REVIEW
                else "UNCHANGED"
            ),
            triggered_rules=triggered_rules,
            unresolved_conflicts=sorted(request.unresolved_conflict_ids),
            recommended_action=action,
            action_confidence=confidence,
            evidence_ids=evidence_ids,
            next_review_conditions=sorted(
                {
                    *plan.manual_information_needs,
                    *(
                        item.signal_code
                        for item in conditions
                        if item.rule_id not in triggered_rules
                    ),
                }
            ),
            hard_blocks=sorted(hard_blocks),
            degradation_codes=sorted(degradation_codes),
            proposal_id=proposal_id,
            created_at=request.to_as_of.astimezone(UTC),
        )
        proposal = PositionActionProposal(
            proposal_id=proposal_id,
            position_id=plan.position_id,
            action=action,
            qty_or_weight_limit=None,
            reasons=sorted(
                {
                    *(condition_by_rule[item.rule_id].signal_code for item in request.signals),
                    *hard_blocks,
                    *([] if candidates else ["NO_HIGHER_PRIORITY_TRIGGER"]),
                }
            ),
            evidence_ids=evidence_ids,
            hard_blocks=sorted(hard_blocks),
            requires_user_confirmation=True,
            plan_id=plan.plan_id,
            review_id=review_id,
            created_at=request.to_as_of.astimezone(UTC),
        )
        review_ref = self.object_store.put_json(review.model_dump(mode="json"))
        proposal_ref = self.object_store.put_json(proposal.model_dump(mode="json"))
        stored_review = self.repository.register_review(
            review,
            object_hash=review_ref.sha256,
            input_hash=request_hash,
            from_as_of=request.from_as_of.astimezone(UTC).isoformat(),
        )
        stored_review_hash = self.repository.review_object_hash(review_id)
        assert stored_review_hash is not None
        stored_proposal = self.repository.register_proposal(
            proposal,
            object_hash=proposal_ref.sha256,
            input_hash=request_hash,
        )
        stored_proposal_hash = self.repository.proposal_object_hash(proposal_id)
        assert stored_proposal_hash is not None
        for artifact_id, artifact_type, schema_version, object_hash, inputs in (
            (
                f"HoldingEvidenceUpdate:{update_id}",
                "HoldingEvidenceUpdate",
                stored_update.schema_version,
                stored_update_hash,
                [plan_hash, rules_hash, request_hash],
            ),
            (
                f"HoldingReviewPack:{review_id}",
                "HoldingReviewPack",
                stored_review.schema_version,
                stored_review_hash,
                [stored_update_hash, plan_hash, rules_hash],
            ),
            (
                f"PositionActionProposal:{proposal_id}",
                "PositionActionProposal",
                stored_proposal.schema_version,
                stored_proposal_hash,
                [stored_review_hash, rules_hash],
            ),
        ):
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                schema_version=schema_version,
                object_hash=object_hash,
                input_hashes=inputs,
            )
        self.state.set_checkpoint(
            scope_type="holding-review",
            scope_key=plan.plan_id,
            cursor={
                "review_id": review_id,
                "to_as_of": request.to_as_of.astimezone(UTC).isoformat(),
                "recommended_action": action.value,
            },
            status="SUCCEEDED",
            object_hash=stored_review_hash,
        )
        return HoldingReviewExecution(
            update=stored_update,
            review=stored_review,
            proposal=stored_proposal,
            update_object_sha256=stored_update_hash,
            review_object_sha256=stored_review_hash,
            proposal_object_sha256=stored_proposal_hash,
        )

    def status(self, position_id: str) -> dict[str, object]:
        plan = self.repository.latest_plan_summary(position_id)
        if plan is None:
            return {"status": "NOT_RUN", "position_id": position_id}
        review = self.repository.latest_review_summary_for_plan(str(plan["plan_id"]))
        return {
            "status": "MONITORED",
            "position_id": position_id,
            "plan": plan,
            "latest_review": review,
        }

    def audit(self, position_id: str) -> dict[str, object]:
        plan_summary = self.repository.latest_plan_summary(position_id)
        if plan_summary is None:
            return {"status": "NOT_RUN", "position_id": position_id}
        plan = self.repository.get_plan(str(plan_summary["plan_id"]))
        if plan is None:
            return {
                "status": "PARTIAL",
                "position_id": position_id,
                "finding_codes": ["MONITORING_PLAN_OBJECT_MISSING"],
            }
        conditions = self._conditions(plan)
        base_case = (
            self.research_repository.get_base_case(plan.base_case_id)
            if plan.base_case_id
            else None
        )
        route = (
            self.research_repository.get_route_plan(plan.route_plan_id)
            if plan.route_plan_id
            else None
        )
        memo = (
            self.research_repository.get_research_memo(plan.memo_id)
            if plan.memo_id
            else None
        )
        plan_metadata_mismatch = int(
            int(str(plan_summary["condition_count"])) != len(conditions)
            or int(str(plan_summary["baseline_evidence_count"]))
            != len(plan.baseline_evidence_ids)
        )
        lineage_mismatch = int(
            base_case is None
            or route is None
            or memo is None
            or route.base_case_id != base_case.base_case_id
            or memo.base_case_id != base_case.base_case_id
            or memo.route_plan_id != route.route_plan_id
            or plan.company_id != base_case.company_id
        )
        plan_artifact_mismatch = self._artifact_mismatch(
            f"PositionMonitoringPlan:{plan.plan_id}",
            str(plan_summary["object_hash"]),
        )
        review_missing = 0
        update_missing = 0
        proposal_missing = 0
        review_metadata_mismatch = 0
        proposal_confirmation_mismatch = 0
        review_artifact_mismatch = 0
        evidence_window_mismatch = 0
        review_window_discontinuity = 0
        expected_from = plan.as_of
        review_summaries = self.repository.review_summaries_for_plan(str(plan.plan_id))
        for window in review_summaries:
            window_from = _parse_utc_text(str(window["from_as_of"]))
            window_to = _parse_utc_text(str(window["to_as_of"]))
            review_window_discontinuity += int(
                expected_from is None
                or window_from.astimezone(UTC) != expected_from.astimezone(UTC)
                or window_to <= window_from
            )
            expected_from = window_to
            review = self.repository.get_review(str(window["review_id"]))
            update = self.repository.get_update(str(window["update_id"]))
            proposal = self.repository.get_proposal(str(window["proposal_id"]))
            review_missing += int(review is None)
            update_missing += int(update is None)
            proposal_missing += int(proposal is None)
            if review is not None:
                review_metadata_mismatch += int(
                    int(str(window["trigger_count"]))
                    != len(review.triggered_rules)
                    or int(str(window["hard_block_count"]))
                    != len(review.hard_blocks)
                    or int(str(window["evidence_count"])) != len(review.evidence_ids)
                    or str(window["recommended_action"])
                    != review.recommended_action.value
                )
                review_artifact_mismatch += self._artifact_mismatch(
                    f"HoldingReviewPack:{review.review_id}",
                    str(window["object_hash"]),
                )
            if update is not None:
                for evidence_id in update.added_evidence_ids:
                    evidence = self.evidence_repository.get_evidence(evidence_id)
                    evidence_window_mismatch += int(
                        evidence is None
                        or evidence.available_to_system_at <= update.from_as_of
                        or evidence.available_to_system_at > update.to_as_of
                        or plan.company_id not in evidence.entity_ids
                    )
                update_hash = self.repository.update_object_hash(str(update.update_id))
                review_artifact_mismatch += self._artifact_mismatch(
                    f"HoldingEvidenceUpdate:{update.update_id}",
                    update_hash or "",
                )
            if proposal is not None:
                proposal_confirmation_mismatch += int(
                    not proposal.requires_user_confirmation
                    or proposal.action
                    is not (review.recommended_action if review else proposal.action)
                )
                proposal_hash = self.repository.proposal_object_hash(proposal.proposal_id)
                review_artifact_mismatch += self._artifact_mismatch(
                    f"PositionActionProposal:{proposal.proposal_id}",
                    proposal_hash or "",
                )
        findings = {
            "PLAN_METADATA_MISMATCH": plan_metadata_mismatch,
            "RESEARCH_LINEAGE_MISMATCH": lineage_mismatch,
            "PLAN_ARTIFACT_MISMATCH": plan_artifact_mismatch,
            "REVIEW_OBJECT_MISSING": review_missing,
            "UPDATE_OBJECT_MISSING": update_missing,
            "PROPOSAL_OBJECT_MISSING": proposal_missing,
            "REVIEW_METADATA_MISMATCH": review_metadata_mismatch,
            "PROPOSAL_CONFIRMATION_MISMATCH": proposal_confirmation_mismatch,
            "REVIEW_ARTIFACT_MISMATCH": review_artifact_mismatch,
            "EVIDENCE_WINDOW_MISMATCH": evidence_window_mismatch,
            "REVIEW_WINDOW_DISCONTINUITY": review_window_discontinuity,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "position_id": position_id,
            "plan_id": plan.plan_id,
            "latest_review_id": (
                str(review_summaries[-1]["review_id"]) if review_summaries else None
            ),
            "finding_codes": finding_codes,
            "finding_counts": findings,
        }

    def _validate_incremental_inputs(
        self,
        request: HoldingReviewRequest,
        plan: PositionMonitoringPlan,
        condition_by_rule: dict[str, LifecycleCondition],
    ) -> None:
        unknown_rules = sorted(
            {item.rule_id for item in request.signals} - set(condition_by_rule)
        )
        if unknown_rules:
            raise ValueError("holding review contains an unknown monitoring rule")
        added = set(request.added_evidence_ids)
        for signal in request.signals:
            if not set(signal.evidence_ids).issubset(added):
                raise ValueError("holding signals can only cite this window's new evidence")
            if not (
                request.from_as_of < signal.occurred_at <= request.to_as_of
            ):
                raise ValueError("holding signal occurred outside the review window")
        for evidence_id in request.added_evidence_ids:
            evidence = self.evidence_repository.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("holding review references unknown new evidence")
            if plan.company_id not in evidence.entity_ids:
                raise ValueError("holding review evidence belongs to another company")
            if not (
                request.from_as_of
                < evidence.available_to_system_at
                <= request.to_as_of
            ):
                raise ValueError("holding review evidence is outside the incremental window")
        invalidated = set(request.invalidated_evidence_ids)
        if not invalidated.issubset(plan.baseline_evidence_ids):
            raise ValueError("only baseline memo evidence can be invalidated in this review")
        for claim_id in request.changed_claim_ids:
            bundle = self.evidence_repository.get_claim_bundle(claim_id)
            if bundle is None or bundle.claim.subject_id != plan.company_id:
                raise ValueError("holding review changed claim is unknown or out of company scope")
            if not (request.from_as_of < bundle.claim.as_of <= request.to_as_of):
                raise ValueError("holding review changed claim is outside the incremental window")
            new_claim_evidence: set[str] = set()
            for link in bundle.links:
                evidence = self.evidence_repository.get_evidence(link.evidence_id)
                if (
                    evidence is None
                    or plan.company_id not in evidence.entity_ids
                    or evidence.available_to_system_at > request.to_as_of
                ):
                    raise ValueError("holding review changed claim has unavailable evidence")
                if evidence.available_to_system_at > request.from_as_of:
                    new_claim_evidence.add(evidence.evidence_id)
            if not new_claim_evidence or not new_claim_evidence.issubset(added):
                raise ValueError(
                    "holding review changed claim must declare its new window evidence"
                )
        for conflict_id in request.unresolved_conflict_ids:
            with self.state.connect() as connection:
                row = connection.execute(
                    "SELECT ec.conflict_json,cr.subject_id FROM evidence_conflict ec "
                    "JOIN claim_record cr ON cr.claim_id=ec.claim_id WHERE ec.conflict_id=?",
                    (conflict_id,),
                ).fetchone()
            if row is None or str(row["subject_id"]) != plan.company_id:
                raise ValueError("holding review conflict is unknown or out of company scope")
            conflict = EvidenceConflict.model_validate_json(row["conflict_json"])
            if conflict.resolution_status is ConflictResolutionStatus.RESOLVED:
                raise ValueError("resolved evidence conflicts cannot remain open in a review")
            new_conflict_evidence: set[str] = set()
            for evidence_id in conflict.evidence_ids:
                evidence = self.evidence_repository.get_evidence(evidence_id)
                if (
                    evidence is None
                    or plan.company_id not in evidence.entity_ids
                    or evidence.available_to_system_at > request.to_as_of
                ):
                    raise ValueError("holding review conflict has unavailable evidence")
                if evidence.available_to_system_at > request.from_as_of:
                    new_conflict_evidence.add(evidence.evidence_id)
            if not new_conflict_evidence.issubset(added):
                raise ValueError(
                    "holding review conflict must declare its new window evidence"
                )

    @staticmethod
    def _conditions(plan: PositionMonitoringPlan) -> list[LifecycleCondition]:
        payloads = [*plan.price_rules, *plan.fundamental_rules, *plan.event_rules]
        return [LifecycleCondition.model_validate(item) for item in payloads]

    @staticmethod
    def _signal_rows(
        rows: list[dict[str, object]],
        conditions: list[LifecycleCondition],
        source_type: LifecycleSourceType,
    ) -> list[dict[str, object]]:
        ids = {item.rule_id for item in conditions if item.source_type is source_type}
        return [row for row in rows if str(row["rule_id"]) in ids]

    def _artifact_mismatch(self, artifact_id: str, object_hash: str) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return int(row is None or str(row["object_hash"]) != object_hash)


def _parse_utc_text(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


__all__ = [
    "HoldingReviewExecution",
    "PositionLifecycleService",
    "PositionPlanExecution",
]
