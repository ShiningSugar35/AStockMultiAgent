from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astock.candidates.seeds import (
    ResearchSeedProviderRouter,
    ResearchSeedService,
    _sina_activity_unavailable,
)
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService, _parse_sina_master
from astock.market_data.reference_config import ReferenceRouteStep, load_market_reference_config
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.config import load_provider_registry
from astock.providers.http_resilience import ResilientHttpClient
from astock.providers.runtime import load_transport_profiles
from astock.providers.sina_reference import SinaReferenceProvider
from astock.schemas import (
    DatasetReleaseManifest,
    FetchStatus,
    InstrumentRecord,
    InstrumentType,
    Market,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferenceFileDescriptor,
    ReferencePitStatus,
    SourceSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)


class _FakeHttpClient:
    def __init__(self, trust_env: bool, mode: str, calls: list[bool]) -> None:
        self.trust_env = trust_env
        self.mode = mode
        self.calls = calls
        self.headers = httpx.Headers()

    def get(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        return self.request("GET", url, params=params)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        del params, data
        self.calls.append(self.trust_env)
        request = httpx.Request(method, url)
        if self.mode == "network_then_ok" and self.trust_env:
            raise httpx.ConnectError("env lane failed", request=request)
        if self.mode == "forbidden":
            return httpx.Response(403, request=request)
        if self.mode == "server_then_ok" and self.trust_env:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    def close(self) -> None:
        return None


def _resilient_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    calls: list[bool],
    retry_methods: tuple[str, ...] = ("GET", "HEAD"),
) -> ResilientHttpClient:
    def factory(**kwargs: object) -> _FakeHttpClient:
        return _FakeHttpClient(bool(kwargs["trust_env"]), mode, calls)

    monkeypatch.setattr("astock.providers.http_resilience.httpx.Client", factory)
    monkeypatch.setattr("astock.providers.http_resilience.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("astock.providers.http_resilience.random.uniform", lambda _a, _b: 0.0)
    return ResilientHttpClient(
        timeout_seconds=2,
        follow_redirects=True,
        headers={},
        lane_trust_env=(True, False),
        max_attempts=2,
        backoff_seconds=0,
        jitter_seconds=0,
        retry_status_codes=(502, 503, 504),
        retry_methods=retry_methods,
    )


def test_env_transport_failure_falls_back_to_direct_without_mutating_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:16890")
    before = os.environ["HTTP_PROXY"]
    client = _resilient_client(monkeypatch, mode="network_then_ok", calls=calls)

    response = client.get("https://example.invalid")

    assert response.status_code == 200
    assert response.extensions["astock_transport_lane"] == "DIRECT"
    assert response.extensions["astock_transport_attempt"] == 2
    assert calls == [True, False]
    assert os.environ["HTTP_PROXY"] == before


def test_nonretryable_403_does_not_consume_direct_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    client = _resilient_client(monkeypatch, mode="forbidden", calls=calls)

    response = client.get("https://example.invalid")

    assert response.status_code == 403
    assert calls == [True]


def test_retryable_503_uses_next_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    client = _resilient_client(monkeypatch, mode="server_then_ok", calls=calls)

    response = client.get("https://example.invalid")

    assert response.status_code == 200
    assert calls == [True, False]
    assert response.extensions["astock_transport_lane"] == "DIRECT"


def test_post_is_not_replayed_unless_transport_profile_explicitly_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    client = _resilient_client(monkeypatch, mode="network_then_ok", calls=calls)
    with pytest.raises(httpx.ConnectError):
        client.request("POST", "https://example.invalid", data={"query": "x"})
    assert calls == [True]

    calls.clear()
    client = _resilient_client(
        monkeypatch,
        mode="network_then_ok",
        calls=calls,
        retry_methods=("GET", "HEAD", "POST"),
    )
    response = client.request("POST", "https://example.invalid", data={"query": "x"})
    assert response.status_code == 200
    assert calls == [True, False]


def test_project_transport_and_reference_configs_enable_bounded_resilience() -> None:
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    reference = load_market_reference_config(
        PROJECT_ROOT / "configs" / "market_reference.yaml",
        registry,
    )

    assert profiles["eastmoney-browser-v1"].proxy_strategy == "ENV_THEN_DIRECT"
    assert profiles["eastmoney-browser-v1"].max_attempts == 2
    assert profiles["eastmoney-browser-v1"].retry_status_codes == (502, 503, 504)
    assert profiles["eastmoney-browser-v1"].retry_methods == ("GET", "HEAD")
    assert profiles["cninfo-official-v1"].retry_methods == ("GET", "HEAD", "POST")
    assert reference.retry_max_attempts == 1
    assert reference.minimum_instrument_records == {
        Market.XSHG: 1500,
        Market.XSHE: 2000,
        Market.BJSE: 150,
    }
    assert [item.operation for item in reference.route("instrument.master")] == [
        "baostock-master",
        "eastmoney-master",
        "sina-master",
    ]


def test_sina_recorded_master_preserves_three_market_boundaries(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    provider = SinaReferenceProvider(
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures" / "reference" / "sina",
    )
    expected = {
        Market.XSHG: "600000",
        Market.XSHE: "000001",
        Market.BJSE: "920000",
    }

    for market, symbol in expected.items():
        payload, snapshot = provider.fetch_master(market)
        records = _parse_sina_master(
            payload,
            snapshot.snapshot_id,
            snapshot.available_to_system_at,
            market,
        )
        assert [item.symbol for item in records] == [symbol]
        assert all(item.market is market for item in records)


def _instrument(symbol: str) -> InstrumentRecord:
    return InstrumentRecord(
        created_at=NOW,
        instrument_id=f"XSHG:{symbol}",
        market=Market.XSHG,
        symbol=symbol,
        name=f"测试{symbol}",
        instrument_type=InstrumentType.STOCK,
        tradable=True,
        status_date=NOW.date(),
        is_st=False,
        source_snapshot_id=f"snapshot:{symbol}",
        available_to_system_at=NOW,
    )


def test_live_master_below_floor_continues_to_sina_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    service.config.minimum_instrument_records[Market.XSHG] = 2

    def route(step: ReferenceRouteStep, market: Market | None, *, live: bool):
        assert market is Market.XSHG
        assert live
        operation = step.operation
        if operation == "baostock-master":
            return [_instrument("600000")], ["snap:bao"], NOW, [], True
        if operation == "eastmoney-master":
            raise ValueError("eastmoney down")
        return [_instrument("600000"), _instrument("600001")], ["snap:sina"], NOW, [], True

    captured: dict[str, object] = {}

    def release(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(service, "_run_master_route_step", route)
    monkeypatch.setattr(service, "_release", release)

    service.sync_instruments(Market.XSHG, live=True)

    assert captured["provider_id"] == "sina-reference"
    assert len(captured["records"]) == 2  # type: ignore[arg-type]
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "BAOSTOCK_REFERENCE_MASTER_BELOW_MINIMUM_COVERAGE" in reasons
    assert "EASTMONEY_MASTER_FAILED" in reasons
    assert "SINA_FALLBACK_USED" in reasons


class _PrimarySeedProvider:
    def fetch_seed_snapshot(self, market: Market, *, live: bool = False):
        del market, live
        return {"rc": 0, "data": {"diff": [{"f12": "600000"}]}}, object()

    def fetch_industry_boards(self, *, live: bool = False):
        del live
        return {}, object()

    def fetch_industry_constituents(self, board_code: str, *, live: bool = False):
        del board_code, live
        return {}, object()


class _FallbackSeedProvider:
    def fetch_seed_snapshot(self, market: Market, *, live: bool = False):
        del live
        return (
            {
                "_astock_source": "SINA_MARKET_CENTER",
                "_astock_request": {"market": market.value, "purpose": "RESEARCH_SEED_ONLY"},
                "rows": [
                    {"symbol": "sh600000", "code": "600000", "name": "甲"},
                    {"symbol": "sh600001", "code": "600001", "name": "乙"},
                ],
            },
            object(),
        )


def test_seed_router_rejects_partial_primary_and_uses_sina_fallback() -> None:
    router = ResearchSeedProviderRouter(
        _PrimarySeedProvider(),
        _FallbackSeedProvider(),
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
    )

    payload, _snapshot = router.fetch_seed_snapshot(Market.XSHG, live=True)

    assert payload["_astock_source"] == "SINA_MARKET_CENTER"


def _premarket_sina_payload() -> dict[str, object]:
    return {
        "_astock_source": "SINA_MARKET_CENTER",
        "_astock_request": {"market": "XSHG", "purpose": "RESEARCH_SEED_ONLY"},
        "rows": [
            {
                "symbol": "sh600000",
                "code": "600000",
                "name": "浦发银行",
                "trade": "0.000",
                "settlement": "9.220",
                "amount": 0,
                "turnoverratio": 0,
                "nmc": 30_707_982.9126,
            },
            {
                "symbol": "sh600004",
                "code": "600004",
                "name": "白云机场",
                "trade": "0.000",
                "settlement": "7.450",
                "amount": 0,
                "turnoverratio": 0,
                "nmc": 1_763_205.120835,
            },
        ],
    }


def test_sina_premarket_activity_proxy_requires_settlement_pattern() -> None:
    payload = _premarket_sina_payload()
    assert _sina_activity_unavailable(payload)

    rows = payload["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first["trade"] = "9.230"
    assert not _sina_activity_unavailable(payload)


def test_sina_seed_parser_uses_settlement_and_converts_market_cap(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = ResearchSeedService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=ObjectStore(tmp_path / "objects"),
        provider=_PrimarySeedProvider(),
    )

    rows = service._parse_market_rows(
        _premarket_sina_payload(),
        Market.XSHG,
        "snapshot:sina",
    )

    assert rows[0].price == 9.22
    assert rows[0].amount_cny == 0
    assert rows[0].float_market_cap_cny == pytest.approx(307_079_829_126.0)


class _FailingSeedProvider(_PrimarySeedProvider):
    def fetch_seed_snapshot(self, market: Market, *, live: bool = False):
        del market, live
        raise ValueError("provider unavailable")


def _cached_sina_release(
    objects: ObjectStore,
    snapshot: SourceSnapshot,
    available: datetime,
) -> dict[str, object]:
    logical_hash = "c" * 64
    descriptor = ReferenceFileDescriptor(
        path="runtime/test.parquet",
        sha256="d" * 64,
        schema_fingerprint="e" * 64,
        row_count=2,
        logical_content_hash=logical_hash,
    )
    batch_id = "b" * 64
    release_id = content_hash(
        {
            "dataset_kind": ReferenceDatasetKind.INSTRUMENT_MASTER.value,
            "scope_key": Market.XSHG.value,
            "provider_id": "sina-reference",
            "batch_id": batch_id,
            "content_hash": logical_hash,
            "previous_release_id": None,
            "available_to_system_at": available.isoformat(),
        }
    )
    manifest = DatasetReleaseManifest(
        release_id=release_id,
        content_hash=logical_hash,
        dataset_kind=ReferenceDatasetKind.INSTRUMENT_MASTER,
        scope_key=Market.XSHG.value,
        provider_id="sina-reference",
        batch_id=batch_id,
        raw_snapshot_ids=[snapshot.snapshot_id],
        observation_files=[descriptor],
        canonical_files=[descriptor.model_copy(update={"path": "runtime/test-canonical.parquet"})],
        coverage=ReferenceCoverage(
            record_count=2,
            status=ReferenceCoverageStatus.COMPLETE,
            created_at=available,
        ),
        pit_status=ReferencePitStatus.RECONSTRUCTED,
        available_to_system_at=available,
        created_at=available,
    )
    manifest_ref = objects.put_bytes(manifest.model_dump_json().encode("utf-8"))
    return {
        "release_id": manifest.release_id,
        "provider_id": "sina-reference",
        "manifest_object_hash": manifest_ref.sha256,
    }


def test_fresh_complete_sina_master_is_reused_after_live_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    ref = objects.put_json(payload)
    available = datetime.now(UTC)
    snapshot = SourceSnapshot(
        snapshot_id="sina-reference:test-master",
        source_id="sina-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/sina-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="a" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_sina_release(objects, snapshot, available)
    monkeypatch.setattr(state, "list_market_reference_releases", lambda *_args: [release])

    router = ResearchSeedProviderRouter(
        _FailingSeedProvider(),
        _FailingSeedProvider(),
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
    )

    restored, restored_snapshot = router.fetch_seed_snapshot(Market.XSHG, live=True)

    assert restored["_astock_source"] == "SINA_MARKET_CENTER"
    assert restored_snapshot.snapshot_id == snapshot.snapshot_id


def test_tampered_sina_release_identity_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    ref = objects.put_json(payload)
    available = datetime.now(UTC)
    snapshot = SourceSnapshot(
        snapshot_id="sina-reference:tampered-master",
        source_id="sina-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/sina-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="c" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_sina_release(objects, snapshot, available)
    release["release_id"] = "0" * 64
    monkeypatch.setattr(state, "list_market_reference_releases", lambda *_args: [release])
    router = ResearchSeedProviderRouter(
        _FailingSeedProvider(),
        _FailingSeedProvider(),
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
    )

    with pytest.raises(ValueError, match="provider unavailable"):
        router.fetch_seed_snapshot(Market.XSHG, live=True)


def test_stale_sina_master_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    ref = objects.put_json(payload)
    available = datetime.now(UTC) - timedelta(hours=1)
    snapshot = SourceSnapshot(
        snapshot_id="sina-reference:stale-master",
        source_id="sina-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/sina-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="b" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_sina_release(objects, snapshot, available)
    monkeypatch.setattr(state, "list_market_reference_releases", lambda *_args: [release])
    router = ResearchSeedProviderRouter(
        _FailingSeedProvider(),
        _FailingSeedProvider(),
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
    )

    with pytest.raises(ValueError, match="provider unavailable"):
        router.fetch_seed_snapshot(Market.XSHG, live=True)
