"""Deterministic private excerpt drafts and unevaluated Skill candidates."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from astock.core.errors import StorageError
from astock.core.hashing import content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.distillation_repository import DistillationRepository
from astock.knowledge.draft_repository import KnowledgeDraftRepository
from astock.schemas import (
    AuthorDraftGenerationReport,
    BookApprovalStatus,
    BookContentClass,
    BookEvaluationStatus,
    BookMethodCategory,
    BookSkillTarget,
    DistillationDecision,
    DistillationUnit,
    HumanReviewStatus,
    PrivateSkillCandidateDraft,
    PrivateSkillCandidatePayload,
    PrivateViewpointDraft,
    PrivateViewpointDraftPayload,
    ViewpointDraftDerivation,
)

_GENERATION_RULE_VERSION = "private-excerpt-draft-v1"
_MAX_VIEWPOINTS_PER_METHOD = 12
_TARGET_BY_METHOD = {
    BookMethodCategory.STOCK_SELECTION: BookSkillTarget.CANDIDATE_SELECTION,
    BookMethodCategory.BUSINESS_MODEL: BookSkillTarget.CANDIDATE_SELECTION,
    BookMethodCategory.INDUSTRY: BookSkillTarget.CANDIDATE_SELECTION,
    BookMethodCategory.VALUATION: BookSkillTarget.CANDIDATE_SELECTION,
    BookMethodCategory.FINANCIAL_QUALITY: BookSkillTarget.CANDIDATE_SELECTION,
    BookMethodCategory.ENTRY: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.HOLDING: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.ADD: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.TRIM: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.EXIT: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.RISK: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.FAILURE_CASE: BookSkillTarget.POSITION_LIFECYCLE,
    BookMethodCategory.COUNTEREVIDENCE_INVALIDATION: (
        BookSkillTarget.POSITION_LIFECYCLE
    ),
    BookMethodCategory.REVIEW: BookSkillTarget.POSITION_LIFECYCLE,
}
_CONTENT_CLASS_BY_METHOD = {
    BookMethodCategory.STOCK_SELECTION: BookContentClass.STOCK_SELECTION,
    BookMethodCategory.BUSINESS_MODEL: BookContentClass.BUSINESS_MODEL,
    BookMethodCategory.INDUSTRY: BookContentClass.INDUSTRY,
    BookMethodCategory.VALUATION: BookContentClass.VALUATION,
    BookMethodCategory.FINANCIAL_QUALITY: BookContentClass.FINANCIAL_QUALITY,
    BookMethodCategory.ENTRY: BookContentClass.ENTRY,
    BookMethodCategory.HOLDING: BookContentClass.HOLDING_VALIDATION,
    BookMethodCategory.ADD: BookContentClass.ADD,
    BookMethodCategory.TRIM: BookContentClass.TRIM,
    BookMethodCategory.EXIT: BookContentClass.EXIT,
    BookMethodCategory.RISK: BookContentClass.RISK_CONTROL,
    BookMethodCategory.FAILURE_CASE: BookContentClass.FAILURE_CASE,
    BookMethodCategory.COUNTEREVIDENCE_INVALIDATION: (
        BookContentClass.COUNTEREVIDENCE_INVALIDATION
    ),
    BookMethodCategory.REVIEW: BookContentClass.REVIEW_METHOD,
}
_VIEWPOINT_GAPS = (
    "APPLICABILITY_SCOPE_NOT_DERIVED",
    "COUNTEREVIDENCE_NOT_DERIVED",
    "FAILURE_CONDITIONS_NOT_DERIVED",
    "PROPOSITION_NOT_SYNTHESIZED",
)
_SKILL_GAPS = (
    "EVALUATION_NOT_RUN",
    "FORMAL_RULE_NOT_DERIVED",
    "HUMAN_REVIEW_REQUIRED",
)


@dataclass(frozen=True, slots=True)
class KnowledgeDraftExecution:
    report: AuthorDraftGenerationReport
    viewpoint_drafts: tuple[PrivateViewpointDraft, ...]
    skill_candidates: tuple[PrivateSkillCandidateDraft, ...]


class KnowledgeDraftService:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store
        self.distillation_repository = DistillationRepository(state)
        self.repository = KnowledgeDraftRepository(state)

    def generate(self, author_source_id: str) -> KnowledgeDraftExecution:
        source_report = self.distillation_repository.latest_author_report(author_source_id)
        if source_report is None:
            raise ValueError(f"distillation has not run for {author_source_id}")
        run = self.distillation_repository.get_run(source_report.run_id)
        if run is None or run.finished_at is None:
            raise ValueError(f"completed distillation run is unavailable: {source_report.run_id}")
        units = self.distillation_repository.units_for_run(run.run_id)
        keep_units = [
            unit
            for unit in units
            if unit.decision is DistillationDecision.KEEP_CANDIDATE
            and unit.duplicate_of_unit_id is None
        ]
        eligible_units = [unit for unit in keep_units if unit.method_categories]
        grouped: dict[BookMethodCategory, list[DistillationUnit]] = defaultdict(list)
        for unit in eligible_units:
            for method in unit.method_categories:
                grouped[method].append(unit)

        selected: dict[BookMethodCategory, list[DistillationUnit]] = {}
        viewpoints: list[PrivateViewpointDraft] = []
        for method in sorted(grouped, key=lambda item: item.value):
            selected[method] = sorted(
                grouped[method],
                key=lambda unit: self._selection_key(method, unit),
            )[:_MAX_VIEWPOINTS_PER_METHOD]
            viewpoints.extend(
                self._viewpoint_draft(run.run_id, author_source_id, method, unit, run.finished_at)
                for unit in selected[method]
            )
        self.repository.register_viewpoint_drafts(viewpoints)
        self.state.set_checkpoint(
            scope_type="knowledge-drafts",
            scope_key=run.run_id,
            cursor={"viewpoint_draft_count": len(viewpoints), "skill_candidate_count": 0},
            status="RUNNING",
            object_hash=content_hash([item.draft_id for item in viewpoints]),
        )
        for draft in viewpoints:
            self.state.register_artifact(
                artifact_id=f"PrivateViewpointDraft:{draft.draft_id}",
                artifact_type="PrivateViewpointDraft",
                schema_version=draft.schema_version,
                object_hash=draft.payload_object_sha256,
                input_hashes=draft.source_excerpt_hashes,
            )

        viewpoints_by_method: dict[BookMethodCategory, list[PrivateViewpointDraft]] = (
            defaultdict(list)
        )
        for draft in viewpoints:
            viewpoints_by_method[draft.method_category].append(draft)
        candidates = [
            self._skill_candidate(
                run.run_id,
                author_source_id,
                method,
                viewpoints_by_method[method],
                run.finished_at,
            )
            for method in sorted(viewpoints_by_method, key=lambda item: item.value)
        ]
        self.repository.register_skill_candidates(candidates)
        for candidate in candidates:
            self.state.register_artifact(
                artifact_id=f"PrivateSkillCandidateDraft:{candidate.candidate_id}",
                artifact_type="PrivateSkillCandidateDraft",
                schema_version=candidate.schema_version,
                object_hash=candidate.payload_object_sha256,
                input_hashes=[content_hash(candidate.source_viewpoint_draft_ids)],
            )

        report = self._report(
            run.run_id,
            author_source_id,
            keep_units,
            eligible_units,
            grouped,
            selected,
            viewpoints,
            candidates,
            run.finished_at,
        )
        report_object = self.object_store.put_json(report.model_dump(mode="json"))
        self.repository.register_report(report, object_hash=report_object.sha256)
        self.state.register_artifact(
            artifact_id=f"AuthorDraftGenerationReport:{report.report_id}",
            artifact_type="AuthorDraftGenerationReport",
            schema_version=report.schema_version,
            object_hash=report_object.sha256,
            input_hashes=[
                content_hash([item.draft_id for item in viewpoints]),
                content_hash([item.candidate_id for item in candidates]),
            ],
        )
        self.state.set_checkpoint(
            scope_type="knowledge-drafts",
            scope_key=run.run_id,
            cursor={
                "viewpoint_draft_count": len(viewpoints),
                "skill_candidate_count": len(candidates),
                "report_id": report.report_id,
            },
            status="SUCCEEDED",
            object_hash=report_object.sha256,
        )
        return KnowledgeDraftExecution(
            report=report,
            viewpoint_drafts=tuple(viewpoints),
            skill_candidates=tuple(candidates),
        )

    def audit(self, author_source_id: str) -> dict[str, object]:
        source_report = self.distillation_repository.latest_author_report(author_source_id)
        report = (
            self.repository.report_for_run(
                source_report.run_id,
                _GENERATION_RULE_VERSION,
            )
            if source_report is not None
            else None
        )
        if report is None:
            return {"status": "NOT_RUN", "author_source_id": author_source_id}
        units = {
            unit.unit_id: unit
            for unit in self.distillation_repository.units_for_run(report.run_id)
        }
        drafts = self.repository.viewpoint_drafts_for_run(
            report.run_id,
            report.generation_rule_version,
        )
        candidates = self.repository.skill_candidates_for_run(
            report.run_id,
            report.generation_rule_version,
        )
        database_refs = self.repository.candidate_refs_for_run(
            report.run_id,
            report.generation_rule_version,
        )
        draft_index = {draft.draft_id: draft for draft in drafts}

        missing_payloads = 0
        invalid_payloads = 0
        source_reference_mismatches = 0
        for draft in drafts:
            unit = units.get(draft.source_unit_ids[0])
            if (
                unit is None
                or unit.normalized_text_sha256 != draft.source_excerpt_hashes[0]
                or unit.run_id != draft.run_id
                or unit.author_source_id != draft.author_source_id
            ):
                source_reference_mismatches += 1
            try:
                payload = PrivateViewpointDraftPayload.model_validate_json(
                    self.object_store.get_bytes(draft.payload_object_sha256)
                )
            except StorageError:
                missing_payloads += 1
                continue
            except ValueError:
                invalid_payloads += 1
                continue
            if (
                payload.source_unit_id != draft.source_unit_ids[0]
                or payload.method_category is not draft.method_category
                or payload.proposition_derivation is not draft.proposition_derivation
                or payload.generation_rule_version != draft.generation_rule_version
                or sha256_bytes(payload.proposition.encode("utf-8"))
                != draft.source_excerpt_hashes[0]
                or (unit is not None and payload.source_locator != unit.locator)
                or payload.applicability_scope
                or payload.counterevidence
                or payload.failure_conditions
            ):
                invalid_payloads += 1

        candidate_reference_mismatches = 0
        pending_gate_mismatches = 0
        for candidate in candidates:
            stored_refs = database_refs.get(candidate.candidate_id)
            expected_units = [
                draft_index[draft_id].source_unit_ids[0]
                for draft_id in candidate.source_viewpoint_draft_ids
                if draft_id in draft_index
            ]
            if (
                stored_refs
                != (
                    candidate.source_viewpoint_draft_ids,
                    candidate.source_unit_ids,
                )
                or len(expected_units) != len(candidate.source_viewpoint_draft_ids)
                or expected_units != candidate.source_unit_ids
                or any(
                    draft_index[draft_id].method_category is not candidate.method_category
                    for draft_id in candidate.source_viewpoint_draft_ids
                    if draft_id in draft_index
                )
            ):
                candidate_reference_mismatches += 1
            if (
                candidate.evaluation_status is not BookEvaluationStatus.NOT_RUN
                or candidate.approval_status is not BookApprovalStatus.PENDING
            ):
                pending_gate_mismatches += 1
            try:
                payload = PrivateSkillCandidatePayload.model_validate_json(
                    self.object_store.get_bytes(candidate.payload_object_sha256)
                )
            except StorageError:
                missing_payloads += 1
                continue
            except ValueError:
                invalid_payloads += 1
                continue
            if (
                payload.formal_rule is not None
                or payload.source_viewpoint_draft_ids
                != candidate.source_viewpoint_draft_ids
                or payload.generation_rule_version != candidate.generation_rule_version
            ):
                invalid_payloads += 1

        report_object_hash = self.repository.report_object_hash(report.report_id)
        missing_report_object = int(
            report_object_hash is None or not self.object_store.verify(report_object_hash)
        )
        report_count_mismatch = int(
            report.viewpoint_draft_count != len(drafts)
            or report.skill_candidate_count != len(candidates)
            or sum(report.selected_viewpoint_counts.values()) != len(drafts)
            or sum(report.target_skill_candidate_counts.values()) != len(candidates)
        )
        findings = {
            "PAYLOAD_OBJECT_MISSING": missing_payloads,
            "PAYLOAD_INVALID": invalid_payloads,
            "SOURCE_REFERENCE_MISMATCH": source_reference_mismatches,
            "CANDIDATE_REFERENCE_MISMATCH": candidate_reference_mismatches,
            "PENDING_GATE_MISMATCH": pending_gate_mismatches,
            "REPORT_OBJECT_MISSING": missing_report_object,
            "REPORT_COUNT_MISMATCH": report_count_mismatch,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "author_source_id": author_source_id,
            "run_id": report.run_id,
            "generation_rule_version": report.generation_rule_version,
            "viewpoint_draft_count": len(drafts),
            "skill_candidate_count": len(candidates),
            "missing_payload_object_count": missing_payloads,
            "invalid_payload_count": invalid_payloads,
            "source_reference_mismatch_count": source_reference_mismatches,
            "candidate_reference_mismatch_count": candidate_reference_mismatches,
            "pending_gate_mismatch_count": pending_gate_mismatches,
            "report_object_missing_count": missing_report_object,
            "report_count_mismatch_count": report_count_mismatch,
            "finding_codes": finding_codes,
        }

    def _viewpoint_draft(
        self,
        run_id: str,
        author_source_id: str,
        method: BookMethodCategory,
        unit: DistillationUnit,
        created_at,
    ) -> PrivateViewpointDraft:
        text = self.object_store.get_bytes(unit.normalized_text_sha256).decode("utf-8")
        payload = PrivateViewpointDraftPayload(
            proposition=text,
            proposition_derivation=(
                ViewpointDraftDerivation.SOURCE_EXCERPT_NOT_SYNTHESIZED
            ),
            generation_rule_version=_GENERATION_RULE_VERSION,
            method_category=method,
            source_unit_id=unit.unit_id,
            source_locator=unit.locator,
            applicability_scope=[],
            counterevidence=[],
            failure_conditions=[],
            quality_gaps=list(_VIEWPOINT_GAPS),
            created_at=created_at,
        )
        payload_object = self.object_store.put_json(payload.model_dump(mode="json"))
        identity = {
            "run_id": run_id,
            "generation_rule_version": _GENERATION_RULE_VERSION,
            "method_category": method.value,
            "source_unit_id": unit.unit_id,
        }
        return PrivateViewpointDraft(
            draft_id=f"private-viewpoint-draft:{content_hash(identity)}",
            run_id=run_id,
            author_source_id=author_source_id,
            method_category=method,
            source_unit_ids=[unit.unit_id],
            source_excerpt_hashes=[unit.normalized_text_sha256],
            payload_object_sha256=payload_object.sha256,
            proposition_derivation=(
                ViewpointDraftDerivation.SOURCE_EXCERPT_NOT_SYNTHESIZED
            ),
            generation_rule_version=_GENERATION_RULE_VERSION,
            human_review_status=HumanReviewStatus.PENDING,
            quality_gaps=list(_VIEWPOINT_GAPS),
            created_at=created_at,
        )

    def _skill_candidate(
        self,
        run_id: str,
        author_source_id: str,
        method: BookMethodCategory,
        drafts: list[PrivateViewpointDraft],
        created_at,
    ) -> PrivateSkillCandidateDraft:
        draft_ids = [draft.draft_id for draft in drafts]
        unit_ids = [draft.source_unit_ids[0] for draft in drafts]
        payload = PrivateSkillCandidatePayload(
            generation_rule_version=_GENERATION_RULE_VERSION,
            formal_rule=None,
            source_viewpoint_draft_ids=draft_ids,
            required_human_steps=[
                "VERIFY_SOURCE_CONTEXT_AND_AUTHOR_INTENT",
                "REWRITE_EXCERPTS_INTO_ONE_TESTABLE_RULE",
                "ADD_APPLICABILITY_COUNTEREVIDENCE_AND_FAILURE_CONDITIONS",
            ],
            generic_safety_gates=[
                "NO_AUTOMATIC_TRADING",
                "OFFICIAL_EVIDENCE_REQUIRED_FOR_COMPANY_FACTS",
                "POINT_IN_TIME_VALIDATION_REQUIRED",
                "OUT_OF_SAMPLE_EVALUATION_REQUIRED_BEFORE_APPROVAL",
            ],
            created_at=created_at,
        )
        payload_object = self.object_store.put_json(payload.model_dump(mode="json"))
        identity = {
            "run_id": run_id,
            "generation_rule_version": _GENERATION_RULE_VERSION,
            "method_category": method.value,
            "source_viewpoint_draft_ids": draft_ids,
        }
        return PrivateSkillCandidateDraft(
            candidate_id=f"private-skill-candidate:{content_hash(identity)}",
            run_id=run_id,
            author_source_id=author_source_id,
            target_skill=_TARGET_BY_METHOD[method],
            method_category=method,
            source_viewpoint_draft_ids=draft_ids,
            source_unit_ids=unit_ids,
            payload_object_sha256=payload_object.sha256,
            generation_rule_version=_GENERATION_RULE_VERSION,
            evaluation_status=BookEvaluationStatus.NOT_RUN,
            approval_status=BookApprovalStatus.PENDING,
            quality_gaps=list(_SKILL_GAPS),
            created_at=created_at,
        )

    @staticmethod
    def _report(
        run_id: str,
        author_source_id: str,
        keep_units: list[DistillationUnit],
        eligible_units: list[DistillationUnit],
        grouped: dict[BookMethodCategory, list[DistillationUnit]],
        selected: dict[BookMethodCategory, list[DistillationUnit]],
        viewpoints: list[PrivateViewpointDraft],
        candidates: list[PrivateSkillCandidateDraft],
        created_at,
    ) -> AuthorDraftGenerationReport:
        target_counts = Counter(candidate.target_skill.value for candidate in candidates)
        identity = {
            "run_id": run_id,
            "generation_rule_version": _GENERATION_RULE_VERSION,
            "viewpoint_draft_ids": [item.draft_id for item in viewpoints],
            "skill_candidate_ids": [item.candidate_id for item in candidates],
        }
        return AuthorDraftGenerationReport(
            report_id=f"author-draft-generation:{content_hash(identity)}",
            run_id=run_id,
            author_source_id=author_source_id,
            generation_rule_version=_GENERATION_RULE_VERSION,
            source_keep_unit_count=len(keep_units),
            eligible_method_unit_count=len(eligible_units),
            viewpoint_draft_count=len(viewpoints),
            skill_candidate_count=len(candidates),
            method_category_unit_counts={
                method.value: len(grouped[method])
                for method in sorted(grouped, key=lambda item: item.value)
            },
            selected_viewpoint_counts={
                method.value: len(selected[method])
                for method in sorted(selected, key=lambda item: item.value)
            },
            target_skill_candidate_counts=dict(sorted(target_counts.items())),
            human_review_status=HumanReviewStatus.PENDING,
            all_evaluations_not_run=True,
            all_approvals_pending=True,
            created_at=created_at,
        )

    @staticmethod
    def _selection_key(
        method: BookMethodCategory,
        unit: DistillationUnit,
    ) -> tuple[float, int, int, int, str]:
        content_class = _CONTENT_CLASS_BY_METHOD[method]
        score = unit.score_by_content_class.get(content_class.value, 0.0)
        return (
            -score,
            abs(unit.normalized_char_count - 120),
            unit.source_item_ordinal,
            unit.segment_ordinal,
            unit.unit_id,
        )


__all__ = [
    "KnowledgeDraftExecution",
    "KnowledgeDraftService",
]
