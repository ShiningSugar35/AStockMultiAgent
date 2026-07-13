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


class DownloadedDocument(AStockModel):
    document: SourceDocument
    snapshot: SourceSnapshot


class DisclosureSyncReport(AStockModel):
    job_id: str
    search_batch_id: str
    discovered_count: int = Field(ge=0)
    downloaded: list[DownloadedDocument]
    skipped_count: int = Field(ge=0)
