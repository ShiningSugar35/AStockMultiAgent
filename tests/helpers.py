from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from astock.core.hashing import content_hash
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    BarRequest,
    Frequency,
    Market,
    MarketBar,
    MarketDataBatch,
    ProviderStatus,
    TimestampSemantics,
    VolumeUnit,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def session_times(trading_date: date = date(2026, 7, 10)) -> list[datetime]:
    values: list[datetime] = []
    for start, end in ((time(9, 35), time(11, 30)), (time(13, 5), time(15, 0))):
        current = datetime.combine(trading_date, start, tzinfo=SHANGHAI)
        final = datetime.combine(trading_date, end, tzinfo=SHANGHAI)
        while current <= final:
            values.append(current)
            current += timedelta(minutes=5)
    return values


def make_batch(
    provider_id: str,
    *,
    volume_unit: VolumeUnit = VolumeUnit.SHARE,
    missing_index: int | None = None,
    bad_ohlc: bool = False,
    symbol: str = "600519",
) -> MarketDataBatch:
    timestamps = session_times()
    if missing_index is not None:
        timestamps.pop(missing_index)
    request = BarRequest(
        symbol=symbol,
        market=Market.XSHG,
        requested_start=datetime(2026, 7, 10, 0, 0, tzinfo=SHANGHAI),
        requested_end=datetime(2026, 7, 10, 23, 59, tzinfo=SHANGHAI),
        adjustment_mode=AdjustmentMode.NONE,
    )
    bars: list[MarketBar] = []
    for index, timestamp in enumerate(timestamps):
        open_price = Decimal("100.00") + Decimal(index) / 100
        close_price = open_price + Decimal("0.10")
        high_price = open_price + Decimal("0.20")
        low_price = open_price - Decimal("0.20")
        if bad_ohlc and index == 0:
            high_price = Decimal("99.00")
        volume_shares = Decimal(100000 + index * 100)
        volume = volume_shares / 100 if volume_unit == VolumeUnit.LOT_100_SHARES else volume_shares
        payload = {
            "provider": provider_id,
            "symbol": symbol,
            "timestamp": timestamp.isoformat(),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
        bars.append(
            MarketBar(
                observation_id=content_hash(payload),
                provider_id=provider_id,
                symbol=symbol,
                market=Market.XSHG,
                frequency=Frequency.M5,
                timestamp=timestamp,
                timestamp_semantics=TimestampSemantics.BAR_END,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                volume_unit=volume_unit,
                amount=volume_shares * close_price,
                amount_unit=AmountUnit.CNY,
                adjustment_mode=AdjustmentMode.NONE,
            )
        )
    return MarketDataBatch(
        batch_id=content_hash(
            {"provider": provider_id, "observations": [bar.observation_id for bar in bars]}
        ),
        provider_id=provider_id,
        request=request,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        actual_start=bars[0].timestamp,
        actual_end=bars[-1].timestamp,
        bar_count=len(bars),
        bars=bars,
        raw_snapshot_id=f"{provider_id}:{'a' * 64}",
        cursor=bars[-1].timestamp.isoformat(),
        provider_latency_ms=10,
        provider_status=ProviderStatus.AVAILABLE,
    )
