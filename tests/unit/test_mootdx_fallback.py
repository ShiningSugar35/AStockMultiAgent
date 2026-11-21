"""Contract and hostile-response checks for the isolated Mootdx fallback."""
from __future__ import annotations

import importlib.util
import json
import struct
import sys
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import astock.providers.mootdx_fallback as module
from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.request_budget import RequestBudgetExhausted, RequestExecutionBudget
from astock.core.state import StateStore
from astock.providers.mootdx_fallback import (
    MootdxFallbackProvider,
    MootdxServer,
    MootdxSettings,
    _normalize_bars,
    build_intraday_fallback_providers,
)
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    Frequency,
    Market,
    TimestampSemantics,
    VolumeUnit,
)

ROOT = Path(__file__).resolve().parents[2]


def _integration(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"test_mootdx_{name}", ROOT / "integrations" / "mootdx" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _settings(**updates: Any) -> MootdxSettings:
    return MootdxSettings(
        servers=[MootdxServer(host="110.41.147.114")], **updates
    )


def _request(**updates: Any) -> BarRequest:
    data = dict(
        symbol="600519", market=Market.XSHG, frequency=Frequency.H1,
        requested_start=datetime(2026, 11, 20, 0, tzinfo=UTC),
        requested_end=datetime(2026, 11, 20, 9, tzinfo=UTC),
    )
    data.update(updates)
    return BarRequest.model_validate(data)


def _row(**updates: Any) -> dict[str, Any]:
    data = {
        "datetime": "2026-11-20 10:30", "open": 1400.1, "high": 1402,
        "low": 1399, "close": 1401.2, "vol": 123, "amount": 17221476,
    }
    data.update(updates)
    return data


def _provider(objects: ObjectStore, state: StateStore) -> MootdxFallbackProvider:
    return MootdxFallbackProvider(objects, state, project_root=ROOT, settings=_settings())


def _fake_worker(
    monkeypatch: pytest.MonkeyPatch, objects: ObjectStore, rows: list[dict[str, Any]],
    **updates: Any,
) -> list[dict[str, Any]]:
    requests = []

    class FakeWorker:
        def __init__(self, _target: object, args: tuple[Any, ...], **_kwargs: Any):
            self.wire = args[2]
            requests.append(self.wire)

        def __call__(self, _task: object = None) -> str:
            payload = {
                "schema_version": "mootdx-raw-v1", "sdk": "mootdx", "sdk_version": "0.11.7",
                "transport_sdk": "tdxpy", "transport_sdk_version": "0.2.7",
                "request": self.wire, "adjustment_mode": "NONE", "rows": rows, "complete": True,
                **updates,
            }
            return objects.put_json(payload).sha256

    monkeypatch.setattr(module, "KillableResearchWorker", FakeWorker)
    monkeypatch.setattr(MootdxFallbackProvider, "availability", lambda _self: (True, "TEST"))
    return requests


def test_raw_prices_identity_units_timezone_and_snapshot(object_store, state, monkeypatch):
    requests = _fake_worker(monkeypatch, object_store, [_row()])
    batch = _provider(object_store, state).fetch_bars(_request())
    assert batch.bar_count == 1
    bar = batch.bars[0]
    assert bar.open == Decimal("1400.1")
    assert bar.market is Market.XSHG and bar.symbol == "600519"
    assert bar.timestamp.isoformat() == "2026-11-20T10:30:00+08:00"
    assert bar.timestamp_semantics is TimestampSemantics.BAR_END
    assert bar.volume == Decimal(123) and bar.volume_unit is VolumeUnit.LOT_100_SHARES
    assert bar.adjustment_mode is AdjustmentMode.NONE and bar.amount == Decimal(17221476)
    raw = json.loads(object_store.get_bytes(batch.raw_snapshot_id.split(":", 1)[1]))
    assert raw["sdk"] == "mootdx" and raw["sdk_version"] == "0.11.7"
    assert requests[0]["market"] == 1 and requests[0]["frequency"] == "60m"
    assert requests[0]["max_pages"] == 3


@pytest.mark.parametrize("changes", [
    {"frequency": Frequency.M1}, {"market": Market.BJSE},
    {"adjustment_mode": AdjustmentMode.QFQ}, {"symbol": "SH600519"},
])
def test_unsupported_requests_fail_before_worker(object_store, state, monkeypatch, changes):
    calls = _fake_worker(monkeypatch, object_store, [_row()])
    with pytest.raises(ProviderError) as caught:
        _provider(object_store, state).fetch_bars(_request(**changes))
    assert caught.value.failure_class is FailureClass.CAPABILITY_UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize("row", [
    _row(open="NaN"), _row(vol=-1), _row(high=1390), _row(low=1410),
    _row(datetime="2026-99-20 10:30"), _row(close=0),
])
def test_bad_numeric_or_timestamp_response_is_rejected(row):
    with pytest.raises(ProviderError) as caught:
        _normalize_bars({"rows": [row]}, _request(), "mootdx-fallback:test")
    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE


def test_exact_overlap_deduplicated_conflicting_overlap_fails():
    rows = _normalize_bars({"rows": [_row(), _row()]}, _request(), "raw:test")
    assert len(rows) == 1
    with pytest.raises(ProviderError):
        _normalize_bars({"rows": [_row(), _row(close=1401.3)]}, _request(), "raw:test")


def test_outside_requested_window_filtered_and_page_budget_remains_partial(
    object_store, state, monkeypatch
):
    _fake_worker(
        monkeypatch, object_store,
        [_row(), _row(datetime="2026-11-19 15:00"), _row(datetime="2026-11-21 15:00")],
        complete=False,
    )
    batch = _provider(object_store, state).fetch_bars(_request())
    assert batch.bar_count == 1 and batch.provider_status.value == "PARTIAL"


def test_failure_raw_is_saved_and_classification_preserved(object_store, state, monkeypatch):
    _fake_worker(monkeypatch, object_store, [], failure_class="NETWORK", complete=False)
    with pytest.raises(ProviderError) as caught:
        _provider(object_store, state).fetch_bars(_request())
    assert caught.value.failure_class is FailureClass.NETWORK
    digest = caught.value.details["snapshot_id"].split(":", 1)[1]
    assert object_store.verify(digest)


def test_transport_cannot_claim_mootdx_without_provenance(object_store, state, monkeypatch):
    _fake_worker(monkeypatch, object_store, [_row()], sdk="tdxpy")
    with pytest.raises(ProviderError) as caught:
        _provider(object_store, state).fetch_bars(_request())
    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE


def test_quote_time_without_date_does_not_fabricate_freshness(object_store, state, monkeypatch):
    quote = {
        "market": 1, "code": "600519", "price": 1401, "last_close": 1400,
        "open": 1400, "high": 1402, "low": 1399, "vol": 10, "amount": 1401000,
        "servertime": "15:00:00",
    }
    _fake_worker(monkeypatch, object_store, [quote])
    result, _snapshot = _provider(object_store, state).fetch_quote("600519", Market.XSHG)
    assert result["source_timestamp"] is None
    assert result["freshness_verified"] is False
    assert result["timestamp_semantics"] == "TIME_OF_DAY_WITHOUT_DATE"


def test_quote_identity_mismatch_fails(object_store, state, monkeypatch):
    _fake_worker(monkeypatch, object_store, [{"market": 0, "code": "000001"}])
    with pytest.raises(ProviderError):
        _provider(object_store, state).fetch_quote("600519", Market.XSHG)


def test_missing_isolated_sdk_is_classified_without_main_import(object_store, state, tmp_path):
    provider = MootdxFallbackProvider(
        object_store, state, project_root=tmp_path, settings=_settings()
    )
    assert provider.availability() == (False, "ISOLATED_SDK_NOT_INSTALLED")
    with pytest.raises(ProviderError) as caught:
        provider.fetch_bars(_request())
    assert caught.value.failure_class is FailureClass.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "::1", "169.254.169.254"])
def test_servers_cannot_escape_to_private_services(host):
    with pytest.raises(ValidationError):
        MootdxServer(host=host)


def test_duplicate_servers_rejected():
    with pytest.raises(ValidationError):
        MootdxSettings(servers=[MootdxServer(host="110.41.147.114")] * 2)


def test_fallback_helper_is_idempotent(object_store, state):
    provider = _provider(object_store, state)
    assert build_intraday_fallback_providers(
        object_store, state, primary_providers=[provider], project_root=ROOT
    ) == [provider]
    assert provider.fallback_only is True


def _bar_body(*, count=1, price=10.0, market=1, code=b"600519") -> bytes:
    header = struct.pack("<H22sHBHI", market, code, 3, 0, count, 0)
    row = struct.pack("<II7f", 20261120, 37800, price, 11, 9, 10.5, 105000, 100, 1)
    return header + row


def test_mac_parser_rejects_truncated_or_overclaimed_rows():
    compat = _integration("transport_compat")
    body = _bar_body()
    assert compat.parse_bars(body, 3, 1, "600519", 1)[0]["datetime"] == "2026-11-20 10:30"
    for invalid in (body[:20], body[:-1], body + b"x", _bar_body(count=800)):
        with pytest.raises(ValueError):
            compat.parse_bars(invalid, 3, 1, "600519", 800)


@pytest.mark.parametrize("body", [
    _bar_body(price=float("nan")), _bar_body(market=0), _bar_body(code=b"000001"),
])
def test_mac_parser_rejects_nonfinite_or_wrong_identity(body):
    with pytest.raises(ValueError):
        _integration("transport_compat").parse_bars(body, 3, 1, "600519", 800)


class _Socket:
    def __init__(self, data: bytes):
        self.data = data
        self.sent = b""

    def sendall(self, data: bytes):
        self.sent += data

    def recv(self, count: int):
        # Fragment every read to prove exact-frame handling.
        count = min(count, 3)
        result, self.data = self.data[:count], self.data[count:]
        return result


def test_mac_frame_handles_fragmentation_and_rejects_truncation():
    compat = _integration("transport_compat")
    body = _bar_body()
    frame = struct.pack("<IIIHH", 0, 0, 0, len(body), len(body)) + body
    assert compat._exchange(_Socket(frame), b"request") == body
    with pytest.raises(ConnectionError):
        compat._exchange(_Socket(frame[:-1]), b"request")


def test_mac_frame_rejects_decompression_bomb():
    compat = _integration("transport_compat")
    zipped = zlib.compress(b"x" * 100000)
    frame = struct.pack("<IIIHH", 0, 0, 0, len(zipped), 20) + zipped
    with pytest.raises(ValueError):
        compat._exchange(_Socket(frame), b"request")


def test_mac_quote_rejects_unknown_fields_and_incomplete_records():
    compat = _integration("transport_compat")
    bitmap = int(compat._FIELD_MASK).to_bytes(16, "little") + bytes(4)
    body = bitmap + struct.pack("<IH", 1, 1) + bytes(68)
    with pytest.raises(ValueError):
        compat.parse_quote(body, [(1, "600519")])
    bad_bitmap = bytes(20) + struct.pack("<IH", 0, 0)
    with pytest.raises(ValueError):
        compat.parse_quote(bad_bitmap, [(1, "600519")])


@pytest.mark.parametrize("is_standard", [True, False])
def test_sdk_socket_closed_even_if_sdk_close_raises(tmp_path, monkeypatch, is_standard):
    worker = _integration("worker")
    home = tmp_path / ".scratchpad" / "profile"
    home.mkdir(parents=True)
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(worker, "version", lambda _name: "test")
    calls: dict[str, Any] = {"socket_closed": False, "sdk_closed": False, "factory": None}

    class Socket:
        def settimeout(self, _timeout):
            pass

        def close(self):
            calls["socket_closed"] = True

    class Session:
        def __init__(self):
            self.client = SimpleNamespace(client=Socket(), get_security_quotes=lambda _stocks: [])

        def close(self):
            calls["sdk_closed"] = True
            raise OSError("close error")

    def factory(**kwargs):
        calls["factory"] = kwargs
        return Session()

    fake_quotes: Any = ModuleType("mootdx.quotes")
    fake_quotes.Quotes = SimpleNamespace(factory=factory)
    fake_quotes.StdQuotes = Session if is_standard else type("StandardSession", (), {})
    monkeypatch.setitem(sys.modules, "mootdx", ModuleType("mootdx"))
    monkeypatch.setitem(sys.modules, "mootdx.quotes", fake_quotes)
    request = {
        "operation": "quote", "symbol": "600519", "market": 1, "protocol": "STANDARD",
        "timeout_seconds": 10, "connect_timeout_seconds": 1, "max_pages": 1,
        "page_size": 1, "max_output_bytes": 4096,
        "servers": [{"host": "110.41.147.114", "port": 7709}],
    }
    result = worker.capture(request)
    expected = "CAPABILITY_UNAVAILABLE" if is_standard else "INVALID_RESPONSE"
    assert result["failure_class"] == expected
    assert calls["socket_closed"] and calls["sdk_closed"]
    assert calls["factory"]["heartbeat"] is False
    assert calls["factory"]["auto_retry"] is False and calls["factory"]["bestip"] is False


def test_recorded_mac_frames_prove_price_units_and_source_time():
    compat = _integration("transport_compat")
    fixture = json.loads(
        (ROOT / "tests/fixtures/providers/mootdx-fallback-success.json").read_text(encoding="utf-8")
    )
    captures = fixture["recorded_evidence"]["captures"]
    quote = compat.parse_quote(
        bytes.fromhex(captures["quote"]["frames"][0]["response_body_hex"]), [(1, "600519")]
    )[0]
    assert quote["source_date"] == 20261120
    assert quote["price"] == 1485.75 and quote["vol"] == 67229
    daily_volumes = []
    for frequency, category in (("60m", 3), ("5m", 0)):
        body = bytes.fromhex(captures[frequency]["frames"][0]["response_body_hex"])
        rows = compat.parse_bars(body, category, 1, "600519", 700)
        assert len(rows) == 700
        bars = _normalize_bars(
            {"rows": rows, "protocol": "MAC_COMPAT"},
            _request(frequency=Frequency(frequency)), "recorded:mac",
        )
        assert len(bars) == (4 if frequency == "60m" else 48)
        assert bars[-1].close == Decimal("1485.75")
        assert all(bar.volume_unit is VolumeUnit.SHARE for bar in bars)
        daily_volumes.append(sum(bar.volume for bar in bars))
        with pytest.raises(ValueError):
            compat.parse_bars(body[:-1], category, 1, "600519", 700)
    assert daily_volumes == [Decimal(6722900), Decimal(6722900)]
    assert quote["vol"] * 100 == daily_volumes[0]


@pytest.mark.parametrize("operation", ["quote", "60m"])
def test_mac_sdk_command_contract_preserves_lock(monkeypatch, operation):
    compat = _integration("transport_compat")
    fixture = json.loads(
        (ROOT / "tests/fixtures/providers/mootdx-fallback-success.json").read_text(encoding="utf-8")
    )
    frame = fixture["recorded_evidence"]["captures"][operation]["frames"][0]
    wire = bytes.fromhex(frame["response_header_hex"] + frame["response_payload_hex"])

    class Lock:
        active = False
        entered = 0

        def __enter__(self):
            self.active = True
            self.entered += 1
            return self

        def __exit__(self, *_exc):
            self.active = False

    lock = Lock()

    class LockedSocket(_Socket):
        def sendall(self, data: bytes):
            assert lock.active
            super().sendall(data)

    fake_sdk: Any = ModuleType("tdxpy")
    fake_hq: Any = ModuleType("tdxpy.hq")
    fake_sdk.hq = fake_hq
    monkeypatch.setitem(sys.modules, "tdxpy", fake_sdk)
    monkeypatch.setitem(sys.modules, "tdxpy.hq", fake_hq)
    compat.install()
    sock = LockedSocket(wire)
    if operation == "quote":
        command = fake_hq.GetSecurityQuotesCmd(sock, lock=lock)
        command.setParams([(1, "600519")])
    else:
        command = fake_hq.GetSecurityBarsCmd(sock, lock=lock)
        command.setParams(3, 1, "600519", 0, 700)
    rows = command.call_api()
    assert len(rows) == (1 if operation == "quote" else 700)
    assert lock.entered == 1 and not lock.active
    assert sock.sent.hex() == frame["request_hex"]


def test_mac_response_wrong_page_fails():
    compat = _integration("transport_compat")
    with pytest.raises(ValueError, match="offset"):
        compat.parse_bars(_bar_body(), 3, 1, "600519", 1, 700)


def test_sdk_row_boundary_classifies_none_and_invalid_shapes():
    worker = _integration("worker")
    with pytest.raises(ConnectionError):
        worker._sdk_rows(None)
    for value in ({}, "text", [1], [None]):
        with pytest.raises(ValueError):
            worker._sdk_rows(value)
    assert worker._sdk_rows([]) == []
    rows = [{"datetime": "2026-11-20 10:30", "vol": 100}]
    assert worker._sdk_rows(rows) is rows


def test_missing_sdk_dependency_has_capability_classification():
    worker = _integration("worker")
    assert worker._failure(ModuleNotFoundError("mootdx")) == "CAPABILITY_UNAVAILABLE"
    assert worker._failure(ImportError("dependency")) == "CAPABILITY_UNAVAILABLE"
    assert worker._failure(worker.PackageNotFoundError("mootdx")) == "CAPABILITY_UNAVAILABLE"


def test_pagination_cycle_is_not_promoted_from_partial_rows(object_store, state, monkeypatch):
    _fake_worker(
        monkeypatch, object_store, [_row()], failure_class="PAGINATION_CYCLE", complete=False
    )
    with pytest.raises(ProviderError) as caught:
        _provider(object_store, state).fetch_bars(_request())
    assert caught.value.failure_class is FailureClass.PAGINATION_CYCLE


def test_changed_protocol_cannot_change_units_silently(object_store, state, monkeypatch):
    _fake_worker(monkeypatch, object_store, [_row()], protocol="MAC_COMPAT")
    with pytest.raises(ProviderError) as caught:
        _provider(object_store, state).fetch_bars(_request())
    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE


def test_shared_budget_caps_sdk_launch_and_socket_timeouts(object_store, state, monkeypatch):
    calls = _fake_worker(monkeypatch, object_store, [_row()])
    budget = RequestExecutionBudget(
        state, "mootdx-sdk-budget", datetime.now(UTC) + timedelta(seconds=2), 1,
    )
    with budget.bind():
        _provider(object_store, state).fetch_bars(_request())
        with pytest.raises(RequestBudgetExhausted):
            _provider(object_store, state).fetch_bars(_request())
    assert len(calls) == 1
    assert 0 < calls[0]["connect_timeout_seconds"] <= calls[0]["timeout_seconds"] <= 2
    assert budget.remaining_attempts() == 0


def test_waiting_for_sdk_slot_cannot_reset_request_deadline(object_store, state, monkeypatch):
    calls = _fake_worker(monkeypatch, object_store, [_row()])
    now = [datetime.now(UTC)]
    released = []

    class DelayedSlot:
        def acquire(self, *, timeout):
            assert 0 < timeout <= 2
            now[0] += timedelta(seconds=3)
            return True

        def release(self):
            released.append(True)

    monkeypatch.setattr(module, "_CONCURRENCY", DelayedSlot())
    budget = RequestExecutionBudget(
        state, "mootdx-delayed-slot", now[0] + timedelta(seconds=2), 1,
        clock=lambda: now[0],
    )
    with budget.bind(), pytest.raises(RequestBudgetExhausted):
        _provider(object_store, state).fetch_bars(_request())
    assert not calls
    assert released == [True]
    assert budget.remaining_attempts() == 1
