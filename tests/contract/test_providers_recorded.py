from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers import EastMoney5mProvider, Sina5mProvider
from astock.schemas import AdjustmentMode, BarRequest, Frequency, Market, VolumeUnit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def request() -> BarRequest:
    return BarRequest(
        symbol="600519",
        market=Market.XSHG,
        requested_start=datetime(2026, 7, 10, 0, 0, tzinfo=SHANGHAI),
        requested_end=datetime(2026, 7, 10, 23, 59, tzinfo=SHANGHAI),
        adjustment_mode=AdjustmentMode.NONE,
    )


def provider_context(tmp_path: Path, content: bytes, status_code: int = 200):
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=content,
            headers={"content-type": "application/json"},
            request=http_request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return state, ObjectStore(tmp_path / "objects"), client


@pytest.mark.parametrize(
    ("provider_class", "fixture_name", "expected_unit", "amount_supported"),
    [
        (
            EastMoney5mProvider,
            "eastmoney_5m_600519.json",
            VolumeUnit.LOT_100_SHARES,
            True,
        ),
        (Sina5mProvider, "sina_5m_600519.json", VolumeUnit.SHARE, False),
    ],
)
def test_recorded_provider_contract(
    tmp_path: Path,
    provider_class,
    fixture_name: str,
    expected_unit: VolumeUnit,
    amount_supported: bool,
) -> None:
    content = (PROJECT_ROOT / "tests" / "fixtures" / "providers" / fixture_name).read_bytes()
    state, objects, client = provider_context(tmp_path, content)
    provider = provider_class(objects, state, client=client)
    batch = provider.fetch_bars(request())
    assert batch.bar_count == 2
    assert batch.bars[0].timestamp.isoformat() == "2026-07-10T09:35:00+08:00"
    assert batch.bars[0].volume_unit is expected_unit
    assert (batch.bars[0].amount is not None) is amount_supported
    assert objects.verify(batch.raw_snapshot_id.rsplit(":", 1)[-1])


def test_rate_limit_is_non_retry_storm(tmp_path: Path) -> None:
    state, objects, client = provider_context(tmp_path, b"{}", status_code=429)
    provider = EastMoney5mProvider(objects, state, client=client)
    with pytest.raises(ProviderError) as error:
        provider.fetch_bars(request())
    assert error.value.failure_class is FailureClass.RATE_LIMITED
    assert not error.value.retryable


def test_malformed_response_is_recorded_before_parse_failure(tmp_path: Path) -> None:
    state, objects, client = provider_context(tmp_path, b"not-json")
    provider = Sina5mProvider(objects, state, client=client)
    with pytest.raises(ProviderError) as error:
        provider.fetch_bars(request())
    assert error.value.failure_class is FailureClass.INVALID_RESPONSE
    assert len(list((tmp_path / "objects").rglob("?" * 64))) == 1


def test_batch_identity_ignores_non_bar_response_envelope(tmp_path: Path) -> None:
    fixture = json.loads(
        (PROJECT_ROOT / "tests" / "fixtures" / "providers" / "eastmoney_5m_600519.json").read_text(
            encoding="utf-8"
        )
    )
    fixture["server_nonce"] = "first"
    first_content = json.dumps(fixture).encode()
    state, objects, first_client = provider_context(tmp_path, first_content)
    first = EastMoney5mProvider(objects, state, client=first_client).fetch_bars(request())
    fixture["server_nonce"] = "second"
    second_content = json.dumps(fixture).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=second_content, request=http_request)

    second_client = httpx.Client(transport=httpx.MockTransport(handler))
    second = EastMoney5mProvider(objects, state, client=second_client).fetch_bars(request())
    assert first.raw_snapshot_id != second.raw_snapshot_id
    assert first.batch_id == second.batch_id


@pytest.mark.parametrize(
    ("failure_kind", "expected_class"),
    [
        ("connect", FailureClass.NETWORK),
        ("timeout", FailureClass.TIMEOUT),
    ],
)
def test_transport_failure_is_classified_and_retryable(
    tmp_path: Path,
    failure_kind: str,
    expected_class: FailureClass,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()

    def handler(http_request: httpx.Request) -> httpx.Response:
        if failure_kind == "timeout":
            raise httpx.ReadTimeout("recorded timeout", request=http_request)
        raise httpx.ConnectError("recorded disconnect", request=http_request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = EastMoney5mProvider(ObjectStore(tmp_path / "objects"), state, client=client)
    with pytest.raises(ProviderError) as error:
        provider.fetch_bars(request())
    assert error.value.failure_class is expected_class
    assert error.value.retryable


def test_recorded_intraday_providers_request_hourly_resolution(tmp_path: Path) -> None:
    east_fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "providers" / "eastmoney_5m_600519.json"
    ).read_bytes().replace(b"09:35", b"10:30").replace(b"09:40", b"11:30")
    sina_fixture = (
        PROJECT_ROOT / "tests" / "fixtures" / "providers" / "sina_5m_600519.json"
    ).read_bytes().replace(b"09:35:00", b"10:30:00").replace(
        b"09:40:00", b"11:30:00"
    )
    hourly_request = request().model_copy(update={"frequency": Frequency.H1})

    observed: dict[str, str] = {}

    def east_handler(http_request: httpx.Request) -> httpx.Response:
        observed["east_klt"] = http_request.url.params["klt"]
        return httpx.Response(200, content=east_fixture, request=http_request)

    def sina_handler(http_request: httpx.Request) -> httpx.Response:
        observed["sina_scale"] = http_request.url.params["scale"]
        return httpx.Response(200, content=sina_fixture, request=http_request)

    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    east = EastMoney5mProvider(
        objects,
        state,
        client=httpx.Client(transport=httpx.MockTransport(east_handler)),
    ).fetch_bars(hourly_request)
    sina = Sina5mProvider(
        objects,
        state,
        client=httpx.Client(transport=httpx.MockTransport(sina_handler)),
    ).fetch_bars(hourly_request)

    assert observed == {"east_klt": "60", "sina_scale": "60"}
    assert east.request.frequency is Frequency.H1
    assert sina.request.frequency is Frequency.H1
    assert all(bar.frequency is Frequency.H1 for bar in [*east.bars, *sina.bars])
