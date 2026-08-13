"""Sina exact-security and daily reference fallback for current A-share research."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.schemas import Market, SourceSnapshot


class SinaReferenceProvider(HttpProviderBase):
    provider_id = "sina-reference"
    quote_endpoint = "https://hq.sinajs.cn/list="
    daily_endpoint = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        "CN_MarketDataService.getKLineData"
    )
    _LIVE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://finance.sina.com.cn/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if client is None:
            client = httpx.Client(
                timeout=12.0,
                follow_redirects=True,
                headers=self._LIVE_HEADERS,
            )
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_identity(
        self,
        symbol: str,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        provider_symbol = _sina_symbol(symbol, market)
        if live:
            response, _ = self._get(f"{self.quote_endpoint}{provider_symbol}", params={})
        else:
            response = self._recorded_response(
                "instrument_identity.js",
                f"{self.quote_endpoint}{provider_symbol}",
                content_type="application/javascript; charset=utf-8",
            )
        snapshot = self._persist_response(response)
        text = _response_text(response)
        payload = _parse_quote_text(text, provider_symbol)
        payload["_astock_request"] = {
            "market": market.value,
            "symbol": symbol,
            "provider_symbol": provider_symbol,
            "purpose": "INSTRUMENT_IDENTITY_EXACT",
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
        provider_symbol = _sina_symbol(symbol, market)
        if live:
            response, _ = self._get(
                self.daily_endpoint,
                params={
                    "symbol": provider_symbol,
                    "scale": 240,
                    "ma": "no",
                    "datalen": 1023,
                },
            )
        else:
            response = self._recorded_response(
                "daily_unadjusted.json",
                self.daily_endpoint,
                content_type="application/json; charset=utf-8",
            )
        snapshot = self._persist_response(response)
        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ValueError("Sina daily response is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Sina daily response root must be an object")
        payload["_astock_request"] = {
            "market": market.value,
            "symbol": symbol,
            "provider_symbol": provider_symbol,
            "start": start,
            "end": end,
            "scale": 240,
            "adjustment": "NONE",
            "volume_unit": "SHARE",
        }
        return payload, snapshot

    def _recorded_response(
        self,
        name: str,
        url: str,
        *,
        content_type: str,
    ) -> httpx.Response:
        path = (self.fixture_root / name).resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise ValueError(f"Missing recorded Sina reference fixture: {name}")
        return httpx.Response(
            200,
            content=path.read_bytes(),
            headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )


def _response_text(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "").lower()
    encoding = "gb18030" if "gb18030" in content_type or "gbk" in content_type else "utf-8"
    return response.content.decode(encoding, errors="strict")


def _parse_quote_text(text: str, provider_symbol: str) -> dict[str, object]:
    prefix = f'var hq_str_{provider_symbol}="'
    stripped = text.strip()
    if not stripped.startswith(prefix) or not stripped.endswith('";'):
        raise ValueError("Sina quote response boundary is malformed")
    values = stripped[len(prefix) : -2].split(",")
    if len(values) < 4 or not values[0].strip():
        raise ValueError("Sina quote response is incomplete")
    return {
        "provider_symbol": provider_symbol,
        "name": values[0].strip(),
        "previous_close": values[2],
        "current_price": values[3],
    }


def _sina_symbol(symbol: str, market: Market) -> str:
    if market is Market.XSHG:
        prefix = "sh"
    elif market is Market.XSHE:
        prefix = "sz"
    elif market is Market.BJSE:
        prefix = "bj"
    elif market is Market.INDEX:
        prefix = "sz" if symbol.startswith("399") else "sh"
    else:  # pragma: no cover - enum boundary
        raise ValueError(f"Unsupported Sina reference market: {market}")
    return f"{prefix}{symbol}"


__all__ = ["SinaReferenceProvider"]
