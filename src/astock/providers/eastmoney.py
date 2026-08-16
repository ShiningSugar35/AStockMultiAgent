"""Low-frequency East Money raw, unadjusted 5-minute bars."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.providers.base import HttpProviderBase
from astock.providers.symbols import eastmoney_secid
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


class EastMoney5mProvider(HttpProviderBase):
    provider_id = "eastmoney-5m"
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def capability(self) -> DataProviderCapability:
        return DataProviderCapability(
            provider_id=self.provider_id,
            markets=[Market.XSHG, Market.XSHE, Market.BJSE, Market.INDEX],
            instrument_types=[InstrumentType.STOCK, InstrumentType.INDEX],
            frequencies=[Frequency.M5, Frequency.H1],
            adjustment_modes=[AdjustmentMode.NONE],
            amount_supported=True,
            timestamp_semantics=TimestampSemantics.BAR_END,
            session_rules="A-share continuous auction; provider emits 5m bar-end labels",
            volume_unit=VolumeUnit.LOT_100_SHARES,
            rate_limit="Undocumented; use sequential low-frequency cached requests",
            last_probe_at=datetime.now(UTC),
            quality_score=Decimal("0.70"),
            status=ProviderStatus.PARTIAL,
            limitations=["Free history is limited", "Endpoint may disconnect intermittently"],
        )

    def fetch_bars(self, request: BarRequest) -> MarketDataBatch:
        _validate_raw_5m_request(request)
        interval_minutes = 5 if request.frequency is Frequency.M5 else 60
        params: dict[str, str | int] = {
            "secid": eastmoney_secid(request),
            "klt": interval_minutes,
            "fqt": 0,
            "beg": request.requested_start.strftime("%Y%m%d"),
            "end": request.requested_end.strftime("%Y%m%d"),
            "lmt": request.limit,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        response, latency_ms = self._get(self.endpoint, params=params)
        snapshot = self._persist_response(response)
        try:
            payload = json.loads(response.content)
            rows = payload["data"]["klines"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(
                "East Money returned an unexpected 5m payload",
                failure_class=FailureClass.INVALID_RESPONSE,
                retryable=False,
                details={"snapshot_id": snapshot.snapshot_id},
            ) from exc
        if not isinstance(rows, list):
            raise ProviderError(
                "East Money did not return a bar list",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        bars: list[MarketBar] = []
        for raw_row in rows:
            try:
                fields = str(raw_row).split(",")
                timestamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=_SHANGHAI)
                if timestamp < request.requested_start or timestamp > request.requested_end:
                    continue
                raw_values = {
                    "timestamp": fields[0],
                    "open": fields[1],
                    "close": fields[2],
                    "high": fields[3],
                    "low": fields[4],
                    "volume": fields[5],
                    "amount": fields[6],
                }
                bars.append(
                    MarketBar(
                        observation_id=content_hash(
                            {"provider": self.provider_id, "symbol": request.symbol, **raw_values}
                        ),
                        provider_id=self.provider_id,
                        symbol=request.symbol,
                        market=request.market,
                        frequency=request.frequency,
                        timestamp=timestamp,
                        timestamp_semantics=TimestampSemantics.BAR_END,
                        open=Decimal(fields[1]),
                        close=Decimal(fields[2]),
                        high=Decimal(fields[3]),
                        low=Decimal(fields[4]),
                        volume=Decimal(fields[5]),
                        volume_unit=VolumeUnit.LOT_100_SHARES,
                        amount=Decimal(fields[6]),
                        amount_unit=AmountUnit.CNY,
                        adjustment_mode=AdjustmentMode.NONE,
                    )
                )
            except (IndexError, InvalidOperation, ValueError) as exc:
                raise ProviderError(
                    "East Money returned an invalid 5m row",
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


def _validate_raw_5m_request(request: BarRequest) -> None:
    if request.frequency not in {Frequency.M5, Frequency.H1}:
        raise ProviderError(
            "Provider only supports 5m or 60m in this adapter",
            failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
        )
    if request.adjustment_mode != AdjustmentMode.NONE:
        raise ProviderError(
            "Paper-trading provider only accepts unadjusted raw prices",
            failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
        )
