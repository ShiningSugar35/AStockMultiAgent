"""Low-frequency Sina raw, unadjusted 5-minute bars."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.providers.base import HttpProviderBase
from astock.providers.eastmoney import _validate_raw_5m_request
from astock.providers.symbols import sina_symbol
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    BarRequest,
    DataProviderCapability,
    Frequency,
    InstrumentType,
    Market,
    MarketBar,
    MarketDataBatch,
    ProviderStatus,
    TimestampSemantics,
    VolumeUnit,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class Sina5mProvider(HttpProviderBase):
    provider_id = "sina-5m"
    endpoint = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

    def capability(self) -> DataProviderCapability:
        return DataProviderCapability(
            provider_id=self.provider_id,
            markets=[Market.XSHG, Market.XSHE, Market.BJSE, Market.INDEX],
            instrument_types=[InstrumentType.STOCK, InstrumentType.INDEX],
            frequencies=[Frequency.M5],
            adjustment_modes=[AdjustmentMode.NONE],
            amount_supported=False,
            timestamp_semantics=TimestampSemantics.BAR_END,
            session_rules=(
                "A-share auction; recorded and 2026-07-13 live samples emit 5m bar-end labels"
            ),
            volume_unit=VolumeUnit.SHARE,
            rate_limit="Undocumented; use low-frequency sequential requests and raw cache",
            last_probe_at=datetime.now(UTC),
            quality_score=Decimal("0.50"),
            status=ProviderStatus.PARTIAL,
            limitations=[
                "No amount field",
                "Free history is limited to the latest 1023 bars",
                "920015 BJSE smoke exceeded the current dual-source deviation thresholds",
                "Undocumented endpoint requires continuing contract probes",
            ],
        )

    def fetch_bars(self, request: BarRequest) -> MarketDataBatch:
        _validate_raw_5m_request(request)
        params: dict[str, str | int] = {
            "symbol": sina_symbol(request),
            "scale": 5,
            "ma": "no",
            "datalen": min(request.limit, 1023),
        }
        response, latency_ms = self._get(self.endpoint, params=params)
        snapshot = self._persist_response(response)
        try:
            rows = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Sina returned an unexpected 5m payload",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            ) from exc
        if not isinstance(rows, list):
            raise ProviderError(
                "Sina did not return a bar list",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        bars: list[MarketBar] = []
        for raw_row in rows:
            try:
                if not isinstance(raw_row, dict):
                    raise TypeError("row is not an object")
                timestamp = datetime.strptime(str(raw_row["day"]), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_SHANGHAI
                )
                if timestamp < request.requested_start or timestamp > request.requested_end:
                    continue
                raw_values = {
                    key: raw_row.get(key)
                    for key in ("day", "open", "high", "low", "close", "volume")
                }
                bars.append(
                    MarketBar(
                        observation_id=content_hash(
                            {"provider": self.provider_id, "symbol": request.symbol, **raw_values}
                        ),
                        provider_id=self.provider_id,
                        symbol=request.symbol,
                        market=request.market,
                        frequency=Frequency.M5,
                        timestamp=timestamp,
                        timestamp_semantics=TimestampSemantics.BAR_END,
                        open=Decimal(str(raw_row["open"])),
                        high=Decimal(str(raw_row["high"])),
                        low=Decimal(str(raw_row["low"])),
                        close=Decimal(str(raw_row["close"])),
                        volume=Decimal(str(raw_row["volume"])),
                        volume_unit=VolumeUnit.SHARE,
                        amount=None,
                        amount_unit=AmountUnit.UNKNOWN,
                        adjustment_mode=AdjustmentMode.NONE,
                    )
                )
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise ProviderError(
                    "Sina returned an invalid 5m row",
                    failure_class=FailureClass.INVALID_RESPONSE,
                    details={"snapshot_id": snapshot.snapshot_id, "row": str(raw_row)[:200]},
                ) from exc
        bars.sort(key=lambda bar: bar.timestamp)
        status = ProviderStatus.AVAILABLE if bars else ProviderStatus.UNAVAILABLE
        batch_id = content_hash(
            {
                "provider": self.provider_id,
                "request": request,
                "bars": [bar.observation_id for bar in bars],
            }
        )
        return MarketDataBatch(
            batch_id=batch_id,
            provider_id=self.provider_id,
            request=request,
            requested_start=request.requested_start,
            requested_end=request.requested_end,
            actual_start=bars[0].timestamp if bars else None,
            actual_end=bars[-1].timestamp if bars else None,
            bar_count=len(bars),
            bars=bars,
            raw_snapshot_id=snapshot.snapshot_id,
            cursor=bars[-1].timestamp.isoformat() if bars else None,
            provider_latency_ms=latency_ms,
            provider_status=status,
        )
