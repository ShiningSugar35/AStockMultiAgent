from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Lock

import pytest

import astock.research.acquisition as acquisition_module
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.schemas import FinancialPeriodType, Market
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> CurrentResearchAcquisitionService:
    runtime = tmp_path / "runtime"
    paths = ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    return CurrentResearchAcquisitionService(paths, state, ObjectStore(paths.objects))


def test_request_scoped_service_reuse_is_thread_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    construction_counts = {"market": 0, "financial": 0}

    class SlowMarketReferenceService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            construction_counts["market"] += 1
            time.sleep(0.01)

    class SlowFinancialSourceService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            construction_counts["financial"] += 1
            time.sleep(0.01)

    monkeypatch.setattr(
        acquisition_module,
        "MarketReferenceService",
        SlowMarketReferenceService,
    )
    monkeypatch.setattr(
        acquisition_module,
        "FinancialSourceService",
        SlowFinancialSourceService,
    )
    service = _service(tmp_path)

    with ThreadPoolExecutor(max_workers=16) as pool:
        market_instances = list(pool.map(lambda _: service._market_service(), range(64)))
        financial_instances = list(pool.map(lambda _: service._financial_service(), range(64)))

    assert len({id(item) for item in market_instances}) == 1
    assert len({id(item) for item in financial_instances}) == 1
    assert construction_counts == {"market": 1, "financial": 1}


def test_service_cache_is_scoped_to_one_acquisition_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    construction_count = 0

    class FakeMarketReferenceService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal construction_count
            construction_count += 1

    monkeypatch.setattr(
        acquisition_module,
        "MarketReferenceService",
        FakeMarketReferenceService,
    )
    first_request = _service(tmp_path / "first")
    second_request = _service(tmp_path / "second")

    first = first_request._market_service()
    second = second_request._market_service()

    assert first is first_request._market_service()
    assert second is second_request._market_service()
    assert first is not second
    assert construction_count == 2


def test_failed_service_construction_does_not_poison_request_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FlakyMarketReferenceService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(
        acquisition_module,
        "MarketReferenceService",
        FlakyMarketReferenceService,
    )
    service = _service(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic constructor failure"):
        service._market_service()

    recovered = service._market_service()
    assert recovered is service._market_service()
    assert attempts == 2


def test_reused_financial_service_serializes_non_thread_safe_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    financial = service._financial_service()
    counter_lock = Lock()
    active = 0
    max_active = 0

    def fake_sync_serialized(*_args: object, **_kwargs: object) -> object:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with counter_lock:
            active -= 1
        return object()

    monkeypatch.setattr(financial, "_sync_serialized", fake_sync_serialized)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                financial.sync,
                "600519",
                Market.XSHG,
                date(2025, 12, 31),
                FinancialPeriodType.ANNUAL,
                live=True,
            ),
            pool.submit(
                financial.sync,
                "600519",
                Market.XSHG,
                date(2026, 6, 30),
                FinancialPeriodType.SEMIANNUAL,
                live=True,
            ),
        ]
        [future.result() for future in futures]

    assert max_active == 1
