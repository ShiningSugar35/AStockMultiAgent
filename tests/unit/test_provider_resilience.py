from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from astock.candidates.seeds import (
    ResearchSeedProviderRouter,
    ResearchSeedService,
    _seed_payload_coverage_ratio,
    _sina_activity_unavailable,
)
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_resilience import SourceFailureClass
from astock.core.state import StateStore
from astock.documents import DisclosureEnumerationProvider
from astock.market_data.reference import MarketReferenceService, _parse_sina_master
from astock.market_data.reference_config import ReferenceRouteStep, load_market_reference_config
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers import ProviderProbeService, RawProbeResponse
from astock.providers.config import load_provider_registry
from astock.providers.http_resilience import ResilientHttpClient
from astock.providers.runtime import (
    ProviderFactory,
    build_provider_http_client,
    load_transport_profiles,
)
from astock.providers.sina_reference import SinaReferenceProvider
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    CompletenessSemantics,
    DailyBarObservation,
    DatasetReleaseManifest,
    FetchStatus,
    InstrumentRecord,
    InstrumentType,
    Market,
    ProviderHealthStatus,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferenceFileDescriptor,
    ReferencePitStatus,
    SourceSnapshot,
    VolumeUnit,
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
        timeout: float | None = None,
    ) -> httpx.Response:
        del params, data, timeout
        self.calls.append(self.trust_env)
        request = httpx.Request(method, url)
        if self.mode == "network_then_ok" and self.trust_env:
            raise httpx.ConnectError("env lane failed", request=request)
        if self.mode == "timeout_then_ok" and self.trust_env:
            raise httpx.ReadTimeout("env lane timed out", request=request)
        if self.mode == "forbidden":
            return httpx.Response(403, request=request)
        if self.mode.startswith("server_") and self.mode.endswith("_then_ok") and self.trust_env:
            status = int(self.mode.split("_", 2)[1])
            return httpx.Response(status, request=request)
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
    elapsed_budget_seconds: float = 30.0,
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
        elapsed_budget_seconds=elapsed_budget_seconds,
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


@pytest.mark.parametrize("status", [502, 503, 504])
def test_retryable_gateway_statuses_use_next_lane(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    calls: list[bool] = []
    client = _resilient_client(
        monkeypatch,
        mode=f"server_{status}_then_ok",
        calls=calls,
    )

    response = client.get("https://example.invalid")

    assert response.status_code == 200
    assert calls == [True, False]
    assert response.extensions["astock_transport_lane"] == "DIRECT"


def test_timeout_uses_next_lane_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    client = _resilient_client(monkeypatch, mode="timeout_then_ok", calls=calls)

    response = client.get("https://example.invalid")

    assert response.status_code == 200
    assert calls == [True, False]
    assert response.extensions["astock_transport_lane"] == "DIRECT"


def test_http_elapsed_budget_prevents_retry_storm_after_first_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    ticks = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "astock.providers.http_resilience.time.monotonic",
        lambda: next(ticks),
    )
    client = _resilient_client(
        monkeypatch,
        mode="network_then_ok",
        calls=calls,
        elapsed_budget_seconds=30.0,
    )

    with pytest.raises(httpx.ConnectError):
        client.get("https://example.invalid")

    assert calls == [True]


def test_elapsed_budget_returns_last_5xx_response_without_second_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    ticks = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "astock.providers.http_resilience.time.monotonic",
        lambda: next(ticks),
    )
    client = _resilient_client(
        monkeypatch,
        mode="server_503_then_ok",
        calls=calls,
        elapsed_budget_seconds=30.0,
    )

    response = client.get("https://example.invalid")

    assert response.status_code == 503
    assert response.extensions["astock_transport_attempt"] == 1
    assert calls == [True]


def test_runtime_http_client_consumes_versioned_source_elapsed_budget() -> None:
    client = build_provider_http_client(
        "eastmoney-reference",
        project_root=PROJECT_ROOT,
    )
    try:
        assert isinstance(client, ResilientHttpClient)
        assert client.elapsed_budget_seconds == 30.0
        assert client.timeout_seconds <= client.elapsed_budget_seconds
    finally:
        client.close()


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
        "bse-official-master",
        "baostock-master",
        "eastmoney-master",
        "sina-master",
    ]


def test_provider_factory_consumes_source_catalog_for_seed_routing(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    capabilities = factory.catalog_capabilities("market.seed_snapshot")
    definitions = factory.definitions_for_capability(
        "market.seed_snapshot",
        require_complete=True,
    )

    assert {item.source_id for item in capabilities} == {
        "eastmoney-reference",
        "sina-reference",
    }
    assert all(
        item.completeness_semantics is CompletenessSemantics.FULL_UNIVERSE
        and item.completeness_score == 1
        for item in capabilities
    )
    assert {item.provider_id for item in definitions} == {
        "eastmoney-reference",
        "sina-reference",
    }


def test_cninfo_catalog_uses_canonical_disclosure_capability_vocabulary(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    capabilities = factory.catalog_capabilities("disclosure.document")
    definitions = factory.definitions_for_capability(
        "disclosure.document",
        formal_use=True,
        require_complete=True,
    )
    provider = factory.create_for_capability(
        "disclosure.enumerate",
        DisclosureEnumerationProvider,
        formal_use=True,
        require_complete=True,
    )

    assert [item.source_id for item in capabilities] == ["cninfo-disclosures"]
    assert capabilities[0].source_class.value == "PRIMARY_OFFICIAL_WEB"
    assert capabilities[0].completeness_semantics is CompletenessSemantics.EXACT_ITEM
    assert [item.provider_id for item in definitions] == ["cninfo-disclosures"]
    assert isinstance(provider, DisclosureEnumerationProvider)


def test_provider_health_failure_is_scoped_to_the_checked_capability(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    ProviderProbeService(
        project_root=PROJECT_ROOT,
        registry=registry,
        state=state,
        objects=objects,
        live_transport=lambda provider: RawProbeResponse(403, b'{"denied":true}'),
    ).probe("sina-reference", live=True, probe_key="identity-denied")
    factory = ProviderFactory(
        registry,
        load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml"),
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    assert (
        factory.capability_health_status("sina-reference", "instrument.identity")
        is ProviderHealthStatus.UNAVAILABLE
    )
    assert (
        factory.capability_health_status("sina-reference", "market.daily_unadjusted")
        is ProviderHealthStatus.NOT_PROBED
    )
    assert not factory.claim_capability_attempt(
        "sina-reference",
        "instrument.identity",
        live=True,
    )
    assert factory.claim_capability_attempt(
        "sina-reference",
        "market.daily_unadjusted",
        live=True,
    )

    drifted_providers = [
        item.model_copy(update={"priority": item.priority + 1})
        if item.provider_id == "sina-reference"
        else item
        for item in registry.providers
    ]
    drifted_registry = registry.model_copy(update={"providers": drifted_providers})
    drifted_factory = ProviderFactory(
        drifted_registry,
        load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml"),
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )
    drifted_status = ProviderProbeService(
        project_root=PROJECT_ROOT,
        registry=drifted_registry,
        state=state,
        objects=objects,
        live_transport=lambda provider: RawProbeResponse(200, b"{}"),
    ).status("sina-reference")

    assert (
        drifted_factory.capability_health_status("sina-reference", "instrument.identity")
        is ProviderHealthStatus.NOT_PROBED
    )
    assert drifted_status.status is ProviderHealthStatus.NOT_PROBED

    with state.transaction() as connection:
        connection.execute(
            "UPDATE provider_health SET latest_probe_id=? WHERE provider_id=?",
            ("0" * 64, "sina-reference"),
        )
    assert (
        factory.capability_health_status("sina-reference", "instrument.identity")
        is ProviderHealthStatus.CORRUPT
    )


def test_reference_route_health_blocking_is_capability_scoped(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    ProviderProbeService(
        project_root=PROJECT_ROOT,
        registry=registry,
        state=state,
        objects=objects,
        live_transport=lambda provider: RawProbeResponse(403, b'{"denied":true}'),
    ).probe("sina-reference", live=True, probe_key="identity-route-denied")
    service = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    identity_step = next(
        step
        for step in service.config.route("instrument.identity")
        if step.provider_id == "sina-reference"
    )
    daily_step = next(
        step
        for step in service.config.route("market.daily_unadjusted")
        if step.provider_id == "sina-reference"
    )

    assert service._route_provider_blocked(
        identity_step,
        "instrument.identity",
        live=True,
    )
    assert not service._route_provider_blocked(
        daily_step,
        "market.daily_unadjusted",
        live=True,
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing_health",
        "blank_pointer",
        "invalid_status",
        "pointer",
        "event",
        "artifact",
        "artifact_json",
        "failure_count",
        "object",
    ],
)
def test_provider_factory_health_fails_closed_on_probe_lineage_corruption(
    tmp_path: Path,
    damage: str,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    probe = ProviderProbeService(
        project_root=PROJECT_ROOT,
        registry=registry,
        state=state,
        objects=objects,
    ).probe("eastmoney-5m")
    factory = ProviderFactory(
        registry,
        load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml"),
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    if damage == "missing_health":
        with state.transaction() as connection:
            connection.execute(
                "DELETE FROM provider_health WHERE provider_id=?",
                ("eastmoney-5m",),
            )
    elif damage == "blank_pointer":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE provider_health SET report_artifact_id=NULL,"
                "report_object_hash=NULL,latest_probe_id=NULL WHERE provider_id=?",
                ("eastmoney-5m",),
            )
    elif damage == "invalid_status":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE provider_health SET status='INVALID' WHERE provider_id=?",
                ("eastmoney-5m",),
            )
    elif damage == "pointer":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE provider_health SET latest_probe_id=? WHERE provider_id=?",
                ("0" * 64, "eastmoney-5m"),
            )
    elif damage == "event":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE provider_probe_event SET status='DEGRADED' WHERE probe_id=?",
                (str(probe.report_artifact_id).split(":", maxsplit=1)[1],),
            )
    elif damage == "artifact":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE artifact_registry SET input_hashes_json='[]' WHERE artifact_id=?",
                (probe.report_artifact_id,),
            )
    elif damage == "artifact_json":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE artifact_registry SET input_hashes_json='not-json' WHERE artifact_id=?",
                (probe.report_artifact_id,),
            )
    elif damage == "failure_count":
        with state.transaction() as connection:
            connection.execute(
                "UPDATE provider_health SET failure_count='not-an-int' WHERE provider_id=?",
                ("eastmoney-5m",),
            )
    else:
        objects.path_for(str(probe.report_object_hash)).write_bytes(b"tampered")

    assert (
        factory.capability_health_status("eastmoney-5m", "market.raw_5m")
        is ProviderHealthStatus.CORRUPT
    )


def test_provider_catalog_does_not_treat_unrelated_snapshot_as_capability_local_cache(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    ref = objects.put_json({"purpose": "unrelated-disclosure"})
    snapshot = SourceSnapshot(
        snapshot_id="eastmoney-reference:unrelated",
        source_id="eastmoney-reference",
        object_sha256=ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/unrelated",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="1" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    capability = next(
        item
        for item in factory.catalog_capabilities("market.seed_snapshot")
        if item.source_id == "eastmoney-reference"
    )

    assert capability.local_availability_score == 0
    assert capability.freshness_score == Decimal("0.5")


def test_market_reference_capability_route_prefers_healthy_source_over_static_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    def health(provider_id: str, capability: str) -> ProviderHealthStatus:
        del capability
        return {
            "baostock-reference": ProviderHealthStatus.UNAVAILABLE,
            "eastmoney-reference": ProviderHealthStatus.HEALTHY,
            "sina-reference": ProviderHealthStatus.HEALTHY,
        }.get(provider_id, ProviderHealthStatus.NOT_PROBED)

    monkeypatch.setattr(service.provider_factory, "capability_health_status", health)

    route = service._capability_route(
        "instrument.master", live=True, formal_use=True, require_complete=True
    )

    assert route[0].provider_id == "eastmoney-reference"
    assert route[-1].provider_id == "baostock-reference"


def test_market_reference_config_rejects_nested_retry_drift(tmp_path: Path) -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    source = (PROJECT_ROOT / "configs" / "market_reference.yaml").read_text(encoding="utf-8")
    config_path = tmp_path / "market_reference.yaml"
    config_path.write_text(
        source.replace("max_attempts: 1", "max_attempts: 2", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retry must stay at one attempt"):
        load_market_reference_config(config_path, registry)


def test_market_reference_config_rejects_formal_eligibility_drift() -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    providers = [
        item.model_copy(
            update={
                "formal_capabilities": [
                    capability
                    for capability in item.formal_capabilities
                    if capability != "instrument.master"
                ]
            }
        )
        if item.provider_id == "eastmoney-reference"
        else item
        for item in registry.providers
    ]

    with pytest.raises(ValueError, match="not formally eligible"):
        load_market_reference_config(
            PROJECT_ROOT / "configs" / "market_reference.yaml",
            registry.model_copy(update={"providers": providers}),
        )


def test_market_reference_config_rejects_completeness_semantics_drift() -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    providers = [
        item.model_copy(
            update={
                "completeness_semantics": {
                    **item.completeness_semantics,
                    "market.daily_unadjusted": CompletenessSemantics.DISCOVERY_ONLY,
                }
            }
        )
        if item.provider_id == "sina-reference"
        else item
        for item in registry.providers
    ]

    with pytest.raises(ValueError, match="invalid completeness semantics"):
        load_market_reference_config(
            PROJECT_ROOT / "configs" / "market_reference.yaml",
            registry.model_copy(update={"providers": providers}),
        )


def test_market_reference_breaker_failure_is_isolated_by_capability(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    for _ in range(3):
        service.source_breaker.record_failure(
            "baostock-reference",
            "instrument.master",
            SourceFailureClass.TRANSIENT_NETWORK,
        )

    assert (
        service._source_attempt_block_reason(
            "baostock-reference",
            "instrument.master",
            live=True,
        )
        == "BAOSTOCK_REFERENCE_CIRCUIT_OPEN"
    )
    assert (
        service._source_attempt_block_reason(
            "baostock-reference",
            "market.calendar",
            live=True,
        )
        is None
    )


def test_market_reference_breaker_failure_is_isolated_by_market_scope(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    service._source_failure(
        "sina-reference",
        "instrument.master",
        SourceFailureClass.COVERAGE_INCOMPLETE,
        live=True,
        breaker_scope=Market.XSHG.value,
    )

    assert (
        service._source_attempt_block_reason(
            "sina-reference",
            "instrument.master",
            live=True,
            breaker_scope=Market.XSHG.value,
        )
        == "SINA_REFERENCE_CIRCUIT_OPEN"
    )
    assert (
        service._source_attempt_block_reason(
            "sina-reference",
            "instrument.master",
            live=True,
            breaker_scope=Market.XSHE.value,
        )
        is None
    )


def test_identity_exact_failure_does_not_block_paginated_fallback(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    service._source_failure(
        "eastmoney-reference",
        "instrument.identity",
        SourceFailureClass.COVERAGE_INCOMPLETE,
        live=True,
        breaker_scope="eastmoney-exact",
    )

    assert (
        service._source_attempt_block_reason(
            "eastmoney-reference",
            "instrument.identity",
            live=True,
            breaker_scope="eastmoney-exact",
        )
        == "EASTMONEY_REFERENCE_CIRCUIT_OPEN"
    )
    assert (
        service._source_attempt_block_reason(
            "eastmoney-reference",
            "instrument.identity",
            live=True,
            breaker_scope="eastmoney-paginated-master",
        )
        is None
    )


def test_market_reference_config_rejects_provider_wide_breaker_drift(
    tmp_path: Path,
) -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    source = (PROJECT_ROOT / "configs" / "market_reference.yaml").read_text(encoding="utf-8")
    config_path = tmp_path / "market_reference.yaml"
    config_path.write_text(
        source + "\ncircuit_breakers:\n  baostock-reference:\n    cooldown_seconds: 1800\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"provider\+capability policy"):
        load_market_reference_config(config_path, registry)


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


def test_sina_live_master_rejects_repeated_pagination_page(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    repeated_page = [
        {"symbol": "sh600000", "code": "600000", "name": "浦发银行"},
        {"symbol": "sh600001", "code": "600001", "name": "测试证券"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=repeated_page, request=request)

    provider = SinaReferenceProvider(
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures" / "reference" / "sina",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.market_center_page_size = 2
    provider.market_center_max_pages = 3

    with pytest.raises(ValueError, match="repeated a pagination page"):
        provider.fetch_master(Market.XSHG, live=True)


def _daily_bar(
    provider_id: str,
    close: str,
    session: date = date(2026, 8, 20),
) -> DailyBarObservation:
    close_value = Decimal(close)
    identity = {
        "provider_id": provider_id,
        "symbol": "600519",
        "session": session.isoformat(),
        "close": close,
    }
    return DailyBarObservation(
        created_at=NOW,
        observation_id=content_hash(identity),
        instrument_id="XSHG:600519",
        market=Market.XSHG,
        symbol="600519",
        session_date=session,
        session_close_at=datetime.combine(
            session,
            datetime.min.time().replace(hour=15),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        open=Decimal("10.00"),
        high=Decimal("11.00"),
        low=Decimal("9.00"),
        close=close_value,
        previous_close=Decimal("9.90"),
        volume=Decimal("1000000"),
        volume_unit=VolumeUnit.SHARE,
        amount=Decimal("10000000"),
        amount_unit=AmountUnit.CNY,
        adjustment_mode=AdjustmentMode.NONE,
        source_snapshot_id=f"{provider_id}:snapshot",
        available_to_system_at=NOW,
    )


def test_official_calendar_proves_secondary_daily_window_only_when_all_sessions_present(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    complete = [
        _daily_bar("eastmoney-reference", "10.00", date(2026, 7, 20)),
        _daily_bar("eastmoney-reference", "10.10", date(2026, 7, 21)),
        _daily_bar("eastmoney-reference", "10.20", date(2026, 7, 22)),
    ]

    assert service._daily_window_complete(
        complete,
        Market.XSHG,
        date(2026, 7, 20),
        date(2026, 7, 22),
    )
    assert not service._daily_window_complete(
        [complete[0], complete[2]],
        Market.XSHG,
        date(2026, 7, 20),
        date(2026, 7, 22),
    )
    assert not service._daily_window_complete(
        complete,
        Market.XSHG,
        date(2027, 7, 20),
        date(2027, 7, 22),
    )


def _instrument(symbol: str, market: Market = Market.XSHG) -> InstrumentRecord:
    return InstrumentRecord(
        created_at=NOW,
        instrument_id=f"{market.value}:{symbol}",
        market=market,
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


def test_bjse_official_master_kill_falls_back_without_cross_market_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    service.config.minimum_instrument_records[Market.BJSE] = 2

    attempted: list[str] = []

    def route(step: ReferenceRouteStep, market: Market | None, *, live: bool):
        assert market is Market.BJSE
        assert live
        attempted.append(step.operation)
        if step.operation == "bse-official-master":
            raise httpx.ReadTimeout(
                "official provider killed",
                request=httpx.Request("POST", "https://www.bseinfo.net/"),
            )
        if step.operation == "baostock-master":
            return (
                [
                    _instrument("430047", Market.BJSE),
                    _instrument("430090", Market.BJSE),
                ],
                ["snap:baostock"],
                NOW,
                [],
                True,
            )
        raise AssertionError(f"unexpected fallback step: {step.operation}")

    captured: dict[str, object] = {}

    def release(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(service, "_run_master_route_step", route)
    monkeypatch.setattr(service, "_release", release)

    service.sync_instruments(Market.BJSE, live=True)

    assert attempted == ["bse-official-master", "baostock-master"]
    assert captured["provider_id"] == "baostock-reference"
    assert captured["complete"] is True
    records = captured["records"]
    assert isinstance(records, list)
    assert {record.market for record in records} == {Market.BJSE}
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "BSE_OFFICIAL_MASTER_FAILED" in reasons
    assert "BAOSTOCK_REFERENCE_FALLBACK_USED" in reasons


@pytest.mark.parametrize(
    ("killed_provider", "killed_operation", "survivor_provider", "survivor_operation"),
    [
        (
            "baostock-reference",
            "baostock-daily",
            "eastmoney-reference",
            "eastmoney-daily",
        ),
        (
            "eastmoney-reference",
            "eastmoney-daily",
            "sina-reference",
            "sina-daily",
        ),
        (
            "sina-reference",
            "sina-daily",
            "baostock-reference",
            "baostock-daily",
        ),
    ],
)
def test_single_market_provider_kill_is_isolated_by_daily_capability_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    killed_provider: str,
    killed_operation: str,
    survivor_provider: str,
    survivor_operation: str,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    route = [
        ReferenceRouteStep(provider_id=killed_provider, operation=killed_operation),
        ReferenceRouteStep(provider_id=survivor_provider, operation=survivor_operation),
    ]
    monkeypatch.setattr(service, "_capability_route", lambda *_args, **_kwargs: route)

    def run_route(step: ReferenceRouteStep, *args: object, **kwargs: object):
        del args, kwargs
        if step.provider_id == killed_provider:
            raise ValueError(f"{killed_provider} killed")
        return [_daily_bar(survivor_provider, "10.00")], ["snap:survivor"], NOW, [], True

    captured: dict[str, object] = {}

    def release(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(service, "_run_daily_route_step", run_route)
    monkeypatch.setattr(service, "_release", release)

    service.sync_daily(
        "600519",
        Market.XSHG,
        date(2026, 8, 20),
        date(2026, 8, 25),
        live=True,
    )

    assert captured["provider_id"] == survivor_provider
    assert captured["complete"] is True
    assert len(captured["records"]) == 1  # type: ignore[arg-type]
    assert any(str(reason).endswith("FAILED") for reason in captured["reasons"])  # type: ignore[index]


def test_baostock_daily_envelope_cannot_bypass_exact_trading_window_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    monkeypatch.setattr(service, "_daily_window_complete", lambda *args, **kwargs: False)

    records, _snapshots, _available, reasons, complete = service._run_daily_route_step(
        ReferenceRouteStep(
            provider_id="baostock-reference",
            operation="baostock-daily",
        ),
        "600519",
        Market.XSHG,
        date(2026, 7, 20),
        date(2026, 7, 22),
        live=False,
    )

    assert records
    assert not complete
    assert reasons == ["BAOSTOCK_DAILY_WINDOW_INCOMPLETE"]


def test_reference_acquisition_never_calls_health_unavailable_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    route = [
        ReferenceRouteStep(provider_id="eastmoney-reference", operation="eastmoney-daily"),
        ReferenceRouteStep(provider_id="baostock-reference", operation="baostock-daily"),
    ]
    monkeypatch.setattr(service, "_capability_route", lambda *_args, **_kwargs: route)

    def health(provider_id: str, capability: str) -> ProviderHealthStatus:
        del capability
        return {
            "eastmoney-reference": ProviderHealthStatus.HEALTHY,
            "baostock-reference": ProviderHealthStatus.UNAVAILABLE,
        }.get(provider_id, ProviderHealthStatus.NOT_PROBED)

    monkeypatch.setattr(service.provider_factory, "capability_health_status", health)
    calls: list[str] = []

    def run_route(step: ReferenceRouteStep, *args: object, **kwargs: object):
        del args, kwargs
        calls.append(step.provider_id)
        if step.provider_id == "eastmoney-reference":
            raise ValueError("healthy source failed during this request")
        raise AssertionError("UNAVAILABLE provider must not be called")

    captured: dict[str, object] = {}

    def release(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(status="FAILED")

    monkeypatch.setattr(service, "_run_daily_route_step", run_route)
    monkeypatch.setattr(service, "_release", release)

    service.sync_daily(
        "600519",
        Market.XSHG,
        date(2026, 8, 20),
        date(2026, 8, 25),
        live=True,
    )

    assert calls == ["eastmoney-reference"]
    assert captured["records"] == []
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "BAOSTOCK_REFERENCE_HEALTH_UNAVAILABLE" in reasons


def test_live_daily_ohlcv_conflict_is_typed_and_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    route = [
        ReferenceRouteStep(provider_id="baostock-reference", operation="baostock-daily"),
        ReferenceRouteStep(provider_id="eastmoney-reference", operation="eastmoney-daily"),
    ]
    monkeypatch.setattr(service, "_capability_route", lambda *_args, **_kwargs: route)

    def run_route(step: ReferenceRouteStep, *args: object, **kwargs: object):
        del args, kwargs
        if step.provider_id == "baostock-reference":
            return (
                [_daily_bar("baostock-reference", "10.00")],
                ["snap:primary"],
                NOW,
                [],
                True,
            )
        return (
            [_daily_bar("eastmoney-reference", "10.10")],
            ["snap:shadow"],
            NOW,
            [],
            False,
        )

    monkeypatch.setattr(service, "_run_daily_route_step", run_route)

    report = service.sync_daily(
        "600519",
        Market.XSHG,
        date(2026, 8, 20),
        date(2026, 8, 20),
        live=True,
    )

    assert report.status is ReferenceCoverageStatus.CONFLICTED
    assert report.coverage.status is ReferenceCoverageStatus.CONFLICTED
    assert report.release_id is None
    assert report.pit_status is ReferencePitStatus.UNVERIFIED
    assert any(code.startswith("OHLCV_CONFLICTED:") for code in report.reason_codes)
    assert state.list_market_reference_releases() == []


def test_live_master_above_floor_but_unproven_continues_to_complete_provider(
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
    service.config.minimum_instrument_records[Market.XSHG] = 1
    route = [
        ReferenceRouteStep(provider_id="eastmoney-reference", operation="eastmoney-master"),
        ReferenceRouteStep(provider_id="baostock-reference", operation="baostock-master"),
    ]
    monkeypatch.setattr(service, "_capability_route", lambda *_args, **_kwargs: route)

    def run_route(step: ReferenceRouteStep, market: Market | None, *, live: bool):
        assert market is Market.XSHG
        assert live
        if step.provider_id == "eastmoney-reference":
            return (
                [_instrument("600000"), _instrument("600001")],
                ["snap:em"],
                NOW,
                ["EASTMONEY_MASTER_COVERAGE_UNPROVEN"],
                False,
            )
        return [_instrument("600000"), _instrument("600001")], ["snap:bao"], NOW, [], True

    captured: dict[str, object] = {}

    def release(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(service, "_run_master_route_step", run_route)
    monkeypatch.setattr(service, "_release", release)

    service.sync_instruments(Market.XSHG, live=True)

    assert captured["provider_id"] == "baostock-reference"
    assert captured["complete"] is True
    assert len(captured["records"]) == 2  # type: ignore[arg-type]


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


def test_sina_row_floor_and_pagination_end_do_not_prove_formal_universe_coverage() -> None:
    payload = _premarket_sina_payload()
    payload["complete"] = True

    assert _seed_payload_coverage_ratio(payload, Market.XSHG) is None

    payload["coverage_denominator"] = 2
    assert _seed_payload_coverage_ratio(payload, Market.XSHG) == 1.0


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


def _cached_master_release(
    objects: ObjectStore,
    snapshot: SourceSnapshot,
    available: datetime,
    provider_id: str = "sina-reference",
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
            "provider_id": provider_id,
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
        provider_id=provider_id,
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
        "provider_id": provider_id,
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
    payload["complete"] = True
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
    release = _cached_master_release(objects, snapshot, available)
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


def test_fresh_complete_master_cache_is_provider_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload: dict[str, object] = {
        "_astock_request": {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"},
        "data": {
            "total": 2,
            "diff": [
                {
                    "f12": "600000",
                    "f14": "甲",
                    "f2": 10.0,
                    "f6": 100_000_000,
                    "f8": 1.0,
                    "f21": 10_000_000_000,
                },
                {
                    "f12": "600001",
                    "f14": "乙",
                    "f2": 11.0,
                    "f6": 110_000_000,
                    "f8": 1.1,
                    "f21": 11_000_000_000,
                },
            ],
        },
    }
    ref = objects.put_json(payload)
    available = datetime.now(UTC)
    snapshot = SourceSnapshot(
        snapshot_id="eastmoney-reference:test-master",
        source_id="eastmoney-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/eastmoney-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="f" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_master_release(
        objects,
        snapshot,
        available,
        provider_id="eastmoney-reference",
    )
    monkeypatch.setattr(state, "list_market_reference_releases", lambda *_args: [release])
    router = ResearchSeedProviderRouter(
        providers=[],
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
    )

    restored, restored_snapshot = router.fetch_seed_snapshot(Market.XSHG, live=True)

    assert restored == payload
    assert restored_snapshot.snapshot_id == snapshot.snapshot_id
    assert _seed_payload_coverage_ratio(restored, Market.XSHG) == 1.0


def test_cached_master_snapshot_provider_lineage_mismatch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    payload["complete"] = True
    ref = objects.put_json(payload)
    available = datetime.now(UTC)
    snapshot = SourceSnapshot(
        snapshot_id="sina-reference:mismatched-master",
        source_id="sina-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/mismatched-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="8" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_master_release(
        objects,
        snapshot,
        available,
        provider_id="eastmoney-reference",
    )
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


def test_tampered_sina_release_identity_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    payload["complete"] = True
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
    release = _cached_master_release(objects, snapshot, available)
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


def test_corrupted_local_master_snapshot_is_rejected_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = _premarket_sina_payload()
    payload["_astock_request"] = {"market": "XSHG", "purpose": "INSTRUMENT_MASTER"}
    payload["complete"] = True
    ref = objects.put_json(payload)
    available = datetime.now(UTC)
    snapshot = SourceSnapshot(
        snapshot_id="sina-reference:corrupt-master",
        source_id="sina-reference",
        object_sha256=ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/sina-master",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash="9" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=available,
    )
    state.register_snapshot(snapshot)
    release = _cached_master_release(objects, snapshot, available)
    objects.path_for(snapshot.object_sha256).write_bytes(b"corrupted-local-snapshot")
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
    payload["complete"] = True
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
    release = _cached_master_release(objects, snapshot, available)
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
