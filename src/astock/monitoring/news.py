"""Low-cost, auditable GDELT news-lead discovery for the continuous monitor.

An empty checked response means no result in the named bounded search, never
proof that adverse news does not exist. A source error is not an empty result.
Raw bodies are deduplicated in ObjectStore; capture identities retain query/time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_resilience import SourceCircuitBreaker, classify_source_error
from astock.core.state import StateStore
from astock.schemas import FetchStatus, SourceSnapshot


@dataclass(frozen=True, slots=True)
class NewsLead:
    title: str
    url: str
    domain: str
    seen_at: datetime
    language: str | None
    source_country: str | None
    snapshot_id: str

    @property
    def lead_id(self) -> str:
        return "gdelt-lead:" + content_hash(
            {
                "title": self.title,
                "url": self.url,
                "seen_at": self.seen_at.astimezone(UTC).isoformat(),
            }
        )


@dataclass(frozen=True, slots=True)
class NewsSearchReceipt:
    """Read projection of one raw request/response; no second news database."""

    snapshot_id: str
    query: str
    start: datetime
    end: datetime
    checked_at: datetime
    max_records: int
    raw_count: int
    rejected_count: int
    duplicate_count: int
    leads: tuple[NewsLead, ...]

    @property
    def limit_reached(self) -> bool:
        return self.raw_count >= self.max_records

    @property
    def checked_successfully(self) -> bool:
        return not self.limit_reached and self.rejected_count == 0


class GdeltNewsLeadProvider:
    provider_id = "gdelt-news-leads"
    capability = "news.discovery.lead"
    default_endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        *,
        endpoint: str = default_endpoint,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        _validate_endpoint(endpoint)
        self.objects = objects
        self.state = state
        self.endpoint = endpoint
        self.breaker = SourceCircuitBreaker(state)
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "AStockMultiAgent/continuous-monitor-v1"},
        )

    def search(
        self,
        *,
        names: list[str],
        symbol: str,
        start: datetime,
        end: datetime,
        max_records: int,
    ) -> list[NewsLead]:
        """Compatibility list API over the same checked request and raw capture."""
        return list(
            self.search_checked(
                names=names,
                symbol=symbol,
                start=start,
                end=end,
                max_records=max_records,
            ).leads
        )

    @staticmethod
    def query_text(*, names: list[str], symbol: str) -> str:
        """Return the exact bounded query string used for both capture and later audit."""
        terms = list(dict.fromkeys(item.strip() for item in names if item.strip()))
        if symbol.strip() and symbol.strip() not in terms:
            terms.append(symbol.strip())
        if not terms or len(terms) > 8 or any(len(term) > 200 for term in terms):
            raise ValueError("GDELT query must contain one to eight bounded search terms")
        if any(any(ord(char) < 32 for char in term) for term in terms):
            raise ValueError("GDELT query contains control characters")
        return " OR ".join(f'"{term}"' if " " in term else term for term in terms)

    def search_checked(
        self,
        *,
        names: list[str],
        symbol: str,
        start: datetime,
        end: datetime,
        max_records: int,
    ) -> NewsSearchReceipt:
        start, end = _validate_interval(start, end)
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 250
        ):
            raise ValueError("GDELT max_records must be an integer within [1, 250]")
        # Preserve the established query syntax for compatibility. Names are an
        # explicit bounded search scope, not authorization for any source action.
        query = self.query_text(names=names, symbol=symbol)
        if not self.breaker.claim_attempt(self.provider_id, self.capability):
            raise RuntimeError("GDELT news capability is temporarily unavailable")
        try:
            response = self.client.get(
                self.endpoint,
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": str(max_records),
                    "sort": "datedesc",
                    "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": end.strftime("%Y%m%d%H%M%S"),
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            _validate_endpoint(str(response.request.url), allow_query=True)
            snapshot = self._persist(response)
            receipt = replay_news_search(self.state, self.objects, snapshot.snapshot_id)
        except Exception as exc:
            self.breaker.record_failure(
                self.provider_id,
                self.capability,
                classify_source_error(exc),
            )
            raise
        self.breaker.record_success(self.provider_id, self.capability)
        return receipt

    def _persist(self, response: httpx.Response) -> SourceSnapshot:
        now = datetime.now(UTC)
        reference = self.objects.put_bytes(response.content)
        identity = content_hash(
            {
                "source_url": str(response.request.url),
                "object_hash": reference.sha256,
                "fetched_at": now.isoformat(),
                "capture_nonce": uuid4().hex,
            }
        )
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:index:{identity}:{reference.sha256}",
            source_id=f"{self.provider_id}:index:{identity}",
            object_sha256=reference.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=str(response.request.url),
            mime=response.headers.get("content-type", "application/json").split(";")[0],
            byte_size=reference.byte_size,
            headers_hash=content_hash(sorted(response.headers.items())),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_NEWS_LEAD",
        )
        self.state.register_snapshot(snapshot)
        return snapshot


def _validate_endpoint(value: str, *, allow_query: bool = False) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.gdeltproject.org"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api/v2/doc/doc"
        or (parsed.query and not allow_query)
    ):
        raise ValueError("GDELT news endpoint must use the exact official HTTPS API")


def _validate_interval(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    for value in (start, end):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("news query time interval must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise ValueError("news query interval must have start before end")
    if end > datetime.now(UTC):
        raise ValueError("news query interval cannot end in the future")
    return start, end


def replay_news_search(
    state: StateStore, objects: ObjectStore, snapshot_id: str
) -> NewsSearchReceipt:
    """Reconstruct counts and findings from captured bytes, not caller booleans."""
    snapshot = state.get_snapshot(snapshot_id)
    if snapshot is None or not (
        snapshot.source_id == "gdelt-news-leads:index"
        or snapshot.source_id.startswith("gdelt-news-leads:index:")
    ):
        raise ValueError("news search raw capture is unavailable")
    if snapshot.snapshot_id != f"{snapshot.source_id}:{snapshot.object_sha256}":
        raise ValueError("news capture identity does not bind its source and raw body")
    if snapshot.fetch_status is not FetchStatus.SUCCEEDED:
        raise ValueError("news search capture did not succeed")
    source_url = snapshot.source_url
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("news search capture lacks its source URL")
    _validate_endpoint(source_url, allow_query=True)
    parameters = parse_qs(urlparse(source_url).query, keep_blank_values=True)
    required = {"query", "mode", "format", "maxrecords", "sort", "startdatetime", "enddatetime"}
    if set(parameters) != required or any(len(value) != 1 for value in parameters.values()):
        raise ValueError("news search capture lacks an unambiguous request scope")
    scalar = {key: value[0] for key, value in parameters.items()}
    if scalar["mode"] != "artlist" or scalar["format"] != "json" or not scalar["query"].strip():
        raise ValueError("news search capture has an unsupported query mode")
    maximum = int(scalar["maxrecords"])
    if not 1 <= maximum <= 250:
        raise ValueError("news search result bound is invalid")
    start = datetime.strptime(scalar["startdatetime"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    end = datetime.strptime(scalar["enddatetime"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    _validate_interval(start, end)
    if snapshot.available_to_system_at < end or snapshot.available_to_system_at > datetime.now(UTC):
        raise ValueError("news search capture has an invalid availability timestamp")
    raw = objects.get_bytes(snapshot.object_sha256)
    if len(raw) != snapshot.byte_size:
        raise ValueError("news search raw byte size does not match its metadata")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("GDELT returned malformed JSON") from exc
    if not isinstance(payload, dict) or "error" in payload or "articles" not in payload:
        # Do not infer "no matches" from a schema-less 200 response. GDELT's
        # legacy search infrastructure may shed/degrade requests under load; only
        # an explicit ArticleList payload can certify a checked empty result.
        raise ValueError("GDELT response is not an articles result")
    articles: object = payload["articles"]
    if not isinstance(articles, list) or len(articles) > maximum:
        raise ValueError("GDELT articles field is malformed or exceeds the requested bound")
    leads: list[NewsLead] = []
    rejected = 0
    for item in articles:
        if not isinstance(item, dict):
            rejected += 1
            continue
        title, url = str(item.get("title") or "").strip(), str(item.get("url") or "").strip()
        seen = _strict_seen_at(item.get("seendate"))
        if not title or not _safe_public_url(url) or seen is None or not start <= seen <= end:
            rejected += 1
            continue
        leads.append(
            NewsLead(
                title=title,
                url=url,
                domain=str(item.get("domain") or urlparse(url).hostname or ""),
                seen_at=seen,
                language=str(item["language"]) if item.get("language") else None,
                source_country=str(item["sourcecountry"]) if item.get("sourcecountry") else None,
                snapshot_id=snapshot.snapshot_id,
            )
        )
    by_identity = {(item.url, item.title): item for item in leads}
    selected = tuple(
        sorted(by_identity.values(), key=lambda item: (item.seen_at, item.url), reverse=True)
    )
    return NewsSearchReceipt(
        snapshot_id=snapshot.snapshot_id,
        query=scalar["query"],
        start=start,
        end=end,
        checked_at=snapshot.available_to_system_at,
        max_records=maximum,
        raw_count=len(articles),
        rejected_count=rejected,
        duplicate_count=len(leads) - len(selected),
        leads=selected,
    )


def _safe_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _strict_seen_at(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def _parse_seen_at(value: object, *, fallback: datetime) -> datetime:
    """Legacy helper for callers; checked capture parsing never invents a date."""
    return _strict_seen_at(value) or fallback.astimezone(UTC)


__all__ = ["GdeltNewsLeadProvider", "NewsLead", "NewsSearchReceipt", "replay_news_search"]
