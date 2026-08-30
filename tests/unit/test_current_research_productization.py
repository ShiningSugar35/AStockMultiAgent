from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import (
    MarketReferenceService,
    _parse_eastmoney_instruments,
    _parse_sina_daily,
)
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.dialects import load_provider_dialects
from astock.providers.sina_financial import _normalize_sina, _sina_report_dates, _sina_rows
from astock.research.acquisition import (
    CurrentResearchAcquisitionService,
    _shanghai_acquisition_dates,
)
from astock.research.presentation import audit_investor_answer, investor_view_from_run
from astock.schemas import FetchStatus, FinancialPeriodType, SourceSnapshot
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CurrentResearchAcquisitionStatus,
    InvestorGapCategory,
)
from astock.schemas.research_runtime import (
    ResearchRunMode,
    ResearchRunPerformanceSummary,
    ResearchRunReport,
    ResearchRunStage,
    ResearchRunStatus,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _paths(tmp_path: Path) -> ProjectPaths:
    runtime = tmp_path / "runtime"
    return ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def _attempt(
    capability: AcquisitionCapability,
    status: AcquisitionAttemptStatus = AcquisitionAttemptStatus.SUCCEEDED,
) -> AcquisitionAttempt:
    return AcquisitionAttempt(
        capability=capability,
        status=status,
        provider_path=["test-provider"],
        fallback_used=False,
        record_count=1,
        latency_ms=1,
        internal_reason_codes=[],
        source_snapshot_ids=[],
        created_at=NOW,
    )


def test_eastmoney_current_master_accepts_numeric_key_dict_shape() -> None:
    payload: dict[str, object] = {
        "rc": 0,
        "_astock_request": {"market": "XSHG"},
        "data": {
            "diff": {
                "1": {"f12": "600938", "f14": "中国海油"},
                "0": {"f12": "600519", "f14": "贵州茅台"},
            }
        },
    }

    records = _parse_eastmoney_instruments(payload, "snapshot:test", NOW, Market.XSHG)

    assert [item.symbol for item in records] == ["600519", "600938"]
    assert all(item.market is Market.XSHG for item in records)


def _reference_service(tmp_path: Path) -> MarketReferenceService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )


def _block_baostock(
    service: MarketReferenceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_source_attempt_block_reason",
        lambda provider_id, _capability, *, live, breaker_scope=None: (
            "CIRCUIT_OPEN" if live and provider_id == "baostock-reference" else None
        ),
    )


def _snapshot(name: str, source_id: str = "eastmoney-reference") -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=f"snapshot:{name}",
        source_id=source_id,
        object_sha256="a" * 64,
        fetched_at=NOW,
        available_to_system_at=NOW,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/current",
        mime="application/json",
        byte_size=1,
        headers_hash="b" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=NOW,
    )


def test_reference_retry_does_not_amplify_transport_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr("astock.market_data.reference._time.sleep", lambda _seconds: None)

    def transient() -> str:
        calls.append(1)
        raise ProviderError(
            "temporary",
            failure_class=FailureClass.NETWORK,
            retryable=True,
        )

    with pytest.raises(ProviderError):
        service._retry_reference_call(transient, live=True)
    assert len(calls) == 1

    calls.clear()

    def permanent() -> str:
        calls.append(1)
        raise ProviderError(
            "denied",
            failure_class=FailureClass.ACCESS_RESTRICTED,
            retryable=False,
        )

    with pytest.raises(ProviderError):
        service._retry_reference_call(permanent, live=True)
    assert len(calls) == 1


def test_exact_instrument_identity_prefers_single_security_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    _block_baostock(service, monkeypatch)

    def exact(symbol: str, market: Market, *, live: bool = False):
        assert symbol == "600989"
        assert market is Market.XSHG
        assert live
        return (
            {
                "rc": 0,
                "_astock_request": {
                    "market": "XSHG",
                    "symbol": "600989",
                    "purpose": "INSTRUMENT_IDENTITY_EXACT",
                },
                "data": {"f57": "600989", "f58": "宝丰能源", "f189": 20190516},
            },
            _snapshot("exact"),
        )

    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"), "fetch_identity", exact
    )
    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"),
        "fetch_master_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pagination must not run after exact identity succeeds")
        ),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "_release", lambda **kwargs: captured.update(kwargs) or kwargs)

    service.sync_instrument_identity("600989", Market.XSHG, live=True)

    records = captured["records"]
    assert isinstance(records, list) and len(records) == 1
    assert records[0].symbol == "600989"
    assert records[0].name == "宝丰能源"
    assert records[0].listing_date == date(2019, 5, 16)
    assert captured["provider_id"] == "eastmoney-reference"
    assert captured["complete"] is True


def test_exact_instrument_identity_falls_back_to_sina_before_bulk_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    _block_baostock(service, monkeypatch)
    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"),
        "fetch_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderError(
                "eastmoney exact unavailable",
                failure_class=FailureClass.NETWORK,
                retryable=False,
            )
        ),
    )

    def sina_exact(symbol: str, market: Market, *, live: bool = False):
        assert symbol == "600989"
        assert market is Market.XSHG
        assert live
        return (
            {
                "provider_symbol": "sh600989",
                "name": "宝丰能源",
                "_astock_request": {
                    "market": "XSHG",
                    "symbol": "600989",
                    "provider_symbol": "sh600989",
                    "purpose": "INSTRUMENT_IDENTITY_EXACT",
                },
            },
            _snapshot("sina-exact", "sina-reference"),
        )

    monkeypatch.setattr(
        service.provider_factory.create("sina-reference"), "fetch_identity", sina_exact
    )
    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"),
        "fetch_master_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bulk pagination must not run after Sina exact fallback")
        ),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "_release", lambda **kwargs: captured.update(kwargs) or kwargs)

    service.sync_instrument_identity("600989", Market.XSHG, live=True)

    records = captured["records"]
    assert isinstance(records, list) and len(records) == 1
    assert records[0].symbol == "600989"
    assert records[0].name == "宝丰能源"
    assert captured["provider_id"] == "sina-reference"
    assert captured["complete"] is True
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "EASTMONEY_EXACT_IDENTITY_FAILED" in reasons
    assert "SINA_FALLBACK_USED" in reasons


def test_exact_instrument_identity_paginates_eastmoney_after_baostock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    pages: list[int] = []
    _block_baostock(service, monkeypatch)
    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"),
        "fetch_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderError(
                "exact unavailable",
                failure_class=FailureClass.NETWORK,
                retryable=False,
            )
        ),
    )
    monkeypatch.setattr(
        service.provider_factory.create("sina-reference"),
        "fetch_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderError(
                "sina exact unavailable",
                failure_class=FailureClass.NETWORK,
                retryable=False,
            )
        ),
    )

    def page(market: Market, number: int, *, live: bool = False):
        assert market is Market.XSHG
        assert live
        pages.append(number)
        symbol = "600519" if number == 1 else "600938"
        return (
            {
                "rc": 0,
                "_astock_request": {"market": "XSHG"},
                "data": {"total": 200, "diff": {"0": {"f12": symbol, "f14": symbol}}},
            },
            _snapshot(str(number)),
        )

    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"), "fetch_master_page", page
    )
    captured: dict[str, object] = {}

    def release(**kwargs: object):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(service, "_release", release)
    service.sync_instrument_identity("600938", Market.XSHG, live=True)

    assert pages == [1, 2]
    assert captured["scope_key"] == "XSHG:600938"
    records = captured["records"]
    assert isinstance(records, list) and len(records) == 1
    assert records[0].symbol == "600938"
    assert captured["complete"] is True
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "EASTMONEY_FALLBACK_USED" in reasons


def test_corporate_action_official_lookup_runs_even_when_baostock_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    _block_baostock(service, monkeypatch)
    calls: list[str] = []

    def official(*_args: object):
        calls.append("official")
        return [], ["official:index"], NOW

    monkeypatch.setattr(service, "_official_actions_live", official)
    captured: dict[str, object] = {}

    def release(**kwargs: object):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(service, "_release", release)
    service.sync_corporate_actions(
        "600938",
        Market.XSHG,
        date(2026, 1, 1),
        date(2026, 8, 12),
        live=True,
    )

    assert calls == ["official"]
    assert captured["failed"] is False
    assert captured["provider_id"] == "cninfo-disclosures"
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "OFFICIAL_INDEX_CAPTURED_NO_MATCH" in reasons


def test_daily_market_falls_back_to_sina_when_baostock_and_eastmoney_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    _block_baostock(service, monkeypatch)
    monkeypatch.setattr(
        service.provider_factory.create("eastmoney-reference"),
        "fetch_daily",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderError(
                "eastmoney unavailable",
                failure_class=FailureClass.NETWORK,
                retryable=False,
            )
        ),
    )

    def sina_daily(
        symbol: str,
        market: Market,
        start: str,
        end: str,
        *,
        live: bool = False,
    ):
        assert (symbol, market, start, end, live) == (
            "600989",
            Market.XSHG,
            "2026-08-10",
            "2026-08-12",
            True,
        )
        return (
            {
                "result": {
                    "status": {"code": 0},
                    "data": [
                        {
                            "day": "2026-08-10",
                            "open": "23.10",
                            "high": "23.80",
                            "low": "22.95",
                            "close": "23.60",
                            "volume": "21000000",
                        },
                        {
                            "day": "2026-08-11",
                            "open": "23.60",
                            "high": "24.00",
                            "low": "23.20",
                            "close": "23.75",
                            "volume": "18000000",
                        },
                        {
                            "day": "2026-08-12",
                            "open": "23.70",
                            "high": "23.90",
                            "low": "23.10",
                            "close": "23.43",
                            "volume": "19500000",
                        },
                    ],
                },
                "_astock_request": {
                    "market": "XSHG",
                    "symbol": "600989",
                    "provider_symbol": "sh600989",
                    "start": "2026-08-10",
                    "end": "2026-08-12",
                    "scale": 240,
                    "adjustment": "NONE",
                    "volume_unit": "SHARE",
                },
            },
            _snapshot("sina-daily", "sina-reference"),
        )

    monkeypatch.setattr(
        service.provider_factory.create("sina-reference"), "fetch_daily", sina_daily
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "_release", lambda **kwargs: captured.update(kwargs) or kwargs)

    service.sync_daily(
        "600989",
        Market.XSHG,
        date(2026, 8, 10),
        date(2026, 8, 12),
        live=True,
    )

    records = captured["records"]
    assert isinstance(records, list) and len(records) == 3
    assert records[-1].close == Decimal("23.43")
    assert records[-1].amount is None
    assert captured["provider_id"] == "sina-reference"
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "EASTMONEY_FALLBACK_FAILED" in reasons
    assert "SINA_FALLBACK_USED" in reasons


def test_sina_daily_drops_incomplete_current_session_before_shanghai_close() -> None:
    available = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "result": {
            "status": {"code": 0},
            "data": [
                {
                    "day": "2026-08-12",
                    "open": "23.70",
                    "high": "23.90",
                    "low": "23.10",
                    "close": "23.43",
                    "volume": "19500000",
                },
                {
                    "day": "2026-08-13",
                    "open": "23.60",
                    "high": "23.64",
                    "low": "23.06",
                    "close": "23.10",
                    "volume": "42961910",
                },
            ],
        },
        "_astock_request": {
            "market": "XSHG",
            "symbol": "600989",
            "provider_symbol": "sh600989",
            "start": "2026-08-12",
            "end": "2026-08-13",
            "scale": 240,
            "adjustment": "NONE",
            "volume_unit": "SHARE",
        },
    }

    records = _parse_sina_daily(
        payload,
        "snapshot:sina",
        available,
        "600989",
        Market.XSHG,
        date(2026, 8, 12),
        date(2026, 8, 13),
    )

    assert [record.session_date for record in records] == [date(2026, 8, 12)]


def test_sina_current_dict_shape_preserves_native_scope_currency_and_converts_yuan() -> None:
    payload: dict[str, object] = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_list": {
                    "20251231": {
                        "rType": "合并期末",
                        "rCurrency": "CNY",
                        "data": [
                            {
                                "item_field": "TOTASSET",
                                "item_title": "资产总计",
                                "item_value": "1098559000000.000000",
                            },
                            {
                                "item_field": "TOTLIAB",
                                "item_title": "负债合计",
                                "item_value": "293375000000.000000",
                            },
                        ],
                    }
                }
            },
        }
    }

    dialect = load_provider_dialects(PROJECT_ROOT / "configs" / "provider_dialects.yaml")[
        "sina-financial"
    ]
    rows = _sina_rows(payload, "BALANCE_SHEET", dialect)
    normalized = _normalize_sina(rows, "600938", date(2025, 12, 31))

    assert normalized == [
        {
            "report_date": "2025-12-31",
            "statement_scope": "CONSOLIDATED",
            "currency": "CNY",
            "total_assets": "109855900.000000",
            "total_liabilities": "29337500.000000",
            "company_id": "600938",
            "period_end": "2025-12-31",
            "scope": "CONSOLIDATED",
        }
    ]


def test_sina_report_period_index_discovers_early_disclosed_half_year() -> None:
    payload: dict[str, object] = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_date": [
                    {"date_value": "20260630"},
                    {"date_value": "20260331"},
                    {"date_value": "20251231"},
                ]
            },
        }
    }

    dialect = load_provider_dialects(PROJECT_ROOT / "configs" / "provider_dialects.yaml")[
        "sina-financial"
    ]
    assert _sina_report_dates(payload, dialect) == [
        date(2026, 6, 30),
        date(2026, 3, 31),
        date(2025, 12, 31),
    ]


def test_current_period_discovery_uses_actual_disclosed_period_not_month_guess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    service = CurrentResearchAcquisitionService(paths, state, ObjectStore(paths.objects))

    class PeriodProvider:
        provider_id = "period-index-provider"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def discover_report_periods(self, *_args: object, **_kwargs: object):
            return (
                [date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31)],
                SimpleNamespace(snapshot_id="period-index"),
            )

    class PeriodFactory:
        def definitions_for_capability(self, capability: str):
            assert capability == "financial.report_period_index"
            return [SimpleNamespace(provider_id="period-index-provider")]

        def create(self, provider_id: str):
            assert provider_id == "period-index-provider"
            return PeriodProvider()

    fake_financial = SimpleNamespace(
        provider_factory=PeriodFactory(),
        providers={"period-index-provider": PeriodProvider()},
    )
    monkeypatch.setattr(service, "_financial_service", lambda: fake_financial)

    specs, reasons = service._discover_financial_periods("600989", Market.XSHG, date(2026, 8, 13))

    assert reasons == ["REPORT_PERIOD_INDEX_USED:period-index-provider"]
    assert specs == [
        (
            AcquisitionCapability.FINANCIAL_ANNUAL,
            date(2025, 12, 31),
            FinancialPeriodType.ANNUAL,
        ),
        (
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
            date(2026, 6, 30),
            FinancialPeriodType.SEMIANNUAL,
        ),
    ]


def test_current_acquisition_uses_shanghai_date_and_last_closed_daily_bound() -> None:
    before_close = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    after_close = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
    after_utc_rollover = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)

    assert _shanghai_acquisition_dates(before_close) == (
        date(2026, 8, 27),
        date(2026, 8, 26),
    )
    assert _shanghai_acquisition_dates(after_close) == (
        date(2026, 8, 27),
        date(2026, 8, 27),
    )
    assert _shanghai_acquisition_dates(after_utc_rollover) == (
        date(2026, 8, 28),
        date(2026, 8, 27),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        _shanghai_acquisition_dates(datetime(2026, 8, 27, 12, 0))


def test_current_acquisition_freezes_decision_time_after_acquisition_and_keeps_manual_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    times = iter([NOW, NOW + timedelta(seconds=2)])
    service = CurrentResearchAcquisitionService(paths, state, objects, clock=lambda: next(times))
    monkeypatch.setattr(
        service,
        "_discover_financial_periods",
        lambda *_args: (
            [
                (
                    AcquisitionCapability.FINANCIAL_ANNUAL,
                    date(2025, 12, 31),
                    FinancialPeriodType.ANNUAL,
                ),
                (
                    AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                    date(2026, 6, 30),
                    FinancialPeriodType.SEMIANNUAL,
                ),
            ],
            [],
        ),
    )

    def fake_reference(capability: AcquisitionCapability, _action: object) -> AcquisitionAttempt:
        return _attempt(capability)

    def fake_financial(
        capability: AcquisitionCapability,
        _company_id: str,
        _market: Market,
        _period_end: date,
        _period_type: object,
        *,
        identity_verified: bool = True,
    ) -> AcquisitionAttempt:
        assert identity_verified
        return _attempt(capability)

    monkeypatch.setattr(service, "_reference_attempt", fake_reference)
    monkeypatch.setattr(service, "_financial_attempt", fake_financial)

    report = service.acquire("600938", Market.XSHG)

    assert report.started_at == NOW
    assert report.decision_as_of == NOW + timedelta(seconds=2)
    assert report.question_time_anchor_used is False
    assert report.decision_snapshot_frozen_after_acquisition is True
    assert report.historical_and_prospective_pit_preserved is True
    assert report.automatic_resolution_budget_seconds == 1800
    assert report.manual_escalation_after_automatic_exhaustion is True
    assert report.parallel_acquisition_used is True
    assert report.external_research_needs == []
    assert report.manual_actions == []


def test_same_request_reuse_reruns_only_failed_capability_and_preserves_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    times = iter(
        [
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=3),
        ]
    )
    service = CurrentResearchAcquisitionService(
        paths,
        state,
        objects,
        clock=lambda: next(times),
    )
    monkeypatch.setattr(
        service,
        "_discover_financial_periods",
        lambda *_args: (
            [
                (
                    AcquisitionCapability.FINANCIAL_ANNUAL,
                    date(2025, 12, 31),
                    FinancialPeriodType.ANNUAL,
                ),
                (
                    AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                    date(2026, 6, 30),
                    FinancialPeriodType.SEMIANNUAL,
                ),
            ],
            [],
        ),
    )
    calls: list[AcquisitionCapability] = []
    counts: dict[AcquisitionCapability, int] = {}

    def fake_task(
        capability: AcquisitionCapability,
        *_args: object,
        **_kwargs: object,
    ) -> Callable[[], AcquisitionAttempt]:
        def run() -> AcquisitionAttempt:
            calls.append(capability)
            current = counts.get(capability, 0) + 1
            counts[capability] = current
            ref = objects.put_json(
                {
                    "capability": capability.value,
                    "attempt": current,
                }
            )
            snapshot = SourceSnapshot(
                snapshot_id=f"snapshot:reuse:{capability.value}:{current}",
                source_id="test-current-research",
                object_sha256=ref.sha256,
                fetched_at=NOW,
                available_to_system_at=NOW,
                fetch_status=FetchStatus.SUCCEEDED,
                source_url="https://example.invalid/current-research",
                mime="application/json",
                byte_size=ref.byte_size,
                headers_hash="b" * 64,
                rights_status="PUBLIC_REFERENCE_DATA",
                created_at=NOW,
            )
            state.register_snapshot(snapshot)
            status = AcquisitionAttemptStatus.SUCCEEDED
            if capability is AcquisitionCapability.FINANCIAL_ANNUAL and current == 1:
                status = AcquisitionAttemptStatus.PARTIAL
            return AcquisitionAttempt(
                capability=capability,
                status=status,
                provider_path=["test-current-research"],
                fallback_used=False,
                record_count=1,
                latency_ms=10,
                internal_reason_codes=[],
                source_snapshot_ids=[snapshot.snapshot_id],
                created_at=NOW,
            )

        return run

    monkeypatch.setattr(service, "_task_for_capability", fake_task)

    first = service.acquire("600938", Market.XSHG)
    first_call_count = len(calls)
    second = service.acquire(
        "600938",
        Market.XSHG,
        reuse_report_artifact_id=first.report_id,
    )
    second_calls = calls[first_call_count:]

    assert first_call_count == len(first.attempts) == 5
    assert second_calls == [AcquisitionCapability.FINANCIAL_ANNUAL]
    assert 1 - len(second_calls) / first_call_count >= 0.30
    assert second.reused_report_artifact_id == first.report_id
    assert second.status is CurrentResearchAcquisitionStatus.READY
    reused = [
        item
        for item in second.attempts
        if "SAME_REQUEST_VERIFIED_REUSE" in item.internal_reason_codes
    ]
    assert len(reused) == 4
    assert all(item.latency_ms == 0 for item in reused)
    first_record = state.artifact_record(first.report_id)
    second_record = state.artifact_record(second.report_id)
    assert first_record is not None and second_record is not None
    assert str(first_record["object_hash"]) in second_record["input_hashes"]


def test_same_request_reuse_rejects_tampered_snapshot(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    service = CurrentResearchAcquisitionService(paths, state, objects)
    ref = objects.put_json({"capability": AcquisitionCapability.INSTRUMENT_IDENTITY.value})
    snapshot = SourceSnapshot(
        snapshot_id="snapshot:reuse:tampered",
        source_id="test-current-research",
        object_sha256=ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/current-research",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="c" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    attempt = AcquisitionAttempt(
        capability=AcquisitionCapability.INSTRUMENT_IDENTITY,
        status=AcquisitionAttemptStatus.SUCCEEDED,
        provider_path=["test-current-research"],
        fallback_used=False,
        record_count=1,
        latency_ms=10,
        internal_reason_codes=[],
        source_snapshot_ids=[snapshot.snapshot_id],
        created_at=NOW,
    )

    assert service._attempt_snapshots_reusable(attempt, NOW + timedelta(seconds=1))
    objects.path_for(ref.sha256).write_bytes(b"tampered")
    assert not service._attempt_snapshots_reusable(attempt, NOW + timedelta(seconds=1))


def test_financial_secondary_hints_prefer_source_with_native_scope_currency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    service = CurrentResearchAcquisitionService(paths, state, ObjectStore(paths.objects))

    class FailingEastMoney:
        provider_id = "eastmoney-financial"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fetch(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("EastMoney should not run after Sina produced usable rows")

    class WorkingSina:
        provider_id = "sina-financial"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fetch(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                snapshots=[SimpleNamespace(snapshot_id="snapshot:sina")],
                tables={
                    "BALANCE_SHEET": [{}],
                    "INCOME_STATEMENT": [{}],
                    "CASH_FLOW_STATEMENT": [{}],
                },
            )

    fake_financial = SimpleNamespace(
        config=SimpleNamespace(provider_order=("sina-financial", "eastmoney-financial")),
        providers={
            "sina-financial": WorkingSina(),
            "eastmoney-financial": FailingEastMoney(),
        },
    )
    monkeypatch.setattr(service, "_financial_service", lambda: fake_financial)

    attempt = service._financial_attempt(
        AcquisitionCapability.FINANCIAL_ANNUAL,
        "600938",
        Market.XSHG,
        date(2025, 12, 31),
        object(),  # type: ignore[arg-type]
        identity_verified=False,
    )

    assert attempt.status is AcquisitionAttemptStatus.PARTIAL
    assert attempt.provider_path == ["sina-financial"]
    assert attempt.fallback_used is False
    assert attempt.record_count == 3
    assert "INSTRUMENT_IDENTITY_UNVERIFIED" in attempt.internal_reason_codes


def test_investor_view_hides_internal_codes_and_execution_gap_by_default() -> None:
    report = ResearchRunReport(
        report_id="report:test",
        run_id="run:test",
        company_id="600938",
        as_of=NOW,
        mode=ResearchRunMode.LIVE,
        request_artifact_id="request:test",
        request_object_hash="1" * 64,
        status=ResearchRunStatus.NEEDS_INFO,
        current_stage=ResearchRunStage.EVIDENCE,
        checkpoints=[],
        output_artifacts={},
        needs_info_codes=[
            "CLAIM_IDS_REQUIRED",
            "EVIDENCE_PACK_REQUIRED",
            "TRADING_CLASSIFICATION_REQUIRED",
        ],
        investor_gap_categories=[InvestorGapCategory.EVIDENCE],
        performance=ResearchRunPerformanceSummary(
            wall_time_ms=1,
            knowledge_top_k_latency_ms=0,
            context_bytes=0,
            estimated_tokens=0,
            estimated_token_limit=0,
            cache_hit_count=0,
            provider_call_count=0,
        ),
        created_at=NOW,
    )

    view = investor_view_from_run(report)
    rendered = view.model_dump_json()

    assert "CLAIM_IDS_REQUIRED" not in rendered
    assert "EVIDENCE_PACK_REQUIRED" not in rendered
    assert "TRADING_CLASSIFICATION_REQUIRED" not in rendered
    assert "影响投资判断" in rendered
    assert view.internal_codes_exposed is False
    assert view.artifact_ids_exposed is False
    visible_text = " ".join([view.headline, *view.plain_language_gaps, view.next_step])
    assert audit_investor_answer(visible_text).status == "PASS"


def test_investor_answer_audit_rejects_backend_vocabulary_and_developer_meta() -> None:
    audit = audit_investor_answer(
        "这套系统先跑 research-plan，current_stage=EVIDENCE，"
        "然后因为 CLAIM_IDS_REQUIRED 返回 NEEDS_INFO；"
        "MarketPriceAnchor 和 ClassifiedTradeProtocol 还没有完成。"
    )

    assert audit.status == "FAIL"
    assert audit.raw_answer_echoed is False
    assert audit.internal_implementation_exposed is True
    assert audit.developer_meta_exposed is True
    assert set(audit.finding_codes) >= {
        "CLI_OR_PIPELINE_EXPOSED",
        "DEVELOPER_META_EXPOSED",
        "INTERNAL_PROTOCOL_TERM_EXPOSED",
        "RAW_MACHINE_STATE_EXPOSED",
    }


def test_investor_answer_audit_accepts_plain_language_investment_answer() -> None:
    audit = audit_investor_answer(
        "结论：我暂时不会追高。公司盈利趋势仍然不错，但当前价格已经反映了不少乐观预期。"
        "如果后续盈利继续超预期、估值回到更有安全边际的位置，买入吸引力会明显提高。"
    )

    assert audit.status == "PASS"
    assert audit.finding_codes == []
    assert audit.internal_implementation_exposed is False
    assert audit.developer_meta_exposed is False


def test_investor_answer_audit_rejects_bloat_and_repetition() -> None:
    repeated = "公司盈利仍在改善，但当前估值已经反映了较多乐观预期。"
    audit = audit_investor_answer(f"{repeated}\n{repeated}\n" + "风险需要继续观察。" * 300)

    assert audit.status == "FAIL"
    assert "INVESTOR_ANSWER_REPETITIVE" in audit.finding_codes
    assert "INVESTOR_ANSWER_TOO_LONG" in audit.finding_codes


def test_probe_is_lightweight_and_does_not_run_full_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    def forbidden_integrity(_self: StateStore) -> str:
        raise AssertionError("probe must not run PRAGMA integrity_check")

    monkeypatch.setattr(StateStore, "integrity_check", forbidden_integrity)
    invoked = CliRunner().invoke(app, ["probe"])

    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["state_health"]["status"] == "OK"
    assert payload["state_integrity"] == "NOT_RUN"
    assert payload["full_integrity_check_run"] is False


def test_live_research_no_longer_requires_question_time_as_of_but_recorded_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    runner = CliRunner()

    live = runner.invoke(app, ["research-plan", "600938", "--mode", "LIVE"])
    recorded = runner.invoke(app, ["research-plan", "600938"])

    assert live.exit_code == 0, live.output
    live_payload = json.loads(live.output)
    assert live_payload["company_id"] == "600938"
    assert recorded.exit_code != 0
    assert "--as-of is required for recorded or historical research" in recorded.output
