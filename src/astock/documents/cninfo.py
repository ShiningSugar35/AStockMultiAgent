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
from astock.core.source_resilience import SourceCircuitBreaker, classify_source_error
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
        self.source_breaker = SourceCircuitBreaker(state)

    def search(self, request: DisclosureSearchRequest) -> DisclosureSearchBatch:
        capability = "disclosure.discover"
        if not self.source_breaker.claim_attempt(self.provider_id, capability):
            raise ProviderError(
                "CNINFO disclosure search circuit is open",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
            )
        try:
            batch = self._search_unchecked(request)
        except Exception as exc:
            self._record_source_failure(capability, exc)
            raise
        self.source_breaker.record_success(self.provider_id, capability)
        return batch

    def _search_unchecked(self, request: DisclosureSearchRequest) -> DisclosureSearchBatch:
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
        except ProviderError:
            # Transport/deterministic provider failures already consumed the single HTTP
            # retry layer. Do not hit the same source again with a heuristic identity.
            raise
        except (OSError, ValueError):
            # Local resolver/cache problems may still use the bounded legacy request hint.
            resolved_org_id = None
        request_org_id = resolved_org_id or cninfo_org_id(request.symbol, request.exchange)
        response, request_latency = self._request_with_org_id(request, request_org_id)
        latency_ms += request_latency
        snapshot = self._persist_response(response, source_id=f"{self.provider_id}:index")
        try:
            payload = json.loads(response.content)
            if not isinstance(payload, dict):
                raise TypeError("CNINFO disclosure index root must be an object")
            raw_announcements = payload.get("announcements") or []
            total_count = int(
                payload.get("totalAnnouncement")
                or payload.get("totalRecordNum")
                or len(raw_announcements)
            )
            total_count = max(total_count, len(raw_announcements))
            raw_total_pages = int(payload.get("totalpages") or 0)
            raw_has_more = payload.get("hasMore")
            if isinstance(raw_has_more, bool):
                has_more = raw_has_more
            elif isinstance(raw_has_more, str) and raw_has_more.casefold() in {"true", "false"}:
                has_more = raw_has_more.casefold() == "true"
            else:
                has_more = raw_total_pages > request.page_number
            minimum_pages = request.page_number + (1 if has_more else 0)
            total_pages = max(
                raw_total_pages,
                minimum_pages,
                request.page_number if total_count > 0 else 0,
            )
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
            has_more=has_more,
            raw_snapshot_id=snapshot.snapshot_id,
            resolution_snapshot_ids=resolution_snapshot_ids,
            provider_latency_ms=latency_ms,
        )

    def search_all(
        self,
        request: DisclosureSearchRequest,
        *,
        max_pages: int = 200,
    ) -> list[DisclosureSearchBatch]:
        capability = "disclosure.enumerate"
        if not self.source_breaker.claim_attempt(self.provider_id, capability):
            raise ProviderError(
                "CNINFO disclosure enumeration circuit is open",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
            )
        try:
            batches = self._search_all_unchecked(request, max_pages=max_pages)
        except Exception as exc:
            self._record_source_failure(capability, exc)
            raise
        self.source_breaker.record_success(self.provider_id, capability)
        return batches

    def _search_all_unchecked(
        self,
        request: DisclosureSearchRequest,
        *,
        max_pages: int = 200,
    ) -> list[DisclosureSearchBatch]:
        if max_pages < 1:
            raise ValueError("CNINFO max_pages must be positive")
        base = request.model_copy(update={"page_number": 1})
        batches = [self._search_unchecked(base)]
        expected_total = batches[0].total_count
        seen_ids = {item.announcement_id for item in batches[0].announcements}
        if len(seen_ids) != len(batches[0].announcements) or len(seen_ids) > expected_total:
            raise ProviderError(
                "CNINFO disclosure pagination returned duplicate or excess announcements",
                failure_class=FailureClass.DATA_QUALITY,
                details={"page_number": 1, "total_count": expected_total},
            )
        _assert_terminal_consistency(batches[0], len(seen_ids), expected_total)
        while batches[-1].has_more:
            next_page = len(batches) + 1
            if next_page > max_pages:
                raise ProviderError(
                    "CNINFO disclosure pagination exceeded the safety bound",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"max_pages": max_pages},
                )
            batch = self._search_unchecked(base.model_copy(update={"page_number": next_page}))
            if batch.total_count != expected_total:
                raise ProviderError(
                    "CNINFO disclosure pagination total_count changed between pages",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={
                        "page_number": next_page,
                        "expected_total": expected_total,
                        "observed_total": batch.total_count,
                    },
                )
            page_ids = [item.announcement_id for item in batch.announcements]
            page_set = set(page_ids)
            if len(page_set) != len(page_ids) or page_set & seen_ids:
                raise ProviderError(
                    "CNINFO disclosure pagination repeated a page or announcement across pages",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"page_number": next_page},
                )
            seen_ids.update(page_set)
            if len(seen_ids) > expected_total:
                raise ProviderError(
                    "CNINFO disclosure pagination exceeded reported total_count",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"page_number": next_page, "total_count": expected_total},
                )
            _assert_terminal_consistency(batch, len(seen_ids), expected_total)
            batches.append(batch)
        if len(seen_ids) != expected_total:
            raise ProviderError(
                "CNINFO disclosure pagination terminated before total_count was enumerated",
                failure_class=FailureClass.DATA_QUALITY,
                details={"expected_total": expected_total, "enumerated_total": len(seen_ids)},
            )
        return batches

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
            if not isinstance(payload, dict):
                raise TypeError("CNINFO organization discovery root must be an object")
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
                "pageSize": str(min(request.page_size, 30)),
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
        capability = "disclosure.document"
        if not self.source_breaker.claim_attempt(self.provider_id, capability):
            raise ProviderError(
                "CNINFO disclosure download circuit is open",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
            )
        try:
            downloaded = self._download_unchecked(announcement)
        except Exception as exc:
            self._record_source_failure(capability, exc)
            raise
        self.source_breaker.record_success(self.provider_id, capability)
        return downloaded

    def _download_unchecked(self, announcement: DisclosureAnnouncement) -> DownloadedDocument:
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

    def _record_source_failure(self, capability: str, error: BaseException) -> None:
        retry_after_seconds: int | None = None
        if isinstance(error, ProviderError):
            raw_retry_after = error.details.get("retry_after_seconds")
            if isinstance(raw_retry_after, (int, float)) and not isinstance(raw_retry_after, bool):
                retry_after_seconds = max(0, int(raw_retry_after))
        self.source_breaker.record_failure(
            self.provider_id,
            capability,
            classify_source_error(error),
            retry_after_seconds=retry_after_seconds,
        )

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
            retry_after = response.extensions.get("astock_retry_after_seconds")
            details = (
                {"retry_after_seconds": retry_after}
                if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
                else {}
            )
            raise ProviderError(
                "CNINFO rate limited the request",
                failure_class=FailureClass.RATE_LIMITED,
                retryable=False,
                details=details,
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


def _assert_terminal_consistency(
    batch: DisclosureSearchBatch,
    enumerated_total: int,
    expected_total: int,
) -> None:
    page_number = batch.request.page_number
    if batch.has_more and enumerated_total >= expected_total:
        raise ProviderError(
            "CNINFO disclosure pagination claims hasMore after total_count was exhausted",
            failure_class=FailureClass.DATA_QUALITY,
            details={"page_number": page_number, "total_count": expected_total},
        )
    if not batch.has_more and batch.total_pages > page_number:
        raise ProviderError(
            "CNINFO disclosure pagination terminal proof contradicts total_pages",
            failure_class=FailureClass.DATA_QUALITY,
            details={
                "page_number": page_number,
                "total_pages": batch.total_pages,
            },
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
