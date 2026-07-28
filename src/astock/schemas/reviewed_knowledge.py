"""Human-reviewed book arguments and source-bounded shadow skill contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.books import BookMethodCategory
from astock.schemas.knowledge_semantics import (
    ArgumentRelationType,
    ParagraphLocator,
    RhetoricalRole,
)


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class ReviewApplicationStatus(StrEnum):
    APPLIED = "APPLIED"
    EXCLUDED = "EXCLUDED"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"


class ReviewedRunStage(StrEnum):
    INPUT_VERIFIED = "INPUT_VERIFIED"
    REVIEW_APPLIED = "REVIEW_APPLIED"
    ARGUMENTS_BUILT = "ARGUMENTS_BUILT"
    EMBEDDINGS_RECOMPUTED = "EMBEDDINGS_RECOMPUTED"
    SKILLS_DISTILLED = "SKILLS_DISTILLED"
    COMPLETE = "COMPLETE"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"
    FAILED = "FAILED"


class ReviewedArgumentStatus(StrEnum):
    READY = "READY"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"


class ReviewedSkillStatus(StrEnum):
    READY_FOR_SHADOW = "READY_FOR_SHADOW"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"


class SourceCoverageState(StrEnum):
    COVERED = "COVERED"
    AUTHOR_SILENT = "AUTHOR_SILENT"
    INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
    CONFLICTING_SOURCE = "CONFLICTING_SOURCE"


class ReviewedSkillKind(StrEnum):
    CANDIDATE_SELECTION = "CANDIDATE_SELECTION"
    POSITION_LIFECYCLE = "POSITION_LIFECYCLE"


class CandidateSelectionCategory(StrEnum):
    BUSINESS_MODEL = "BUSINESS_MODEL"
    INDUSTRY_AND_VALUE_CHAIN = "INDUSTRY_AND_VALUE_CHAIN"
    FINANCIAL_QUALITY = "FINANCIAL_QUALITY"
    VALUATION = "VALUATION"
    STOCK_SELECTION = "STOCK_SELECTION"
    CATALYST = "CATALYST"
    COUNTEREVIDENCE = "COUNTEREVIDENCE"
    RISK = "RISK"


class PositionLifecycleCategory(StrEnum):
    ENTRY = "ENTRY"
    STAGED_ENTRY = "STAGED_ENTRY"
    HOLDING_VALIDATION = "HOLDING_VALIDATION"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    TIME_STOP = "TIME_STOP"
    PRICE_STOP = "PRICE_STOP"
    THESIS_STOP = "THESIS_STOP"
    REVIEW = "REVIEW"


class ReviewParagraphRange(AStockModel):
    start_page: int = Field(ge=1)
    start_paragraph_ordinal: int = Field(ge=1)
    end_page: int = Field(ge=1)
    end_paragraph_ordinal: int = Field(ge=1)
    start_summary: str | None = None
    end_summary: str | None = None

    @model_validator(mode="after")
    def validate_order(self) -> ReviewParagraphRange:
        if (self.end_page, self.end_paragraph_ordinal) < (
            self.start_page,
            self.start_paragraph_ordinal,
        ):
            raise ValueError("review paragraph range is reversed")
        return self


class ReviewWorkbookRecord(AStockModel):
    excel_row: int = Field(ge=2)
    page_number: int = Field(ge=1)
    source_start_ordinal: int = Field(ge=1)
    source_end_ordinal: int = Field(ge=1)
    topics: list[str] = Field(min_length=1)
    review_reason: str = Field(min_length=1)
    image_marker: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    source_preview: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    verdict: ReviewVerdict

    @model_validator(mode="after")
    def validate_source_range(self) -> ReviewWorkbookRecord:
        if self.source_end_ordinal < self.source_start_ordinal:
            raise ValueError("review source range is reversed")
        return self


class ReviewArgumentTarget(AStockModel):
    title: str = Field(min_length=1)
    ranges: list[ReviewParagraphRange] = Field(min_length=1)
    topics: list[str] = Field(min_length=1)


class ReviewDecision(AStockModel):
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    excel_row: int = Field(ge=2)
    source_argument_unit_id: str = Field(min_length=1)
    verdict: ReviewVerdict
    application_status: ReviewApplicationStatus
    targets: list[ReviewArgumentTarget]
    corrected_topics: list[str]
    uncertainty_reason: str | None = None
    review_conclusion_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_application(self) -> ReviewDecision:
        if self.application_status is ReviewApplicationStatus.EXCLUDED:
            if self.verdict is not ReviewVerdict.REJECT or self.targets:
                raise ValueError("only rejected review rows can be excluded")
        elif self.application_status is ReviewApplicationStatus.APPLIED:
            if self.verdict is ReviewVerdict.REJECT or not self.targets:
                raise ValueError("applied review rows require retained ranges")
        elif not self.uncertainty_reason:
            raise ValueError("unresolved review decisions require a reason")
        return self


class ReviewedParagraphRef(AStockModel):
    ref_ordinal: int = Field(ge=1)
    source_paragraph_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    paragraph_ordinal: int = Field(ge=1)
    paragraph_head: str = Field(min_length=1)
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rhetorical_role: RhetoricalRole
    rhetorical_roles: list[RhetoricalRole] = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    locator: ParagraphLocator
    visual_evidence_ids: list[str] = Field(default_factory=list)
    visual_chart_unit_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_projection(self) -> ReviewedParagraphRef:
        if (
            self.locator.page_number != self.page_number
            or self.locator.content_id != self.content_id
            or self.locator.source_snapshot_id != self.source_snapshot_id
        ):
            raise ValueError("reviewed paragraph locator projection is inconsistent")
        if self.rhetorical_role not in self.rhetorical_roles:
            raise ValueError("reviewed paragraph primary role must be listed")
        if len(self.visual_evidence_ids) != len(set(self.visual_evidence_ids)):
            raise ValueError("reviewed paragraph visual evidence ids must be unique")
        if len(self.visual_chart_unit_ids) != len(set(self.visual_chart_unit_ids)):
            raise ValueError("reviewed paragraph chart ids must be unique")
        return self


class ReviewedArgumentRelation(AStockModel):
    relation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    source_ref_ordinal: int = Field(ge=1)
    target_ref_ordinal: int = Field(ge=1)
    relation_type: ArgumentRelationType
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_endpoints(self) -> ReviewedArgumentRelation:
        if self.source_ref_ordinal == self.target_ref_ordinal:
            raise ValueError("reviewed argument relations require two refs")
        return self


class ReviewedArgumentUnit(AStockModel):
    argument_unit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    decision_ids: list[str] = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    paragraph_refs: list[ReviewedParagraphRef] = Field(min_length=1)
    start_locator: ParagraphLocator
    end_locator: ParagraphLocator
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rhetorical_roles: list[RhetoricalRole] = Field(min_length=1)
    relations: list[ReviewedArgumentRelation]
    method_categories: list[BookMethodCategory]
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    standalone_distillable: bool
    status: ReviewedArgumentStatus
    source_argument_unit_ids: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> ReviewedArgumentUnit:
        ordinals = [item.ref_ordinal for item in self.paragraph_refs]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("reviewed paragraph refs must be contiguous and ordered")
        paragraph_ids = [item.source_paragraph_id for item in self.paragraph_refs]
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("reviewed argument paragraph ids must be unique")
        if self.start_locator != self.paragraph_refs[0].locator:
            raise ValueError("reviewed argument start locator is inconsistent")
        if self.end_locator != self.paragraph_refs[-1].locator:
            raise ValueError("reviewed argument end locator is inconsistent")
        if len(self.source_argument_unit_ids) != len(set(self.source_argument_unit_ids)):
            raise ValueError("reviewed argument source AU ids must be unique")
        if len(self.decision_ids) != len(set(self.decision_ids)):
            raise ValueError("reviewed argument decision ids must be unique")
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("reviewed argument source snapshots must be unique")
        relation_ids = [item.relation_id for item in self.relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("reviewed argument relation ids must be unique")
        valid_refs = set(ordinals)
        if any(
            relation.argument_unit_id != self.argument_unit_id
            or relation.run_id != self.run_id
            or relation.source_ref_ordinal not in valid_refs
            or relation.target_ref_ordinal not in valid_refs
            for relation in self.relations
        ):
            raise ValueError("reviewed argument relation crosses its boundary")
        if self.standalone_distillable and self.status is not ReviewedArgumentStatus.READY:
            raise ValueError("distillable reviewed arguments must be ready")
        return self


class ReviewedSemanticRun(AStockModel):
    run_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    review_workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_version: str = Field(min_length=1)
    stage: ReviewedRunStage
    review_record_count: int = Field(ge=0)
    reviewed_argument_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> ReviewedSemanticRun:
        terminal = {
            ReviewedRunStage.COMPLETE,
            ReviewedRunStage.NEEDS_USER_REVIEW,
            ReviewedRunStage.FAILED,
        }
        if (self.stage in terminal) != (self.finished_at is not None):
            raise ValueError("reviewed run terminal timestamp is inconsistent")
        if self.stage is ReviewedRunStage.COMPLETE and self.unresolved_count:
            raise ValueError("complete reviewed runs cannot have unresolved decisions")
        return self


class ReviewedEmbeddingManifest(AStockModel):
    manifest_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_parquet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score_parquet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method_vector_parquet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_embedding_manifest_id: str | None = None
    source_embedding_reused: bool = False
    vector_count: int = Field(ge=1)
    score_count: int = Field(ge=1)
    method_vector_count: int = Field(ge=1)

    @model_validator(mode="after")
    def prevent_source_reuse(self) -> ReviewedEmbeddingManifest:
        if self.source_embedding_reused:
            raise ValueError("reviewed embeddings cannot reuse the source run result")
        return self


class ReviewedSourceRef(AStockModel):
    argument_unit_id: str = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)
    page_numbers: list[int] = Field(min_length=1)
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_source_refs(self) -> ReviewedSourceRef:
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("method source paragraph ids must be unique")
        if self.page_numbers != sorted(set(self.page_numbers)):
            raise ValueError("method source pages must be sorted and unique")
        return self


class ViewpointCard(AStockModel):
    card_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    proposition: str = Field(min_length=1)
    method_category: BookMethodCategory
    source_refs: list[ReviewedSourceRef] = Field(min_length=1)
    counterevidence: list[str] = Field(min_length=1)
    failure_conditions: list[str] = Field(min_length=1)
    status: ReviewedSkillStatus


class MethodRule(AStockModel):
    rule_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    semantic_signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_question: str = Field(min_length=1)
    applicable_conditions: list[str] = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)
    positive_signals: list[str] = Field(min_length=1)
    negative_signals: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    known_failure_modes: list[str] = Field(min_length=1)
    applicable_industries: list[str] = Field(min_length=1)
    holding_horizon: list[str] = Field(min_length=1)
    method_categories: list[BookMethodCategory] = Field(min_length=1)
    source_refs: list[ReviewedSourceRef] = Field(min_length=1)
    status: ReviewedSkillStatus

    @model_validator(mode="after")
    def validate_categories(self) -> MethodRule:
        if self.method_categories != sorted(
            set(self.method_categories),
            key=lambda item: item.value,
        ):
            raise ValueError("method rule categories must be sorted and unique")
        return self


class CandidateSelectionSkill(AStockModel):
    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    category: CandidateSelectionCategory
    rule_ids: list[str]
    source_argument_unit_ids: list[str]
    coverage_state: SourceCoverageState
    status: ReviewedSkillStatus
    shadow_enabled: bool
    formal_committee_weight_allowed: bool = False

    @model_validator(mode="after")
    def validate_shadow_boundary(self) -> CandidateSelectionSkill:
        if self.shadow_enabled != (
            self.status is ReviewedSkillStatus.READY_FOR_SHADOW
            and self.coverage_state is SourceCoverageState.COVERED
        ):
            raise ValueError("candidate skill shadow flag is inconsistent")
        if self.formal_committee_weight_allowed:
            raise ValueError("reviewed skills cannot self-authorize committee weight")
        return self


class PositionLifecycleSkill(AStockModel):
    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    category: PositionLifecycleCategory
    rule_ids: list[str]
    source_argument_unit_ids: list[str]
    coverage_state: SourceCoverageState
    status: ReviewedSkillStatus
    shadow_enabled: bool
    formal_committee_weight_allowed: bool = False

    @model_validator(mode="after")
    def validate_shadow_boundary(self) -> PositionLifecycleSkill:
        if self.shadow_enabled != (
            self.status is ReviewedSkillStatus.READY_FOR_SHADOW
            and self.coverage_state is SourceCoverageState.COVERED
        ):
            raise ValueError("lifecycle skill shadow flag is inconsistent")
        if self.formal_committee_weight_allowed:
            raise ValueError("reviewed skills cannot self-authorize committee weight")
        return self


class ReviewedAuthorSkillCoverage(AStockModel):
    coverage_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    candidate_selection: dict[CandidateSelectionCategory, SourceCoverageState]
    position_lifecycle: dict[PositionLifecycleCategory, SourceCoverageState]
    source_argument_count: int = Field(ge=0)
    ready_for_shadow_count: int = Field(ge=0)
    needs_user_review_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> ReviewedAuthorSkillCoverage:
        if set(self.candidate_selection) != set(CandidateSelectionCategory):
            raise ValueError("candidate selection coverage matrix is incomplete")
        if set(self.position_lifecycle) != set(PositionLifecycleCategory):
            raise ValueError("position lifecycle coverage matrix is incomplete")
        return self


class ReviewedCoverageReport(AStockModel):
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    coverage_status: str = Field(pattern=r"^(COMPLETE|NEEDS_USER_REVIEW|FAILED)$")
    review_record_count: int = Field(ge=0)
    mapped_record_count: int = Field(ge=0)
    reviewed_argument_count: int = Field(ge=0)
    visual_argument_count: int = Field(ge=0)
    visual_ref_count: int = Field(ge=0)
    unresolved_excel_rows: list[int]
    source_run_unchanged: bool
    source_embedding_reused: bool
    source_skill_reused: bool
    foreign_key_check_passed: bool
    integrity_check_passed: bool
    acceptance_statistics: dict[str, int] = Field(default_factory=dict)


class ReviewedShadowBundle(AStockModel):
    bundle_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    ready_skill_ids: list[str]
    needs_user_review_skill_ids: list[str]
    source_argument_unit_ids: list[str]
    formal_committee_weight_allowed: bool = False

    @model_validator(mode="after")
    def validate_boundary(self) -> ReviewedShadowBundle:
        if self.formal_committee_weight_allowed:
            raise ValueError("shadow bundles cannot self-authorize committee weight")
        if set(self.ready_skill_ids).intersection(self.needs_user_review_skill_ids):
            raise ValueError("shadow bundle skill states must be disjoint")
        return self


class ReviewedAcceptanceStats(AStockModel):
    review_record_count: int = Field(ge=0)
    mapped_record_count: int = Field(ge=0)
    pass_inherited_count: int = Field(ge=0)
    rejected_excluded_count: int = Field(ge=0)
    same_page_adjustment_count: int = Field(ge=0)
    same_page_split_count: int = Field(ge=0)
    cross_page_rebuild_count: int = Field(ge=0)
    topic_correction_count: int = Field(ge=0)
    visual_participation_count: int = Field(ge=0)
    needs_user_review_count: int = Field(ge=0)
    reviewed_argument_count: int = Field(ge=0)
    viewpoint_card_count: int = Field(ge=0)
    method_rule_count: int = Field(ge=0)
    candidate_selection_skill_count: int = Field(ge=0)
    position_lifecycle_skill_count: int = Field(ge=0)
    ready_for_shadow_count: int = Field(ge=0)
    needs_user_review_skill_count: int = Field(ge=0)


__all__ = [
    "CandidateSelectionCategory",
    "CandidateSelectionSkill",
    "MethodRule",
    "PositionLifecycleCategory",
    "PositionLifecycleSkill",
    "ReviewApplicationStatus",
    "ReviewArgumentTarget",
    "ReviewDecision",
    "ReviewParagraphRange",
    "ReviewVerdict",
    "ReviewWorkbookRecord",
    "ReviewedAcceptanceStats",
    "ReviewedAuthorSkillCoverage",
    "ReviewedArgumentRelation",
    "ReviewedArgumentStatus",
    "ReviewedArgumentUnit",
    "ReviewedCoverageReport",
    "ReviewedEmbeddingManifest",
    "ReviewedParagraphRef",
    "ReviewedRunStage",
    "ReviewedSemanticRun",
    "ReviewedShadowBundle",
    "ReviewedSkillKind",
    "ReviewedSkillStatus",
    "ReviewedSourceRef",
    "SourceCoverageState",
    "ViewpointCard",
]
