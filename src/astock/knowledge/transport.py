"""Low-frequency Zhihu response transport that persists bytes before classification."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import FetchStatus, SourceSnapshot, ZhihuContentType, ZhihuTransport


@dataclass(frozen=True, slots=True)
class PersistedZhihuResponse:
    requested_url: str
    status_code: int
    content_type: str
    body: bytes
    snapshot: SourceSnapshot
    transport: ZhihuTransport
    latency_ms: int


class ZhihuResponseTransport(Protocol):
    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse: ...


class ZhihuHttpTransport:
    provider_id = "zhihu-python-http"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            },
        )

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        url = normalize_zhihu_api_url(url)
        started = time.perf_counter()
        try:
            response = self.client.get(
                url,
                headers={"Referer": _profile_referer(author_source_id)},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Zhihu request timed out",
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Zhihu network request failed",
                failure_class=FailureClass.NETWORK,
                retryable=True,
                details={"error_class": type(exc).__name__},
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        return self.persist_imported_response(
            author_source_id=author_source_id,
            content_type=content_type,
            requested_url=str(response.request.url),
            status_code=response.status_code,
            content_type_header=response.headers.get("content-type", "application/octet-stream"),
            body=response.content,
            transport=ZhihuTransport.PYTHON_HTTP,
            latency_ms=latency_ms,
            selected_headers={
                "content-type": response.headers.get("content-type"),
                "content-length": response.headers.get("content-length"),
                "retry-after": response.headers.get("retry-after"),
                "location": response.headers.get("location"),
            },
        )

    def persist_imported_response(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        requested_url: str,
        status_code: int,
        content_type_header: str,
        body: bytes,
        transport: ZhihuTransport,
        latency_ms: int = 0,
        selected_headers: dict[str, str | None] | None = None,
        fetched_at: datetime | None = None,
    ) -> PersistedZhihuResponse:
        if urlsplit(requested_url).hostname == "zhuanlan.zhihu.com":
            validate_zhihu_article_url(requested_url)
        else:
            _validate_zhihu_url(requested_url)
        now = datetime.now(UTC)
        observed_at = fetched_at or now
        raw = self.object_store.put_bytes(body)
        scope = content_type.value if content_type else "profile"
        snapshot_source_id = f"{author_source_id}:{scope}:{transport.value.lower()}"
        fetch_status = _fetch_status(status_code)
        snapshot = SourceSnapshot(
            snapshot_id=f"{snapshot_source_id}:{raw.sha256}",
            source_id=snapshot_source_id,
            object_sha256=raw.sha256,
            fetched_at=observed_at,
            available_to_system_at=now,
            source_url=requested_url,
            mime=content_type_header.split(";", 1)[0].strip().lower(),
            byte_size=raw.byte_size,
            headers_hash=content_hash(selected_headers or {}),
            fetch_status=fetch_status,
            rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
        )
        self.state.register_snapshot(snapshot)
        return PersistedZhihuResponse(
            requested_url=requested_url,
            status_code=status_code,
            content_type=content_type_header,
            body=body,
            snapshot=snapshot,
            transport=transport,
            latency_ms=latency_ms,
        )


class ZhihuArticleHtmlTransport:
    """Fetch an already-enumerated canonical Zhihu article page without credentials."""

    provider_id = "zhihu-article-html"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        if content_type is not ZhihuContentType.ARTICLES:
            raise ProviderError(
                "Zhihu article HTML transport only accepts article details",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        validate_zhihu_article_url(url)
        started = time.perf_counter()
        try:
            response = self.client.get(
                url,
                headers={"Referer": _profile_referer(author_source_id)},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Zhihu article request timed out",
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Zhihu article network request failed",
                failure_class=FailureClass.NETWORK,
                retryable=True,
                details={"error_class": type(exc).__name__},
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        return self.persist_imported_response(
            author_source_id=author_source_id,
            requested_url=str(response.request.url),
            status_code=response.status_code,
            content_type_header=response.headers.get("content-type", "application/octet-stream"),
            body=response.content,
            latency_ms=latency_ms,
            selected_headers={
                "content-type": response.headers.get("content-type"),
                "content-length": response.headers.get("content-length"),
                "retry-after": response.headers.get("retry-after"),
                "location": response.headers.get("location"),
            },
        )

    def persist_imported_response(
        self,
        *,
        author_source_id: str,
        requested_url: str,
        status_code: int,
        content_type_header: str,
        body: bytes,
        latency_ms: int = 0,
        selected_headers: dict[str, str | None] | None = None,
        fetched_at: datetime | None = None,
    ) -> PersistedZhihuResponse:
        validate_zhihu_article_url(requested_url)
        now = datetime.now(UTC)
        observed_at = fetched_at or now
        raw = self.object_store.put_bytes(body)
        snapshot_source_id = f"{author_source_id}:articles:python_http_html"
        snapshot = SourceSnapshot(
            snapshot_id=f"{snapshot_source_id}:{raw.sha256}",
            source_id=snapshot_source_id,
            object_sha256=raw.sha256,
            fetched_at=observed_at,
            available_to_system_at=now,
            source_url=requested_url,
            mime=content_type_header.split(";", 1)[0].strip().lower(),
            byte_size=raw.byte_size,
            headers_hash=content_hash(selected_headers or {}),
            fetch_status=_fetch_status(status_code),
            rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
        )
        self.state.register_snapshot(snapshot)
        return PersistedZhihuResponse(
            requested_url=requested_url,
            status_code=status_code,
            content_type=content_type_header,
            body=body,
            snapshot=snapshot,
            transport=ZhihuTransport.PYTHON_HTTP,
            latency_ms=latency_ms,
        )


def classify_response_failure(response: PersistedZhihuResponse) -> FailureClass | None:
    if response.status_code == 429:
        return FailureClass.RATE_LIMITED
    if response.status_code == 401:
        return FailureClass.AUTH_REQUIRED
    if response.status_code == 403:
        return FailureClass.ACCESS_RESTRICTED
    if response.status_code >= 500:
        return FailureClass.NETWORK
    if response.status_code >= 400:
        return FailureClass.INVALID_RESPONSE
    if not response.body:
        return FailureClass.INVALID_RESPONSE
    mime = response.content_type.lower()
    if "json" not in mime:
        lowered = response.body[:200_000].lower()
        if b"signflow" in lowered or "安全验证".encode() in lowered:
            return FailureClass.AUTH_REQUIRED
        return FailureClass.INVALID_RESPONSE
    return None


def classify_article_html_failure(
    response: PersistedZhihuResponse,
) -> FailureClass | None:
    if response.status_code == 429:
        return FailureClass.RATE_LIMITED
    if response.status_code == 401:
        return FailureClass.AUTH_REQUIRED
    if response.status_code == 403:
        return FailureClass.ACCESS_RESTRICTED
    if response.status_code >= 500:
        return FailureClass.NETWORK
    if response.status_code != 200:
        return FailureClass.INVALID_RESPONSE
    if "text/html" not in response.content_type.lower():
        return FailureClass.INVALID_RESPONSE
    lowered = response.body[:500_000].lower()
    if (
        not lowered.strip()
        or b"signflow" in lowered
        or b"passport.zhihu.com" in lowered
        or "安全验证".encode() in lowered
    ):
        return FailureClass.AUTH_REQUIRED
    return None


def _fetch_status(status_code: int) -> FetchStatus:
    if status_code == 200:
        return FetchStatus.SUCCEEDED
    if status_code in {401, 403, 429}:
        return FetchStatus.ACCESS_RESTRICTED
    return FetchStatus.FETCH_FAILED


def _validate_zhihu_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(
            "Zhihu cursor contains an invalid port",
            failure_class=FailureClass.POLICY_REJECTED,
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.zhihu.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ProviderError(
            "Zhihu cursor escaped the allowlisted HTTPS origin",
            failure_class=FailureClass.POLICY_REJECTED,
        )
    if not parsed.path.startswith("/api/"):
        raise ProviderError(
            "Zhihu transport only accepts verified structured API paths",
            failure_class=FailureClass.POLICY_REJECTED,
        )


def validate_zhihu_article_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(
            "Zhihu article URL contains an invalid port",
            failure_class=FailureClass.POLICY_REJECTED,
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "zhuanlan.zhihu.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.query)
        or bool(parsed.fragment)
        or re.fullmatch(r"/p/[1-9][0-9]*", parsed.path) is None
    ):
        raise ProviderError(
            "Zhihu article transport only accepts canonical HTTPS article pages",
            failure_class=FailureClass.POLICY_REJECTED,
        )


def normalize_zhihu_api_url(url: str) -> str:
    """Upgrade an exact same-origin API cursor to HTTPS and reject all other URLs."""

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(
            "Zhihu cursor contains an invalid port",
            failure_class=FailureClass.POLICY_REJECTED,
        ) from exc
    if (
        parsed.scheme == "http"
        and parsed.hostname == "www.zhihu.com"
        and parsed.path.startswith("/api/")
        and not parsed.username
        and not parsed.password
        and port in {None, 80}
        and not parsed.fragment
    ):
        parsed = parsed._replace(scheme="https", netloc="www.zhihu.com")
        url = urlunsplit(parsed)
    _validate_zhihu_url(url)
    return url


def _profile_referer(author_source_id: str) -> str:
    token = author_source_id.removeprefix("zhihu:")
    return f"https://www.zhihu.com/people/{token}"


__all__ = [
    "PersistedZhihuResponse",
    "ZhihuArticleHtmlTransport",
    "ZhihuHttpTransport",
    "ZhihuResponseTransport",
    "classify_article_html_failure",
    "classify_response_failure",
    "normalize_zhihu_api_url",
    "validate_zhihu_article_url",
]
