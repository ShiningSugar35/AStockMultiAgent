from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from astock.investor_orchestration.models import ScheduledWindow
from astock.investor_orchestration.store import InvestorOrchestrationStore


class StateTradingCalendar:
    """Read the canonical official calendar without scanning unrelated SQL tables.

    The existing validated year/closure implementation owns the rules. Missing
    coverage or not-yet-available official verification is not a closed session.
    """

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        *,
        exchanges: tuple[str, ...] = ("XSHG", "XSHE", "BJSE"),
        config_path: Path | None = None,
    ) -> None:
        from astock.market_data.official_calendar import load_official_trading_calendar
        from astock.schemas.market import Market

        self.store = store
        self.exchanges = tuple(Market(value) for value in exchanges)
        if not self.exchanges or Market.INDEX in self.exchanges:
            raise ValueError("calendar requires explicit supported stock exchanges")
        path = (
            config_path
            or Path(__file__).resolve().parents[3] / "configs/official_trading_calendar.yaml"
        )
        self.config = load_official_trading_calendar(path)

    def is_trading_day(self, day: date, *, as_of: datetime | None = None) -> bool:
        available_at = as_of or datetime.now(UTC)
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("calendar availability must be timezone-aware")
        year = self.config.years.get(day.year)
        if year is None or any(
            not self.config.covers(exchange, day, day) for exchange in self.exchanges
        ):
            raise RuntimeError("TRADING_CALENDAR_DATE_NOT_COVERED")
        if year.verified_at > available_at:
            raise RuntimeError("OFFICIAL_TRADING_CALENDAR_UNAVAILABLE_AS_OF")
        return any(bool(self.config.open_dates(exchange, day, day)) for exchange in self.exchanges)


@dataclass(frozen=True)
class ScheduleTickPlan:
    due: tuple[tuple[ScheduledWindow, str], ...]
    expired: tuple[tuple[ScheduledWindow, str], ...]
    trading_day: bool


class DailyTrackingSchedule:
    """Resolve pre-open, intraday and post-close buckets on trading days.

    Trading-day truth is injected from the existing official calendar service.
    The class deliberately has no weekday-only production fallback.
    """

    def __init__(
        self,
        *,
        market_timezone: str,
        window_times: Mapping[ScheduledWindow, tuple[time, ...]],
        is_trading_day: Callable[[date], bool],
    ) -> None:
        self.timezone = ZoneInfo(market_timezone)
        self.window_times = dict(window_times)
        self.is_trading_day = is_trading_day

    def plan(
        self,
        now: datetime,
        *,
        last_completed_buckets: set[str] | None = None,
        grace_minutes: int = 20,
        catch_up_minutes: int = 180,
        max_catch_up_buckets: int = 2,
    ) -> ScheduleTickPlan:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("schedule time must be timezone-aware")
        if grace_minutes < 0 or catch_up_minutes < grace_minutes:
            raise ValueError("catch_up_minutes must be at least grace_minutes")
        if max_catch_up_buckets < 0:
            raise ValueError("max_catch_up_buckets must be non-negative")
        local_now = now.astimezone(self.timezone)
        if not self.is_trading_day(local_now.date()):
            return ScheduleTickPlan(due=(), expired=(), trading_day=False)
        completed = last_completed_buckets or set()
        on_time: list[tuple[datetime, ScheduledWindow, str]] = []
        catch_up: list[tuple[datetime, ScheduledWindow, str]] = []
        expired: list[tuple[datetime, ScheduledWindow, str]] = []
        for window, times in self.window_times.items():
            for scheduled_time in times:
                scheduled_at = datetime.combine(
                    local_now.date(), scheduled_time, tzinfo=self.timezone
                )
                delay_seconds = (local_now - scheduled_at).total_seconds()
                if delay_seconds < 0:
                    continue
                bucket = (
                    f"{local_now.date().isoformat()}:{window.value}:"
                    f"{scheduled_time.strftime('%H:%M')}"
                )
                if bucket in completed:
                    continue
                candidate = (scheduled_at, window, bucket)
                if delay_seconds <= grace_minutes * 60:
                    on_time.append(candidate)
                elif delay_seconds <= catch_up_minutes * 60:
                    catch_up.append(candidate)
                else:
                    expired.append(candidate)
        selected_catch_up = sorted(catch_up, key=lambda item: item[0])[:max_catch_up_buckets]
        selected = sorted((*selected_catch_up, *on_time), key=lambda item: item[0])
        return ScheduleTickPlan(
            due=tuple((window, bucket) for _, window, bucket in selected),
            expired=tuple(
                (window, bucket) for _, window, bucket in sorted(expired, key=lambda item: item[0])
            ),
            trading_day=True,
        )

    def due(
        self,
        now: datetime,
        *,
        last_completed_buckets: set[str] | None = None,
        grace_minutes: int = 20,
    ) -> tuple[tuple[ScheduledWindow, str], ...]:
        return self.plan(
            now,
            last_completed_buckets=last_completed_buckets,
            grace_minutes=grace_minutes,
            catch_up_minutes=grace_minutes,
            max_catch_up_buckets=0,
        ).due

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        *,
        is_trading_day: Callable[[date], bool],
    ) -> DailyTrackingSchedule:
        raw_windows = config["windows"]
        if not isinstance(raw_windows, Mapping):
            raise ValueError("windows must be a mapping")
        parsed: dict[ScheduledWindow, tuple[time, ...]] = {}
        for name, raw in raw_windows.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"window {name} must be a mapping")
            values: list[str] = []
            if "local_time" in raw:
                values.append(str(raw["local_time"]))
            if "local_times" in raw:
                raw_times = raw["local_times"]
                if not isinstance(raw_times, list):
                    raise ValueError(f"window {name}.local_times must be a list")
                values.extend(str(item) for item in raw_times)
            parsed[ScheduledWindow(str(name))] = tuple(
                time.fromisoformat(value) for value in values
            )
        return cls(
            market_timezone=str(config.get("market_timezone", "Asia/Shanghai")),
            window_times=parsed,
            is_trading_day=is_trading_day,
        )
