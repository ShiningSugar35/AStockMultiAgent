"""Low-cost GDELT news-lead discovery for the continuous monitor.

News from this adapter is explicitly a lead. It is never promoted directly to a
company fact or a trading action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
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


class GdeltNewsLeadProvider:
    provider_id = "gdelt-news-leads"

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        *,
        endpoint: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "api.gdeltproject.org":
            raise ValueError("GDELT news endpoint must use the official HTTPS host")
        self.objects = objects
        self.state = state
        self.endpoint = endpoint
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
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
        terms = [item.strip() for item in names if item.strip()]
        if symbol not in terms:
            terms.append(symbol)
        query_terms = [f'"{item}"' if " " in item else item for item in terms[:8]]
        query = " OR ".join(query_terms)
        response = self.client.get(
            self.endpoint,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": str(max_records),
                "sort": "datedesc",
                "startdatetime": start.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
                "enddatetime": end.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
            },
        )
        response.raise_for_status()
        snapshot = self._persist(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("GDELT returned malformed JSON") from exc
        raw_articles = payload.get("articles", []) if isinstance(payload, dict) else []
        if not isinstance(raw_articles, list):
            raise ValueError("GDELT articles field is malformed")
        leads: list[NewsLead] = []
        for raw in raw_articles:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not title or not _safe_public_url(url):
                continue
            seen = _parse_seen_at(raw.get("seendate"), fallback=end)
            if not (start.astimezone(UTC) <= seen <= end.astimezone(UTC)):
                continue
            leads.append(
                NewsLead(
                    title=title,
                    url=url,
                    domain=str(raw.get("domain") or urlparse(url).hostname or ""),
                    seen_at=seen,
                    language=(str(raw["language"]) if raw.get("language") else None),
                    source_country=(
                        str(raw["sourcecountry"]) if raw.get("sourcecountry") else None
                    ),
                    snapshot_id=snapshot.snapshot_id,
                )
            )
        by_identity = {(item.url, item.title): item for item in leads}
        return sorted(by_identity.values(), key=lambda item: (item.seen_at, item.url), reverse=True)

    def _persist(self, response: httpx.Response) -> SourceSnapshot:
        now = datetime.now(UTC)
        ref = self.objects.put_bytes(response.content)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:index:{ref.sha256}",
            source_id=f"{self.provider_id}:index",
            object_sha256=ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=str(response.request.url),
            mime=response.headers.get("content-type", "application/json").split(";")[0],
            byte_size=ref.byte_size,
            headers_hash=content_hash(sorted(response.headers.items())),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_NEWS_LEAD",
        )
        self.state.register_snapshot(snapshot)
        return snapshot


def _safe_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username


def _parse_seen_at(value: object, *, fallback: datetime) -> datetime:
    if value is None:
        return fallback.astimezone(UTC)
    raw = str(value).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback.astimezone(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["GdeltNewsLeadProvider", "NewsLead"]
