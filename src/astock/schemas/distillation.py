"""Cross-source private knowledge distillation contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.books import (
    BOOK_DOWNWEIGHT_CLASSES,
    BOOK_KEEP_CLASSES,
    BookApprovalStatus,
    BookContentClass,
    BookEvaluationStatus,
    BookMethodCategory,
    BookSkillTarget,
    HumanReviewStatus,
)
from astock.schemas.knowledge import CoverageStatus


class DistillationLocatorType(StrEnum):
    PAGE_TEXT = "PAGE_TEXT"
    BLOCK_TEXT = "BLOCK_TEXT"
    ZHIHU_CONTENT = "ZHIHU_CONTENT"
    ZHIHU_COMMENT = "ZHIHU_COMMENT"


class DistillationDecision(StrEnum):
    KEEP_CANDIDATE = "KEEP_CANDIDATE"
    DOWNWEIGHT_CANDIDATE = "DOWNWEIGHT_CANDIDATE"
    UNCLASSIFIED = "UNCLASSIFIED"


class DistillationClassificationScope(StrEnum):
    """Boundary at which the field library was evaluated."""

    LEGACY_SEGMENT = "LEGACY_SEGMENT"
    SOURCE_PIECE = "SOURCE_PIECE"


class DistillationRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class KnowledgeMaterialKind(StrEnum):
    PRIVATE_PDF = "PRIVATE_PDF"
    PRIVATE_DOCX = "PRIVATE_DOCX"
    ZHIHU_ONLINE = "ZHIHU_ONLINE"


class KnowledgeProcessingStrategy(StrEnum):
    PDF_PAGE_WRAPPED_PARAGRAPH_V1 = "PDF_PAGE_WRAPPED_PARAGRAPH_V1"
    DOCX_STABLE_BLOCK_V1 = "DOCX_STABLE_BLOCK_V1"
    ZHIHU_VERIFIED_VISIBLE_HTML_V2 = "ZHIHU_VERIFIED_VISIBLE_HTML_V2"


class ViewpointDraftDerivation(StrEnum):
    SOURCE_EXCERPT_NOT_SYNTHESIZED = "SOURCE_EXCERPT_NOT_SYNTHESIZED"


class DistillationSourceLocator(AStockModel):
    locator_type: DistillationLocatorType
    source_snapshot_id: str = Field(min_length=1)
    source_unit_id: str = Field(min_length=1)
    source_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1)
    block_index: int | None = Field(default=None, ge=1)
    content_id: str | None = None
    comment_id: str | None = None
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    parser_or_schema_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_locator_identity(self) -> DistillationSourceLocator:
        if self.char_end <= self.char_start:
            raise ValueError("distillation locator char range must be positive")
        if self.locator_type is DistillationLocatorType.PAGE_TEXT:
            if (
                self.page_number is None
                or self.block_index is not None
                or self.content_id is not None
                or self.comment_id is not None
            ):
                raise ValueError("PAGE_TEXT requires only page_number")
        elif self.locator_type is DistillationLocatorType.BLOCK_TEXT:
            if (
                self.block_index is None
                or self.page_number is not None
                or self.content_id is not None
                or self.comment_id is not None
            ):
                raise ValueError("BLOCK_TEXT requires only block_index")
        elif self.locator_type is DistillationLocatorType.ZHIHU_CONTENT:
            if (
                not self.content_id
                or self.page_number is not None
                or self.block_index is not None
                or self.comment_id is not None
            ):
                raise ValueError("ZHIHU_CONTENT requires only content_id")
        elif (
            not self.content_id
            or not self.comment_id
            or self.page_number is not None
            or self.block_index is not None
        ):
            raise ValueError("ZHIHU_COMMENT requires content_id and comment_id")
        return self


class DistillationClassRuleSet(AStockModel):
    rule_version: str = Field(min_length=1)
    comment_chain_filter_version: str = Field(min_length=1)
    minimum_unit_char_count: int = Field(ge=1)
    content_class_terms: dict[BookContentClass, list[str]]

    @model_validator(mode="after")
    def validate_class_coverage(self) -> DistillationClassRuleSet:
        required = set(BOOK_DOWNWEIGHT_CLASSES) | set(BOOK_KEEP_CLASSES)
        if set(self.content_class_terms) != required:
            raise ValueError("distillation rules must cover every content class")
        for content_class, terms in self.content_class_terms.items():
            normalized = [term.strip().casefold() for term in terms]
            if any(not term for term in normalized) or len(normalized) != len(set(normalized)):
                raise ValueError(
                    f"distillation terms must be nonempty and unique: {content_class.value}"
                )
        return self


class DistillationUnit(AStockModel):
    unit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_item_ordinal: int = Field(ge=1)
    segment_ordinal: int = Field(ge=1)
    classification_scope: DistillationClassificationScope = (
        DistillationClassificationScope.LEGACY_SEGMENT
    )
    classification_piece_id: str | None = None
    classification_piece_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    classification_piece_source_item_count: int = Field(default=1, ge=1)
    classification_piece_segment_count: int = Field(default=1, ge=1)
    locator: DistillationSourceLocator
    normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_char_count: int = Field(ge=1)
    duplicate_of_unit_id: str | None = None
    content_classes: list[BookContentClass]
    method_categories: list[BookMethodCategory]
    decision: DistillationDecision
    reason_codes: list[str] = Field(min_length=1)
    score_by_content_class: dict[str, float]
    classification_rule_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_classification(self) -> DistillationUnit:
        if len(self.content_classes) != len(set(self.content_classes)):
            raise ValueError("distillation content classes must be unique")
        if len(self.method_categories) != len(set(self.method_categories)):
            raise ValueError("distillation method categories must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("distillation reason codes must be unique")
        if self.classification_scope is DistillationClassificationScope.SOURCE_PIECE:
            if not self.classification_piece_id or not self.classification_piece_sha256:
                raise ValueError("piece-scoped classification requires piece identity and hash")
        if self.duplicate_of_unit_id is not None:
            if self.duplicate_of_unit_id == self.unit_id:
                raise ValueError("a distillation unit cannot duplicate itself")
            if self.classification_scope is DistillationClassificationScope.LEGACY_SEGMENT and (
                self.decision is not DistillationDecision.DOWNWEIGHT_CANDIDATE
                or BookContentClass.REPETITION_WITHOUT_NEW_INFORMATION not in self.content_classes
                or "EXACT_DUPLICATE" not in self.reason_codes
            ):
                raise ValueError("duplicate units require an explicit downweight decision")
        return self


class DistillationRun(AStockModel):
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    classification_rule_version: str = Field(min_length=1)
    input_hashes: list[str] = Field(min_length=1)
    input_source_ids: list[str] = Field(min_length=1)
    status: DistillationRunStatus
    input_source_item_count: int = Field(ge=0)
    empty_source_item_count: int = Field(ge=0)
    produced_unit_count: int = Field(ge=0)
    canonical_unit_count: int = Field(ge=0)
    duplicate_unit_count: int = Field(ge=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_run_counts(self) -> DistillationRun:
        if len(self.input_hashes) != len(set(self.input_hashes)):
            raise ValueError("distillation input hashes must be unique")
        if len(self.input_source_ids) != len(set(self.input_source_ids)):
            raise ValueError("distillation input source ids must be unique")
        if self.canonical_unit_count + self.duplicate_unit_count != self.produced_unit_count:
            raise ValueError("canonical and duplicate counts must equal produced units")
        if self.status is DistillationRunStatus.COMPLETE and self.finished_at is None:
            raise ValueError("completed distillation runs require finished_at")
        if self.status is DistillationRunStatus.RUNNING and self.finished_at is not None:
            raise ValueError("running distillation runs cannot have finished_at")
        return self


class KnowledgeSourceStructureProfile(AStockModel):
    """Text-free structural metrics and the selected processing strategy."""

    profile_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    input_source_id: str = Field(min_length=1)
    material_kind: KnowledgeMaterialKind
    processing_strategy: KnowledgeProcessingStrategy
    input_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_object_count: int = Field(ge=0)
    source_item_count: int = Field(ge=0)
    zero_length_source_item_count: int = Field(ge=0)
    semantic_empty_source_item_count: int = Field(ge=0)
    structure_unit_count: int = Field(ge=0)
    semantic_segment_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    block_count: int = Field(ge=0)
    heading_count: int = Field(ge=0)
    table_cell_block_count: int = Field(ge=0)
    verified_content_count: int = Field(ge=0)
    target_author_comment_count: int = Field(ge=0)
    content_type_counts: dict[str, int]
    char_count_p50: int = Field(ge=0)
    char_count_p90: int = Field(ge=0)
    char_count_max: int = Field(ge=0)
    recommended_action_codes: list[str] = Field(min_length=1)
    coverage_status: CoverageStatus
    human_review_status: HumanReviewStatus = HumanReviewStatus.PENDING

    @model_validator(mode="after")
    def validate_structure_counts(self) -> KnowledgeSourceStructureProfile:
        if self.zero_length_source_item_count > self.source_item_count:
            raise ValueError("zero-length items cannot exceed source items")
        if self.semantic_empty_source_item_count > self.source_item_count:
            raise ValueError("semantic-empty items cannot exceed source items")
        if len(self.recommended_action_codes) != len(set(self.recommended_action_codes)):
            raise ValueError("structure-profile action codes must be unique")
        if any(count < 0 for count in self.content_type_counts.values()):
            raise ValueError("content type counts cannot be negative")
        return self


class AuthorDistillationReport(AStockModel):
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    input_source_ids: list[str] = Field(min_length=1)
    local_manifest_ids: list[str]
    input_source_item_count: int = Field(ge=0)
    empty_source_item_count: int = Field(ge=0)
    classification_scope: DistillationClassificationScope = (
        DistillationClassificationScope.LEGACY_SEGMENT
    )
    classification_piece_count: int = Field(default=0, ge=0)
    dropped_source_item_count: int = Field(default=0, ge=0)
    dropped_segment_count: int = Field(default=0, ge=0)
    unit_count: int = Field(ge=0)
    canonical_unit_count: int = Field(ge=0)
    duplicate_unit_count: int = Field(ge=0)
    keep_candidate_count: int = Field(ge=0)
    downweight_candidate_count: int = Field(ge=0)
    unclassified_count: int = Field(ge=0)
    content_class_counts: dict[str, int]
    method_category_counts: dict[str, int]
    online_content_count: int = Field(ge=0)
    target_author_comment_count: int = Field(ge=0)
    qualified_comment_chain_count: int = Field(default=0, ge=0)
    qualified_comment_context_count: int = Field(default=0, ge=0)
    comment_chain_filter_version: str | None = None
    open_collection_gap_count: int = Field(ge=0)
    missing_object_count: int = Field(ge=0)
    coverage_status: CoverageStatus
    human_review_status: HumanReviewStatus
    review_queue_id: str = Field(min_length=1)
    parquet_object_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_report_counts(self) -> AuthorDistillationReport:
        if self.canonical_unit_count + self.duplicate_unit_count != self.unit_count:
            raise ValueError("report canonical and duplicate counts must equal units")
        decisions = (
            self.keep_candidate_count + self.downweight_candidate_count + self.unclassified_count
        )
        if decisions != self.unit_count:
            raise ValueError("every distillation unit requires exactly one decision")
        if self.classification_scope is DistillationClassificationScope.SOURCE_PIECE:
            if self.unit_count and not self.classification_piece_count:
                raise ValueError("piece-scoped reports require classification pieces")
            if self.dropped_source_item_count or self.dropped_segment_count:
                raise ValueError("piece-scoped distillation may not drop source material")
        if (self.qualified_comment_chain_count or self.qualified_comment_context_count) and not (
            self.comment_chain_filter_version
        ):
            raise ValueError("qualified comment chains require a filter version")
        if self.human_review_status is HumanReviewStatus.APPROVED:
            raise ValueError("automatic distillation reports cannot self-approve")
        return self


class DistillationReviewQueue(AStockModel):
    queue_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    unit_ids: list[str]
    human_review_status: HumanReviewStatus = HumanReviewStatus.PENDING

    @model_validator(mode="after")
    def validate_queue(self) -> DistillationReviewQueue:
        if len(self.unit_ids) != len(set(self.unit_ids)):
            raise ValueError("review queue unit ids must be unique")
        if self.human_review_status is HumanReviewStatus.APPROVED:
            raise ValueError("review queues require explicit review decisions")
        return self


class PrivateViewpointDraftPayload(AStockModel):
    proposition: str = Field(min_length=1)
    proposition_derivation: ViewpointDraftDerivation
    generation_rule_version: str = Field(min_length=1)
    method_category: BookMethodCategory
    source_unit_id: str = Field(min_length=1)
    source_locator: DistillationSourceLocator
    applicability_scope: list[str]
    counterevidence: list[str]
    failure_conditions: list[str]
    quality_gaps: list[str] = Field(min_length=1)


class PrivateViewpointDraft(AStockModel):
    draft_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    method_category: BookMethodCategory
    source_unit_ids: list[str] = Field(min_length=1, max_length=1)
    source_excerpt_hashes: list[str] = Field(min_length=1, max_length=1)
    payload_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposition_derivation: ViewpointDraftDerivation
    generation_rule_version: str = Field(min_length=1)
    human_review_status: HumanReviewStatus = HumanReviewStatus.PENDING
    quality_gaps: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def prevent_automatic_viewpoint_approval(self) -> PrivateViewpointDraft:
        if self.human_review_status is HumanReviewStatus.APPROVED:
            raise ValueError("private viewpoint drafts require an explicit review decision")
        if len(self.source_unit_ids) != len(set(self.source_unit_ids)):
            raise ValueError("viewpoint draft source units must be unique")
        if len(self.source_excerpt_hashes) != len(set(self.source_excerpt_hashes)):
            raise ValueError("viewpoint draft excerpt hashes must be unique")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_excerpt_hashes
        ):
            raise ValueError("viewpoint draft excerpt hashes must be SHA-256")
        return self


class PrivateSkillCandidatePayload(AStockModel):
    generation_rule_version: str = Field(min_length=1)
    formal_rule: dict[str, object] | None = None
    source_viewpoint_draft_ids: list[str] = Field(min_length=1)
    required_human_steps: list[str] = Field(min_length=1)
    generic_safety_gates: list[str] = Field(min_length=1)


class PrivateSkillCandidateDraft(AStockModel):
    candidate_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    target_skill: BookSkillTarget
    method_category: BookMethodCategory
    source_viewpoint_draft_ids: list[str] = Field(min_length=1)
    source_unit_ids: list[str] = Field(min_length=1)
    payload_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_rule_version: str = Field(min_length=1)
    evaluation_status: BookEvaluationStatus = BookEvaluationStatus.NOT_RUN
    approval_status: BookApprovalStatus = BookApprovalStatus.PENDING
    quality_gaps: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def prevent_automatic_skill_approval(self) -> PrivateSkillCandidateDraft:
        if self.evaluation_status is not BookEvaluationStatus.NOT_RUN:
            raise ValueError("automatic Skill candidates cannot claim an evaluation result")
        if self.approval_status is not BookApprovalStatus.PENDING:
            raise ValueError("automatic Skill candidates must remain PENDING")
        if len(self.source_viewpoint_draft_ids) != len(set(self.source_viewpoint_draft_ids)):
            raise ValueError("Skill candidate viewpoint drafts must be unique")
        if len(self.source_unit_ids) != len(set(self.source_unit_ids)):
            raise ValueError("Skill candidate source units must be unique")
        return self


class AuthorDraftGenerationReport(AStockModel):
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    generation_rule_version: str = Field(min_length=1)
    source_keep_unit_count: int = Field(ge=0)
    eligible_method_unit_count: int = Field(ge=0)
    viewpoint_draft_count: int = Field(ge=0)
    skill_candidate_count: int = Field(ge=0)
    method_category_unit_counts: dict[str, int]
    selected_viewpoint_counts: dict[str, int]
    target_skill_candidate_counts: dict[str, int]
    human_review_status: HumanReviewStatus = HumanReviewStatus.PENDING
    all_evaluations_not_run: bool = True
    all_approvals_pending: bool = True

    @model_validator(mode="after")
    def validate_pending_generation(self) -> AuthorDraftGenerationReport:
        if self.human_review_status is HumanReviewStatus.APPROVED:
            raise ValueError("draft generation reports cannot self-approve")
        if not self.all_evaluations_not_run or not self.all_approvals_pending:
            raise ValueError("automatic draft generation must remain unevaluated and pending")
        if sum(self.selected_viewpoint_counts.values()) != self.viewpoint_draft_count:
            raise ValueError("selected viewpoint counts must equal draft count")
        if sum(self.target_skill_candidate_counts.values()) != self.skill_candidate_count:
            raise ValueError("target Skill counts must equal candidate count")
        return self


__all__ = [
    "AuthorDraftGenerationReport",
    "AuthorDistillationReport",
    "DistillationClassRuleSet",
    "DistillationDecision",
    "DistillationLocatorType",
    "DistillationReviewQueue",
    "DistillationRun",
    "DistillationRunStatus",
    "DistillationSourceLocator",
    "DistillationUnit",
    "PrivateSkillCandidateDraft",
    "PrivateSkillCandidatePayload",
    "PrivateViewpointDraft",
    "PrivateViewpointDraftPayload",
    "ViewpointDraftDerivation",
]
