"""Deterministic A-share trading calendars reconstructed from versioned official notices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import FetchStatus, Market, SourceSnapshot, TradingSession

_OFFICIAL_CALENDAR_DOMAINS = {
    Market.XSHG: "sse.com.cn",
    Market.XSHE: "szse.cn",
    Market.BJSE: "bse.cn",
}


@dataclass(frozen=True, slots=True)
class OfficialCalendarSource:
    authority: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class OfficialCalendarClosure:
    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class OfficialCalendarYear:
    year: int
    published_at: datetime
    verified_at: datetime
    sources: dict[Market, OfficialCalendarSource]
    closures: tuple[OfficialCalendarClosure, ...]


@dataclass(frozen=True, slots=True)
class OfficialTradingCalendarPolicy:
    runtime_priority: str
    search_refresh_allowed: bool
    api_fallback_allowed: bool
    search_must_use_authoritative_domains: bool
    configured_year_missing_action: str


@dataclass(frozen=True, slots=True)
class OfficialTradingCalendarConfig:
    schema_version: str
    calendar_family: str
    policy: OfficialTradingCalendarPolicy
    years: dict[int, OfficialCalendarYear]

    def covers(self, exchange: Market, start: date, end: date) -> bool:
        if exchange is Market.INDEX or end < start:
            return False
        for year in range(start.year, end.year + 1):
            item = self.years.get(year)
            if item is None or exchange not in item.sources:
                return False
        return True

    def open_dates(self, exchange: Market, start: date, end: date) -> tuple[date, ...] | None:
        """Return the exact configured exchange sessions without performing I/O."""

        if not self.covers(exchange, start, end):
            return None
        closures = [
            closure
            for year in range(start.year, end.year + 1)
            for closure in self.years[year].closures
        ]
        sessions: list[date] = []
        current = start
        while current <= end:
            if current.weekday() < 5 and not any(
                closure.start <= current <= closure.end for closure in closures
            ):
                sessions.append(current)
            current += timedelta(days=1)
        return tuple(sessions)


class OfficialTradingCalendarResolver:
    """Build one complete day-by-day calendar without a runtime network dependency."""

    source_id = "official-trading-calendar-config"

    def __init__(
        self,
        config: OfficialTradingCalendarConfig,
        objects: ObjectStore,
        state: StateStore,
    ) -> None:
        self.config = config
        self.objects = objects
        self.state = state

    def materialize(
        self,
        exchange: Market,
        start: date,
        end: date,
        *,
        available_at: datetime | None = None,
    ) -> tuple[list[TradingSession], SourceSnapshot] | None:
        if not self.config.covers(exchange, start, end):
            return None
        years = [self.config.years[year] for year in range(start.year, end.year + 1)]
        available = available_at or max(item.verified_at for item in years).astimezone(UTC)
        if available.tzinfo is None:
            raise ValueError("official calendar availability must be timezone-aware")

        closures = [closure for item in years for closure in item.closures]
        source = years[0].sources[exchange]
        payload = {
            "available_to_system_at": available.isoformat(),
            "schema_version": self.config.schema_version,
            "calendar_family": self.config.calendar_family,
            "exchange": exchange.value,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "generation_rule": "weekday<5 AND not_in_official_closure",
            "sources": [
                {
                    "year": item.year,
                    "authority": item.sources[exchange].authority,
                    "title": item.sources[exchange].title,
                    "url": item.sources[exchange].url,
                    "published_at": item.published_at.isoformat(),
                    "verified_at": item.verified_at.isoformat(),
                }
                for item in years
            ],
            "closures": [
                {
                    "name": item.name,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                }
                for item in closures
            ],
        }
        ref = self.objects.put_json(payload)
        snapshot = SourceSnapshot(
            created_at=available,
            snapshot_id=f"{self.source_id}:{exchange.value}:{ref.sha256}",
            source_id=self.source_id,
            object_sha256=ref.sha256,
            fetched_at=available,
            available_to_system_at=available,
            source_url=source.url,
            mime="application/yaml+json",
            byte_size=ref.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_OFFICIAL_NOTICE_RECONSTRUCTED",
        )
        self.state.register_snapshot(snapshot)

        records: list[TradingSession] = []
        current = start
        while current <= end:
            is_closed = any(item.start <= current <= item.end for item in closures)
            records.append(
                TradingSession(
                    created_at=available,
                    exchange=exchange,
                    session_date=current,
                    is_open=current.weekday() < 5 and not is_closed,
                    source_snapshot_id=snapshot.snapshot_id,
                    available_to_system_at=available,
                )
            )
            current += timedelta(days=1)
        return records, snapshot


def load_official_trading_calendar(path: Path) -> OfficialTradingCalendarConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid official trading calendar configuration: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "official-trading-calendar-v1":
        raise ValueError("Unsupported official trading calendar configuration")
    if raw.get("calendar_family") != "CN_A_SHARE":
        raise ValueError("Official trading calendar family must be CN_A_SHARE")
    raw_policy = raw.get("policy")
    if not isinstance(raw_policy, dict):
        raise ValueError("Official trading calendar policy is required")
    policy = OfficialTradingCalendarPolicy(
        runtime_priority=str(raw_policy.get("runtime_priority") or ""),
        search_refresh_allowed=bool(raw_policy.get("search_refresh_allowed")),
        api_fallback_allowed=bool(raw_policy.get("api_fallback_allowed")),
        search_must_use_authoritative_domains=bool(
            raw_policy.get("search_must_use_authoritative_domains")
        ),
        configured_year_missing_action=str(
            raw_policy.get("configured_year_missing_action") or ""
        ),
    )
    if policy.runtime_priority != "LOCAL_OFFICIAL_FIRST":
        raise ValueError("Official trading calendar must remain LOCAL_OFFICIAL_FIRST")
    if policy.configured_year_missing_action != "SEARCH_OFFICIAL_THEN_API_FALLBACK":
        raise ValueError("Official trading calendar missing-year action is unsupported")
    raw_years = raw.get("years")
    if not isinstance(raw_years, dict) or not raw_years:
        raise ValueError("Official trading calendar requires at least one configured year")

    required_markets = {Market.XSHG, Market.XSHE, Market.BJSE}
    years: dict[int, OfficialCalendarYear] = {}
    for raw_year, raw_item in raw_years.items():
        if not isinstance(raw_item, dict):
            raise ValueError("Official trading calendar year must be an object")
        year = int(raw_year)
        published_at = _aware_datetime(raw_item.get("published_at"), "published_at")
        verified_at = _aware_datetime(raw_item.get("verified_at"), "verified_at")
        raw_sources = raw_item.get("sources")
        if not isinstance(raw_sources, dict):
            raise ValueError("Official trading calendar sources must be an object")
        sources: dict[Market, OfficialCalendarSource] = {}
        for raw_market, raw_source in raw_sources.items():
            market = Market(str(raw_market))
            if market not in required_markets or not isinstance(raw_source, dict):
                raise ValueError("Official trading calendar source market is invalid")
            authority = str(raw_source.get("authority") or "").strip()
            title = str(raw_source.get("title") or "").strip()
            url = str(raw_source.get("url") or "").strip()
            if not authority or not title or not url.startswith("https://"):
                raise ValueError("Official trading calendar source is incomplete")
            host = (urlparse(url).hostname or "").lower().rstrip(".")
            allowed_domain = _OFFICIAL_CALENDAR_DOMAINS[market]
            if host != allowed_domain and not host.endswith(f".{allowed_domain}"):
                raise ValueError("Official trading calendar source domain is not authoritative")
            sources[market] = OfficialCalendarSource(authority=authority, title=title, url=url)
        if set(sources) != required_markets:
            raise ValueError("Official trading calendar must cite XSHG/XSHE/BJSE sources")

        raw_closures = raw_item.get("closures")
        if not isinstance(raw_closures, list):
            raise ValueError("Official trading calendar closures must be a list")
        closures: list[OfficialCalendarClosure] = []
        for raw_closure in raw_closures:
            if not isinstance(raw_closure, dict):
                raise ValueError("Official trading calendar closure must be an object")
            name = str(raw_closure.get("name") or "").strip()
            start = date.fromisoformat(str(raw_closure.get("start")))
            end = date.fromisoformat(str(raw_closure.get("end")))
            if not name or end < start or start.year != year or end.year != year:
                raise ValueError("Official trading calendar closure range is invalid")
            closures.append(OfficialCalendarClosure(name=name, start=start, end=end))
        _validate_non_overlapping(closures)
        years[year] = OfficialCalendarYear(
            year=year,
            published_at=published_at,
            verified_at=verified_at,
            sources=sources,
            closures=tuple(closures),
        )

    return OfficialTradingCalendarConfig(
        schema_version="official-trading-calendar-v1",
        calendar_family="CN_A_SHARE",
        policy=policy,
        years=years,
    )


def _aware_datetime(value: object, field: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"Official trading calendar {field} must be timezone-aware")
    return parsed


def _validate_non_overlapping(closures: list[OfficialCalendarClosure]) -> None:
    ordered = sorted(closures, key=lambda item: (item.start, item.end))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.start <= previous.end:
            raise ValueError("Official trading calendar closure ranges overlap")


__all__ = [
    "OfficialTradingCalendarConfig",
    "OfficialTradingCalendarResolver",
    "load_official_trading_calendar",
]
