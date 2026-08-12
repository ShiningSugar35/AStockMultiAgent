"""Direct EastMoney reference-data backup adapter (never an AKShare wrapper)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.schemas import FetchStatus, Market, SourceSnapshot


class EastMoneyReferenceProvider(HttpProviderBase):
    provider_id = "eastmoney-reference"
    master_endpoint = "https://push2.eastmoney.com/api/qt/clist/get"
    daily_endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_master(
        self, market: Market | None, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if live:
            if market is None:
                raise ValueError("live EastMoney master fallback requires an explicit market")
            response, _ = self._get(
                self.master_endpoint,
                params={
                    "pn": 1,
                    "pz": 10000,
                    "fs": _market_filter(market),
                    "fields": "f12,f13,f14,f17,f18,f20,f21",
                },
            )
        else:
            response = self._recorded_response("instrument_master.json", self.master_endpoint)
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {"market": market.value if market else "ALL"}
        return payload, snapshot

    def fetch_master_page(
        self,
        market: Market,
        page: int,
        *,
        page_size: int = 100,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if page < 1 or page_size < 1 or page_size > 500:
            raise ValueError("invalid EastMoney master page request")
        if live:
            response, _ = self._get(
                self.master_endpoint,
                params={
                    "pn": page,
                    "pz": page_size,
                    "fs": _market_filter(market),
                    "fields": "f12,f13,f14,f17,f18,f20,f21",
                },
            )
        else:
            if page != 1:
                raise ValueError("recorded EastMoney master contains one frozen page")
            response = self._recorded_response("instrument_master.json", self.master_endpoint)
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {
            "market": market.value,
            "page": page,
            "page_size": page_size,
            "purpose": "INSTRUMENT_IDENTITY",
        }
        return payload, snapshot

    def fetch_seed_snapshot(
        self,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        """Fetch one broad current-market snapshot for research seeding only."""

        if market is Market.INDEX:
            raise ValueError("research seed snapshot requires an equity market")
        if live:
            response, _ = self._get(
                self.master_endpoint,
                params={
                    "pn": 1,
                    "pz": 10000,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f6",
                    "fs": _market_filter(market),
                    "fields": "f2,f3,f6,f8,f12,f13,f14,f20,f21,f24,f25",
                },
            )
        else:
            response = self._recorded_response(
                f"seed_snapshot_{market.value}.json",
                self.master_endpoint,
            )
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {
            "market": market.value,
            "purpose": "RESEARCH_SEED_ONLY",
        }
        return payload, snapshot

    def fetch_industry_boards(
        self,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        """Fetch EastMoney public industry-board taxonomy for expert-domain seeding."""

        if live:
            response, _ = self._get(
                self.master_endpoint,
                params={
                    "pn": 1,
                    "pz": 1000,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f3",
                    "fs": "m:90+t:2+f:!50",
                    "fields": "f12,f14",
                },
            )
        else:
            response = self._recorded_response("industry_boards.json", self.master_endpoint)
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {
            "purpose": "EXPERT_DOMAIN_TAXONOMY",
        }
        return payload, snapshot

    def fetch_industry_constituents(
        self,
        board_code: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        """Fetch public constituents for one previously discovered industry board."""

        if not board_code.startswith("BK") or not board_code[2:].isdigit():
            raise ValueError("invalid EastMoney industry board code")
        if live:
            response, _ = self._get(
                self.master_endpoint,
                params={
                    "pn": 1,
                    "pz": 1000,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f6",
                    "fs": f"b:{board_code}",
                    "fields": "f2,f3,f6,f8,f12,f13,f14,f20,f21,f24,f25",
                },
            )
        else:
            response = self._recorded_response(
                f"industry_{board_code}.json",
                self.master_endpoint,
            )
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {
            "board_code": board_code,
            "purpose": "EXPERT_DOMAIN_CONSTITUENTS",
        }
        return payload, snapshot

    def fetch_daily(
        self,
        symbol: str,
        market: Market,
        start: str,
        end: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if live:
            response, _ = self._get(
                self.daily_endpoint,
                params={
                    "secid": _secid(symbol, market),
                    "klt": 101,
                    "fqt": 0,
                    "beg": start.replace("-", ""),
                    "end": end.replace("-", ""),
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                },
            )
        else:
            response = self._recorded_response("daily_unadjusted.json", self.daily_endpoint)
        snapshot = self._persist_stable_response(response)
        payload = _decode_json(response.content)
        payload["_astock_request"] = {
            "symbol": symbol,
            "market": market.value,
            "start": start,
            "end": end,
            "fqt": 0,
            "volume_unit": "LOT_100_SHARES",
        }
        return payload, snapshot

    def _persist_stable_response(self, response: httpx.Response) -> SourceSnapshot:
        """Return the originally persisted availability for identical response bytes."""

        object_ref = self.object_store.put_bytes(response.content)
        snapshot_id = f"{self.provider_id}:{object_ref.sha256}"
        existing = self.state.get_snapshot(snapshot_id)
        if existing is not None:
            if not self.object_store.verify(existing.object_sha256):
                raise ValueError("Persisted EastMoney snapshot object is corrupt")
            return existing
        now = datetime.now(UTC)
        snapshot = SourceSnapshot(
            created_at=now,
            snapshot_id=snapshot_id,
            source_id=self.provider_id,
            object_sha256=object_ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=str(response.request.url),
            mime=response.headers.get("content-type", "application/octet-stream").split(";")[0],
            byte_size=object_ref.byte_size,
            headers_hash=content_hash(sorted(response.headers.items())),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_REFERENCE_DATA",
        )
        self.state.register_snapshot(snapshot)
        persisted = self.state.get_snapshot(snapshot_id)
        if persisted is None:
            raise ValueError("EastMoney snapshot registration failed")
        return persisted

    def _recorded_response(self, name: str, url: str) -> httpx.Response:
        path = (self.fixture_root / name).resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise ValueError(f"Missing recorded EastMoney fixture: {name}")
        return httpx.Response(
            200,
            content=path.read_bytes(),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", url),
        )


def _decode_json(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("EastMoney response root must be an object")
    return value


def _market_filter(market: Market) -> str:
    filters = {
        Market.XSHG: "m:1+t:2,m:1+t:23",
        Market.XSHE: "m:0+t:6,m:0+t:13,m:0+t:80",
        Market.BJSE: "m:0+t:81+s:2048",
        Market.INDEX: "m:1+s:2,m:0+t:5",
    }
    return filters[market]


def _secid(symbol: str, market: Market) -> str:
    if market is Market.XSHG:
        prefix = "1"
    elif market in {Market.XSHE, Market.BJSE}:
        prefix = "0"
    elif market is Market.INDEX:
        prefix = "0" if symbol.startswith("399") else "1"
    else:  # pragma: no cover
        raise ValueError(f"Unsupported EastMoney market: {market}")
    return f"{prefix}.{symbol}"


__all__ = ["EastMoneyReferenceProvider"]
