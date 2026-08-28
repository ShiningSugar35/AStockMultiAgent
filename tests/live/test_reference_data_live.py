from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers import BaoStockReferenceProvider, EastMoneyReferenceProvider
from astock.schemas import Market

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("ASTOCK_RUN_LIVE") != "1", reason="explicit live opt-in"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def state_and_objects(tmp_path: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state, ObjectStore(tmp_path / "objects")


def test_baostock_089_master_calendar_and_unadjusted_daily_live(
    state_and_objects: tuple[StateStore, ObjectStore],
) -> None:
    state, objects = state_and_objects
    provider = BaoStockReferenceProvider(
        objects, state, PROJECT_ROOT / "tests" / "fixtures" / "reference" / "baostock"
    )
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    start = end - timedelta(days=30)
    master, _ = provider.fetch(
        "instrument.master", {"symbol": "600519", "market": "XSHG"}, live=True
    )
    calendar, _ = provider.fetch(
        "market.calendar",
        {"exchange": "XSHG", "start": start.isoformat(), "end": end.isoformat()},
        live=True,
    )
    daily, _ = provider.fetch(
        "market.daily_unadjusted",
        {
            "symbol": "600519",
            "market": "XSHG",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustflag": "3",
        },
        live=True,
    )

    assert master.sdk_version == "0.8.9"
    assert master.complete and master.rows
    assert calendar.complete and calendar.rows
    assert daily.complete
    assert all(len(row) == len(master.fields) for row in master.rows)
    assert all(len(row) == len(calendar.fields) for row in calendar.rows)
    assert all(len(row) == len(daily.fields) for row in daily.rows)
    adjustflag = daily.fields.index("adjustflag")
    assert {row[adjustflag] for row in daily.rows} == {"3"}
    daily_dates = [date.fromisoformat(row[daily.fields.index("date")]) for row in daily.rows]
    assert daily_dates == sorted(set(daily_dates))
    assert all(start <= item <= end for item in daily_dates)


@pytest.mark.parametrize("market", [Market.BJSE, Market.INDEX])
def test_eastmoney_explicit_bjse_and_index_boundaries_live(
    state_and_objects: tuple[StateStore, ObjectStore], market: Market
) -> None:
    state, objects = state_and_objects
    provider = EastMoneyReferenceProvider(
        objects, state, PROJECT_ROOT / "tests" / "fixtures" / "reference" / "eastmoney"
    )
    payload, snapshot = provider.fetch_master(market, live=True)

    assert snapshot.source_id == "eastmoney-reference"
    assert payload.get("rc") == 0
    data = payload.get("data")
    assert isinstance(data, dict)
    diff = data.get("diff")
    assert isinstance(diff, (list, dict))
    assert diff
