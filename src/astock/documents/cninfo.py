"""Low-frequency CNINFO official disclosure search and PDF download."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.identity import OfficialIdentityResolver
from astock.providers.runtime import build_provider_http_client
from astock.schemas import (
    DisclosureAnnouncement,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchBatch,
    DisclosureSearchRequest,
    DocumentType,
    DownloadedDocument,
    FetchStatus,
    SourceDocument,
    SourceSnapshot,
)

_TITLE_TAG = re.compile(r"<[^>]+>")
_CATEGORY_CODES = {
    DisclosureCategory.ANNUAL_REPORT: "category_ndbg_szsh;",
    DisclosureCategory.SEMIANNUAL_REPORT: "category_bndbg_szsh;",
    DisclosureCategory.QUARTERLY_REPORT: "category_yjdbg_szsh;category_sjdbg_szsh;",
    DisclosureCategory.ALL: "",
}
_DOCUMENT_TYPES = {
    DisclosureCategory.ANNUAL_REPORT: DocumentType.ANNUAL_REPORT,
    DisclosureCategory.SEMIANNUAL_REPORT: DocumentType.SEMIANNUAL_REPORT,
    DisclosureCategory.QUARTERLY_REPORT: DocumentType.QUARTERLY_REPORT,
    DisclosureCategory.ALL: DocumentType.ANNOUNCEMENT,
}


class CninfoDisclosureProvider:
    provider_id = "cninfo-disclosures"
    search_endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    download_origin = "https://static.cninfo.com.cn"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        maximum_pdf_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.client = client or build_provider_http_client(self.provider_id)
        self.identity_resolver = OfficialIdentityResolver(state, self.provider_id)

    def search(self, request: DisclosureSearchRequest) -> DisclosureSearchBatch:
        resolution_snapshot_ids: list[str] = []
        latency_ms = 0
        resolved_org_id: str | None = None
        try:
            resolution, discovery_latency = self.identity_resolver.resolve(
                request.symbol,
                request.exchange,
                lambda: self._discover_org_id(request),
            )
            latency_ms += discovery_latency
            resolved_org_id = resolution.external_id
            if resolution.discovery_snapshot_id:
                resolution_snapshot_ids.append(resolution.discovery_snapshot_id)
        except (ProviderError, OSError, ValueError):
            # Discovery is preferred, but a legacy id remains a bounded request hint.
            resolved_org_id = None
        request_org_id = resolved_org_id or cninfo_org_id(request.symbol, request.exchange)
        response, request_latency = self._request_with_org_id(request, request_org_id)
        latency_ms += request_latency
        snapshot = self._persist_response(response, source_id=f"{self.provider_id}:index")
        try:
            payload = json.loads(response.content)
            raw_announcements = payload.get("announcements") or []
            total_count = int(payload.get("totalAnnouncement") or 0)
            total_pages = int(payload.get("totalpages") or 0)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ProviderError(
                "CNINFO returned an invalid disclosure index",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            ) from exc
        if not isinstance(raw_announcements, list):
            raise ProviderError(
                "CNINFO announcements field is not a list",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        announcements = [self._parse_announcement(item, request) for item in raw_announcements]
        batch_id = content_hash(
            {
                "provider_id": self.provider_id,
                "request": request,
                "announcement_ids": [item.announcement_id for item in announcements],
                "resolution_snapshot_ids": resolution_snapshot_ids,
            }
        )
        return DisclosureSearchBatch(
            batch_id=batch_id,
            provider_id=self.provider_id,
            request=request,
            announcements=announcements,
            total_count=total_count,
            total_pages=total_pages,
            raw_snapshot_id=snapshot.snapshot_id,
            resolution_snapshot_ids=resolution_snapshot_ids,
            provider_latency_ms=latency_ms,
        )

    def _discover_org_id(
        self,
        request: DisclosureSearchRequest,
    ) -> tuple[str | None, SourceSnapshot, int]:
        response, latency_ms = self._request(
            "POST",
            self.search_endpoint,
            data={
                "pageNum": "1",
                "pageSize": "30",
                "column": _column_for_exchange(request.exchange),
                "tabName": "fulltext",
                "plate": "",
                "stock": "",
                "searchkey": request.symbol,
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
        )
        snapshot = self._persist_response(
            response,
            source_id=f"{self.provider_id}:org-discovery",
        )
        try:
            payload = json.loads(response.content)
            rows = payload.get("announcements") or []
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProviderError(
                "CNINFO returned an invalid organization discovery index",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            ) from exc
        if not isinstance(rows, list):
            raise ProviderError(
                "CNINFO organization discovery rows are malformed",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        org_ids = {
            str(item.get("orgId"))
            for item in rows
            if isinstance(item, dict)
            and str(item.get("secCode")) == request.symbol
            and item.get("orgId")
        }
        if len(org_ids) > 1:
            raise ProviderError(
                "CNINFO organization discovery returned conflicting identities",
                failure_class=FailureClass.CONFLICT,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        return (next(iter(org_ids)) if org_ids else None), snapshot, latency_ms

    def _request_with_org_id(
        self,
        request: DisclosureSearchRequest,
        org_id: str,
    ) -> tuple[httpx.Response, int]:
        return self._request(
            "POST",
            self.search_endpoint,
            data={
                "pageNum": str(request.page_number),
                "pageSize": str(request.page_size),
                "column": _column_for_exchange(request.exchange),
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{request.symbol},{org_id}",
                "searchkey": request.keyword,
                "secid": "",
                "category": _CATEGORY_CODES[request.category],
                "trade": "",
                "seDate": f"{request.start_date.isoformat()}~{request.end_date.isoformat()}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
        )

    def download(self, announcement: DisclosureAnnouncement) -> DownloadedDocument:
        self._validate_download_url(announcement.source_url)
        response, _ = self._request("GET", announcement.source_url)
        if len(response.content) > self.maximum_pdf_bytes:
            raise ProviderError(
                "CNINFO PDF exceeds the configured size limit",
                failure_class=FailureClass.DATA_QUALITY,
                details={"byte_size": len(response.content)},
            )
        snapshot = self._persist_response(
            response,
            source_id=f"{self.provider_id}:document:{announcement.announcement_id}",
        )
        if not response.content.lstrip().startswith(b"%PDF-"):
            raise ProviderError(
                "CNINFO download is not a PDF",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        document = SourceDocument(
            document_id=announcement.document_id,
            title=announcement.title,
            publisher="CNINFO",
            document_type=announcement.document_type,
            company_ids=[announcement.symbol],
            published_at=announcement.published_at,
            effective_at=announcement.published_at,
            disclosure_id=announcement.announcement_id,
            source_url=announcement.source_url,
            rights_status="PUBLIC_DISCLOSURE",
        )
        return DownloadedDocument(document=document, snapshot=snapshot)

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
    ) -> tuple[httpx.Response, int]:
        started = time.perf_counter()
        try:
            response = self.client.request(method, url, data=data)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "CNINFO request timed out",
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "CNINFO network request failed",
                failure_class=FailureClass.NETWORK,
                retryable=True,
                details={"error": str(exc)},
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code == 429:
            raise ProviderError(
                "CNINFO rate limited the request",
                failure_class=FailureClass.RATE_LIMITED,
                retryable=False,
            )
        if response.status_code in {401, 403}:
            raise ProviderError(
                "CNINFO denied access",
                failure_class=FailureClass.ACCESS_RESTRICTED,
                retryable=False,
            )
        if response.status_code >= 500:
            raise ProviderError(
                f"CNINFO server error {response.status_code}",
                failure_class=FailureClass.NETWORK,
                retryable=True,
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"CNINFO HTTP error {response.status_code}",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        return response, latency_ms

    def _persist_response(self, response: httpx.Response, *, source_id: str) -> SourceSnapshot:
        now = datetime.now(UTC)
        object_ref = self.object_store.put_bytes(response.content)
        snapshot = SourceSnapshot(
            snapshot_id=f"{source_id}:{object_ref.sha256}",
            source_id=source_id,
            object_sha256=object_ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=str(response.request.url),
            mime=response.headers.get("content-type", "application/octet-stream").split(";")[0],
            byte_size=object_ref.byte_size,
            headers_hash=content_hash(sorted(response.headers.items())),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_DISCLOSURE",
        )
        self.state.register_snapshot(snapshot)
        return snapshot

    def _parse_announcement(
        self,
        item: object,
        request: DisclosureSearchRequest,
    ) -> DisclosureAnnouncement:
        if not isinstance(item, dict):
            raise ProviderError(
                "CNINFO announcement is not an object",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        try:
            announcement_id = str(item["announcementId"])
            adjunct_path = str(item["adjunctUrl"]).lstrip("/")
            source_url = f"{self.download_origin}/{adjunct_path}"
            self._validate_download_url(source_url)
            timestamp = datetime.fromtimestamp(int(item["announcementTime"]) / 1000, tz=UTC)
            title = html.unescape(_TITLE_TAG.sub("", str(item["announcementTitle"]))).strip()
            symbol = str(item["secCode"])
            company_name = str(item["secName"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                "CNINFO announcement is missing required fields",
                failure_class=FailureClass.INVALID_RESPONSE,
            ) from exc
        return DisclosureAnnouncement(
            announcement_id=announcement_id,
            document_id=f"cninfo:{announcement_id}",
            symbol=symbol,
            company_name=company_name,
            title=title,
            published_at=timestamp,
            adjunct_path=adjunct_path,
            source_url=source_url,
            document_type=_DOCUMENT_TYPES[request.category],
            org_id=str(item["orgId"]) if item.get("orgId") else None,
        )

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "static.cninfo.com.cn"
            or ".." in parsed.path.split("/")
            or not parsed.path.lower().endswith(".pdf")
        ):
            raise ProviderError(
                "Refusing a non-official CNINFO PDF URL",
                failure_class=FailureClass.POLICY_REJECTED,
                details={"url": url},
            )


def cninfo_org_id(symbol: str, exchange: DisclosureExchange) -> str:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("CNINFO symbol must be six digits")
    prefix = "gssz" if exchange is DisclosureExchange.SZSE else "gssh"
    return f"{prefix}0{symbol}"


def _column_for_exchange(exchange: DisclosureExchange) -> str:
    return "sse" if exchange is DisclosureExchange.SSE else "szse"


def _announcement_count(raw: bytes) -> int:
    try:
        payload = json.loads(raw)
        return int(payload.get("totalAnnouncement") or 0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return 0
