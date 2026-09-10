"""Tencent Finance exact/batch quote fallback for official-master-backed research seeds."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.schemas import FetchStatus, Market, SourceSnapshot

_QUOTE_ENDPOINT = "https://qt.gtimg.cn/q="
_PREFIX_BY_MARKET = {
    Market.XSHG: "sh",
    Market.XSHE: "sz",
    Market.BJSE: "bj",
}
_LINE_RE = re.compile(r'^v_(?P<symbol>(?:sh|sz|bj)\d{6})="(?P<body>.*)";$')
_MAX_BATCH_SIZE = 200
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class TencentReferenceProvider(HttpProviderBase):
    """Independent quote family; it never defines the stock Universe itself.

    Callers must supply the exact symbol set frozen from an Instrument Master. The
    adapter persists every raw batch before parsing and then persists one normalized
    aggregate whose lineage lists every raw SourceSnapshot.
    """

    provider_id = "tencent-reference"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        fixture_root: Path,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(object_store, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_identity(
        self,
        symbol: str,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        return self.fetch_seed_snapshot_for_symbols(market, [symbol], live=live)

    def fetch_seed_snapshot_for_symbols(
        self,
        market: Market,
        symbols: Sequence[str],
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        prefix = _PREFIX_BY_MARKET.get(market)
        if prefix is None:
            raise ValueError("Tencent quote fallback requires an equity market")
        requested = tuple(dict.fromkeys(str(item).strip() for item in symbols))
        if not requested or any(len(item) != 6 or not item.isdigit() for item in requested):
            raise ValueError("Tencent quote fallback requires unique six-digit stock symbols")
        if len(requested) != len(symbols):
            raise ValueError("Tencent quote fallback rejects duplicate requested symbols")

        raw_snapshots: list[SourceSnapshot] = []
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for start in range(0, len(requested), _MAX_BATCH_SIZE):
            batch = requested[start : start + _MAX_BATCH_SIZE]
            if live:
                query = ",".join(f"{prefix}{symbol}" for symbol in batch)
                response, _ = self._get(f"{_QUOTE_ENDPOINT}{query}", params={})
            else:
                if len(requested) != 1:
                    raise ValueError("recorded Tencent probe supports one exact instrument")
                fixture = self.fixture_root / "instrument_identity.txt"
                if not fixture.exists():
                    raise ValueError("recorded Tencent quote fixture is unavailable")
                response = httpx.Response(
                    200,
                    content=fixture.read_bytes(),
                    headers={"content-type": "text/plain; charset=gbk"},
                    request=httpx.Request("GET", f"{_QUOTE_ENDPOINT}{prefix}{batch[0]}"),
                )
            raw_snapshot = self._persist_response(response)
            raw_snapshots.append(raw_snapshot)
            decoded = response.content.decode("gb18030", errors="strict")
            parsed = self._parse_batch(decoded, market=market, requested=set(batch))
            for row in parsed:
                code = str(row["code"])
                if code in seen:
                    raise ValueError("Tencent quote fallback returned a duplicate stock symbol")
                seen.add(code)
                rows.append(row)

        requested_set = set(requested)
        returned_set = {str(item["code"]) for item in rows}
        if not returned_set.issubset(requested_set):
            raise ValueError("Tencent quote fallback returned an unrequested stock symbol")
        rows.sort(key=lambda item: str(item["code"]))
        payload: dict[str, object] = {
            "schema_version": "tencent-quote-batch-v1",
            "_astock_source": "TENCENT_QUOTE_BATCH",
            "_astock_request": {
                "market": market.value,
                "purpose": "RESEARCH_SEED_ONLY",
            },
            "created_at": datetime.now(UTC).isoformat(),
            "requested_symbol_count": len(requested),
            "returned_symbol_count": len(returned_set),
            "complete": returned_set == requested_set,
            "raw_snapshot_ids": [item.snapshot_id for item in raw_snapshots],
            "rows": rows,
        }
        ref = self.object_store.put_json(payload)
        available_at = max(item.available_to_system_at for item in raw_snapshots)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:batch:{ref.sha256}",
            source_id=self.provider_id,
            object_sha256=ref.sha256,
            fetched_at=datetime.now(UTC),
            available_to_system_at=available_at,
            source_url=_QUOTE_ENDPOINT,
            mime="application/json",
            byte_size=ref.byte_size,
            headers_hash=content_hash([item.snapshot_id for item in raw_snapshots]),
            fetch_status=(
                FetchStatus.SUCCEEDED if returned_set == requested_set else FetchStatus.PARTIAL
            ),
            rights_status="PUBLIC_REFERENCE_DATA",
        )
        self.state.register_snapshot(snapshot)
        return payload, snapshot

    @staticmethod
    def _parse_batch(
        text: str,
        *,
        market: Market,
        requested: set[str],
    ) -> list[dict[str, object]]:
        prefix = _PREFIX_BY_MARKET[market]
        result: list[dict[str, object]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _LINE_RE.match(line)
            if match is None:
                raise ValueError("Tencent quote fallback returned a malformed record")
            transport_symbol = match.group("symbol")
            code = transport_symbol[-6:]
            if not transport_symbol.startswith(prefix) or code not in requested:
                raise ValueError("Tencent quote fallback market/symbol provenance mismatch")
            body = match.group("body")
            if not body:
                # A missing quote is a coverage miss, not a reason to discard the
                # other symbols in the batch. The caller keeps the result PARTIAL.
                continue
            fields = body.split("~")
            if len(fields) <= 46 or fields[2] != code:
                raise ValueError("Tencent quote fallback schema is incomplete")
            name = fields[1].strip()
            price = _positive_number(fields[3])
            settlement = _positive_number(fields[4])
            turnover = _nonnegative_number(fields[38])
            float_cap_100m = _nonnegative_number(fields[44])
            amount = _amount_from_composite(fields[35])
            if not name or (price <= 0 and settlement <= 0):
                raise ValueError("Tencent quote fallback lacks a usable identity/price")
            if turnover < 0 or float_cap_100m < 0 or amount < 0:
                raise ValueError("Tencent quote fallback lacks required seed metrics")
            quote_time = _quote_time(fields[30])
            if quote_time > datetime.now(UTC) + _MAX_FUTURE_SKEW:
                raise ValueError("Tencent quote fallback timestamp is in the future")
            result.append(
                {
                    "symbol": transport_symbol,
                    "code": code,
                    "name": name,
                    "trade": price,
                    "settlement": settlement,
                    "amount": amount,
                    "turnoverratio": turnover,
                    "float_market_cap_cny": float_cap_100m * 100_000_000,
                    "quote_time": quote_time.isoformat(),
                }
            )
        return result


def _number(value: str) -> float:
    normalized = value.strip()
    if normalized in {"", "-", "--"}:
        return -1.0
    try:
        return float(normalized)
    except ValueError as exc:
        raise ValueError("Tencent quote fallback contains a non-numeric field") from exc


def _positive_number(value: str) -> float:
    number = _number(value)
    return number if number > 0 else 0.0


def _nonnegative_number(value: str) -> float:
    return _number(value)


def _amount_from_composite(value: str) -> float:
    parts = value.split("/")
    if len(parts) != 3:
        raise ValueError("Tencent quote fallback amount composite is malformed")
    return _nonnegative_number(parts[2])


def _quote_time(value: str) -> datetime:
    normalized = value.strip()
    if len(normalized) != 14 or not normalized.isdigit():
        raise ValueError("Tencent quote fallback timestamp is malformed")
    try:
        local = datetime.strptime(normalized, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError("Tencent quote fallback timestamp is invalid") from exc
    # Tencent A-share quote timestamps are Asia/Shanghai local time (UTC+08).
    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


__all__ = ["TencentReferenceProvider"]
