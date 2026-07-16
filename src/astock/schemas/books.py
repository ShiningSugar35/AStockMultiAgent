"""Private-book source, parsing, cleaning, distillation, and review contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.documents import DocumentType, PageExtractionMethod
from astock.schemas.knowledge import CoverageStatus
from astock.schemas.pit import PointInTimeMetadata


class BookProcessingStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    SAMPLE_ONLY = "SAMPLE_ONLY"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class BookParseScope(StrEnum):
    SAMPLE_PAGES = "SAMPLE_PAGES"
    FULL_SOURCE = "FULL_SOURCE"


class HumanReviewStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class HumanReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_CHANGES = "NEEDS_CHANGES"


class BookSkillTarget(StrEnum):
    CANDIDATE_SELECTION = "CANDIDATE_SELECTION"
    POSITION_LIFECYCLE = "POSITION_LIFECYCLE"


class BookEvaluationStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"


class BookApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BookMethodCategory(StrEnum):
    STOCK_SELECTION = "STOCK_SELECTION"
    BUSINESS_MODEL = "BUSINESS_MODEL"
    INDUSTRY = "INDUSTRY"
    VALUATION = "VALUATION"
    FINANCIAL_QUALITY = "FINANCIAL_QUALITY"
    ENTRY = "ENTRY"
    HOLDING = "HOLDING"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    RISK = "RISK"
    FAILURE_CASE = "FAILURE_CASE"
    COUNTEREVIDENCE_INVALIDATION = "COUNTEREVIDENCE_INVALIDATION"
    REVIEW = "REVIEW"


class BookContentClass(StrEnum):
    REPEATED_SLOGAN = "REPEATED_SLOGAN"
    MARKETING_PROMOTION = "MARKETING_PROMOTION"
    NON_METHOD_STORY = "NON_METHOD_STORY"
    REPETITION_WITHOUT_NEW_INFORMATION = "REPETITION_WITHOUT_NEW_INFORMATION"
    UNSUPPORTED_EMOTIONAL_JUDGMENT = "UNSUPPORTED_EMOTIONAL_JUDGMENT"
    STALE_INSTANT_PRICE_CONCLUSION = "STALE_INSTANT_PRICE_CONCLUSION"
    STOCK_SELECTION = "STOCK_SELECTION"
    BUSINESS_MODEL = "BUSINESS_MODEL"
    INDUSTRY = "INDUSTRY"
    VALUATION = "VALUATION"
    FINANCIAL_QUALITY = "FINANCIAL_QUALITY"
    ENTRY = "ENTRY"
    HOLDING_VALIDATION = "HOLDING_VALIDATION"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    RISK_CONTROL = "RISK_CONTROL"
    FAILURE_CASE = "FAILURE_CASE"
    COUNTEREVIDENCE_INVALIDATION = "COUNTEREVIDENCE_INVALIDATION"
    REVIEW_METHOD = "REVIEW_METHOD"


BOOK_DOWNWEIGHT_CLASSES = (
    BookContentClass.REPEATED_SLOGAN,
    BookContentClass.MARKETING_PROMOTION,
    BookContentClass.NON_METHOD_STORY,
    BookContentClass.REPETITION_WITHOUT_NEW_INFORMATION,
    BookContentClass.UNSUPPORTED_EMOTIONAL_JUDGMENT,
    BookContentClass.STALE_INSTANT_PRICE_CONCLUSION,
)
BOOK_KEEP_CLASSES = (
    BookContentClass.STOCK_SELECTION,
    BookContentClass.BUSINESS_MODEL,
    BookContentClass.INDUSTRY,
    BookContentClass.VALUATION,
    BookContentClass.FINANCIAL_QUALITY,
    BookContentClass.ENTRY,
    BookContentClass.HOLDING_VALIDATION,
    BookContentClass.ADD,
    BookContentClass.TRIM,
    BookContentClass.EXIT,
    BookContentClass.RISK_CONTROL,
    BookContentClass.FAILURE_CASE,
    BookContentClass.COUNTEREVIDENCE_INVALIDATION,
    BookContentClass.REVIEW_METHOD,
)


class BookSourceManifest(AStockModel):
    manifest_id: str
    source_id: str
    display_name: str
    author_source_id: str
    document_id: str
    snapshot_id: str
    pit_id: str
    document_type: DocumentType
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_version: str
    byte_size: int = Field(ge=0)
    source_page_count: int = Field(ge=0)
    parser_pipeline_version: str
    rights_status: str
    git_policy: str = "EXCLUDED"
    external_republication_policy: str = "PROHIBITED"
    raw_retention_policy: str = "PERMANENT"
    cleaning_reconstructable: bool = True

    @model_validator(mode="after")
    def validate_private_source_policy(self) -> BookSourceManifest:
        if self.document_type not in {
            DocumentType.PRIVATE_BOOK,
            DocumentType.PRIVATE_PDF,
            DocumentType.PRIVATE_DOCX,
        }:
            raise ValueError("BookSourceManifest requires a private document type")
        if self.document_type is DocumentType.PRIVATE_DOCX and self.source_page_count != 0:
            raise ValueError("reflowable DOCX sources must not claim a stable page count")
        if self.file_sha256 != self.raw_object_sha256:
            raise ValueError("raw object hash must equal the ingested file hash")
        if (
            self.git_policy != "EXCLUDED"
            or self.external_republication_policy != "PROHIBITED"
            or self.raw_retention_policy != "PERMANENT"
            or not self.cleaning_reconstructable
        ):
            raise ValueError("private-source retention and non-republication policy is mandatory")
        return self


class BookPageReference(AStockModel):
    page_id: str
    page_number: int = Field(ge=1)
    section_path: list[str]
    parser_version: str
    extraction_method: PageExtractionMethod
    text_char_count: int = Field(ge=0)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BookParseReport(AStockModel):
    book_parse_report_id: str
    manifest_id: str
    document_id: str
    snapshot_id: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_name: str
    parser_version: str
    parse_scope: BookParseScope
    processing_status: BookProcessingStatus
    source_page_count: int = Field(ge=0)
    requested_pages: list[int]
    processed_page_count: int = Field(ge=0)
    native_page_count: int = Field(ge=0)
    ocr_page_count: int = Field(ge=0)
    empty_page_count: int = Field(ge=0)
    failed_page_count: int = Field(ge=0)
    parsed_text_char_count: int = Field(ge=0)
    pages: list[BookPageReference]
    underlying_parse_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_object_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_scope(self) -> BookParseReport:
        if self.processed_page_count != len(self.pages):
            raise ValueError("processed_page_count must equal page references")
        classified = (
            self.native_page_count
            + self.ocr_page_count
            + self.empty_page_count
            + self.failed_page_count
        )
        if classified != self.processed_page_count:
            raise ValueError("page extraction counts must equal processed_page_count")
        if self.requested_pages != [page.page_number for page in self.pages]:
            raise ValueError("requested pages must match ordered page references")
        if self.parse_scope is BookParseScope.SAMPLE_PAGES:
            if self.processing_status is not BookProcessingStatus.SAMPLE_ONLY:
                raise ValueError("sample parse must remain explicitly SAMPLE_ONLY")
            if self.source_page_count and self.processed_page_count >= self.source_page_count:
                raise ValueError("SAMPLE_PAGES cannot claim full-source coverage")
        elif (
            self.processing_status is not BookProcessingStatus.COMPLETE
            or self.processed_page_count != self.source_page_count
        ):
            raise ValueError("FULL_SOURCE requires complete page coverage")
        return self


class PrivateDocxParseReport(AStockModel):
    docx_parse_report_id: str
    manifest_id: str
    document_id: str
    snapshot_id: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_name: str
    parser_version: str
    processing_status: BookProcessingStatus
    coverage_status: CoverageStatus
    source_part_count: int = Field(ge=1)
    source_paragraph_count: int = Field(ge=0)
    processed_block_count: int = Field(ge=0)
    nonempty_block_count: int = Field(ge=0)
    empty_block_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    table_cell_count: int = Field(ge=0)
    hyperlink_count: int = Field(ge=0)
    embedded_visual_count: int = Field(ge=0)
    unsupported_object_count: int = Field(ge=0)
    parsed_text_char_count: int = Field(ge=0)
    block_ids: list[str]
    block_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    report_object_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_coverage(self) -> PrivateDocxParseReport:
        if self.processed_block_count != len(self.block_ids):
            raise ValueError("processed_block_count must equal block_ids")
        if self.nonempty_block_count + self.empty_block_count != self.processed_block_count:
            raise ValueError("empty and nonempty block counts must equal processed blocks")
        if self.processed_block_count != self.source_paragraph_count:
            raise ValueError("every eligible OOXML paragraph must produce one block")
        if self.processing_status is BookProcessingStatus.COMPLETE:
            if self.coverage_status is not CoverageStatus.COMPLETE or self.gaps:
                raise ValueError("COMPLETE DOCX parsing cannot contain coverage gaps")
        elif self.processing_status is BookProcessingStatus.PARTIAL:
            if self.coverage_status is not CoverageStatus.PARTIAL or not self.gaps:
                raise ValueError("PARTIAL DOCX parsing requires explicit gaps")
        else:
            raise ValueError("DOCX parse reports must be COMPLETE or PARTIAL")
        return self


class BookCleaningReport(AStockModel):
    report_id: str
    manifest_id: str
    input_parse_report_ids: list[str]
    cleaning_pipeline_version: str
    processing_status: BookProcessingStatus
    original_page_count: int | None = Field(default=None, ge=0)
    original_char_count: int | None = Field(default=None, ge=0)
    successfully_parsed_page_count: int | None = Field(default=None, ge=0)
    ocr_page_count: int | None = Field(default=None, ge=0)
    duplicate_paragraph_count: int | None = Field(default=None, ge=0)
    downweight_or_remove_candidate_count: int | None = Field(default=None, ge=0)
    methodology_paragraph_count: int | None = Field(default=None, ge=0)
    case_paragraph_count: int | None = Field(default=None, ge=0)
    unclassified_paragraph_count: int | None = Field(default=None, ge=0)
    human_review_status: HumanReviewStatus
    downweight_classes: list[BookContentClass]
    keep_classes: list[BookContentClass]
    raw_content_preserved: bool = True
    cleaning_reconstructable: bool = True

    @model_validator(mode="after")
    def validate_cleaning_safety_and_completeness(self) -> BookCleaningReport:
        if not self.raw_content_preserved or not self.cleaning_reconstructable:
            raise ValueError("book cleaning may never delete or sever raw-source lineage")
        if set(self.downweight_classes) != set(BOOK_DOWNWEIGHT_CLASSES):
            raise ValueError("all required downweight classes must be represented")
        if set(self.keep_classes) != set(BOOK_KEEP_CLASSES):
            raise ValueError("all required keep classes must be represented")
        if len(self.downweight_classes) != len(set(self.downweight_classes)) or len(
            self.keep_classes
        ) != len(set(self.keep_classes)):
            raise ValueError("cleaning policy classes must not contain duplicates")
        metrics = (
            self.original_page_count,
            self.original_char_count,
            self.successfully_parsed_page_count,
            self.ocr_page_count,
            self.duplicate_paragraph_count,
            self.downweight_or_remove_candidate_count,
            self.methodology_paragraph_count,
            self.case_paragraph_count,
            self.unclassified_paragraph_count,
        )
        if self.processing_status is BookProcessingStatus.COMPLETE and any(
            value is None for value in metrics
        ):
            raise ValueError("a completed cleaning report requires every mandatory metric")
        return self


class BookMethodCoverageMetric(AStockModel):
    paragraph_count: int | None = Field(default=None, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    status: CoverageStatus


class BookMethodCoverageReport(AStockModel):
    report_id: str
    manifest_id: str
    input_cleaning_report_id: str | None = None
    processing_status: BookProcessingStatus
    human_review_status: HumanReviewStatus
    selection: BookMethodCoverageMetric
    entry: BookMethodCoverageMetric
    holding: BookMethodCoverageMetric
    add: BookMethodCoverageMetric
    trim: BookMethodCoverageMetric
    exit: BookMethodCoverageMetric
    risk: BookMethodCoverageMetric
    review: BookMethodCoverageMetric

    @model_validator(mode="after")
    def prevent_unfounded_author_silence(self) -> BookMethodCoverageReport:
        metrics = (
            self.selection,
            self.entry,
            self.holding,
            self.add,
            self.trim,
            self.exit,
            self.risk,
            self.review,
        )
        if any(metric.status is CoverageStatus.AUTHOR_SILENT for metric in metrics) and (
            self.processing_status is not BookProcessingStatus.COMPLETE
            or self.human_review_status is not HumanReviewStatus.APPROVED
        ):
            raise ValueError("AUTHOR_SILENT requires complete source coverage and human approval")
        if self.processing_status is BookProcessingStatus.COMPLETE and any(
            metric.paragraph_count is None for metric in metrics
        ):
            raise ValueError("completed method coverage requires every category count")
        return self


class BookViewpointCard(AStockModel):
    card_id: str
    manifest_id: str
    proposition: str
    method_category: BookMethodCategory
    evidence_ids: list[str] = Field(min_length=1)
    source_page_numbers: list[int] = Field(min_length=1)
    source_excerpt_hashes: list[str] = Field(min_length=1)
    counterevidence: list[str]
    failure_conditions: list[str]
    human_review_status: HumanReviewStatus

    @model_validator(mode="after")
    def validate_source_references(self) -> BookViewpointCard:
        if any(page < 1 for page in self.source_page_numbers):
            raise ValueError("book source pages are 1-based")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_excerpt_hashes
        ):
            raise ValueError("every viewpoint excerpt reference must be a SHA-256 hash")
        return self


class BookSkillCandidate(AStockModel):
    candidate_id: str
    manifest_id: str
    target_skill: BookSkillTarget
    method_category: BookMethodCategory
    rule_json: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1)
    source_page_numbers: list[int] = Field(min_length=1)
    source_excerpt_hashes: list[str] = Field(min_length=1)
    evaluation_status: BookEvaluationStatus
    evaluation_results: dict[str, Any]
    approval_status: BookApprovalStatus

    @model_validator(mode="after")
    def validate_rule_lineage(self) -> BookSkillCandidate:
        if any(page < 1 for page in self.source_page_numbers):
            raise ValueError("book source pages are 1-based")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_excerpt_hashes
        ):
            raise ValueError("every skill rule must cite excerpt hashes")
        return self


class HumanReviewDecision(AStockModel):
    decision_id: str
    artifact_type: str
    artifact_id: str
    verdict: HumanReviewVerdict
    reviewer_id: str
    reviewed_at: AwareDatetime
    rationale: str
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_approval_evidence(self) -> HumanReviewDecision:
        if self.verdict is HumanReviewVerdict.APPROVE and not self.evidence_ids:
            raise ValueError("approval requires evidence references")
        return self


class PrivatePdfIngestResult(AStockModel):
    manifest: BookSourceManifest
    pit_metadata: PointInTimeMetadata
    parse_report: BookParseReport | None = None


class PrivateDocxIngestResult(AStockModel):
    manifest: BookSourceManifest
    pit_metadata: PointInTimeMetadata
    parse_report: PrivateDocxParseReport
