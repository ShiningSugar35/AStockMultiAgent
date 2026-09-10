"""Read-through intraday locators over canonical files and captured provider bytes.

No source, price, account or report is written here. Provider parsers are reused
with an in-memory captured response; neither their HTTP transport nor their
persistence path is called. A locator pins the current manifest, not a claim
that a historical scheduled run occurred.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pyarrow.parquet as pq

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.investor_orchestration.utils import utc_now
from astock.market_data.quality import _expected_times, cross_validate_batches
from astock.market_data.storage import CanonicalMarketStore, _canonical_bar
from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.sina import Sina5mProvider
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    FetchStatus,
    Frequency,
    InstrumentType,
    Market,
    MarketBar,
    MarketDataBatch,
    QualityStatus,
    ReplayQuality,
    SourceSnapshot,
    TimestampSemantics,
)

_PREFIX = "intraday-observation:"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_SOURCES = 256
_MAX_BYTES = 64 * 1024 * 1024


def _hex_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _bar_end(bar: MarketBar) -> datetime:
    if bar.frequency not in {Frequency.M5, Frequency.H1}:
        raise ValueError("intraday view requires a 5m or 60m bar")
    if bar.timestamp_semantics is TimestampSemantics.BAR_END:
        return bar.timestamp
    if bar.timestamp_semantics is TimestampSemantics.BAR_START:
        return bar.timestamp + timedelta(minutes=5 if bar.frequency is Frequency.M5 else 60)
    raise ValueError("intraday bar has unknown timestamp semantics")


class _CapturedResponse:
    """Read-only response binding for the existing parser implementations."""

    def __init__(self, snapshot: SourceSnapshot, raw: bytes) -> None:
        self.snapshot = snapshot
        self.raw = raw

    def _get(self, url: str, *, params: dict[str, str | int]) -> tuple[httpx.Response, int]:
        captured = urlsplit(self.snapshot.source_url or "")
        expected = urlsplit(url)
        if (captured.scheme, captured.netloc, captured.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ) or captured.fragment:
            raise ValueError("intraday raw response belongs to another endpoint")
        query = parse_qs(captured.query, keep_blank_values=True)
        identity_fields = (
            ("secid", "klt", "fqt") if "secid" in params else ("symbol", "scale", "ma")
        )
        if any(query.get(key) != [str(params[key])] for key in identity_fields):
            raise ValueError("intraday raw capture has another security, adjustment or frequency")
        if "secid" in params:
            payload = json.loads(self.raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            market_code, symbol = str(params["secid"]).split(".", 1)
            if isinstance(data, dict) and (
                ("code" in data and str(data["code"]) != symbol)
                or ("market" in data and str(data["market"]) != market_code)
            ):
                raise ValueError("intraday response body contradicts its captured security query")
        return httpx.Response(
            200, content=self.raw, request=httpx.Request("GET", self.snapshot.source_url or "")
        ), 0

    def _persist_response(self, response: httpx.Response) -> SourceSnapshot:
        if response.content != self.raw:
            raise ValueError("intraday replay changed captured bytes")
        return self.snapshot


class _CapturedEastMoney(_CapturedResponse, EastMoney5mProvider):
    """Original EastMoney parser over a read-only captured response."""


class _CapturedSina(_CapturedResponse, Sina5mProvider):
    """Original Sina parser over a read-only captured response."""


@dataclass(frozen=True)
class VerifiedIntradayPrice:
    locator: str
    bar: MarketBar
    observed_at: datetime
    available_at: datetime
    source_revisions: dict[str, str]


@dataclass(frozen=True)
class _Parent:
    manifest: dict[str, Any]
    bars: tuple[MarketBar, ...]
    batches: tuple[tuple[SourceSnapshot, MarketDataBatch], ...]
    source_revisions: dict[str, str]


class ScheduledIntradayViews:
    def __init__(
        self, state: StateStore, objects: ObjectStore, canonical: CanonicalMarketStore | None = None
    ) -> None:
        if not state.path.is_file():
            raise ValueError("intraday read view requires an existing canonical database")
        self.state = state
        self.objects = objects
        self.canonical = canonical or CanonicalMarketStore(
            state.path.parent / "data/parquet", state.path.parent / "manifests"
        )

    @staticmethod
    def recognizes(locator: str) -> bool:
        return locator.startswith(_PREFIX)

    @staticmethod
    def _identity(instrument_id: str, frequency: Frequency) -> tuple[Market, str]:
        parts = normalized_instrument(instrument_id).split(":")
        if (
            len(parts) != 2
            or parts[0] not in {"XSHG", "XSHE", "BJSE"}
            or len(parts[1]) != 6
            or not parts[1].isascii()
            or not parts[1].isdigit()
            or frequency not in {Frequency.M5, Frequency.H1}
        ):
            raise ValueError(
                "intraday view needs an explicit A-share security and 5m/60m frequency"
            )
        return Market(parts[0]), parts[1]

    @staticmethod
    def _time(as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None or as_of > utc_now():
            raise ValueError("intraday cutoff must be timezone-aware and not in the future")

    def latest(
        self, instrument_id: str, frequency: Frequency, *, as_of: datetime
    ) -> VerifiedIntradayPrice:
        self._time(as_of)
        market, symbol = self._identity(instrument_id, frequency)
        parent = self._parent(market, symbol, frequency, as_of, expected_hash=None)
        eligible = [bar for bar in parent.bars if _bar_end(bar) <= as_of]
        if not eligible:
            raise ValueError("no closed intraday bar exists at the requested cutoff")
        bar = max(eligible, key=_bar_end)
        locator = (
            f"{_PREFIX}{market.value}:{symbol}:{frequency.value}:"
            f"{parent.manifest['content_hash']}:{bar.observation_id}"
        )
        return self._price(parent, bar, locator, as_of)

    def load_many(
        self, locators: tuple[str, ...], *, as_of: datetime
    ) -> dict[str, VerifiedIntradayPrice]:
        self._time(as_of)
        if len(locators) > 256:
            raise ValueError("intraday locator batch exceeds the bounded audit")
        parents: dict[tuple[str, ...], _Parent] = {}
        security: tuple[Market, str] | None = None
        result = {}
        for locator in dict.fromkeys(locators):
            parts = locator.split(":")
            if (
                len(parts) != 6
                or parts[0] != "intraday-observation"
                or not all(_hex_digest(part) for part in parts[4:])
            ):
                raise ValueError("invalid intraday locator")
            frequency = Frequency(parts[3])
            market, symbol = self._identity(f"{parts[1]}:{parts[2]}", frequency)
            if security is not None and security != (market, symbol):
                raise ValueError("one intraday audit cannot scan unrelated securities")
            security = market, symbol
            key = tuple(parts[1:5])
            if key not in parents:
                parents[key] = self._parent(
                    market, symbol, frequency, as_of, expected_hash=parts[4]
                )
            parent = parents[key]
            matches = [bar for bar in parent.bars if bar.observation_id == parts[5]]
            if len(matches) != 1:
                raise ValueError("intraday locator does not identify exactly one canonical bar")
            result[locator] = self._price(parent, matches[0], locator, as_of)
        return result

    def _parent(
        self,
        market: Market,
        symbol: str,
        frequency: Frequency,
        as_of: datetime,
        *,
        expected_hash: str | None,
    ) -> _Parent:
        probe = BarRequest(
            symbol=symbol,
            market=market,
            frequency=frequency,
            requested_start=as_of - timedelta(days=1),
            requested_end=as_of,
        )
        manifest = self.canonical.load_manifest(probe)
        if manifest is None:
            raise ValueError("canonical intraday manifest is missing")
        body = dict(manifest)
        digest = body.pop("content_hash", None)
        if digest != content_hash(body) or (expected_hash is not None and digest != expected_hash):
            raise ValueError("intraday manifest changed; re-freeze the source locator")
        expected_quality = (
            ReplayQuality.DUAL_SOURCE_5M_VERIFIED
            if frequency is Frequency.M5
            else ReplayQuality.PROVIDER_1H_APPROX
        )
        if (
            manifest.get("schema_version") != "1.4"
            or manifest.get("market") != market.value
            or manifest.get("symbol") != symbol
            or manifest.get("frequency") != frequency.value
            or manifest.get("instrument_type") not in {"STOCK", "ETF"}
            or manifest.get("selected_provider") not in {"eastmoney-5m", "sina-5m"}
            or manifest.get("adjustment_mode") != "NONE"
            or manifest.get("quality_status") != QualityStatus.PASS.value
            or manifest.get("replay_quality") != expected_quality.value
        ):
            raise ValueError("canonical intraday identity or quality is not admissible")
        files = manifest.get("files")
        sources = manifest.get("source_snapshot_ids")
        if (
            not isinstance(files, list)
            or not 0 < len(files) <= 128
            or not all(isinstance(path, str) for path in files)
        ):
            raise ValueError("intraday canonical file inventory is invalid or unbounded")
        paths = [(self.canonical.data_root / Path(path)).resolve() for path in files]
        if any(not path.is_relative_to(self.canonical.data_root) for path in paths):
            raise ValueError("intraday canonical path escapes the data root")
        if sum(path.stat().st_size for path in paths) > _MAX_BYTES:
            raise ValueError("intraday canonical files exceed the bounded audit")
        if (
            not isinstance(sources, list)
            or not 2 <= len(sources) <= _MAX_SOURCES
            or not all(isinstance(item, str) and item for item in sources)
            or len(sources) != len(set(sources))
        ):
            raise ValueError("intraday view requires distinct bounded raw captures")
        expected_count = manifest.get("bar_count")
        if type(expected_count) is not int or not 0 < expected_count <= 100000:
            raise ValueError("intraday canonical row budget is invalid")
        if sum(pq.ParquetFile(path).metadata.num_rows for path in paths) != expected_count:
            raise ValueError("intraday Parquet row count differs from the canonical manifest")
        bars = tuple(self.canonical._read_manifest_bars(manifest))
        if not bars or len(bars) != expected_count:
            raise ValueError("intraday canonical bar coverage is inconsistent or unbounded")
        if bars[0].timestamp.isoformat() != manifest.get("actual_start") or bars[
            -1
        ].timestamp.isoformat() != manifest.get("actual_end"):
            raise ValueError("intraday canonical range is inconsistent")
        request = probe.model_copy(
            update={
                "requested_start": bars[0].timestamp,
                "requested_end": max(bars[-1].timestamp, as_of),
                "instrument_type": InstrumentType(str(manifest["instrument_type"])),
            }
        )
        batches = []
        revisions = {}
        scanned = 0
        for identity in sources:
            snapshot = self.state.get_snapshot(identity)
            if snapshot is None or snapshot.fetch_status is not FetchStatus.SUCCEEDED:
                raise ValueError("intraday raw capture is missing or failed")
            if snapshot.fetched_at > as_of or snapshot.available_to_system_at > as_of:
                raise ValueError("intraday raw capture was not available at cutoff")
            if snapshot.available_to_system_at < snapshot.fetched_at:
                raise ValueError("intraday raw capture backdates its availability")
            if identity != f"{snapshot.source_id}:{snapshot.object_sha256}":
                raise ValueError("intraday raw identity differs from the original capture contract")
            scanned += snapshot.byte_size
            if scanned > _MAX_BYTES:
                raise ValueError("intraday raw captures exceed the bounded audit")
            if self.objects.path_for(snapshot.object_sha256).stat().st_size != snapshot.byte_size:
                raise ValueError("intraday raw object size differs from its captured metadata")
            raw = self.objects.get_bytes(snapshot.object_sha256)
            if len(raw) != snapshot.byte_size:
                raise ValueError("intraday raw capture length changed")
            if snapshot.source_id == EastMoney5mProvider.provider_id:
                batch = _CapturedEastMoney(snapshot, raw).fetch_bars(request)
            elif snapshot.source_id == Sina5mProvider.provider_id:
                batch = _CapturedSina(snapshot, raw).fetch_bars(request)
            else:
                raise ValueError("intraday raw capture has no admitted replay parser")
            # A provider may include the still-forming last bar. Never use it as
            # a closed observation or to earn cross-source history coverage.
            visible = [
                bar for bar in batch.bars if _bar_end(bar) <= snapshot.available_to_system_at
            ]
            if not visible:
                continue
            batch = batch.model_copy(
                update={
                    "bars": visible,
                    "bar_count": len(visible),
                    "actual_start": visible[0].timestamp,
                    "actual_end": visible[-1].timestamp,
                }
            )
            batches.append((snapshot, batch))
            revisions[identity] = snapshot.object_sha256
            revisions[f"capture-metadata:{identity}"] = content_hash(
                snapshot.model_dump(mode="json")
            )
        return _Parent(dict(manifest), bars, tuple(batches), revisions)

    @staticmethod
    def _price(
        parent: _Parent, bar: MarketBar, locator: str, as_of: datetime
    ) -> VerifiedIntradayPrice:
        manifest = parent.manifest
        close_at = _bar_end(bar)
        if (
            bar.market.value != manifest["market"]
            or bar.symbol != manifest["symbol"]
            or bar.frequency.value != manifest["frequency"]
            or bar.provider_id != f"canonical:{manifest['selected_provider']}"
            or bar.adjustment_mode is not AdjustmentMode.NONE
            or not bar.close.is_finite()
            or bar.close <= 0
            or close_at > as_of
            or bar.timestamp.astimezone(_SHANGHAI).time().replace(tzinfo=None)
            not in _expected_times(bar.timestamp_semantics, bar.frequency)
        ):
            raise ValueError("intraday canonical bar identity, price or closed session is invalid")
        selected: dict[str, tuple[SourceSnapshot, MarketDataBatch, MarketBar]] = {}
        for snapshot, batch in parent.batches:
            matches = [item for item in batch.bars if item.timestamp == bar.timestamp]
            if len(matches) > 1:
                raise ValueError("intraday raw capture contains duplicate bar timestamps")
            if not matches:
                continue
            previous = selected.get(snapshot.source_id)
            if (
                previous is None
                or snapshot.available_to_system_at > previous[0].available_to_system_at
            ):
                selected[snapshot.source_id] = snapshot, batch, matches[0]
        if set(selected) != {EastMoney5mProvider.provider_id, Sina5mProvider.provider_id}:
            raise ValueError("intraday target bar lacks two independent captured providers")
        primary = selected[str(manifest["selected_provider"])]
        secondary = next(
            value for key, value in selected.items() if key != manifest["selected_provider"]
        )
        canonical = _canonical_bar(primary[2])
        if canonical.model_dump(exclude={"created_at"}) != bar.model_dump(exclude={"created_at"}):
            raise ValueError("intraday canonical bar no longer matches its captured primary source")
        quality = cross_validate_batches(primary[1], secondary[1])
        if quality.quality_status is not QualityStatus.PASS:
            raise ValueError(
                "intraday captured sources fail the existing cross-source quality gate"
            )
        return VerifiedIntradayPrice(
            locator,
            bar,
            close_at,
            max(value[0].available_to_system_at for value in selected.values()),
            {locator: str(manifest["content_hash"]), **parent.source_revisions},
        )
