"""Typed contracts for browser-captured community search evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class SocialPlatform(StrEnum):
    XUEQIU = "XUEQIU"
    ZHIHU = "ZHIHU"


class SocialQueryKind(StrEnum):
    COMPANY = "COMPANY"
    SYMBOL = "SYMBOL"
    INDUSTRY = "INDUSTRY"
    MANAGEMENT = "MANAGEMENT"


class SocialCaptureStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class SocialParseStatus(StrEnum):
    PARSED = "PARSED"
    EMPTY = "EMPTY"
    UNPARSED = "UNPARSED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    ERROR = "ERROR"


class SocialSearchQuery(AStockModel):
    query_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=240)
    kind: SocialQueryKind
    search_url: str = Field(min_length=1, max_length=2048)


class SocialSearchRequest(AStockModel):
    symbol: str = Field(default="", pattern=r"^(?:\d{6})?$")
    company_id: str = Field(default="", max_length=80)
    company_names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    industry_keywords: tuple[str, ...] = ()
    management_keywords: tuple[str, ...] = ()
    start: AwareDatetime
    end: AwareDatetime
    max_queries_per_platform: int = Field(default=4, ge=1, le=8)

    @model_validator(mode="after")
    def validate_window_and_terms(self) -> SocialSearchRequest:
        if self.start >= self.end:
            raise ValueError("social search interval must be ordered")
        for values in (
            self.company_names,
            self.aliases,
            self.industry_keywords,
            self.management_keywords,
        ):
            if len(values) > 32 or any(not item.strip() or len(item) > 120 for item in values):
                raise ValueError("social search terms must be non-empty and bounded")
        if len(set(self.company_names)) != len(self.company_names):
            raise ValueError("social company names must be unique")
        return self


class SocialSearchJob(AStockModel):
    job_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    platform: SocialPlatform
    queries: tuple[SocialSearchQuery, ...] = Field(min_length=1, max_length=8)
    start: AwareDatetime
    end: AwareDatetime
    max_scrolls: int = Field(default=4, ge=0, le=10)
    max_items_per_query: int = Field(default=80, ge=1, le=200)


class SocialCapturedItem(AStockModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=1200)
    text: str = Field(default="", max_length=16000)
    author: str = Field(default="", max_length=300)
    published_raw: str = Field(default="", max_length=160)
    published_at: str = Field(default="", max_length=100)
    is_original: bool | None = None
    is_video: bool | None = None
    content_type: str = Field(default="UNKNOWN", max_length=80)
    matched_query: str = Field(default="", max_length=240)
    raw_excerpt: str = Field(default="", max_length=12000)


class SocialQueryCapture(AStockModel):
    query_id: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=240)
    query_kind: SocialQueryKind
    page_url: str = Field(min_length=1, max_length=2048)
    page_title: str = Field(default="", max_length=500)
    captured_at: AwareDatetime
    selector_version: str = Field(min_length=1, max_length=40)
    parse_status: SocialParseStatus
    no_results_detected: bool = False
    items: tuple[SocialCapturedItem, ...] = Field(default=(), max_length=200)
    has_more: bool = False
    scroll_rounds: int = Field(default=0, ge=0, le=10)
    error_code: str = Field(default="", max_length=120)


class SocialCaptureResult(AStockModel):
    job_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    platform: SocialPlatform
    captured_at: AwareDatetime
    extension_version: str = Field(default="", max_length=40)
    query_results: tuple[SocialQueryCapture, ...] = Field(default=(), max_length=8)
    error_code: str = Field(default="", max_length=120)


class SocialLead(AStockModel):
    lead_id: str = Field(min_length=1, max_length=180)
    platform: SocialPlatform
    symbol: str = Field(default="", pattern=r"^(?:\d{6})?$")
    company_id: str = Field(default="", max_length=80)
    company_name: str = Field(default="", max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=1200)
    text: str = Field(default="", max_length=12000)
    author: str = Field(default="", max_length=300)
    published_at: AwareDatetime
    published_precision: Literal["INSTANT", "DAY", "RELATIVE"] = "INSTANT"
    published_raw: str = Field(default="", max_length=160)
    captured_at: AwareDatetime
    matched_queries: tuple[str, ...] = ()
    query_kinds: tuple[SocialQueryKind, ...] = ()
    content_type: str = Field(default="UNKNOWN", max_length=80)
    is_original: bool | None = None
    is_video: bool | None = None
    sentiment_hint: Literal["POSITIVE_HINT", "NEGATIVE_HINT", "MIXED_OR_NEUTRAL"] = (
        "MIXED_OR_NEUTRAL"
    )
    sentiment_terms: tuple[str, ...] = ()
    snapshot_id: str = Field(min_length=1, max_length=240)


class SocialPlatformCoverage(AStockModel):
    platform: SocialPlatform
    start: AwareDatetime
    end: AwareDatetime
    query_count: int = Field(ge=0)
    completed_query_count: int = Field(ge=0)
    raw_item_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    filtered_time_count: int = Field(ge=0)
    undated_count: int = Field(default=0, ge=0)
    filtered_content_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    complete: bool
    reason: str = Field(default="", max_length=240)
    checked_through: AwareDatetime | None = None
    snapshot_ids: tuple[str, ...] = ()


class SocialSearchReceipt(AStockModel):
    schema_version: str = Field(
        default="social-search-receipt-v1", pattern=r"^social-search-receipt-v1$"
    )
    receipt_id: str = Field(min_length=1, max_length=240)
    request: SocialSearchRequest
    requested_at: AwareDatetime
    completed_at: AwareDatetime
    status: SocialCaptureStatus
    coverage: tuple[SocialPlatformCoverage, ...]
    lead_ids: tuple[str, ...]
    snapshot_ids: tuple[str, ...]
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SocialSearchResult(AStockModel):
    schema_version: str = Field(
        default="social-search-result-v1", pattern=r"^social-search-result-v1$"
    )
    result_artifact_id: str = Field(min_length=1, max_length=240)
    receipt_id: str = Field(min_length=1, max_length=240)
    snapshot_id: str = Field(min_length=1, max_length=240)
    snapshot_ids: tuple[str, ...]
    leads: tuple[SocialLead, ...]
    coverage: tuple[SocialPlatformCoverage, ...]
    complete: bool
