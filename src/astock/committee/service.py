"""Deterministic frozen-input investment committee and protocol generation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from astock.committee.repository import CommitteeRepository
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.policy import PolicyEngine
from astock.core.state import StateStore
from astock.schemas import (
    BaseCasePack,
    CommitteeAccessPolicy,
    CommitteeArtifactReference,
    CommitteeAssessment,
    CommitteeAssessmentSnapshot,
    CommitteeBudgetReport,
    CommitteeDecisionRequest,
    CommitteeDecisionScope,
    CommitteeInputBundle,
    CommitteeInputRole,
    CommitteeInvestigationTask,
    CommitteeMemberRole,
    CommitteeNarrativeMode,
    CommitteePlanReport,
    CommitteeProtocolStatus,
    CommitteeRuleConfig,
    CommitteeVerdict,
    ContextBudgetReport,
    CounterCaseDraft,
    CounterCasePack,
    CounterCaseTriggerCode,
    DecisionPack,
    FinancialCoverageStatus,
    FinancialFindingStatus,
    FinancialIntegrityEvidencePack,
    FinancialRiskLevel,
    FinancialSeverity,
    FrozenEvidencePack,
    HoldingEvidenceUpdate,
    HoldingReviewPack,
    KnowledgeSkillDelta,
    PositionAction,
    PositionActionProposal,
    PositionMonitoringPlan,
    ResearchCoverageStatus,
    ResearchGapSeverity,
    ResearchMemoArtifact,
    SpecialistCoverageStatus,
    SpecialistDelta,
    SpecialistDiagnosticReport,
    SpecialistRoutePlan,
    TradeProtocol,
)
from astock.schemas.base import AStockModel

_INITIAL_ARTIFACT_MODELS: dict[str, type[AStockModel]] = {
    "FrozenEvidencePack": FrozenEvidencePack,
    "BaseCasePack": BaseCasePack,
    "SpecialistRoutePlan": SpecialistRoutePlan,
    "SpecialistDelta": SpecialistDelta,
    "KnowledgeSkillDelta": KnowledgeSkillDelta,
    "SpecialistDiagnosticReport": SpecialistDiagnosticReport,
    "ResearchMemoArtifact": ResearchMemoArtifact,
    "FinancialIntegrityEvidencePack": FinancialIntegrityEvidencePack,
    "PositionMonitoringPlan": PositionMonitoringPlan,
    "HoldingEvidenceUpdate": HoldingEvidenceUpdate,
    "HoldingReviewPack": HoldingReviewPack,
    "PositionActionProposal": PositionActionProposal,
}

_EXPECTED_ROLES = {
    "FrozenEvidencePack": CommitteeInputRole.PRIMARY,
    "BaseCasePack": CommitteeInputRole.PRIMARY,
    "SpecialistRoutePlan": CommitteeInputRole.SPECIALIST,
    "SpecialistDelta": CommitteeInputRole.SPECIALIST,
    "KnowledgeSkillDelta": CommitteeInputRole.SPECIALIST,
    "SpecialistDiagnosticReport": CommitteeInputRole.SPECIALIST,
    "ResearchMemoArtifact": CommitteeInputRole.PRIMARY,
    "FinancialIntegrityEvidencePack": CommitteeInputRole.FINANCIAL,
    "PositionMonitoringPlan": CommitteeInputRole.LIFECYCLE,
    "HoldingEvidenceUpdate": CommitteeInputRole.LIFECYCLE,
    "HoldingReviewPack": CommitteeInputRole.LIFECYCLE,
    "PositionActionProposal": CommitteeInputRole.LIFECYCLE,
}

_GENERATED_ARTIFACT_TYPES = {
    "CommitteeAssessmentSnapshot",
    "CommitteeRuleConfig",
    "CounterCasePack",
}

_SERENITY_SKILL_IDS = {
    "DailyTrendHealthSkill",
    "EventToAlphaSkill",
    "GrowthProbabilitySkill",
    "GrowthValuationLens",
    "IndustryBottleneckSkill",
    "SerenityRecordedSkill",
}
_ZHIHU_EXPERT_SKILL_IDS = {"ZhihuExpertRecordedSkill", "ZhihuExpertSkill"}
_TRADE_PROTOCOL_CONTRACT_VERSION = "paper-only-confirmed-v2"


@dataclass(frozen=True, slots=True)
class CommitteeExecution:
    assessment: CommitteeAssessmentSnapshot
    bundle: CommitteeInputBundle
    counter_case: CounterCasePack | None
    decision: DecisionPack
    protocol: TradeProtocol
    investigation_tasks: list[CommitteeInvestigationTask]
    object_sha256_by_type: dict[str, str]


@dataclass(frozen=True, slots=True)
class _LoadedInputs:
    by_type: dict[str, list[AStockModel]]
    evidence_ids: set[str]
    claim_ids: set[str]
    skill_versions: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Prepared:
    request_hash: str
    assessment: CommitteeAssessmentSnapshot
    assessment_object_hash: str
    rules: CommitteeRuleConfig
    rules_object_hash: str
    loaded: _LoadedInputs
    triggers: list[CounterCaseTriggerCode]
    counter_case: CounterCasePack | None
    counter_case_object_hash: str | None
    bundle: CommitteeInputBundle
    bundle_object_hash: str
    decision: DecisionPack
    decision_object_hash: str
    protocol: TradeProtocol
    protocol_object_hash: str
    tasks: list[CommitteeInvestigationTask]
    task_object_hashes: dict[str, str]


class CommitteeService:
    """A no-network committee over registered immutable artifacts only."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        rules: CommitteeRuleConfig,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.configured_rules = rules
        self.repository = CommitteeRepository(state, object_store)
        self.policy_engine = PolicyEngine()

    @staticmethod
    def supported_input_types() -> list[str]:
        return sorted(_INITIAL_ARTIFACT_MODELS)

    def resolve_reference(self, artifact_id: str) -> CommitteeArtifactReference:
        row = self._registry_row(artifact_id)
        if row is None:
            raise ValueError(f"unknown registered committee artifact: {artifact_id}")
        artifact_type = str(row["type"])
        role = _EXPECTED_ROLES.get(artifact_type)
        if role is None:
            raise ValueError(f"unsupported committee artifact type: {artifact_type}")
        object_hash = str(row["object_hash"])
        if not self.object_store.verify(object_hash):
            raise ValueError(f"registered committee artifact object is unavailable: {artifact_id}")
        return CommitteeArtifactReference(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            object_sha256=object_hash,
            role=role,
        )

    def register_rules(self) -> tuple[CommitteeRuleConfig, str]:
        return self._rules_reference(self.configured_rules, persist=True)

    def plan(self, request: CommitteeDecisionRequest) -> CommitteePlanReport:
        prepared = self._prepare(request, persist=False)
        return CommitteePlanReport(
            request_sha256=prepared.request_hash,
            prospective_bundle_id=prepared.bundle.bundle_id,
            prospective_decision_id=prepared.decision.decision_id,
            verdict=prepared.decision.verdict,
            hard_blocks=prepared.decision.hard_blocks,
            counter_case_trigger_codes=prepared.triggers,
            missing_counter_case=(bool(prepared.triggers) and prepared.counter_case is None),
            investigation_reason_codes=sorted(task.reason_code for task in prepared.tasks),
            context_budget=prepared.decision.context_budget,
            created_at=request.assessment.as_of,
        )

    def decide(self, request: CommitteeDecisionRequest) -> CommitteeExecution:
        prepared = self._prepare(request, persist=True)
        return CommitteeExecution(
            assessment=prepared.assessment,
            bundle=prepared.bundle,
            counter_case=prepared.counter_case,
            decision=prepared.decision,
            protocol=prepared.protocol,
            investigation_tasks=prepared.tasks,
            object_sha256_by_type={
                "CommitteeAssessmentSnapshot": prepared.assessment_object_hash,
                "CommitteeInputBundle": prepared.bundle_object_hash,
                "DecisionPack": prepared.decision_object_hash,
                "TradeProtocol": prepared.protocol_object_hash,
                **(
                    {"CounterCasePack": prepared.counter_case_object_hash}
                    if prepared.counter_case_object_hash is not None
                    else {}
                ),
            },
        )

    def decide_investment(self, request: CommitteeDecisionRequest) -> CommitteeExecution:
        """Run the Phase 6 committee only when every mandatory member is frozen."""

        if not request.member_bindings:
            raise ValueError("investment committee member bindings are required")
        return self.decide(request)

    def recover(self, request: CommitteeDecisionRequest) -> dict[str, object]:
        execution = self.decide(request)
        audit = self.audit(execution.decision.decision_id)
        return {
            "status": "RECOVERED_OR_ALREADY_COMPLETE",
            "decision_id": execution.decision.decision_id,
            "protocol_id": execution.protocol.protocol_id,
            "audit_status": audit["status"],
            "finding_codes": audit["finding_codes"],
        }

    def status(
        self,
        *,
        decision_id: str | None = None,
        company_id: str | None = None,
    ) -> dict[str, object]:
        if decision_id is None and company_id is None:
            raise ValueError("committee status requires decision_id or company_id")
        summary = (
            self.repository.decision_summary(decision_id)
            if decision_id is not None
            else self.repository.latest_decision_summary(cast(str, company_id))
        )
        if summary is None:
            return {
                "status": "NOT_RUN",
                "decision_id": decision_id,
                "company_id": company_id,
            }
        resolved_decision_id = str(summary["decision_id"])
        protocol = self.repository.protocol_for_decision(resolved_decision_id)
        tasks = self.repository.task_summaries_for_decision(resolved_decision_id)
        return {
            "status": "AVAILABLE" if protocol is not None else "PARTIAL",
            "decision": summary,
            "protocol": protocol,
            "investigation_tasks": tasks,
        }

    def task_status(self, task_id: str) -> dict[str, object]:
        task = self.repository.task_summary(task_id)
        return task or {"status": "NOT_RUN", "task_id": task_id}

    def resolve_task(
        self,
        task_id: str,
        resolution_artifact_id: str,
    ) -> dict[str, object]:
        task = self.repository.task_summary(task_id)
        if task is None:
            raise ValueError(f"unknown committee investigation task: {task_id}")
        registry = self._registry_row(resolution_artifact_id)
        if registry is None:
            raise ValueError("committee task resolution artifact is not registered")
        resolution_hash = str(registry["object_hash"])
        if not self.object_store.verify(resolution_hash):
            raise ValueError("committee task resolution artifact is unavailable")
        decision = self.repository.get_decision(str(task["decision_id"]))
        if decision is None:
            raise ValueError("committee task parent decision is unavailable")
        if resolution_hash in decision.frozen_input_hashes:
            raise ValueError("committee tasks require a new frozen resolution artifact")
        return self.repository.resolve_task(
            task_id,
            resolution_artifact_id=resolution_artifact_id,
            resolution_object_hash=resolution_hash,
        )

    def audit(self, decision_id: str) -> dict[str, object]:
        findings: set[str] = set()
        decision_summary = self.repository.decision_summary(decision_id)
        decision = self.repository.get_decision(decision_id)
        if decision_summary is None or decision is None:
            return {
                "status": "NOT_RUN",
                "decision_id": decision_id,
                "finding_codes": ["DECISION_NOT_RUN"],
            }
        bundle = self.repository.get_bundle(decision.bundle_id)
        if bundle is None:
            return {
                "status": "PARTIAL",
                "decision_id": decision_id,
                "finding_codes": ["BUNDLE_MISSING"],
            }
        rules = self.repository.get_rules(decision.rules_version)
        if rules is None:
            findings.add("RULES_MISSING")
        assessment_ref = next(
            (
                item
                for item in bundle.artifact_references
                if item.artifact_type == "CommitteeAssessmentSnapshot"
            ),
            None,
        )
        if assessment_ref is None:
            findings.add("ASSESSMENT_REFERENCE_MISSING")
            assessment = None
        else:
            assessment_id = assessment_ref.artifact_id.removeprefix("CommitteeAssessmentSnapshot:")
            assessment = self.repository.get_assessment(assessment_id)
            if assessment is None:
                findings.add("ASSESSMENT_MISSING")

        expected_input_rows = sorted(
            (
                item.artifact_id,
                item.artifact_type,
                item.role.value,
                item.object_sha256,
            )
            for item in bundle.artifact_references
        )
        actual_input_rows = sorted(
            (
                str(item["artifact_id"]),
                str(item["artifact_type"]),
                str(item["artifact_role"]),
                str(item["object_hash"]),
            )
            for item in self.repository.bundle_inputs(bundle.bundle_id)
        )
        if expected_input_rows != actual_input_rows:
            findings.add("BUNDLE_INPUT_INDEX_MISMATCH")
        for reference in bundle.artifact_references:
            if not self._reference_matches_registry(reference):
                findings.add("BUNDLE_INPUT_ARTIFACT_MISMATCH")

        protocol_summary = self.repository.protocol_for_decision(decision_id)
        protocol = None
        if protocol_summary is None:
            findings.add("TRADE_PROTOCOL_MISSING")
        else:
            protocol = self.repository.get_protocol(str(protocol_summary["protocol_id"]))
            if protocol is None:
                findings.add("TRADE_PROTOCOL_OBJECT_INVALID")
        task_rows = self.repository.task_summaries_for_decision(decision_id)
        task_ids = sorted(str(row["task_id"]) for row in task_rows)
        if task_ids != decision.needs_info_task_ids:
            findings.add("INVESTIGATION_TASK_INDEX_MISMATCH")
        for row in task_rows:
            task_id = str(row["task_id"])
            task_object_hash = str(row["object_hash"])
            if not self._artifact_matches(
                f"CommitteeInvestigationTask:{task_id}",
                "CommitteeInvestigationTask",
                task_object_hash,
            ):
                findings.add("INVESTIGATION_TASK_ARTIFACT_MISMATCH")
            status = str(row["status"])
            resolution_artifact_id = row["resolution_artifact_id"]
            resolution_object_hash = row["resolution_object_hash"]
            if status == "RESOLVED":
                registry = (
                    self._registry_row(str(resolution_artifact_id))
                    if resolution_artifact_id is not None
                    else None
                )
                if (
                    registry is None
                    or resolution_object_hash is None
                    or str(registry["object_hash"]) != str(resolution_object_hash)
                    or str(resolution_object_hash) in decision.frozen_input_hashes
                    or not self.object_store.verify(str(resolution_object_hash))
                ):
                    findings.add("INVESTIGATION_TASK_RESOLUTION_MISMATCH")
            elif resolution_artifact_id is not None or resolution_object_hash is not None:
                findings.add("INVESTIGATION_TASK_RESOLUTION_MISMATCH")

        bundle_summary = self.repository.bundle_summary(bundle.bundle_id)
        assert bundle_summary is not None
        for artifact_id, artifact_type, object_hash in (
            (
                f"CommitteeInputBundle:{bundle.bundle_id}",
                "CommitteeInputBundle",
                str(bundle_summary["object_hash"]),
            ),
            (
                f"DecisionPack:{decision.decision_id}",
                "DecisionPack",
                str(decision_summary["object_hash"]),
            ),
        ):
            if not self._artifact_matches(artifact_id, artifact_type, object_hash):
                findings.add(f"{artifact_type.upper()}_REGISTRY_MISMATCH")
        if protocol_summary is not None and not self._artifact_matches(
            f"TradeProtocol:{protocol_summary['protocol_id']}",
            "TradeProtocol",
            str(protocol_summary["object_hash"]),
        ):
            findings.add("TRADEPROTOCOL_REGISTRY_MISMATCH")

        if rules is not None and assessment is not None:
            try:
                reconstructed = self._reconstruct_request(bundle, assessment)
                prepared = self._prepare(
                    reconstructed,
                    persist=False,
                    rules_override=rules,
                )
                if canonical_json_bytes(prepared.bundle.model_dump(mode="json")) != (
                    canonical_json_bytes(bundle.model_dump(mode="json"))
                ):
                    findings.add("BUNDLE_RECOMPUTE_MISMATCH")
                if canonical_json_bytes(prepared.decision.model_dump(mode="json")) != (
                    canonical_json_bytes(decision.model_dump(mode="json"))
                ):
                    findings.add("DECISION_RECOMPUTE_MISMATCH")
                if protocol is not None and canonical_json_bytes(
                    prepared.protocol.model_dump(mode="json")
                ) != canonical_json_bytes(protocol.model_dump(mode="json")):
                    findings.add("TRADE_PROTOCOL_RECOMPUTE_MISMATCH")
                expected_tasks = {
                    task.task_id: sha256_bytes(canonical_json_bytes(task.model_dump(mode="json")))
                    for task in prepared.tasks
                }
                actual_tasks = {str(row["task_id"]): str(row["object_hash"]) for row in task_rows}
                if expected_tasks != actual_tasks:
                    findings.add("INVESTIGATION_TASK_RECOMPUTE_MISMATCH")
            except (ValueError, TypeError):
                findings.add("DETERMINISTIC_RECOMPUTE_FAILED")

        return {
            "status": "PASS" if not findings else "PARTIAL",
            "decision_id": decision_id,
            "bundle_id": decision.bundle_id,
            "verdict": decision.verdict.value,
            "finding_codes": sorted(findings),
        }

    def _prepare(
        self,
        request: CommitteeDecisionRequest,
        *,
        persist: bool,
        rules_override: CommitteeRuleConfig | None = None,
    ) -> _Prepared:
        rules = rules_override or self.configured_rules
        self.policy_engine.check_committee_access(
            request.access_policy,
            expected_hashes=[item.object_sha256 for item in request.artifact_references],
        )
        if rules.effective_from > request.assessment.as_of:
            raise ValueError("committee rules are not effective at the requested as_of")
        rules, rules_object_hash = self._rules_reference(rules, persist=persist)
        loaded = self._load_and_validate_initial_inputs(request, rules)
        request_hash = content_hash(request)
        assessment_hash = content_hash(request.assessment)
        assessment_id = f"committee-assessment:{assessment_hash}"
        assessment_payload = cast(
            dict[str, Any],
            _replace_created_at(
                request.assessment.model_dump(
                    mode="python",
                    exclude={"schema_version", "created_at"},
                ),
                request.assessment.as_of,
            ),
        )
        assessment = CommitteeAssessmentSnapshot(
            **assessment_payload,
            schema_version=request.assessment.schema_version,
            assessment_id=assessment_id,
            request_sha256=assessment_hash,
            created_at=request.assessment.as_of,
        )
        assessment_bytes = canonical_json_bytes(assessment.model_dump(mode="json"))
        assessment_object_hash = sha256_bytes(assessment_bytes)
        if persist:
            self.object_store.put_bytes(assessment_bytes)
            self.repository.register_assessment(
                assessment,
                object_hash=assessment_object_hash,
            )
            self.state.register_artifact(
                artifact_id=f"CommitteeAssessmentSnapshot:{assessment.assessment_id}",
                artifact_type="CommitteeAssessmentSnapshot",
                schema_version=assessment.schema_version,
                object_hash=assessment_object_hash,
                input_hashes=[assessment_hash],
            )

        triggers = self._counter_case_triggers(assessment, loaded, rules)
        if request.counter_case is not None and not triggers:
            raise ValueError("counter-case supplied without a configured trigger")
        counter_case = self._build_counter_case(
            request.counter_case,
            assessment,
            triggers,
            loaded,
            [
                *(item.object_sha256 for item in request.artifact_references),
                assessment_object_hash,
                rules_object_hash,
            ],
        )
        counter_case_object_hash: str | None = None
        if counter_case is not None:
            counter_case_bytes = canonical_json_bytes(counter_case.model_dump(mode="json"))
            counter_case_object_hash = sha256_bytes(counter_case_bytes)
            if persist:
                self.object_store.put_bytes(counter_case_bytes)
                self.repository.register_counter_case(
                    counter_case,
                    assessment_id=assessment.assessment_id,
                    object_hash=counter_case_object_hash,
                )
                self.state.register_artifact(
                    artifact_id=f"CounterCasePack:{counter_case.counter_case_id}",
                    artifact_type="CounterCasePack",
                    schema_version=counter_case.schema_version,
                    object_hash=counter_case_object_hash,
                    input_hashes=counter_case.frozen_input_hashes,
                )

        generated_references = [
            CommitteeArtifactReference(
                artifact_id=f"CommitteeAssessmentSnapshot:{assessment.assessment_id}",
                artifact_type="CommitteeAssessmentSnapshot",
                object_sha256=assessment_object_hash,
                role=CommitteeInputRole.STATE,
                created_at=assessment.as_of,
            ),
            CommitteeArtifactReference(
                artifact_id=f"CommitteeRuleConfig:{rules.rules_version}",
                artifact_type="CommitteeRuleConfig",
                object_sha256=rules_object_hash,
                role=CommitteeInputRole.STATE,
                created_at=assessment.as_of,
            ),
        ]
        if counter_case is not None and counter_case_object_hash is not None:
            generated_references.append(
                CommitteeArtifactReference(
                    artifact_id=f"CounterCasePack:{counter_case.counter_case_id}",
                    artifact_type="CounterCasePack",
                    object_sha256=counter_case_object_hash,
                    role=CommitteeInputRole.COUNTER_CASE,
                    created_at=assessment.as_of,
                )
            )
        normalized_initial_references = [
            item.model_copy(update={"created_at": assessment.as_of})
            for item in request.artifact_references
        ]
        normalized_member_bindings = [
            item.model_copy(update={"created_at": assessment.as_of})
            for item in request.member_bindings
        ]
        bundle_references = sorted(
            [*normalized_initial_references, *generated_references],
            key=lambda item: item.artifact_id,
        )
        bundle_identity = {
            "company_id": assessment.company_id,
            "scope": assessment.scope.value,
            "as_of": assessment.as_of,
            "references": [
                (
                    item.artifact_id,
                    item.artifact_type,
                    item.role.value,
                    item.object_sha256,
                )
                for item in bundle_references
            ],
            "member_bindings": [
                (item.role.value, item.artifact_id, item.object_sha256)
                for item in normalized_member_bindings
            ],
            "rules_version": rules.rules_version,
            "engine_version": rules.engine_version,
        }
        bundle_hash = content_hash(bundle_identity)
        bundle = CommitteeInputBundle(
            bundle_id=f"committee-bundle:{bundle_hash}",
            company_id=assessment.company_id,
            scope=assessment.scope,
            as_of=assessment.as_of,
            artifact_references=bundle_references,
            member_bindings=normalized_member_bindings,
            access_policy=CommitteeAccessPolicy(
                frozen_artifact_hashes=sorted(item.object_sha256 for item in bundle_references),
                created_at=assessment.as_of,
            ),
            rules_version=rules.rules_version,
            engine_version=rules.engine_version,
            skill_versions=loaded.skill_versions,
            bundle_sha256=bundle_hash,
            created_at=assessment.as_of,
        )
        bundle_bytes = canonical_json_bytes(bundle.model_dump(mode="json"))
        bundle_object_hash = sha256_bytes(bundle_bytes)
        if persist:
            self.object_store.put_bytes(bundle_bytes)
            self.repository.register_bundle(
                bundle,
                assessment_id=assessment.assessment_id,
                counter_case_id=(
                    counter_case.counter_case_id if counter_case is not None else None
                ),
                object_hash=bundle_object_hash,
            )
            self.state.register_artifact(
                artifact_id=f"CommitteeInputBundle:{bundle.bundle_id}",
                artifact_type="CommitteeInputBundle",
                schema_version=bundle.schema_version,
                object_hash=bundle_object_hash,
                input_hashes=sorted(item.object_sha256 for item in bundle.artifact_references),
            )

        generated_sizes = {
            assessment_object_hash: len(assessment_bytes),
            rules_object_hash: len(canonical_json_bytes(rules.model_dump(mode="json"))),
            **(
                {
                    cast(str, counter_case_object_hash): len(
                        canonical_json_bytes(
                            cast(CounterCasePack, counter_case).model_dump(mode="json")
                        )
                    )
                }
                if counter_case_object_hash is not None
                else {}
            ),
        }
        budget = self._budget(bundle, assessment, generated_sizes, rules)
        verdict, hard_blocks, reason_codes = self._verdict(
            assessment,
            loaded,
            rules,
            triggers,
            counter_case,
        )
        decision_identity = {
            "bundle_hash": bundle.bundle_sha256,
            "rules_version": rules.rules_version,
            "engine_version": rules.engine_version,
            "verdict": verdict.value,
            "hard_blocks": hard_blocks,
            "reason_codes": reason_codes,
            "counter_case_id": (counter_case.counter_case_id if counter_case is not None else None),
            "max_position": str(self._max_position(verdict, assessment, rules)),
            "review_at": assessment.review_at,
        }
        decision_hash = content_hash(decision_identity)
        decision_id = f"decision:{decision_hash}"
        task_reason_codes = hard_blocks if verdict is CommitteeVerdict.NEEDS_INFO else []
        tasks = [
            self._investigation_task(
                decision_id=decision_id,
                bundle_id=bundle.bundle_id,
                company_id=assessment.company_id,
                reason_code=reason_code,
                as_of=assessment.as_of,
            )
            for reason_code in task_reason_codes
        ]
        task_ids = sorted(task.task_id for task in tasks)
        evidence_ids = sorted(
            set(assessment.support_evidence_ids)
            | set(assessment.expected_return_range.evidence_ids)
            | set(assessment.downside_range.evidence_ids)
            | set(assessment.coverage.evidence_ids)
            | set(assessment.portfolio_risk.evidence_ids)
            | set(assessment.protocol.evidence_ids)
            | (set(counter_case.evidence_ids) if counter_case is not None else set())
        )
        rationale_codes = sorted(
            {
                f"VERDICT:{verdict.value}",
                *reason_codes,
                *(f"COUNTER_CASE:{item.value}" for item in triggers),
            }
        )
        decision = DecisionPack(
            decision_id=decision_id,
            bundle_id=bundle.bundle_id,
            company_id=assessment.company_id,
            scope=assessment.scope,
            as_of=assessment.as_of,
            rules_version=rules.rules_version,
            engine_version=rules.engine_version,
            frozen_input_hashes=sorted(item.object_sha256 for item in bundle.artifact_references),
            verdict=verdict,
            expected_return_range=assessment.expected_return_range,
            downside_range=assessment.downside_range,
            confidence=assessment.confidence,
            hard_blocks=hard_blocks,
            needs_info_task_ids=task_ids,
            counter_case_trigger_codes=triggers,
            counter_case_id=(counter_case.counter_case_id if counter_case is not None else None),
            current_position=assessment.current_position,
            max_position=self._max_position(verdict, assessment, rules),
            review_at=assessment.review_at,
            rationale_codes=rationale_codes,
            evidence_ids=evidence_ids,
            context_budget=budget,
            decision_sha256=decision_hash,
            created_at=assessment.as_of,
        )
        decision_bytes = canonical_json_bytes(decision.model_dump(mode="json"))
        decision_object_hash = sha256_bytes(decision_bytes)
        if persist:
            self.object_store.put_bytes(decision_bytes)
            self.repository.register_decision(decision, object_hash=decision_object_hash)
            self.state.register_artifact(
                artifact_id=f"DecisionPack:{decision.decision_id}",
                artifact_type="DecisionPack",
                schema_version=decision.schema_version,
                object_hash=decision_object_hash,
                input_hashes=decision.frozen_input_hashes,
            )

        protocol = self._trade_protocol(decision, assessment, bundle.skill_versions)
        protocol_bytes = canonical_json_bytes(protocol.model_dump(mode="json"))
        protocol_object_hash = sha256_bytes(protocol_bytes)
        protocol_input_hash = content_hash(
            {
                "decision_sha256": decision.decision_sha256,
                "protocol_draft": assessment.protocol,
                "rules_version": rules.rules_version,
                "execution_contract": _TRADE_PROTOCOL_CONTRACT_VERSION,
            }
        )
        task_object_hashes: dict[str, str] = {}
        if persist:
            self.object_store.put_bytes(protocol_bytes)
            self.repository.register_protocol(
                protocol,
                object_hash=protocol_object_hash,
                input_hash=protocol_input_hash,
            )
            self.state.register_artifact(
                artifact_id=f"TradeProtocol:{protocol.protocol_id}",
                artifact_type="TradeProtocol",
                schema_version=protocol.schema_version,
                object_hash=protocol_object_hash,
                input_hashes=[decision_object_hash],
            )
        for task in tasks:
            task_bytes = canonical_json_bytes(task.model_dump(mode="json"))
            task_object_hash = sha256_bytes(task_bytes)
            task_object_hashes[task.task_id] = task_object_hash
            if persist:
                self.object_store.put_bytes(task_bytes)
                task_input_hash = content_hash(
                    {
                        "decision_sha256": decision.decision_sha256,
                        "reason_code": task.reason_code,
                    }
                )
                self.repository.register_task(
                    task,
                    object_hash=task_object_hash,
                    input_hash=task_input_hash,
                )
                self.state.register_artifact(
                    artifact_id=f"CommitteeInvestigationTask:{task.task_id}",
                    artifact_type="CommitteeInvestigationTask",
                    schema_version=task.schema_version,
                    object_hash=task_object_hash,
                    input_hashes=[decision_object_hash],
                )
        return _Prepared(
            request_hash=request_hash,
            assessment=assessment,
            assessment_object_hash=assessment_object_hash,
            rules=rules,
            rules_object_hash=rules_object_hash,
            loaded=loaded,
            triggers=triggers,
            counter_case=counter_case,
            counter_case_object_hash=counter_case_object_hash,
            bundle=bundle,
            bundle_object_hash=bundle_object_hash,
            decision=decision,
            decision_object_hash=decision_object_hash,
            protocol=protocol,
            protocol_object_hash=protocol_object_hash,
            tasks=tasks,
            task_object_hashes=task_object_hashes,
        )

    def _rules_reference(
        self,
        rules: CommitteeRuleConfig,
        *,
        persist: bool,
    ) -> tuple[CommitteeRuleConfig, str]:
        config_hash = content_hash(rules)
        payload = canonical_json_bytes(rules.model_dump(mode="json"))
        object_hash = sha256_bytes(payload)
        summary = self.repository.rule_summary(rules.rules_version)
        if summary is not None:
            if (
                str(summary["config_hash"]) != config_hash
                or str(summary["object_hash"]) != object_hash
                or not self.object_store.verify(object_hash)
            ):
                raise ValueError("committee rules changed or were damaged without a version bump")
            stored = self.repository.get_rules(rules.rules_version)
            assert stored is not None
            return stored, object_hash
        if persist:
            self.object_store.put_bytes(payload)
            self.repository.register_rules(
                rules,
                object_hash=object_hash,
                config_hash=config_hash,
            )
            self.state.register_artifact(
                artifact_id=f"CommitteeRuleConfig:{rules.rules_version}",
                artifact_type="CommitteeRuleConfig",
                schema_version=rules.schema_version,
                object_hash=object_hash,
                input_hashes=[config_hash],
            )
        return rules, object_hash

    def _load_and_validate_initial_inputs(
        self,
        request: CommitteeDecisionRequest,
        rules: CommitteeRuleConfig,
    ) -> _LoadedInputs:
        by_type: dict[str, list[AStockModel]] = {}
        for reference in request.artifact_references:
            model_type = _INITIAL_ARTIFACT_MODELS.get(reference.artifact_type)
            if model_type is None:
                raise ValueError(
                    f"unsupported frozen committee input type: {reference.artifact_type}"
                )
            if reference.role is not _EXPECTED_ROLES[reference.artifact_type]:
                raise ValueError(f"committee artifact role mismatch: {reference.artifact_id}")
            if not self._reference_matches_registry(reference):
                raise ValueError(
                    f"registered frozen committee input mismatch: {reference.artifact_id}"
                )
            model = model_type.model_validate_json(
                self.object_store.get_bytes(reference.object_sha256)
            )
            artifact_as_of = getattr(model, "as_of", None)
            if artifact_as_of is not None and artifact_as_of > request.assessment.as_of:
                raise ValueError("committee input as_of cannot be later than the decision as_of")
            by_type.setdefault(reference.artifact_type, []).append(model)

        for required in (
            "FrozenEvidencePack",
            "BaseCasePack",
            "SpecialistRoutePlan",
            "ResearchMemoArtifact",
        ):
            if len(by_type.get(required, [])) != 1:
                raise ValueError(f"committee requires exactly one {required}")
        for singular in (
            "FinancialIntegrityEvidencePack",
            "KnowledgeSkillDelta",
            "PositionMonitoringPlan",
            "HoldingEvidenceUpdate",
            "HoldingReviewPack",
            "PositionActionProposal",
        ):
            if len(by_type.get(singular, [])) > 1:
                raise ValueError(f"committee accepts at most one {singular}")

        evidence_pack = cast(FrozenEvidencePack, by_type["FrozenEvidencePack"][0])
        base = cast(BaseCasePack, by_type["BaseCasePack"][0])
        route = cast(SpecialistRoutePlan, by_type["SpecialistRoutePlan"][0])
        memo = cast(ResearchMemoArtifact, by_type["ResearchMemoArtifact"][0])
        if (
            evidence_pack.company_id != request.assessment.company_id
            or base.company_id != request.assessment.company_id
            or memo.company_id != request.assessment.company_id
            or base.evidence_pack_id != evidence_pack.pack_id
            or route.evidence_pack_id != evidence_pack.pack_id
            or route.base_case_id != base.base_case_id
            or memo.base_case_id != base.base_case_id
            or memo.route_plan_id != route.route_plan_id
            or base.as_of != evidence_pack.as_of
            or memo.as_of != base.as_of
        ):
            raise ValueError("committee research lineage/company/as_of mismatch")
        knowledge_models = by_type.get("KnowledgeSkillDelta", [])
        if knowledge_models:
            knowledge_delta = cast(KnowledgeSkillDelta, knowledge_models[0])
            if (
                knowledge_delta.company_id != request.assessment.company_id
                or knowledge_delta.as_of != request.assessment.as_of
            ):
                raise ValueError("committee KnowledgeSkillDelta company/as_of mismatch")

        deltas = [cast(SpecialistDelta, item) for item in by_type.get("SpecialistDelta", [])]
        expected_delta_ids = sorted(item.delta_id for item in memo.delta_references)
        actual_delta_ids = sorted(item.delta_id for item in deltas)
        if expected_delta_ids != actual_delta_ids:
            raise ValueError("committee must freeze every SpecialistDelta referenced by the memo")
        route_skill_versions = {item.skill_id: item.skill_version for item in route.selected}
        for delta in deltas:
            if (
                delta.base_case_id != base.base_case_id
                or delta.evidence_pack_id != evidence_pack.pack_id
                or delta.route_plan_id != route.route_plan_id
                or route_skill_versions.get(delta.skill_id) != delta.skill_version
            ):
                raise ValueError("committee SpecialistDelta lineage mismatch")
        if request.member_bindings:
            self._validate_investment_members(request, by_type, deltas)
        delta_by_id = {item.delta_id: item for item in deltas}
        for diagnostic_model in by_type.get("SpecialistDiagnosticReport", []):
            diagnostic = cast(SpecialistDiagnosticReport, diagnostic_model)
            delta = delta_by_id.get(diagnostic.delta_id)
            if (
                delta is None
                or diagnostic.base_case_id != base.base_case_id
                or diagnostic.route_plan_id != route.route_plan_id
                or diagnostic.skill_id != delta.skill_id
                or diagnostic.skill_version != delta.skill_version
            ):
                raise ValueError("committee diagnostic lineage mismatch")

        financial_models = by_type.get("FinancialIntegrityEvidencePack", [])
        if financial_models:
            financial = cast(FinancialIntegrityEvidencePack, financial_models[0])
            if (
                financial.company_id != request.assessment.company_id
                or financial.as_of > request.assessment.as_of
            ):
                raise ValueError("committee financial pack company/as_of mismatch")

        plans = by_type.get("PositionMonitoringPlan", [])
        updates = by_type.get("HoldingEvidenceUpdate", [])
        reviews = by_type.get("HoldingReviewPack", [])
        proposals = by_type.get("PositionActionProposal", [])
        if request.assessment.scope is CommitteeDecisionScope.NEW_CANDIDATE:
            if any((plans, updates, reviews, proposals)):
                raise ValueError("new-candidate committees cannot consume holding artifacts")
        else:
            if not all(len(items) == 1 for items in (plans, updates, reviews, proposals)):
                raise ValueError(
                    "position committees require plan, incremental update, review, and proposal"
                )
            plan = cast(PositionMonitoringPlan, plans[0])
            update = cast(HoldingEvidenceUpdate, updates[0])
            review = cast(HoldingReviewPack, reviews[0])
            proposal = cast(PositionActionProposal, proposals[0])
            if (
                plan.company_id != request.assessment.company_id
                or plan.evidence_snapshot_id != evidence_pack.pack_id
                or plan.position_id != update.position_id
                or plan.position_id != review.position_id
                or plan.position_id != proposal.position_id
                or update.plan_id != plan.plan_id
                or review.plan_id != plan.plan_id
                or proposal.plan_id != plan.plan_id
                or review.evidence_update_id != update.update_id
                or proposal.review_id != review.review_id
                or review.proposal_id != proposal.proposal_id
                or review.as_of > request.assessment.as_of
            ):
                raise ValueError("committee holding lifecycle lineage mismatch")

        evidence_ids: set[str] = set()
        for models in by_type.values():
            for model in models:
                evidence_ids.update(_collect_evidence_ids(model.model_dump(mode="python")))
        assessment_evidence = _assessment_evidence_ids(request.assessment)
        if not assessment_evidence.issubset(evidence_ids):
            raise ValueError("committee assessment cites evidence outside the frozen artifact set")
        if request.assessment.protocol.evidence_snapshot_id != evidence_pack.pack_id:
            raise ValueError("trade protocol must reference the frozen EvidencePack")

        skill_versions = dict(route_skill_versions)
        skill_versions["ResearchMemoComposer"] = (
            memo.composer_version or "research-memo-composer-v1"
        )
        if plans:
            plan = cast(PositionMonitoringPlan, plans[0])
            for skill_id, version in plan.skill_versions.items():
                existing = skill_versions.get(skill_id)
                if existing is not None and existing != version:
                    raise ValueError("holding plan Skill version conflicts with research lineage")
                skill_versions[skill_id] = version
            if plan.rules_version:
                skill_versions["PositionLifecycleRules"] = plan.rules_version
        if request.assessment.protocol.skill_versions != skill_versions:
            raise ValueError(
                "trade protocol Skill versions must exactly match the frozen approved lineage"
            )
        if rules.engine_version == "":  # defensive; schema already rejects this
            raise ValueError("committee engine version is unavailable")
        return _LoadedInputs(
            by_type=by_type,
            evidence_ids=evidence_ids,
            claim_ids=set(evidence_pack.claim_ids),
            skill_versions=dict(sorted(skill_versions.items())),
        )

    def _counter_case_triggers(
        self,
        assessment: CommitteeAssessmentSnapshot,
        loaded: _LoadedInputs,
        rules: CommitteeRuleConfig,
    ) -> list[CounterCaseTriggerCode]:
        triggers: set[CounterCaseTriggerCode] = set()
        if assessment.requested_position > rules.high_position_threshold:
            triggers.add(CounterCaseTriggerCode.HIGH_PLANNED_POSITION)
        if assessment.base_specialist_conflict:
            triggers.add(CounterCaseTriggerCode.BASE_SPECIALIST_CONFLICT)
        if assessment.multiple_specialist_disagreement:
            triggers.add(CounterCaseTriggerCode.SPECIALIST_DISAGREEMENT)
        if assessment.material_new_disclosure:
            triggers.add(CounterCaseTriggerCode.MATERIAL_NEW_DISCLOSURE)
        if assessment.invalidation_near_trigger:
            triggers.add(CounterCaseTriggerCode.INVALIDATION_NEAR)
        if assessment.portfolio_risk_changed:
            triggers.add(CounterCaseTriggerCode.PORTFOLIO_RISK_CHANGE)
        coverage = assessment.coverage
        if (
            coverage.data_coverage < rules.min_data_coverage
            or coverage.evidence_coverage < rules.min_evidence_coverage
            or coverage.specialist_coverage < rules.min_specialist_coverage
            or coverage.pit_coverage < rules.min_pit_coverage
        ):
            triggers.add(CounterCaseTriggerCode.LOW_COVERAGE_DOMAIN)
        if (
            assessment.expected_return_range.lower >= rules.high_potential_return_lower
            and coverage.evidence_coverage
            < min(Decimal("1"), rules.min_evidence_coverage + rules.low_coverage_margin)
        ):
            triggers.add(CounterCaseTriggerCode.HIGH_RETURN_LOW_COVERAGE)
        financial_models = loaded.by_type.get("FinancialIntegrityEvidencePack", [])
        if financial_models:
            financial = cast(FinancialIntegrityEvidencePack, financial_models[0])
            if (
                financial.risk_level is FinancialRiskLevel.HIGH
                or financial.time_series_anomalies
                or financial.peer_anomalies
            ):
                triggers.add(CounterCaseTriggerCode.FINANCIAL_ANOMALY)
        return sorted(triggers, key=lambda item: item.value)

    def _build_counter_case(
        self,
        draft: CounterCaseDraft | None,
        assessment: CommitteeAssessmentSnapshot,
        triggers: list[CounterCaseTriggerCode],
        loaded: _LoadedInputs,
        frozen_input_hashes: list[str],
    ) -> CounterCasePack | None:
        if draft is None:
            return None
        if not set(draft.challenged_claim_ids).issubset(loaded.claim_ids):
            raise ValueError("counter-case challenges claims outside the frozen EvidencePack")
        if not set(draft.evidence_ids).issubset(loaded.evidence_ids):
            raise ValueError("counter-case cites evidence outside the frozen inputs")
        identity = {
            "draft": draft,
            "company_id": assessment.company_id,
            "scope": assessment.scope.value,
            "as_of": assessment.as_of,
            "triggers": [item.value for item in triggers],
            "frozen_input_hashes": sorted(frozen_input_hashes),
        }
        input_hash = content_hash(identity)
        return CounterCasePack(
            **draft.model_dump(
                mode="python",
                exclude={"schema_version", "created_at"},
            ),
            schema_version=draft.schema_version,
            counter_case_id=f"counter-case:{input_hash}",
            company_id=assessment.company_id,
            scope=assessment.scope,
            as_of=assessment.as_of,
            trigger_codes=triggers,
            frozen_input_hashes=sorted(frozen_input_hashes),
            input_sha256=input_hash,
            created_at=assessment.as_of,
        )

    def _verdict(
        self,
        assessment: CommitteeAssessmentSnapshot,
        loaded: _LoadedInputs,
        rules: CommitteeRuleConfig,
        triggers: list[CounterCaseTriggerCode],
        counter_case: CounterCasePack | None,
    ) -> tuple[CommitteeVerdict, list[str], list[str]]:
        reject: set[str] = set()
        needs_info: set[str] = set()
        reasons: set[str] = set()
        if not assessment.tradable:
            reject.add("NOT_TRADABLE")
        if assessment.explicitly_prohibited:
            reject.add("EXPLICITLY_PROHIBITED")
        if assessment.qualified_or_adverse_audit_risk:
            reject.add("AUDIT_OPINION_MAJOR_RISK")
        if assessment.manual_emergency_stop:
            reject.add("MANUAL_EMERGENCY_STOP")
        if assessment.leverage_requested:
            reject.add("LEVERAGE_PROHIBITED")
        if assessment.requested_position > rules.max_single_position:
            reject.add("POSITION_CAP_EXCEEDED")
        portfolio = assessment.portfolio_risk
        if portfolio.post_decision_total_exposure > rules.max_total_exposure:
            reject.add("TOTAL_EXPOSURE_LIMIT_EXCEEDED")
        if portfolio.post_decision_industry_exposure > rules.max_industry_exposure:
            reject.add("INDUSTRY_EXPOSURE_LIMIT_EXCEEDED")
        if portfolio.max_abs_correlation > rules.max_abs_correlation:
            reject.add("PORTFOLIO_CORRELATION_LIMIT_EXCEEDED")
        if portfolio.portfolio_drawdown >= rules.max_portfolio_drawdown:
            reject.add("PORTFOLIO_DRAWDOWN_FREEZE")
        if portfolio.consecutive_loss_count >= rules.max_consecutive_losses:
            reject.add("CONSECUTIVE_LOSS_FREEZE")
        if (
            assessment.scope is CommitteeDecisionScope.NEW_CANDIDATE
            and assessment.thesis_invalidated
        ):
            reject.add("THESIS_INVALIDATED")
        if not assessment.market_data_quality_pass:
            needs_info.add("DATA_QUALITY_FAILED")
        if portfolio.material_announcement_freeze:
            needs_info.add("MATERIAL_ANNOUNCEMENT_FREEZE")
        if portfolio.data_anomaly_freeze:
            needs_info.add("DATA_ANOMALY_FREEZE")
        coverage = assessment.coverage
        for value, threshold, code in (
            (coverage.data_coverage, rules.min_data_coverage, "DATA_COVERAGE_INSUFFICIENT"),
            (
                coverage.evidence_coverage,
                rules.min_evidence_coverage,
                "EVIDENCE_COVERAGE_INSUFFICIENT",
            ),
            (
                coverage.specialist_coverage,
                rules.min_specialist_coverage,
                "SPECIALIST_COVERAGE_INSUFFICIENT",
            ),
            (coverage.pit_coverage, rules.min_pit_coverage, "PIT_COVERAGE_INSUFFICIENT"),
            (coverage.liquidity_score, rules.min_liquidity_score, "LIQUIDITY_INSUFFICIENT"),
        ):
            if value < threshold:
                needs_info.add(code)
        if assessment.key_fact_community_only:
            needs_info.add("COMMUNITY_ONLY_KEY_FACT")

        evidence_pack = cast(FrozenEvidencePack, loaded.by_type["FrozenEvidencePack"][0])
        base = cast(BaseCasePack, loaded.by_type["BaseCasePack"][0])
        memo = cast(ResearchMemoArtifact, loaded.by_type["ResearchMemoArtifact"][0])
        if evidence_pack.open_conflict_ids:
            needs_info.add("CORE_SOURCE_CONFLICT")
        if evidence_pack.coverage_status is not ResearchCoverageStatus.COMPLETE:
            needs_info.add("FROZEN_EVIDENCE_INCOMPLETE")
        if any(gap.severity is ResearchGapSeverity.BLOCKING for gap in base.evidence_gaps):
            needs_info.add("BLOCKING_RESEARCH_GAP")
        if base.coverage_status is ResearchCoverageStatus.INSUFFICIENT:
            needs_info.add("BASE_CASE_INSUFFICIENT")
        if (
            memo.coverage_status is SpecialistCoverageStatus.INSUFFICIENT
            or memo.missing_selected_skill_ids
        ):
            needs_info.add("SPECIALIST_INPUT_INCOMPLETE")

        financial_models = loaded.by_type.get("FinancialIntegrityEvidencePack", [])
        if rules.financial_integrity_required and not financial_models:
            needs_info.add("FINANCIAL_INTEGRITY_NOT_RUN")
        if financial_models:
            financial = cast(FinancialIntegrityEvidencePack, financial_models[0])
            severe_findings = {
                finding.rule_id
                for finding in [*financial.rule_findings, *financial.governance_findings]
                if finding.status is FinancialFindingStatus.FLAG
                and finding.severity is FinancialSeverity.HIGH
                and finding.rule_id in rules.financial_reject_rule_ids
            }
            if severe_findings:
                reject.add("FINANCIAL_INTEGRITY_SEVERE")
            if (
                financial.coverage_status is FinancialCoverageStatus.BLOCKED
                or financial.evidence_gaps
            ):
                needs_info.add("FINANCIAL_EVIDENCE_GAP")
        if triggers and counter_case is None:
            needs_info.add("COUNTER_CASE_REQUIRED")
        if counter_case is not None and counter_case.missing_evidence_codes:
            needs_info.update(
                f"COUNTER_CASE_GAP:{code}" for code in counter_case.missing_evidence_codes
            )

        if reject:
            reasons.update(reject)
            return CommitteeVerdict.REJECT, sorted(reject), sorted(reasons)
        if needs_info:
            reasons.update(needs_info)
            return CommitteeVerdict.NEEDS_INFO, sorted(needs_info), sorted(reasons)

        proposals = loaded.by_type.get("PositionActionProposal", [])
        proposal_action = cast(PositionActionProposal, proposals[0]).action if proposals else None
        if assessment.scope is not CommitteeDecisionScope.NEW_CANDIDATE:
            if assessment.thesis_invalidated or proposal_action is PositionAction.EXIT:
                reasons.add("HOLDING_EXIT_TRIGGER")
                return CommitteeVerdict.PAPER_EXIT, [], sorted(reasons)
            if proposal_action in {PositionAction.REVIEW, PositionAction.TRIM}:
                reasons.add(f"HOLDING_ACTION:{cast(PositionAction, proposal_action).value}")
                return CommitteeVerdict.WATCH, [], sorted(reasons)
            reasons.add("HOLDING_CONTINUES")
            return CommitteeVerdict.PAPER_HOLD, [], sorted(reasons)

        if (
            assessment.expected_return_range.lower >= rules.min_expected_return_lower
            and abs(assessment.downside_range.lower) <= rules.max_downside_absolute
        ):
            reasons.add("RISK_RETURN_THRESHOLD_PASSED")
            return CommitteeVerdict.PAPER_ELIGIBLE, [], sorted(reasons)
        reasons.add("RISK_RETURN_THRESHOLD_NOT_MET")
        return CommitteeVerdict.WATCH, [], sorted(reasons)

    def _budget(
        self,
        bundle: CommitteeInputBundle,
        assessment: CommitteeAssessmentSnapshot,
        generated_sizes: dict[str, int],
        rules: CommitteeRuleConfig,
    ) -> CommitteeBudgetReport:
        total_bytes = 0
        for reference in bundle.artifact_references:
            size = generated_sizes.get(reference.object_sha256)
            if size is None:
                size = len(self.object_store.get_bytes(reference.object_sha256))
            total_bytes += size
        estimated_tokens = (total_bytes + 3) // 4
        within_limit = (
            total_bytes <= rules.max_context_bytes
            and estimated_tokens <= rules.max_estimated_text_tokens
        )
        degradation: set[str] = set()
        if not assessment.optional_narrative_requested:
            mode = CommitteeNarrativeMode.DISABLED
        elif not within_limit:
            mode = CommitteeNarrativeMode.BUDGET_EXCEEDED
            degradation.add("OPTIONAL_NARRATIVE_DISABLED_BY_CONTEXT_BUDGET")
        elif assessment.estimated_provider_cost_cny > 0 and not rules.provider_enabled:
            mode = CommitteeNarrativeMode.DETERMINISTIC_ONLY
            degradation.add("OPTIONAL_PROVIDER_DISABLED")
        elif assessment.estimated_provider_cost_cny > rules.provider_cost_ceiling_cny:
            mode = CommitteeNarrativeMode.PROVIDER_COST_EXCEEDED
            degradation.add("OPTIONAL_PROVIDER_COST_CEILING_EXCEEDED")
        else:
            mode = CommitteeNarrativeMode.CODEX_FROZEN_INPUT_ONLY
        context = ContextBudgetReport(
            selected_skills=sorted(bundle.skill_versions),
            selected_artifacts=[item.artifact_id for item in bundle.artifact_references],
            artifact_byte_size=total_bytes,
            estimated_text_tokens=estimated_tokens,
            full_documents_to_open=[],
            evidence_excerpts_to_open=[],
            expected_browser_steps=0,
            expected_mcp_calls=0,
            expected_api_calls=0,
            duplicate_inputs_avoided=[],
            created_at=assessment.as_of,
        )
        return CommitteeBudgetReport(
            context=context,
            within_limit=within_limit,
            narrative_mode=mode,
            provider_estimated_cost_cny=assessment.estimated_provider_cost_cny,
            provider_cost_ceiling_cny=rules.provider_cost_ceiling_cny,
            degradation_codes=sorted(degradation),
            created_at=assessment.as_of,
        )

    def _max_position(
        self,
        verdict: CommitteeVerdict,
        assessment: CommitteeAssessmentSnapshot,
        rules: CommitteeRuleConfig,
    ) -> Decimal:
        if verdict in {CommitteeVerdict.PAPER_ELIGIBLE, CommitteeVerdict.PAPER_HOLD}:
            return min(
                rules.max_single_position,
                max(assessment.current_position, assessment.requested_position),
            )
        if verdict is CommitteeVerdict.WATCH and (
            assessment.scope is not CommitteeDecisionScope.NEW_CANDIDATE
        ):
            return assessment.current_position
        if verdict is CommitteeVerdict.NEEDS_INFO and (
            assessment.scope is not CommitteeDecisionScope.NEW_CANDIDATE
        ):
            return assessment.current_position
        return Decimal("0")

    def _investigation_task(
        self,
        *,
        decision_id: str,
        bundle_id: str,
        company_id: str,
        reason_code: str,
        as_of: Any,
    ) -> CommitteeInvestigationTask:
        identity = {
            "decision_id": decision_id,
            "bundle_id": bundle_id,
            "reason_code": reason_code,
        }
        task_id = f"committee-task:{content_hash(identity)}"
        return CommitteeInvestigationTask(
            task_id=task_id,
            decision_id=decision_id,
            bundle_id=bundle_id,
            reason_code=reason_code,
            expected_minutes=30,
            sources=["OFFICIAL_DISCLOSURE_OR_VERIFIED_LOCAL_DATA"],
            search_terms=sorted([company_id, f"reason:{reason_code}"]),
            steps=sorted(
                [
                    "COLLECT_ONLY_THE_NAMED_MISSING_EVIDENCE",
                    "FREEZE_AND_REGISTER_THE_NEW_EVIDENCE",
                    "RERUN_COMMITTEE_WITH_A_NEW_INPUT_BUNDLE",
                ]
            ),
            required_materials=[f"EVIDENCE_FOR:{reason_code}"],
            support_signal=f"RESOLVES:{reason_code}",
            refute_signal=f"CONFIRMS_BLOCK:{reason_code}",
            stop_condition="ONE_FROZEN_RESOLUTION_ARTIFACT_OR_CONFIRMED_UNAVAILABLE",
            fallback_evidence=[],
            created_at=as_of,
        )

    def _trade_protocol(
        self,
        decision: DecisionPack,
        assessment: CommitteeAssessmentSnapshot,
        skill_versions: dict[str, str],
    ) -> TradeProtocol:
        draft = assessment.protocol
        active = decision.verdict in {
            CommitteeVerdict.PAPER_ELIGIBLE,
            CommitteeVerdict.PAPER_EXIT,
        }
        execution_enabled = decision.verdict in {
            CommitteeVerdict.PAPER_ELIGIBLE,
            CommitteeVerdict.PAPER_EXIT,
        }
        blocking_codes = (
            [] if active else sorted(decision.hard_blocks or [f"VERDICT_{decision.verdict.value}"])
        )
        identity = {
            "decision_sha256": decision.decision_sha256,
            "protocol_draft": draft,
            "execution_contract": _TRADE_PROTOCOL_CONTRACT_VERSION,
            "status": "ACTIVE" if active else "BLOCKED",
            "blocking_codes": blocking_codes,
        }
        protocol_id = f"trade-protocol:{content_hash(identity)}"
        return TradeProtocol(
            protocol_id=protocol_id,
            decision_id=decision.decision_id,
            decision_sha256=decision.decision_sha256,
            company_id=decision.company_id,
            verdict=decision.verdict,
            protocol_status=(
                CommitteeProtocolStatus.ACTIVE if active else CommitteeProtocolStatus.BLOCKED
            ),
            blocking_codes=blocking_codes,
            strategy_id=draft.strategy_id,
            skill_versions=skill_versions,
            signal_time=assessment.as_of,
            earliest_executable_time=draft.earliest_executable_time,
            holding_horizon_days=assessment.holding_horizon_days,
            entry_rule=draft.entry_rule,
            entry_order_type=draft.entry_order_type,
            position_size_rule=draft.position_size_rule,
            price_stop_rule=draft.price_stop_rule,
            volatility_stop_rule=draft.volatility_stop_rule,
            trailing_stop_rule=draft.trailing_stop_rule,
            time_stop_rule=draft.time_stop_rule,
            thesis_invalidation_rule=draft.thesis_invalidation_rule,
            take_profit_rule=draft.take_profit_rule,
            review_events=draft.review_events,
            max_holding_period_days=draft.max_holding_period_days,
            cost_model_version=draft.cost_model_version,
            fill_model_version=draft.fill_model_version,
            evidence_snapshot_id=draft.evidence_snapshot_id,
            evidence_ids=draft.evidence_ids,
            effective_from=draft.earliest_executable_time,
            broker_execution_allowed=False,
            paper_simulation_allowed=execution_enabled,
            ledger_write_allowed=execution_enabled,
            created_at=assessment.as_of,
        )

    def _reconstruct_request(
        self,
        bundle: CommitteeInputBundle,
        assessment: CommitteeAssessmentSnapshot,
    ) -> CommitteeDecisionRequest:
        initial_references = [
            item
            for item in bundle.artifact_references
            if item.artifact_type not in _GENERATED_ARTIFACT_TYPES
        ]
        counter_case_ref = next(
            (
                item
                for item in bundle.artifact_references
                if item.artifact_type == "CounterCasePack"
            ),
            None,
        )
        counter_case_draft = None
        if counter_case_ref is not None:
            counter_case_id = counter_case_ref.artifact_id.removeprefix("CounterCasePack:")
            pack = self.repository.get_counter_case(counter_case_id)
            if pack is None:
                raise ValueError("counter-case object is unavailable")
            counter_case_draft = CounterCaseDraft(
                **pack.model_dump(
                    mode="python",
                    include={
                        "challenged_claim_ids",
                        "alternative_explanations",
                        "downside_paths",
                        "missing_evidence_codes",
                        "evidence_ids",
                        "estimated_tokens",
                        "estimated_minutes",
                        "estimated_cost_cny",
                    },
                ),
                created_at=assessment.as_of,
            )
        assessment_payload = assessment.model_dump(
            mode="python",
            exclude={
                "assessment_id",
                "request_sha256",
                "schema_version",
                "created_at",
            },
        )
        reconstructed_assessment = CommitteeAssessment(
            **assessment_payload,
            schema_version=assessment.schema_version,
            created_at=assessment.as_of,
        )
        return CommitteeDecisionRequest(
            artifact_references=initial_references,
            member_bindings=bundle.member_bindings,
            assessment=reconstructed_assessment,
            counter_case=counter_case_draft,
            access_policy=CommitteeAccessPolicy(
                frozen_artifact_hashes=sorted(item.object_sha256 for item in initial_references),
                created_at=assessment.as_of,
            ),
            created_at=assessment.as_of,
        )

    @staticmethod
    def _validate_investment_members(
        request: CommitteeDecisionRequest,
        by_type: dict[str, list[AStockModel]],
        deltas: list[SpecialistDelta],
    ) -> None:
        binding_by_role = {item.role: item for item in request.member_bindings}
        reference_by_id = {item.artifact_id: item for item in request.artifact_references}
        expected_type_by_role = {
            CommitteeMemberRole.BASE_CASE: "BaseCasePack",
            CommitteeMemberRole.SERENITY_DELTA: "SpecialistDelta",
            CommitteeMemberRole.ZHIHU_EXPERT_DELTA: "SpecialistDelta",
            CommitteeMemberRole.FINANCIAL_INTEGRITY: "FinancialIntegrityEvidencePack",
        }
        for role, expected_type in expected_type_by_role.items():
            binding = binding_by_role[role]
            reference = reference_by_id[binding.artifact_id]
            if reference.artifact_type != expected_type:
                raise ValueError(f"committee member {role.value} has the wrong artifact type")

        delta_by_artifact_id = {f"SpecialistDelta:{item.delta_id}": item for item in deltas}
        serenity = delta_by_artifact_id.get(
            binding_by_role[CommitteeMemberRole.SERENITY_DELTA].artifact_id
        )
        zhihu = delta_by_artifact_id.get(
            binding_by_role[CommitteeMemberRole.ZHIHU_EXPERT_DELTA].artifact_id
        )
        if serenity is None or serenity.skill_id not in _SERENITY_SKILL_IDS:
            raise ValueError("SERENITY_DELTA is not produced by an approved Serenity Skill")
        if zhihu is None or zhihu.skill_id not in _ZHIHU_EXPERT_SKILL_IDS:
            raise ValueError("ZHIHU_EXPERT_DELTA is not produced by an approved Zhihu Skill")
        if not by_type.get("FinancialIntegrityEvidencePack"):
            raise ValueError("investment committee requires Financial Integrity")

    def _reference_matches_registry(self, reference: CommitteeArtifactReference) -> bool:
        row = self._registry_row(reference.artifact_id)
        return bool(
            row is not None
            and str(row["type"]) == reference.artifact_type
            and str(row["object_hash"]) == reference.object_sha256
            and self.object_store.verify(reference.object_sha256)
        )

    def _artifact_matches(
        self,
        artifact_id: str,
        artifact_type: str,
        object_hash: str,
    ) -> bool:
        row = self._registry_row(artifact_id)
        return bool(
            row is not None
            and str(row["type"]) == artifact_type
            and str(row["object_hash"]) == object_hash
            and self.object_store.verify(object_hash)
        )

    def _registry_row(self, artifact_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT artifact_id,type,schema_version,object_hash,input_hashes_json "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return dict(row) if row else None


def _collect_evidence_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).endswith("evidence_ids") and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            found.update(_collect_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_evidence_ids(child))
    return found


def _assessment_evidence_ids(assessment: CommitteeAssessment) -> set[str]:
    return (
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


def _replace_created_at(value: object, created_at: object) -> object:
    if isinstance(value, dict):
        return {
            key: (created_at if key == "created_at" else _replace_created_at(child, created_at))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_created_at(child, created_at) for child in value]
    return value


__all__ = ["CommitteeExecution", "CommitteeService"]
