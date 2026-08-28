from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from astock.core.object_store import ObjectStore
from astock.core.source_resilience import SourceFailureClass
from astock.core.state import StateStore
from astock.market_data.official_calendar import (
    OfficialTradingCalendarResolver,
    load_official_trading_calendar,
)
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_config import ReferenceRouteStep
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.schemas import Market, ReferenceDatasetKind, TradingSession

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "configs" / "official_trading_calendar.yaml"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "reference"
AVAILABLE = datetime(2026, 8, 25, 3, 0, tzinfo=UTC)


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state


def test_official_calendar_config_cites_all_three_exchanges() -> None:
    config = load_official_trading_calendar(CONFIG)

    year = config.years[2026]
    assert set(year.sources) == {Market.XSHG, Market.XSHE, Market.BJSE}
    assert year.sources[Market.XSHG].url.startswith("https://www.sse.com.cn/")
    assert year.sources[Market.XSHE].url.startswith("https://www.szse.cn/")
    assert year.sources[Market.BJSE].url.startswith("https://www.bse.cn/")
    assert config.covers(Market.XSHG, date(2026, 1, 1), date(2026, 12, 31))
    assert not config.covers(Market.XSHG, date(2027, 1, 1), date(2027, 1, 2))
    assert config.open_dates(Market.XSHG, date(2026, 2, 13), date(2026, 2, 24)) == (
        date(2026, 2, 13),
        date(2026, 2, 24),
    )
    assert config.open_dates(Market.XSHG, date(2027, 1, 1), date(2027, 1, 2)) is None


@pytest.mark.parametrize("market", [Market.XSHG, Market.XSHE, Market.BJSE])
def test_official_calendar_reconstructs_known_open_and_closed_days(
    tmp_path: Path,
    market: Market,
) -> None:
    state = _state(tmp_path)
    objects = ObjectStore(tmp_path / "objects")
    resolver = OfficialTradingCalendarResolver(
        load_official_trading_calendar(CONFIG),
        objects,
        state,
    )

    result = resolver.materialize(
        market,
        date(2026, 2, 13),
        date(2026, 2, 24),
        available_at=AVAILABLE,
    )

    assert result is not None
    records, snapshot = result
    by_day = {item.session_date: item.is_open for item in records}
    assert len(records) == 12
    assert by_day[date(2026, 2, 13)] is True
    assert by_day[date(2026, 2, 14)] is False
    assert by_day[date(2026, 2, 16)] is False
    assert by_day[date(2026, 2, 23)] is False
    assert by_day[date(2026, 2, 24)] is True
    assert snapshot.source_id == "official-trading-calendar-config"
    assert objects.verify(snapshot.object_sha256)
    assert state.get_snapshot(snapshot.snapshot_id) is not None


def test_official_calendar_rejects_non_authoritative_source_domain(tmp_path: Path) -> None:
    config_path = tmp_path / "official_trading_calendar.yaml"
    config_path.write_text(
        CONFIG.read_text(encoding="utf-8").replace(
            "https://www.sse.com.cn/",
            "https://calendar.example.com/",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="domain is not authoritative"):
        load_official_trading_calendar(config_path)


def test_official_calendar_rejects_unconfigured_year(tmp_path: Path) -> None:
    resolver = OfficialTradingCalendarResolver(
        load_official_trading_calendar(CONFIG),
        ObjectStore(tmp_path / "objects"),
        _state(tmp_path),
    )

    assert resolver.materialize(Market.XSHG, date(2027, 1, 1), date(2027, 1, 5)) is None


def test_live_sync_calendar_requires_authoritative_search_before_api_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        FIXTURES,
    )

    def should_not_call_api(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("calendar API route must wait for authoritative Search")

    monkeypatch.setattr(service, "_run_calendar_route_step", should_not_call_api)
    report = service.sync_calendar(
        Market.XSHG,
        date(2027, 1, 1),
        date(2027, 1, 5),
        live=True,
    )

    assert report.status.value == "FAILED"
    assert report.reason_codes == [
        "OFFICIAL_CALENDAR_YEAR_NOT_CONFIGURED",
        "AUTHORITATIVE_SEARCH_REQUIRED",
        "AUTHORITATIVE_DOMAIN_ONLY",
    ]


def test_live_api_calendar_success_closes_breaker_and_preserves_step_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        FIXTURES,
    )
    for _ in range(2):
        service.source_breaker.record_failure(
            "baostock-reference",
            "market.calendar",
            SourceFailureClass.TRANSIENT_NETWORK,
        )

    session = TradingSession(
        created_at=AVAILABLE,
        exchange=Market.XSHG,
        session_date=date(2027, 1, 4),
        is_open=True,
        source_snapshot_id="test-calendar-snapshot",
        available_to_system_at=AVAILABLE,
    )

    def api_calendar(*_args: object, **_kwargs: object):
        return [session], [session.source_snapshot_id], AVAILABLE, ["API_CALENDAR_USED"], True

    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "_run_calendar_route_step", api_calendar)
    monkeypatch.setattr(
        service,
        "_release",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(status="COMPLETE"),
    )

    service.sync_calendar(
        Market.XSHG,
        date(2027, 1, 4),
        date(2027, 1, 4),
        live=True,
        official_search_completed=True,
    )

    breaker = service.source_breaker.status("baostock-reference", "market.calendar")
    assert breaker["state"] == "CLOSED"
    assert breaker["failure_count"] == 0
    assert captured["reasons"] == ["API_CALENDAR_USED"]


def test_live_sync_calendar_prefers_local_official_notice_without_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        FIXTURES,
    )

    def should_not_call_api(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("calendar API route must not run for a configured official year")

    monkeypatch.setattr(service, "_run_calendar_route_step", should_not_call_api)
    report = service.sync_calendar(
        Market.XSHG,
        date(2026, 2, 26),
        date(2026, 8, 25),
        live=True,
    )

    assert report.status.value == "COMPLETE"
    assert report.provider_id == "official-trading-calendar-config"
    assert report.coverage.record_count == 181
    assert report.reason_codes == ["OFFICIAL_NOTICE_CALENDAR_USED"]
    status = service.status(ReferenceDatasetKind.TRADING_CALENDAR, Market.XSHG.value)
    assert status["status"] == "AVAILABLE"
    assert status["release"]["provider_id"] == "official-trading-calendar-config"


def test_non_live_calendar_keeps_recorded_provider_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        FIXTURES,
    )
    called: list[str] = []
    original = service._run_calendar_route_step

    def wrapped(
        step: ReferenceRouteStep,
        exchange: Market,
        start: date,
        end: date,
        *,
        live: bool,
    ):
        called.append("api-route")
        return original(step, exchange, start, end, live=live)

    monkeypatch.setattr(service, "_run_calendar_route_step", wrapped)
    service.sync_calendar(Market.XSHG, date(2024, 1, 1), date(2024, 1, 3), live=False)

    assert called == ["api-route"]
