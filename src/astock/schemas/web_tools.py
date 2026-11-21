"""Invocation-scoped Web tools; host tools remain owned by the current host."""
from __future__ import annotations

import ipaddress
import re
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Availability = Literal["AVAILABLE", "UNAVAILABLE", "QUOTA_EXHAUSTED", "UNKNOWN"]
Operation = Literal["SEARCH", "FETCH"]


def public_web_url(value: str) -> str:
    """Reject local/credential-bearing URLs without contacting the target."""
    if re.search(r"[\x00-\x20\x7f]", value) or "\\" in value:
        raise ValueError("Web URL contains ambiguous characters")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"https", "http"} or not host:
        raise ValueError("Web URL must be public HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Web URL must not contain credentials")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Web URL uses a non-public service port")
    if "." not in host or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Web URL must not target a local name")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host):
            raise ValueError("Web URL contains an ambiguous numeric address") from None
    else:
        if not address.is_global:
            raise ValueError("Web URL must not target a private address")
    return value


class HostWebTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    availability: Availability = "UNKNOWN"
    tool_name: str | None = Field(default=None, min_length=1, max_length=160)
    billing: Literal["INCLUDED", "EXTRA", "UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def validate_tool(self) -> HostWebTool:
        if self.availability == "AVAILABLE" and not self.tool_name:
            raise ValueError("available host capability requires an observed tool name")
        return self


class HostWebCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["host-web-capabilities-v1"] = "host-web-capabilities-v1"
    host_id: str = Field(min_length=1, max_length=80)
    invocation_id: str = Field(min_length=1, max_length=160)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    search: HostWebTool = Field(default_factory=HostWebTool)
    fetch: HostWebTool = Field(default_factory=HostWebTool)

    @model_validator(mode="after")
    def validate_window(self) -> HostWebCapabilities:
        if self.expires_at <= self.observed_at:
            raise ValueError("host capability expiry must follow observation")
        return self

    def tool_for(self, operation: Operation) -> HostWebTool:
        return self.search if operation == "SEARCH" else self.fetch


class WebToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal["web-tool-request-v1"] = "web-tool-request-v1"
    request_id: str = Field(min_length=1, max_length=240)
    operation: Operation
    query: str | None = Field(default=None, min_length=1, max_length=240)
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    requested_capability: str = Field(min_length=1, max_length=128)
    evidence_scope: str = Field(min_length=1, max_length=240)
    expected_fact: str = Field(min_length=1, max_length=1000)
    fresh_after: AwareDatetime
    continuation_id: str | None = None
    task_id: str | None = None
    continuation_dispatch_id: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_request(self) -> WebToolRequest:
        if self.operation == "SEARCH":
            if not self.query or self.url is not None:
                raise ValueError("SEARCH requires query and no URL")
            if re.search(r"[\x00-\x1f\x7f]", self.query):
                raise ValueError("search query contains control characters")
        elif self.query is not None or not self.url:
            raise ValueError("FETCH requires URL and no query")
        else:
            public_web_url(self.url)
        if bool(self.continuation_id) != bool(self.task_id):
            raise ValueError("continuation and external task must be supplied together")
        if self.continuation_dispatch_id is not None and not self.continuation_id:
            raise ValueError("continuation dispatch requires a bound continuation and task")
        return self


class WebToolHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    title: str = Field(default="", max_length=2000)
    snippet: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=262144)

    @model_validator(mode="after")
    def validate_url(self) -> WebToolHit:
        public_web_url(self.url)
        return self


class HostWebResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["host-web-result-v1"] = "host-web-result-v1"
    operation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_id: str = Field(min_length=1, max_length=80)
    invocation_id: str = Field(min_length=1, max_length=160)
    tool_name: str = Field(min_length=1, max_length=160)
    status: Literal["SUCCEEDED", "UNAVAILABLE", "QUOTA_EXHAUSTED", "FAILED", "CANCELLED"]
    observed_at: AwareDatetime
    hits: list[WebToolHit] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_status(self) -> HostWebResult:
        if self.status != "SUCCEEDED" and self.hits:
            raise ValueError("failed host operation cannot contain successful evidence")
        return self


class WebOfficialDocumentSelection(BaseModel):
    """Metadata taken from the selected original; never inferred from a search snippet."""

    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_id: str | None = Field(default=None, min_length=1, max_length=500)
    url: str
    title: str = Field(min_length=1, max_length=2000)
    published_at: AwareDatetime
    document_type: Literal[
        "ANNUAL_REPORT", "SEMIANNUAL_REPORT", "QUARTERLY_REPORT", "ANNOUNCEMENT"
    ]
    period_end: date | None = None
    disclosure_id: str | None = None

    @model_validator(mode="after")
    def valid_original(self) -> WebOfficialDocumentSelection:
        public_web_url(self.url)
        if self.document_type != "ANNOUNCEMENT" and self.period_end is None:
            raise ValueError("Financial original requires its reporting period")
        return self


class CurrentResearchWebBatch(BaseModel):
    """One host invocation's bounded callbacks for an existing continuation."""

    model_config = ConfigDict(extra="forbid")
    continuation_id: str = Field(min_length=1, max_length=300)
    host_capabilities: HostWebCapabilities | None = None
    host_results: list[HostWebResult] = Field(default_factory=list, max_length=16)
    originals: list[WebOfficialDocumentSelection] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_operations(self) -> CurrentResearchWebBatch:
        for items in (self.host_results, self.originals):
            identifiers = [item.operation_id for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("a Web batch cannot repeat an operation")
        return self


class WebToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["web-tool-policy-v1"] = "web-tool-policy-v1"
    host_capability_max_age_seconds: int = Field(ge=30, le=86400)
    max_results: int = Field(ge=1, le=50)
    max_response_bytes: int = Field(ge=4096, le=4194304)
    max_document_bytes: int = Field(default=33554432, ge=1024, le=67108864)
    max_document_pages: int = Field(default=600, ge=1, le=2000)
    max_document_redirects: int = Field(default=3, ge=0, le=5)
    timeout_seconds: int = Field(ge=1, le=120)
    deepseek_enabled: bool
    max_paid_per_request: int = Field(ge=0, le=100)
    max_paid_per_day: int = Field(ge=0, le=10000)
    max_paid_per_month: int = Field(ge=0, le=100000)
    max_paid_concurrency: int = Field(ge=1, le=4)
    max_owned_concurrency: int = Field(default=4, ge=1, le=8)
    max_owned_per_provider: int = Field(default=2, ge=1, le=4)
    max_owned_per_domain: int = Field(default=2, ge=1, le=4)
    shared_search_max_age_seconds: int = Field(default=300, ge=0, le=3600)
    shared_pdf_max_age_seconds: int = Field(default=3600, ge=0, le=86400)
    min_paid_interval_seconds: float = Field(ge=0.5, le=60)
    paid_request_price_cny: str = Field(pattern=r"^[0-9]+(?:[.][0-9]+)?$")
