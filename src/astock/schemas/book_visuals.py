"""Private-book image placement, OCR, layout, and semantic-lineage contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel

Sha256 = str
BBox = tuple[float, float, float, float]


class BookVisualRunStage(StrEnum):
    INPUT_FROZEN = "INPUT_FROZEN"
    LAYOUT_ENUMERATED = "LAYOUT_ENUMERATED"
    OCR_COMPLETED = "OCR_COMPLETED"
    CHARTS_CLASSIFIED = "CHARTS_CLASSIFIED"
    SEMANTIC_MATERIALIZED = "SEMANTIC_MATERIALIZED"
    AUDITED = "AUDITED"
    FAILED = "FAILED"


class BookVisualCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class BookVisualQualityStatus(StrEnum):
    PASS = "PASS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class ImageExtractionMode(StrEnum):
    XREF_ORIGINAL = "XREF_ORIGINAL"
    BBOX_CLIP_300_DPI = "BBOX_CLIP_300_DPI"


class ImageExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ImageOcrStatus(StrEnum):
    SUCCESS = "SUCCESS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NO_TEXT = "NO_TEXT"
    FAILED = "FAILED"


class ChartUnitType(StrEnum):
    CHART = "CHART"
    TABLE = "TABLE"
    DIAGRAM = "DIAGRAM"
    TEXT_IMAGE = "TEXT_IMAGE"
    DECORATIVE = "DECORATIVE"
    UNKNOWN = "UNKNOWN"


class BookLayoutAtomKind(StrEnum):
    TEXT_BLOCK = "TEXT_BLOCK"
    IMAGE_EVIDENCE = "IMAGE_EVIDENCE"


class BookVisualDistillationConfig(AStockModel):
    pipeline_version: str = Field(min_length=1)
    layout_version: str = Field(min_length=1)
    classification_version: str = Field(min_length=1)
    clip_fallback_dpi: int = 300
    low_confidence_threshold: float = Field(gt=0.0, le=1.0)
    minimum_visible_ocr_chars: int = 4
    caption_margin_points: float = Field(gt=0.0)
    cover_minimum_area_ratio: float = Field(gt=0.0, le=1.0)
    decorative_maximum_area_ratio: float = Field(gt=0.0, le=0.01)
    chart_terms: list[str] = Field(min_length=1)
    table_terms: list[str] = Field(min_length=1)
    diagram_terms: list[str] = Field(min_length=1)
    method_signal_terms: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frozen_rules(self) -> BookVisualDistillationConfig:
        if self.clip_fallback_dpi != 300:
            raise ValueError("book visual clip fallback must remain exactly 300 dpi")
        if self.minimum_visible_ocr_chars != 4:
            raise ValueError("book visual no-text threshold must remain exactly four characters")
        if self.decorative_maximum_area_ratio != 0.01:
            raise ValueError("small decorative exclusion threshold must remain exactly one percent")
        for terms in (
            self.chart_terms,
            self.table_terms,
            self.diagram_terms,
            self.method_signal_terms,
        ):
            folded = [term.casefold().strip() for term in terms]
            if any(not term for term in folded) or len(folded) != len(set(folded)):
                raise ValueError("book visual classification terms must be nonempty and unique")
        return self


class BookVisualPlan(AStockModel):
    source_manifest_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_pages: int = Field(ge=0)
    image_pages: int = Field(ge=0)
    image_placements: int = Field(ge=0)
    input_hashes: list[str] = Field(min_length=1)


class BookVisualRun(AStockModel):
    run_id: str = Field(min_length=1)
    source_manifest_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    raw_object_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_version: str = Field(min_length=1)
    layout_version: str = Field(min_length=1)
    classification_version: str = Field(min_length=1)
    stage: BookVisualRunStage
    input_hashes: list[str] = Field(min_length=1)
    source_page_count: int = Field(ge=0)
    image_page_count: int = Field(ge=0)
    image_placement_count: int = Field(ge=0)
    processed_placement_count: int = Field(ge=0)
    semantic_run_id: str | None = None
    coverage_report_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    run_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_run(self) -> BookVisualRun:
        if len(self.input_hashes) != len(set(self.input_hashes)):
            raise ValueError("book visual input hashes must be unique")
        if self.image_page_count > self.source_page_count:
            raise ValueError("image pages cannot exceed source pages")
        if self.processed_placement_count > self.image_placement_count:
            raise ValueError("processed placements cannot exceed enumerated placements")
        if self.stage in {
            BookVisualRunStage.SEMANTIC_MATERIALIZED,
            BookVisualRunStage.AUDITED,
        } and self.semantic_run_id is None:
            raise ValueError("semantic stages require a semantic run id")
        if self.stage is BookVisualRunStage.AUDITED:
            if self.coverage_report_object_sha256 is None or self.finished_at is None:
                raise ValueError("audited visual runs require a report and finished_at")
        elif self.finished_at is not None and self.stage is not BookVisualRunStage.FAILED:
            raise ValueError("only terminal visual runs may carry finished_at")
        return self


class ImageEvidenceAttempt(AStockModel):
    attempt_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    attempt_ordinal: int = Field(ge=1)
    extraction_mode: ImageExtractionMode
    status: ImageExtractionStatus
    image_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    error_code: str | None = None
    attempt_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_attempt(self) -> ImageEvidenceAttempt:
        if self.status is ImageExtractionStatus.SUCCESS:
            if self.image_object_sha256 is None or self.error_code is not None:
                raise ValueError("successful extraction requires only an image object")
        elif self.image_object_sha256 is not None or not self.error_code:
            raise ValueError("failed extraction requires only a stable error code")
        return self


class ImageEvidence(AStockModel):
    evidence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    placement_index: int = Field(ge=1)
    placement_ordinal: int = Field(ge=1)
    xref: int | None = Field(default=None, ge=1)
    bbox: BBox
    page_width: float = Field(gt=0.0)
    page_height: float = Field(gt=0.0)
    attempt_ids: list[str] = Field(min_length=1)
    image_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    duplicate_of_evidence_id: str | None = None
    evidence_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> ImageEvidence:
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError("image evidence bbox must have positive area")
        if len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("image evidence attempt ids must be unique")
        if self.duplicate_of_evidence_id == self.evidence_id:
            raise ValueError("image evidence cannot duplicate itself")
        return self


class ImageOcrResult(AStockModel):
    evidence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: ImageOcrStatus
    text_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    average_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    engine_name: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    result_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_ocr(self) -> ImageOcrResult:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("OCR reason codes must be unique")
        if self.status is ImageOcrStatus.FAILED:
            if self.text_object_sha256 is not None or self.average_confidence is not None:
                raise ValueError("failed OCR cannot claim text or confidence")
        elif self.text_object_sha256 is None:
            raise ValueError("completed OCR attempts require an immutable text object")
        if (
            self.status is ImageOcrStatus.LOW_CONFIDENCE
            and self.average_confidence is None
        ):
            raise ValueError("low-confidence OCR requires a confidence value")
        return self


class BookLayoutAtom(AStockModel):
    atom_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    page_ordinal: int = Field(ge=1)
    global_ordinal: int = Field(ge=1)
    atom_kind: BookLayoutAtomKind
    bbox: BBox
    text_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_id: str | None = None
    atom_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_atom(self) -> BookLayoutAtom:
        x0, y0, x1, y1 = self.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError("layout bbox must have positive area")
        if self.atom_kind is BookLayoutAtomKind.TEXT_BLOCK:
            if self.text_object_sha256 is None or self.evidence_id is not None:
                raise ValueError("text atoms require only a text object")
        elif self.evidence_id is None or self.text_object_sha256 is not None:
            raise ValueError("image atoms require only image evidence")
        return self


class ChartUnit(AStockModel):
    chart_unit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    chart_type: ChartUnitType
    classification_confidence: float = Field(ge=0.0, le=1.0)
    decorative_excluded: bool
    caption_present: bool
    review_reason_codes: list[str]
    unit_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_classification(self) -> ChartUnit:
        if self.decorative_excluded != (self.chart_type is ChartUnitType.DECORATIVE):
            raise ValueError("only strict decorative units may be automatically excluded")
        if len(self.review_reason_codes) != len(set(self.review_reason_codes)):
            raise ValueError("chart review reasons must be unique")
        if (
            self.chart_type is ChartUnitType.UNKNOWN
            and "UNKNOWN_CLASSIFICATION" not in self.review_reason_codes
        ):
            raise ValueError("unknown visuals must remain explicitly review-required")
        return self


class BookVisualSemanticRef(AStockModel):
    ref_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    chart_unit_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    paragraph_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    relation_ids: list[str]
    ref_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_relations(self) -> BookVisualSemanticRef:
        if len(self.relation_ids) != len(set(self.relation_ids)):
            raise ValueError("book visual semantic relation ids must be unique")
        return self


class BookVisualCoverageReport(AStockModel):
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    coverage_status: BookVisualCoverageStatus
    quality_status: BookVisualQualityStatus
    source_pages: int = Field(ge=0)
    image_pages: int = Field(ge=0)
    image_placements: int = Field(ge=0)
    processed_placements: int = Field(ge=0)
    ocr_failed: int = Field(ge=0)
    low_confidence: int = Field(ge=0)
    no_text: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    classification_counts: dict[ChartUnitType, int]
    affected_argument_unit_count: int = Field(ge=0)
    image_only_ready_candidate_count: int = Field(ge=0)
    report_object_sha256: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_report(self) -> BookVisualCoverageReport:
        if set(self.classification_counts) != set(ChartUnitType):
            raise ValueError("coverage report must include every visual classification")
        if any(count < 0 for count in self.classification_counts.values()):
            raise ValueError("classification counts cannot be negative")
        if sum(self.classification_counts.values()) != self.processed_placements:
            raise ValueError("classification counts must partition processed placements")
        if self.processed_placements > self.image_placements:
            raise ValueError("processed placements cannot exceed source placements")
        if self.coverage_status is BookVisualCoverageStatus.COMPLETE and (
            self.processed_placements != self.image_placements
        ):
            raise ValueError("complete coverage requires every placement")
        return self


__all__ = [
    "BookLayoutAtom",
    "BookLayoutAtomKind",
    "BookVisualCoverageReport",
    "BookVisualCoverageStatus",
    "BookVisualDistillationConfig",
    "BookVisualPlan",
    "BookVisualQualityStatus",
    "BookVisualRun",
    "BookVisualRunStage",
    "BookVisualSemanticRef",
    "ChartUnit",
    "ChartUnitType",
    "ImageEvidence",
    "ImageEvidenceAttempt",
    "ImageExtractionMode",
    "ImageExtractionStatus",
    "ImageOcrResult",
    "ImageOcrStatus",
]
