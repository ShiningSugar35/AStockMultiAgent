from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astock.core.object_store import ObjectStore
from astock.market_data.quality import cross_validate_batches, validate_batch
from astock.providers import EastMoney5mProvider, Sina5mProvider
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    InstrumentType,
    Market,
    QualityStatus,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("ASTOCK_RUN_LIVE") != "1",
        reason="set ASTOCK_RUN_LIVE=1 for low-frequency external provider probes",
    ),
]

SHANGHAI = ZoneInfo("Asia/Shanghai")
CASES = [
    ("600519", Market.XSHG, InstrumentType.STOCK),
    ("000001", Market.XSHE, InstrumentType.STOCK),
    ("300750", Market.XSHE, InstrumentType.STOCK),
    ("688981", Market.XSHG, InstrumentType.STOCK),
    ("920015", Market.BJSE, InstrumentType.STOCK),
    ("000300", Market.INDEX, InstrumentType.INDEX),
]


@pytest.mark.parametrize(("symbol", "market", "instrument_type"), CASES)
def test_dual_provider_recent_5m_contract(
    symbol: str,
    market: Market,
    instrument_type: InstrumentType,
    tmp_path: Path,
    state,
) -> None:
    now = datetime.now(SHANGHAI)
    request = BarRequest(
        symbol=symbol,
        market=market,
        instrument_type=instrument_type,
        requested_start=now - timedelta(days=14),
        requested_end=now,
        adjustment_mode=AdjustmentMode.NONE,
    )
    objects = ObjectStore(tmp_path / "objects")
    east = EastMoney5mProvider(objects, state).fetch_bars(request)
    sina = Sina5mProvider(objects, state).fetch_bars(request)
    assert validate_batch(east).quality_status is not QualityStatus.FAIL
    assert validate_batch(sina).quality_status is not QualityStatus.FAIL
    report = cross_validate_batches(east, sina)
    assert report.quality_status is not QualityStatus.FAIL
    coverage_ratio = report.cross_source_diffs["coverage_ratio"]
    assert isinstance(coverage_ratio, float)
    assert coverage_ratio >= 0.98
