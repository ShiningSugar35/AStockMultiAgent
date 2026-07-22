from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

import astock.providers.baostock as baostock_module
from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.providers import BaoStockReferenceProvider, EastMoneyReferenceProvider
from astock.providers.baostock import BaoStockCaptureError
from astock.schemas import FetchStatus, Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "reference"


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state


def test_partial_sdk_envelope_preserves_fetched_prefix_and_snapshot(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / "baostock", fixture_root)
    path = fixture_root / "market_daily_unadjusted.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"] = payload["rows"][:1]
    payload["complete"] = False
    payload["result_error_code"] = "ITERATION_INTERRUPTED"
    payload["result_error_message"] = "simulated interruption"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state = _state(tmp_path)
    objects = ObjectStore(tmp_path / "objects")
    provider = BaoStockReferenceProvider(objects, state, fixture_root)

    envelope, snapshot = provider.fetch(
        "market.daily_unadjusted",
        {
            "symbol": "600519",
            "market": "XSHG",
            "start": "2026-07-20",
            "end": "2026-07-22",
            "adjustflag": "3",
        },
    )

    assert not envelope.complete and len(envelope.rows) == 1
    assert snapshot.fetch_status is FetchStatus.PARTIAL
    assert objects.verify(snapshot.object_sha256)
    assert state.get_snapshot(snapshot.snapshot_id) is not None


def test_malformed_baostock_envelope_is_snapshotted_before_strict_rejection(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / "baostock", fixture_root)
    path = fixture_root / "market_daily_unadjusted.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][1] = payload["rows"][1][:-1]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state = _state(tmp_path)
    objects = ObjectStore(tmp_path / "objects")
    provider = BaoStockReferenceProvider(objects, state, fixture_root)

    with pytest.raises(BaoStockCaptureError) as raised:
        provider.fetch(
            "market.daily_unadjusted",
            {
                "symbol": "600519",
                "market": "XSHG",
                "start": "2026-07-20",
                "end": "2026-07-22",
                "adjustflag": "3",
            },
        )

    snapshot = raised.value.snapshot
    assert objects.verify(snapshot.object_sha256)
    raw = json.loads(objects.get_bytes(snapshot.object_sha256))
    assert len(raw["rows"]) == 3
    assert len(raw["rows"][1]) == len(raw["fields"]) - 1


def test_baostock_login_exception_still_logs_out_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenLoginSdk:
        logout_calls = 0

        @staticmethod
        def login() -> object:
            raise OSError("simulated login failure")

        @classmethod
        def logout(cls) -> None:
            cls.logout_calls += 1

    state = _state(tmp_path)
    objects = ObjectStore(tmp_path / "objects")
    provider = BaoStockReferenceProvider(objects, state, FIXTURES / "baostock")
    monkeypatch.setattr(
        baostock_module.importlib, "import_module", lambda _name: BrokenLoginSdk
    )

    envelope, snapshot = provider.fetch("instrument.master", {}, live=True)

    assert envelope.complete is False
    assert envelope.result_error_code == "LOGIN_EXCEPTION"
    assert BrokenLoginSdk.logout_calls == 1
    assert snapshot.fetch_status is FetchStatus.FETCH_FAILED
    assert objects.verify(snapshot.object_sha256)


def test_baostock_heartbeat_renews_during_blocking_sdk_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_started = threading.Event()
    release_query = threading.Event()

    class Result:
        error_code = "0"
        error_msg = "success"
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]

        @staticmethod
        def next() -> bool:
            return False

    class BlockingSdk:
        @staticmethod
        def login() -> object:
            return type("Login", (), {"error_code": "0", "error_msg": "success"})()

        @staticmethod
        def query_stock_basic(*, code: str) -> Result:
            assert code == ""
            query_started.set()
            assert release_query.wait(timeout=3)
            return Result()

        @staticmethod
        def logout() -> None:
            return None

    state = _state(tmp_path)
    second_state = StateStore(state.path, PROJECT_ROOT / "migrations")
    provider = BaoStockReferenceProvider(
        ObjectStore(tmp_path / "objects"), state, FIXTURES / "baostock"
    )
    monkeypatch.setattr(
        baostock_module.importlib, "import_module", lambda _name: BlockingSdk
    )
    monkeypatch.setattr(baostock_module, "_LEASE_TTL", baostock_module.timedelta(seconds=0.2))
    monkeypatch.setattr(baostock_module, "_LEASE_HEARTBEAT_SECONDS", 0.03)
    result: list[object] = []

    worker = threading.Thread(
        target=lambda: result.append(provider.fetch("instrument.master", {}, live=True)),
        daemon=True,
    )
    worker.start()
    assert query_started.wait(timeout=2)
    time.sleep(0.5)
    now = baostock_module.datetime.now(baostock_module.UTC)
    intruder = second_state.acquire_reference_provider_lease(
        baostock_module._LEASE_KEY,
        "intruder",
        now=now,
        lease_until=now + baostock_module.timedelta(seconds=1),
    )
    release_query.set()
    worker.join(timeout=3)

    assert intruder is None
    assert len(result) == 1


@pytest.mark.parametrize(
    ("exception", "failure_class"),
    [
        (httpx.ConnectError("offline"), FailureClass.NETWORK),
        (httpx.ReadTimeout("slow"), FailureClass.TIMEOUT),
    ],
)
def test_eastmoney_live_transport_failures_are_classified(
    tmp_path: Path, exception: Exception, failure_class: FailureClass
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise exception

    state = _state(tmp_path)
    provider = EastMoneyReferenceProvider(
        ObjectStore(tmp_path / "objects"),
        state,
        FIXTURES / "eastmoney",
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )
    with pytest.raises(ProviderError) as raised:
        provider.fetch_master(Market.BJSE, live=True)
    assert raised.value.failure_class is failure_class


def test_incomplete_baostock_daily_uses_direct_eastmoney_not_akshare(tmp_path: Path) -> None:
    fixture_root = tmp_path / "reference"
    shutil.copytree(FIXTURES, fixture_root)
    path = fixture_root / "baostock" / "market_daily_unadjusted.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"] = []
    payload["complete"] = False
    payload["result_error_code"] = "NETWORK"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state = _state(tmp_path)
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        fixture_root,
    )

    report = service.sync_daily(
        "600519",
        Market.XSHG,
        date(2026, 7, 20),
        date(2026, 7, 22),
    )

    assert report.provider_id == "eastmoney-reference"
    assert "EASTMONEY_FALLBACK_USED" in report.reason_codes
    assert all("akshare" not in item.lower() for item in report.reason_codes)
    status = service.status(report.dataset_kind, report.scope_key)
    descriptor = status["release"]["canonical_files"][0]
    row = json.loads(
        pq.ParquetFile(service.parquet.root / descriptor["path"])
        .read()
        .column("record_json")
        .to_pylist()[0]
    )
    assert row["volume"] == "2500000"
    assert row["volume_unit"] == "SHARE"
    average_price = Decimal(row["amount"]) / Decimal(row["volume"])
    assert Decimal(row["low"]) <= average_price <= Decimal(row["high"])


def test_eastmoney_same_body_returns_original_persisted_snapshot(tmp_path: Path) -> None:
    state = _state(tmp_path)
    provider = EastMoneyReferenceProvider(
        ObjectStore(tmp_path / "objects"), state, FIXTURES / "eastmoney"
    )
    _, first = provider.fetch_daily(
        "600519", Market.XSHG, "2026-07-20", "2026-07-22"
    )
    _, second = provider.fetch_daily(
        "600519", Market.XSHG, "2026-07-20", "2026-07-22"
    )
    assert second == first
