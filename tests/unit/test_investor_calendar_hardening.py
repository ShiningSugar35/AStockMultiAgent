"""Calendar authority and timezone regressions; official config is the only fact source."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from astock.investor_orchestration.models import ScheduledWindow
from astock.investor_orchestration.schedule_clock import DailyTrackingSchedule, StateTradingCalendar
from astock.investor_orchestration.store import InvestorOrchestrationStore


@pytest.fixture
def store(tmp_path: Path) -> InvestorOrchestrationStore:
    value = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    value.initialize()
    return value


def _inject_unrelated_session(store: InvestorOrchestrationStore, day: date) -> None:
    with store.transaction() as connection:
        connection.execute("CREATE TABLE unrelated_user_session(session_date TEXT)")
        connection.execute("INSERT INTO unrelated_user_session VALUES(?)", (day.isoformat(),))


def test_unrelated_session_row_does_not_turn_weekend_into_trading_day(
    store: InvestorOrchestrationStore,
) -> None:
    day = date(2026, 9, 6)
    _inject_unrelated_session(store, day)
    assert StateTradingCalendar(store).is_trading_day(day) is False


def test_official_config_does_not_require_a_fabricated_sql_calendar(
    store: InvestorOrchestrationStore,
) -> None:
    assert StateTradingCalendar(store).is_trading_day(date(2026, 9, 7)) is True


def test_uncovered_year_is_not_certified_by_a_similarly_named_table(
    store: InvestorOrchestrationStore,
) -> None:
    day = date(2099, 9, 7)
    _inject_unrelated_session(store, day)
    with pytest.raises(RuntimeError, match="NOT_COVERED|UNAVAILABLE"):
        StateTradingCalendar(store).is_trading_day(day)


def test_schedule_does_not_guess_the_timezone_of_a_naive_datetime() -> None:
    schedule = DailyTrackingSchedule(
        market_timezone="Asia/Shanghai",
        window_times={ScheduledWindow.PRE_OPEN: (time(9),)},
        is_trading_day=lambda _: True,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.plan(datetime(2026, 9, 7, 9, 1))
