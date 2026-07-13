"""Official-disclosure search, download, and source-document contracts."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.evidence import SourceSnapshot


class DisclosureExchange(StrEnum):
    SZSE = "SZSE"
    SSE = "SSE"


class DisclosureCategory(StrEnum):
    ANNUAL_REPORT = "ANNUAL_REPORT"
    SEMIANNUAL_REPORT = "SEMIANNUAL_REPORT"
    QUARTERLY_REPORT = "QUARTERLY_REPORT"
    ALL = "ALL"


class DocumentType(StrEnum):
    ANNUAL_REPORT = "ANNUAL_REPORT"
    SEMIANNUAL_REPORT = "SEMIANNUAL_REPORT"
    QUARTERLY_REPORT = "QUARTERLY_REPORT"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    PRIVATE_BOOK = "PRIVATE_BOOK"
    PRIVATE_PDF = "PRIVATE_PDF"


class PageExtractionMethod(StrEnum):
    NATIVE_TEXT = "NATIVE_TEXT"
    OCR = "OCR"
    EMPTY = "EMPTY"
    OCR_FAILED = "OCR_FAILED"


class ParseStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DisclosureSearchRequest(AStockModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    exchange: DisclosureExchange
    start_date: date
    end_date: date
    category: DisclosureCategory = DisclosureCategory.ALL
    keyword: str = ""
    page_number: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self) -> DisclosureSearchRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class DisclosureAnnouncement(AStockModel):
    announcement_id: str
    document_id: str
    symbol: str = Field(pattern=r"^\d{6}$")
    company_name: str
    title: str
    published_at: AwareDatetime
    adjunct_path: str
    source_url: str
    document_type: DocumentType
    org_id: str


class DisclosureSearchBatch(AStockModel):
    batch_id: str
    provider_id: str
    request: DisclosureSearchRequest
    announcements: list[DisclosureAnnouncement]
    total_count: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    raw_snapshot_id: str
    provider_latency_ms: int = Field(ge=0)


class SourceDocument(AStockModel):
    document_id: str
    title: str
    publisher: str
    document_type: DocumentType
    company_ids: list[str]
    published_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    disclosure_id: str
    source_url: str
    rights_status: str


class DocumentPage(AStockModel):
    page_id: str
    document_id: str
    snapshot_id: str
    page_number: int = Field(ge=1)
    width_points: float = Field(gt=0)
    height_points: float = Field(gt=0)
    native_text_char_count: int = Field(ge=0)
    text_char_count: int = Field(ge=0)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_method: PageExtractionMethod
    ocr_applied: bool
    ocr_engine: str | None = None
    ocr_engine_version: str | None = None
    ocr_average_confidence: float | None = Field(default=None, ge=0, le=1)
    page_image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parser_name: str
    parser_version: str
    section_path: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DocumentParseReport(AStockModel):
    parse_run_id: str
    document_id: str
    snapshot_id: str
    parser_name: str
    parser_version: str
    source_page_count: int = Field(ge=0)
    requested_pages: list[int]
    processed_page_count: int = Field(ge=0)
    native_page_count: int = Field(ge=0)
    ocr_page_count: int = Field(ge=0)
    empty_page_count: int = Field(ge=0)
    failed_page_count: int = Field(ge=0)
    total_text_char_count: int = Field(ge=0)
    page_ids: list[str]
    parse_status: ParseStatus
    report_object_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DownloadedDocument(AStockModel):
    document: SourceDocument
    snapshot: SourceSnapshot


class DisclosureSyncReport(AStockModel):
    job_id: str
    search_batch_id: str
    discovered_count: int = Field(ge=0)
    downloaded: list[DownloadedDocument]
    skipped_count: int = Field(ge=0)
