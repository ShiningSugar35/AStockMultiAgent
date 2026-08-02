"""Schemas for direct-source real-run contract validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_EMPTY_BYTES_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class DirectSourceRealRunModel(BaseModel):
    """Strict base for direct-source real-run contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class DirectSourceRealRunAuditedEmptyUnit(DirectSourceRealRunModel):
    """Exact zero-length source unit retained for coverage, never distillation."""

    source_kind: Literal["PDF", "DOCX"]
    unit_index: int = Field(ge=1)
    start_offset: Literal[0] = 0
    end_offset: Literal[0] = 0
    object_hash: Literal[
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ] = _EMPTY_BYTES_SHA256


class DirectSourceRealRunPdfFingerprint(DirectSourceRealRunModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=1)
    pages: int = Field(ge=1)
    audited_empty_units: list[DirectSourceRealRunAuditedEmptyUnit] = Field(
        default_factory=list
    )


class DirectSourceRealRunAuditedArticleBoundaryMarker(DirectSourceRealRunModel):
    """One exact parser-faithful article boundary without heading style metadata."""

    article_index: int = Field(ge=1)
    block_index: int = Field(ge=1)
    title_hash: str = Field(pattern=_SHA256_PATTERN)
    title_anchor_matches: Literal[True]
    is_heading: Literal[False]
    style_id: None = None
    heading_level: None = None
    metadata_object_hash: str = Field(pattern=_SHA256_PATTERN)
    parser_version: str = Field(min_length=1)


class DirectSourceRealRunDocxFingerprint(DirectSourceRealRunModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=1)
    paragraphs: int = Field(ge=1)
    sections: int = Field(ge=1)
    paragraph_locator_scheme: str = Field(min_length=1)
    title_anchor_rule: str = Field(min_length=1)
    audited_article_boundary_markers: list[
        DirectSourceRealRunAuditedArticleBoundaryMarker
    ] = Field(default_factory=list)


class DirectSourceRealRunRowFingerprint(DirectSourceRealRunModel):
    """Count plus canonical SHA-256 for a deterministic ordered row set."""

    count: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class DirectSourceRealRunZeroLengthFragment(DirectSourceRealRunModel):
    """Auditable representation for an empty OOXML body paragraph."""

    source_kind: Literal["DOCX"] = "DOCX"
    start_offset: Literal[0] = 0
    end_offset: Literal[0] = 0
    object_hash: Literal[
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ] = _EMPTY_BYTES_SHA256


class DirectSourceRealRunDocxBlockContract(DirectSourceRealRunModel):
    """Frozen coverage for every DOCX body w:p, including legal empty paragraphs."""

    parser_version: str = Field(min_length=1)
    paragraph_locator_scheme: str = Field(min_length=1)
    paragraph_count: int = Field(ge=1)
    article_count: int = Field(ge=1)
    empty_paragraph_count: int = Field(ge=0)
    zero_length_representation: DirectSourceRealRunZeroLengthFragment
    block_rows: DirectSourceRealRunRowFingerprint
    empty_block_indexes: DirectSourceRealRunRowFingerprint
    article_boundaries: DirectSourceRealRunRowFingerprint

    @model_validator(mode="after")
    def validate_coverage_counts(self) -> DirectSourceRealRunDocxBlockContract:
        if self.block_rows.count != self.paragraph_count:
            raise ValueError("DOCX block-row fingerprint count must equal paragraph_count")
        if self.empty_block_indexes.count != self.empty_paragraph_count:
            raise ValueError("DOCX empty-index fingerprint count must equal empty_paragraph_count")
        if self.article_boundaries.count != self.article_count:
            raise ValueError("DOCX article-boundary count must equal article_count")
        return self


class DirectSourceRealRunVisualAdjudication(DirectSourceRealRunModel):
    """One exact, non-generalizable exclusion over immutable visual facts."""

    action: Literal["NON_SEMANTIC_EXCLUDE"]
    evidence_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    placement_index: int = Field(ge=1)
    placement_ordinal: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    image_object_hash: str = Field(pattern=_SHA256_PATTERN)
    evidence_object_hash: str = Field(pattern=_SHA256_PATTERN)
    chart_unit_id: str = Field(min_length=1)
    original_chart_type: Literal["CHART", "TABLE", "DIAGRAM", "TEXT_IMAGE", "DECORATIVE", "UNKNOWN"]
    original_decorative_excluded: bool
    original_review_reason_codes: list[str] = Field(default_factory=list)
    ocr_status: Literal["NO_TEXT"]
    ocr_result_object_hash: str = Field(pattern=_SHA256_PATTERN)
    semantic_ref_id: str | None = None
    semantic_ref_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_semantic_ref_pair(self) -> DirectSourceRealRunVisualAdjudication:
        if (self.semantic_ref_id is None) != (self.semantic_ref_object_hash is None):
            raise ValueError("visual adjudication semantic ref id/hash must be paired")
        return self


class DirectSourceRealRunVisualReuse(DirectSourceRealRunModel):
    book_manifest_id: str = Field(min_length=1)
    book_report_id: str = Field(min_length=1)
    visual_run_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    pdf_sha256: str = Field(pattern=_SHA256_PATTERN)
    page_count: int = Field(ge=1)
    image_page_count: int = Field(ge=0)
    placement_count: int = Field(ge=0)
    semantic_ref_count: int = Field(ge=0)
    coverage_status: Literal["COMPLETE"]
    quality_status: Literal["REVIEW_REQUIRED"]
    adjudications: list[DirectSourceRealRunVisualAdjudication] = Field(default_factory=list)
    non_decorative_placement_count: int = Field(ge=0)


class DirectSourceRealRunFingerprints(DirectSourceRealRunModel):
    pdf: DirectSourceRealRunPdfFingerprint
    docx: DirectSourceRealRunDocxFingerprint
    visual_reuse: DirectSourceRealRunVisualReuse


class DirectSourceRealRunPdfBatch(DirectSourceRealRunModel):
    batch_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    page_count: int = Field(ge=1)
    type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    boundary_basis: str = Field(min_length=1)
    boundary_requires_sol: bool

    @model_validator(mode="after")
    def validate_pdf_span(self) -> DirectSourceRealRunPdfBatch:
        if self.page_end < self.page_start:
            raise ValueError("pdf page_end must be >= page_start")
        expected = self.page_end - self.page_start + 1
        if self.page_count != expected:
            raise ValueError("pdf page_count is inconsistent with page span")
        return self


class DirectSourceRealRunDocxBatch(DirectSourceRealRunModel):
    article_index: int = Field(ge=1)
    title: str = Field(min_length=1)
    start_paragraph: int = Field(ge=1)
    end_paragraph: int = Field(ge=1)
    paragraph_count: int = Field(ge=1)
    paragraph_locator_scheme: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_docx_span(self) -> DirectSourceRealRunDocxBatch:
        if self.end_paragraph < self.start_paragraph:
            raise ValueError("docx end_paragraph must be >= start_paragraph")
        expected = self.end_paragraph - self.start_paragraph + 1
        if self.paragraph_count != expected:
            raise ValueError("docx paragraph_count is inconsistent with paragraph span")
        return self


class DirectSourceRealRunRemaining(DirectSourceRealRunModel):
    pdf: list[str] = Field(default_factory=list)
    docx: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_remaining_ids(self) -> DirectSourceRealRunRemaining:
        if len(self.pdf) != len(set(self.pdf)):
            raise ValueError("remaining pdf batch ids must be unique")
        if len(self.docx) != len(set(self.docx)):
            raise ValueError("remaining docx batch ids must be unique")
        return self


class DirectSourceRealRunManifest(DirectSourceRealRunModel):
    """Complete contract snapshot used by the real-run validator."""

    schema_version: str = Field(default="2.0", pattern=r"^2\.0$")
    version: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    freeze_status: str = Field(min_length=1)
    source_fingerprints: DirectSourceRealRunFingerprints
    pdf_batches: list[DirectSourceRealRunPdfBatch] = Field(min_length=1)
    docx_batches: list[DirectSourceRealRunDocxBatch] = Field(min_length=1)
    completed: list[str] = Field(default_factory=list)
    remaining: DirectSourceRealRunRemaining
    pdf_completed_pages: list[int] = Field(default_factory=list)
    docx_completed_sections: list[int] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_section: str | None = None
    skill_count_by_module: dict[str, int] = Field(default_factory=dict)
    skill_status_counts: dict[str, int] = Field(default_factory=dict)
    ready_count: int = Field(ge=0)
    needs_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_completed_sections(self) -> DirectSourceRealRunManifest:
        if len(self.completed) != len(set(self.completed)):
            raise ValueError("completed batch ids must be unique")
        if len(self.pdf_completed_pages) != len(set(self.pdf_completed_pages)):
            raise ValueError("pdf completed pages must be unique")
        if len(self.docx_completed_sections) != len(set(self.docx_completed_sections)):
            raise ValueError("docx completed sections must be unique")
        if any(page < 1 for page in self.pdf_completed_pages):
            raise ValueError("pdf completed page must be >=1")
        if any(section < 1 for section in self.docx_completed_sections):
            raise ValueError("docx completed section must be >=1")
        if any(item.strip() == "" for item in self.open_questions):
            raise ValueError("open question entries must be non-empty")
        return self


class DirectSourceRealRunScopeContract(DirectSourceRealRunModel):
    """Normalized projection used for explicit validation cache keys."""

    pdf_batch_count: int = Field(ge=0)
    docx_batch_count: int = Field(ge=0)
    pdf_source_hash: str = Field(pattern=_SHA256_PATTERN)
    docx_source_hash: str = Field(pattern=_SHA256_PATTERN)


class DirectSourceRealRunPlan(DirectSourceRealRunModel):
    run_id: str = Field(min_length=1)
    schema_version: str = Field(default="2.0", pattern=r"^2\.0$")
    status: str = Field(min_length=1)
    pdf_batch_count: int = Field(ge=0)
    docx_batch_count: int = Field(ge=0)
    completed_batch_count: int = Field(ge=0)
    remaining_pdf_batch_count: int = Field(ge=0)
    remaining_docx_batch_count: int = Field(ge=0)


class DirectSourceRealRunStatus(DirectSourceRealRunModel):
    """Deterministic service output for real-run contract validation."""

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    run_stage: str = Field(min_length=1)
    schema_version: str = Field(default="2.0", pattern=r"^2\.0$")
    contract_version: str = Field(min_length=1)
    source_contract_hash: str = Field(pattern=_SHA256_PATTERN)
    frozen_source_count: int = Field(ge=0)
    frozen_batch_count: int = Field(ge=0)
    contract_source_count: int = Field(ge=0)
    contract_batch_count: int = Field(ge=0)
    contract_matches: bool
    idempotent_replay: bool
    formal_committee_weight_allowed: bool = False


class DirectSourceRealRunImportPlan(DirectSourceRealRunModel):
    """Private-safe, completed-only import instruction for a prepared direct run."""

    schema_version: Literal["direct-source-real-run-import-plan-v1"]
    run_id: str = Field(min_length=1)
    init_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    total_batch_count: int = Field(ge=1)
    completed_only_batch_ids: list[str] = Field(default_factory=list)
    remaining_pdf_batch_ids: list[str] = Field(default_factory=list)
    remaining_docx_batch_ids: list[str] = Field(default_factory=list)
    completed_batch_count: int = Field(ge=0)
    remaining_pdf_batch_count: int = Field(ge=0)
    remaining_docx_batch_count: int = Field(ge=0)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_completed_only_partition(self) -> DirectSourceRealRunImportPlan:
        identifiers = (
            self.completed_only_batch_ids
            + self.remaining_pdf_batch_ids
            + self.remaining_docx_batch_ids
        )
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("real-run import plan batch ids must be unique")
        if self.completed_batch_count != len(self.completed_only_batch_ids):
            raise ValueError("completed batch count does not match completed-only ids")
        if self.remaining_pdf_batch_count != len(self.remaining_pdf_batch_ids):
            raise ValueError("remaining PDF batch count does not match ids")
        if self.remaining_docx_batch_count != len(self.remaining_docx_batch_ids):
            raise ValueError("remaining DOCX batch count does not match ids")
        if self.total_batch_count != len(identifiers):
            raise ValueError("real-run import plan does not cover every frozen batch")
        return self


__all__ = [
    "DirectSourceRealRunAuditedArticleBoundaryMarker",
    "DirectSourceRealRunAuditedEmptyUnit",
    "DirectSourceRealRunDocxBlockContract",
    "DirectSourceRealRunDocxBatch",
    "DirectSourceRealRunDocxFingerprint",
    "DirectSourceRealRunFingerprints",
    "DirectSourceRealRunImportPlan",
    "DirectSourceRealRunManifest",
    "DirectSourceRealRunModel",
    "DirectSourceRealRunPlan",
    "DirectSourceRealRunPdfBatch",
    "DirectSourceRealRunPdfFingerprint",
    "DirectSourceRealRunRemaining",
    "DirectSourceRealRunRowFingerprint",
    "DirectSourceRealRunScopeContract",
    "DirectSourceRealRunStatus",
    "DirectSourceRealRunVisualReuse",
    "DirectSourceRealRunVisualAdjudication",
    "DirectSourceRealRunZeroLengthFragment",
]
