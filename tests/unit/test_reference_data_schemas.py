from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from astock.schemas import (
    AdjustmentMode,
    DailyBarObservation,
    InstrumentRecord,
    InstrumentType,
    Market,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_reference_contract_rejects_adjusted_daily_and_tradable_index() -> None:
    common = {
        "observation_id": "a" * 64,
        "instrument_id": "XSHG:600519",
        "market": Market.XSHG,
        "symbol": "600519",
        "session_date": date(2026, 7, 22),
        "session_close_at": datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI),
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.5"),
        "volume": Decimal("100"),
        "source_snapshot_id": "snapshot:test",
        "available_to_system_at": datetime(2026, 7, 22, 16, 0, tzinfo=SHANGHAI),
    }
    with pytest.raises(ValueError, match="unadjusted"):
        DailyBarObservation(**common, adjustment_mode=AdjustmentMode.QFQ)

    with pytest.raises(ValueError, match="not tradable"):
        InstrumentRecord(
            instrument_id="INDEX:000300",
            market=Market.INDEX,
            symbol="000300",
            name="沪深300",
            instrument_type=InstrumentType.INDEX,
            tradable=True,
            status_date=date(2026, 7, 22),
            is_st=False,
            source_snapshot_id="snapshot:test",
            available_to_system_at=datetime(2026, 7, 22, 16, 0, tzinfo=SHANGHAI),
        )


def test_daily_pit_requires_exact_shanghai_close_and_post_close_availability() -> None:
    common = {
        "observation_id": "b" * 64,
        "instrument_id": "XSHG:600519",
        "market": Market.XSHG,
        "symbol": "600519",
        "session_date": date(2026, 7, 22),
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.5"),
        "volume": Decimal("100"),
        "source_snapshot_id": "snapshot:test",
    }
    with pytest.raises(ValueError, match="Shanghai 15:00"):
        DailyBarObservation(
            **common,
            session_close_at=datetime(2026, 7, 22, 15, 0, 1, tzinfo=SHANGHAI),
            available_to_system_at=datetime(2026, 7, 22, 16, 0, tzinfo=SHANGHAI),
        )
    with pytest.raises(ValueError, match="before"):
        DailyBarObservation(
            **common,
            session_close_at=datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI),
            available_to_system_at=datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI)
            - timedelta(microseconds=1),
        )
