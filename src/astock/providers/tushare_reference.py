"""Narrow Tushare mappings into existing reference records and source-bound metrics."""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.tushare_structured import (
    DEFAULT_FIELDS,
    SHANGHAI,
    TushareQueryRequest,
    TushareStructuredProvider,
)
from astock.schemas import (
    DailyBarObservation,
    InstrumentRecord,
    InstrumentType,
    Market,
    SourceSnapshot,
)

_SUFFIX = {Market.XSHG: "SH", Market.XSHE: "SZ", Market.BJSE: "BJ"}
_EXCHANGE = {Market.XSHG: "SSE", Market.XSHE: "SZSE", Market.BJSE: "BSE"}


class TushareMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric: str
    value: Decimal | None
    unit: Literal["CNY", "SHARE", "PERCENT", "RATIO", "INDEX"]
    period_basis: Literal["SESSION_END", "YTD", "MONTH", "QUARTER_YOY"]


class TushareMetricCapture(BaseModel):
    """Vendor observations, never an invented official release or historical vintage."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["tushare-metric-capture-v1"] = "tushare-metric-capture-v1"
    endpoint: Literal["daily_basic", "cn_gdp", "cn_cpi"]
    subject_id: str
    period: str
    available_to_system_at: AwareDatetime
    source_snapshot_id: str
    source_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_artifact_id: str
    row_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: tuple[TushareMetric, ...]
    source_group: Literal["TUSHARE"] = "TUSHARE"
    source_class: Literal["SECONDARY_STRUCTURED"] = "SECONDARY_STRUCTURED"
    publication_time_precision: Literal["UNKNOWN"] = "UNKNOWN"
    historical_vintage_verified: Literal[False] = False
    formal_use_allowed: Literal[False] = False

    @model_validator(mode="after")
    def unique_metrics(self) -> TushareMetricCapture:
        if len({item.metric for item in self.metrics}) != len(self.metrics):
            raise ValueError("Metric capture contains duplicate fields")
        return self

    @property
    def artifact_id(self) -> str:
        """Registered object identity includes capture time and serialized field values."""
        return "TushareMetricCapture:" + sha256_bytes(
            canonical_json_bytes(self.model_dump(mode="json"))
        )


class TushareReferenceProvider(TushareStructuredProvider):
    """Use one account quota/cache/raw store; complete releases remain service-owned."""

    provider_id = "tushare-reference"
    recorded_supported = False

    def fetch_instruments(
        self, market: Market, *, symbol: str | None = None, live: bool = False,
        listing_status: Literal["L", "D", "P", "G", "UN"] = "L",
    ) -> tuple[list[InstrumentRecord], SourceSnapshot]:
        if not live:
            raise ValueError("Tushare recorded reference acquisition is unavailable")
        if market not in _SUFFIX:
            raise ValueError("Tushare stock master requires a stock exchange")
        params = {"exchange": _EXCHANGE[market], "list_status": listing_status}
        if symbol is not None:
            params["ts_code"] = _code(symbol, market)
        request = TushareQueryRequest(endpoint="stock_basic", params=params)
        result, snapshot = self._capture(request)
        records = normalize_instruments(
            result["rows"], snapshot, market, symbol=symbol, listing_status=listing_status
        )
        return records, snapshot

    def fetch_daily_records(
        self, symbol: str, market: Market, start: date, end: date, *, live: bool = False,
    ) -> tuple[list[DailyBarObservation], SourceSnapshot]:
        if not live:
            raise ValueError("Tushare recorded reference acquisition is unavailable")
        request = TushareQueryRequest(endpoint="daily", params={
            "ts_code": _code(symbol, market),
            "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d"),
        })
        result, snapshot = self._capture(request)
        return normalize_daily(result["rows"], snapshot, symbol, market, start, end), snapshot

    def valuation_metrics(
        self, symbol: str, market: Market, session: date,
    ) -> TushareMetricCapture:
        request = TushareQueryRequest(endpoint="daily_basic", params={
            "ts_code": _code(symbol, market), "trade_date": session.strftime("%Y%m%d"),
        })
        result, snapshot = self._capture(request)
        if len(result["rows"]) != 1:
            raise ValueError("Daily metrics require exactly one matching security/session row")
        if snapshot.available_to_system_at < datetime.combine(session, time(15), SHANGHAI):
            raise ValueError("Daily metrics cannot be consumed before session close")
        return self._metrics(result, snapshot, result["rows"][0], f"{market.value}:{symbol}")

    def macro_metrics(
        self, endpoint: Literal["cn_gdp", "cn_cpi"], period: str,
    ) -> TushareMetricCapture:
        parameter = "q" if endpoint == "cn_gdp" else "m"
        request = TushareQueryRequest(endpoint=endpoint, params={parameter: period})
        result, snapshot = self._capture(request)
        if len(result["rows"]) != 1:
            raise ValueError("Macro metrics require exactly one matching period")
        year = int(period[:4])
        month = int(period[-1]) * 3 if endpoint == "cn_gdp" else int(period[-2:])
        period_end = date(year, month, calendar.monthrange(year, month)[1])
        if snapshot.available_to_system_at.astimezone(SHANGHAI).date() <= period_end:
            raise ValueError("Macro period is not complete at capture")
        return self._metrics(result, snapshot, result["rows"][0], "CN")

    def _capture(
        self, request: TushareQueryRequest,
    ) -> tuple[dict[str, Any], SourceSnapshot]:
        result = self.query(request)
        if result["request"] != request.identity():
            raise ValueError("Tushare reference request identity mismatch")
        snapshot = self.state.get_snapshot(result["source_snapshot_id"])
        if (
            snapshot is None or not self.objects.verify(snapshot.object_sha256)
            or snapshot.object_sha256 != result["source_object_hash"]
        ):
            raise ValueError("Tushare raw reference provenance is incomplete")
        # Another same-source adapter may have populated the shared query cache.
        # Alias only the adapter identity; bytes and original capture time stay unchanged.
        if snapshot.source_id != self.provider_id:
            snapshot = snapshot.model_copy(update={
                "snapshot_id": f"{self.provider_id}:{snapshot.object_sha256}",
                "source_id": self.provider_id,
            })
            self.state.register_snapshot(snapshot)
        return result, snapshot

    def _metrics(
        self, result: dict[str, Any], snapshot: SourceSnapshot, row: dict[str, Any], subject: str,
    ) -> TushareMetricCapture:
        endpoint = result["request"]["endpoint"]
        fields = normalize_tushare_metrics(endpoint, row)
        capture = TushareMetricCapture(
            endpoint=endpoint, subject_id=subject,
            period=str(row.get("trade_date") or row.get("quarter") or row.get("month")),
            available_to_system_at=snapshot.available_to_system_at,
            source_snapshot_id=snapshot.snapshot_id,
            source_object_hash=snapshot.object_sha256,
            capture_artifact_id=result["artifact_id"], row_hash=content_hash(row),
            metrics=tuple(sorted(fields, key=lambda item: item.metric)),
        )
        ref = self.objects.put_json(capture.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=capture.artifact_id,
            artifact_type="TushareMetricCapture", schema_version=capture.schema_version,
            object_hash=ref.sha256, input_hashes=[snapshot.object_sha256],
        )
        return capture


def load_metric_capture(
    state: StateStore, objects: ObjectStore, artifact_id: str, *, subject_id: str, as_of: datetime,
) -> TushareMetricCapture:
    record = state.artifact_record(artifact_id)
    if record is None or record["type"] != "TushareMetricCapture":
        raise ValueError("unknown normalized Tushare metric capture")
    digest = str(record["object_hash"])
    if not objects.verify(digest):
        raise ValueError("Tushare metric object hash mismatch")
    capture = TushareMetricCapture.model_validate_json(objects.get_bytes(digest))
    if as_of.tzinfo is None or capture.available_to_system_at > as_of:
        raise ValueError("Tushare metric capture unavailable at cutoff")
    if capture.subject_id != subject_id:
        raise ValueError("Tushare metric subject mismatch")
    snapshot = state.get_snapshot(capture.source_snapshot_id)
    if (
        snapshot is None or snapshot.object_sha256 != capture.source_object_hash
        or snapshot.available_to_system_at != capture.available_to_system_at
        or not objects.verify(capture.source_object_hash)
    ):
        raise ValueError("Tushare metric raw lineage mismatch")
    source = state.artifact_record(capture.capture_artifact_id)
    if source is None or source["type"] != "TushareStructuredCapture":
        raise ValueError("Tushare metric query capture missing")
    source_hash = str(source["object_hash"])
    if not objects.verify(source_hash):
        raise ValueError("Tushare query capture corrupt")
    query = json.loads(objects.get_bytes(source_hash))
    if (
        query["request"]["endpoint"] != capture.endpoint
        or query["source_object_hash"] != capture.source_object_hash
    ):
        raise ValueError("Tushare metric endpoint mismatch")
    raw = json.loads(objects.get_bytes(capture.source_object_hash))
    rows = [dict(zip(raw["data"]["fields"], item, strict=True)) for item in raw["data"]["items"]]
    selected = [row for row in rows if content_hash(row) == capture.row_hash]
    if (
        len(selected) != 1
        or normalize_tushare_metrics(capture.endpoint, selected[0]) != capture.metrics
    ):
        raise ValueError("Tushare metric normalization differs from original response")
    row = selected[0]
    period = str(row.get("trade_date") or row.get("quarter") or row.get("month"))
    if period != capture.period:
        raise ValueError("Tushare metric period mismatch")
    if capture.endpoint == "daily_basic":
        market, symbol = subject_id.split(":", 1)
        if row["ts_code"] != _code(symbol, Market(market)):
            raise ValueError("Tushare daily metric security mismatch")
    elif subject_id != "CN":
        raise ValueError("Tushare macro subject must be CN")
    return capture


def normalize_tushare_metrics(endpoint: str, row: dict[str, Any]) -> tuple[TushareMetric, ...]:
    fields: list[TushareMetric] = []
    if endpoint not in {"daily_basic", "cn_gdp", "cn_cpi"}:
        raise ValueError("unsupported normalized metric endpoint")
    if not set(row) <= set(DEFAULT_FIELDS[endpoint]):
        raise ValueError("Tushare metric field semantics are not reviewed")
    identity = {"ts_code", "trade_date", "quarter", "month"}
    for key, raw in row.items():
        if key in identity:
            continue
        unit: Literal["CNY", "SHARE", "PERCENT", "RATIO", "INDEX"]
        basis: Literal["SESSION_END", "YTD", "MONTH", "QUARTER_YOY"]
        multiplier = Decimal(1)
        if endpoint == "daily_basic":
            basis = "SESSION_END"
            if key in {"total_share", "float_share", "free_share"}:
                unit, multiplier = "SHARE", Decimal(10000)
            elif key in {"total_mv", "circ_mv"}:
                unit, multiplier = "CNY", Decimal(10000)
            elif key == "close":
                unit = "CNY"
            elif key in {"turnover_rate", "turnover_rate_f", "dv_ratio", "dv_ttm"}:
                unit = "PERCENT"
            else:
                unit = "RATIO"
        elif endpoint == "cn_gdp":
            unit = "PERCENT" if key.endswith("_yoy") else "CNY"
            basis = "QUARTER_YOY" if key == "gdp_yoy" else "YTD"
            multiplier = Decimal(1) if unit == "PERCENT" else Decimal(100000000)
        else:
            unit = "PERCENT" if key.endswith(("_yoy", "_mom")) else "INDEX"
            basis = "YTD" if key.endswith("_accu") else "MONTH"
        value = None if raw is None else _decimal(raw) * multiplier
        fields.append(TushareMetric(metric=key, value=value, unit=unit, period_basis=basis))
    return tuple(sorted(fields, key=lambda item: item.metric))


def _code(symbol: str, market: Market) -> str:
    if market not in _SUFFIX or len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("Tushare reference requires one exchange-qualified stock")
    return symbol + "." + _SUFFIX[market]


def _decimal(raw: object) -> Decimal:
    if isinstance(raw, bool):
        raise ValueError("Boolean is not a numeric observation")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid numeric observation") from exc
    if not value.is_finite():
        raise ValueError("Non-finite numeric observation")
    return value


def normalize_instruments(
    rows: list[dict[str, Any]], snapshot: SourceSnapshot, market: Market, *,
    symbol: str | None = None, listing_status: str = "L",
) -> list[InstrumentRecord]:
    records: list[InstrumentRecord] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row["ts_code"])
        security = str(row["symbol"])
        if (
            code != _code(security, market) or (symbol is not None and security != symbol)
            or row.get("exchange") != _EXCHANGE[market]
            or row.get("list_status") != listing_status or row.get("curr_type") != "CNY"
        ):
            raise ValueError("Tushare stock identity, listing state or currency mismatch")
        if security in seen:
            raise ValueError("Duplicate Tushare stock identity")
        seen.add(security)
        listing = _optional_date(row.get("list_date"))
        delisting = _optional_date(row.get("delist_date"))
        if listing and delisting and delisting < listing:
            raise ValueError("Delisting precedes listing")
        name = str(row["name"]).strip()
        if not name:
            raise ValueError("Instrument name is empty")
        records.append(InstrumentRecord(
            instrument_id=f"{market.value}:{security}", market=market, symbol=security,
            name=name, instrument_type=InstrumentType.STOCK, tradable=listing_status == "L",
            status_date=snapshot.available_to_system_at.astimezone(SHANGHAI).date(),
            is_st="ST" in name.upper(), listing_date=listing, delisting_date=delisting,
            source_snapshot_id=snapshot.snapshot_id,
            available_to_system_at=snapshot.available_to_system_at,
        ))
    return sorted(records, key=lambda item: item.instrument_id)


def normalize_daily(
    rows: list[dict[str, Any]], snapshot: SourceSnapshot, symbol: str, market: Market,
    start: date, end: date,
) -> list[DailyBarObservation]:
    records: list[DailyBarObservation] = []
    seen: set[date] = set()
    for row in rows:
        session = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
        if row["ts_code"] != _code(symbol, market) or not start <= session <= end:
            raise ValueError("Tushare daily row is outside the identity/window")
        if session in seen:
            raise ValueError("Duplicate or revised Tushare daily row is ambiguous")
        seen.add(session)
        records.append(DailyBarObservation(
            observation_id=content_hash({
                "source": snapshot.object_sha256, "row": row, "mapping": "tushare-daily-v1",
            }),
            instrument_id=f"{market.value}:{symbol}", market=market, symbol=symbol,
            session_date=session, session_close_at=datetime.combine(session, time(15), SHANGHAI),
            open=_decimal(row["open"]), high=_decimal(row["high"]),
            low=_decimal(row["low"]), close=_decimal(row["close"]),
            previous_close=None if row.get("pre_close") is None else _decimal(row["pre_close"]),
            volume=_decimal(row["vol"]) * 100, amount=_decimal(row["amount"]) * 1000,
            source_snapshot_id=snapshot.snapshot_id,
            available_to_system_at=snapshot.available_to_system_at,
        ))
    return sorted(records, key=lambda item: item.session_date)


def _optional_date(raw: object) -> date | None:
    return None if raw is None or raw == "" else datetime.strptime(str(raw), "%Y%m%d").date()
