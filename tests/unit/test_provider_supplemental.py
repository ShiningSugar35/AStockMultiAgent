"""Optional-source contracts, origins, nonblocking setup and safe capture."""
from __future__ import annotations

import json
import subprocess
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from astock.core.errors import FailureClass, ProviderError
from astock.providers.supplemental import (
    AKShareHintProvider,
    FinnhubNewsHintProvider,
    SupplementalEvidenceService,
    SupplementalRequest,
    TushareHintProvider,
    _sdk_environment,
    normalize_daily,
)
from tests.unit.test_current_research_continuation import PROJECT_ROOT, _runtime


def request(capability="market.reference.hint"):
    return SupplementalRequest(
        capability=capability, symbol="600519",
        start=date(2026, 9, 1), end=date(2026, 9, 2),
    )


def ak_row(**overrides):
    return {
        "日期": "2026-09-01", "股票代码": "600519", "开盘": "10", "最高": "12",
        "最低": "9", "收盘": "11", "成交量": "2", "成交额": "2100", **overrides,
    }


def ts_row(**overrides):
    return {
        "trade_date": "20260901", "ts_code": "600519.SH", "open": "10", "high": "12",
        "low": "9", "close": "11", "vol": "2", "amount": "2.1", **overrides,
    }


def ts_payload(row=None):
    row = row or ts_row()
    return {"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}


def test_source_units_and_extra_columns():
    a = normalize_daily([ak_row(extra_column="new")], request(), kind="akshare")[0]
    t = normalize_daily([ts_row(extra_column="new")], request(), kind="tushare")[0]
    assert Decimal(a["volume_shares"]) == Decimal(t["volume_shares"]) == 200
    assert Decimal(a["amount_yuan"]) == Decimal(t["amount_yuan"]) == 2100
    assert a["adjustment"] == t["adjustment"] == "UNADJUSTED"


@pytest.mark.parametrize("override", [
    {"股票代码": "000001"}, {"日期": "2025-01-01"}, {"收盘": "NaN"},
    {"成交量": "-1"}, {"最高": "8"}, {"最低": "11"}, {"开盘": "0"},
])
def test_invalid_reference_is_not_accepted(override):
    with pytest.raises(ProviderError) as caught:
        normalize_daily([ak_row(**override)], request(), kind="akshare")
    assert caught.value.failure_class is FailureClass.DATA_QUALITY


def test_duplicate_dates():
    assert len(normalize_daily([ak_row(), ak_row()], request(), kind="akshare")) == 1
    with pytest.raises(ProviderError):
        normalize_daily([ak_row(), ak_row(收盘="12")], request(), kind="akshare")


@pytest.mark.parametrize("denied", [False, True])
def test_old_network_probe_can_recover_but_access_denial_cannot(tmp_path, denied):
    from datetime import timedelta

    from astock.providers.probe import ProviderProbeService, RawProbeResponse
    from astock.schemas import ProviderHealthStatus

    _, state, objects = _runtime(tmp_path)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)

    def transport(_provider):
        if denied:
            return RawProbeResponse(403, b'{"denied":true}')
        raise httpx.ConnectError("synthetic transport failure")

    report = ProviderProbeService(
        project_root=PROJECT_ROOT, registry=service.factory.registry,
        state=state, objects=objects, live_transport=transport,
    ).probe("sina-reference", live=True, probe_key="temporary-network-probe")
    factory = service.factory
    original = state.get_provider_probe_health_snapshot("sina-reference")
    assert report.last_probe_at is not None
    observed = report.last_probe_at
    factory.clock = lambda: observed
    assert (
        factory.capability_health_status("sina-reference", "instrument.identity")
        is ProviderHealthStatus.UNAVAILABLE
    )
    factory.clock = lambda: observed + timedelta(
        seconds=factory.source_breaker.policy.cooldown_seconds + 1,
    )
    expected = ProviderHealthStatus.UNAVAILABLE if denied else ProviderHealthStatus.NOT_PROBED
    assert factory.capability_health_status("sina-reference", "instrument.identity") is expected
    assert (
        factory.claim_capability_attempt("sina-reference", "instrument.identity", live=True)
        is not denied
    )
    assert state.get_provider_probe_health_snapshot("sina-reference") == original


def test_missing_credentials_are_not_need_info(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(
        "astock.providers.runtime.credential_is_configured",
        lambda *args, **kwargs: False,
    )
    result = SupplementalEvidenceService(PROJECT_ROOT, state, objects).collect(
        request("news.global.lead"), live=True,
    )
    assert result["status"] == "PUBLIC_DATA_UNAVAILABLE"
    assert result["attempts"] == []
    assert result["manual_actions"] == []
    assert not result["formal_use_allowed"]
    assert result["automatic_recovery_next"].startswith("OFFICIAL_WEB")


def test_nonformal_admission_and_original_source_independence(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-never-real")
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    formal = service.factory.definitions_for_capability("market.reference.hint", formal_use=True)
    assert formal == []
    definitions = {d.provider_id: d for d in service.factory.registry.providers}
    origin = definitions["eastmoney-reference"].independence_group
    assert definitions["akshare-reference-hints"].independence_group == origin
    assert definitions["baostock-reference"].formal_capabilities


def test_tushare_json_auth_and_raw_response(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    credential = "synthetic-unit-test-key"
    monkeypatch.setenv("TUSHARE_TOKEN", credential)
    seen = []

    def transport(incoming):
        seen.append(incoming)
        return httpx.Response(200, json=ts_payload())

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        rows, snapshot = TushareHintProvider(objects, state, client=client).fetch_hints(request())
    assert json.loads(seen[0].content)["token"] == credential
    assert credential not in str(seen[0].url)
    assert credential not in snapshot.model_dump_json()
    assert objects.verify(snapshot.object_sha256)
    assert rows[0]["volume_shares"] == "200"


@pytest.mark.parametrize("status,payload", [(429, {}), (403, {}), (200, {"code": 2002})])
def test_quota_and_permission_do_not_retry_storm(tmp_path, monkeypatch, status, payload):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-unit-test-key")
    calls = []

    def transport(req):
        calls.append(req)
        return httpx.Response(status, json=payload, headers={"Retry-After": "60"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ProviderError):
            TushareHintProvider(objects, state, client=client).fetch_hints(request())
    assert len(calls) == 1


def test_key_echo_is_not_persisted(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    credential = "synthetic-echo-test-key"
    monkeypatch.setenv("TUSHARE_TOKEN", credential)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"code": 0, "msg": credential}),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ProviderError, match="echoed authentication"):
            TushareHintProvider(objects, state, client=client).fetch_hints(request())
    assert not list(objects.root.rglob("*"))


def test_finnhub_header_and_news_scope(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.setenv("FINNHUB_API_KEY", "synthetic-news-test-key")
    seen = []

    def transport(incoming):
        seen.append(incoming)
        return httpx.Response(200, json=[{
            "headline": "Example news", "url": "https://example.org/news",
            "datetime": 1788220800, "source": "Example",
        }])

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = FinnhubNewsHintProvider(objects, state, client=client)
        rows, snapshot = provider.fetch_hints(request("news.global.lead"))
    assert seen[0].headers["X-Finnhub-Token"] == "synthetic-news-test-key"
    assert "token" not in str(seen[0].url).lower()
    assert snapshot.source_id == "finnhub-news-hints"
    assert len(rows) == 1


def test_worker_timeout_and_error_privacy(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 20, stderr="do-not-leak")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProviderError) as caught:
        AKShareHintProvider(objects, state).fetch_hints(request())
    assert caught.value.failure_class is FailureClass.TIMEOUT
    assert "do-not-leak" not in str(caught.value)


def test_sdk_capture_is_not_a_fabricated_http_snapshot(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    payload = {
        "rows": [ak_row()], "sdk_version": "1.18.94", "upstream": "EASTMONEY",
        "operation": "stock_zh_a_hist", "adjust": "",
    }
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload),
    ))
    rows, snapshot = AKShareHintProvider(objects, state).fetch_hints(request())
    assert rows[0]["symbol"] == "600519"
    assert snapshot.source_url.startswith("akshare://")
    assert objects.verify(snapshot.object_sha256)


def test_sdk_does_not_receive_unrelated_credentials(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-test-only")
    monkeypatch.setenv("FINNHUB_API_KEY", "unit-test-only")
    monkeypatch.setenv("UNRELATED_SECRET", "unit-test-only")
    env = _sdk_environment()
    assert "TUSHARE_TOKEN" not in env
    assert "FINNHUB_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env
    assert str(PROJECT_ROOT) in env["TEMP"]


def test_recorded_exact_request_and_no_formal_rights(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    result = service.collect(request(), live=False)
    assert len(result["captures"]) == 2
    assert not result["formal_use_allowed"]
    wrong = request().model_copy(update={"symbol": "000001"})
    assert service.collect(wrong, live=False)["captures"] == []


def test_cli_schema_registration():
    from astock.cli import app
    result = CliRunner().invoke(app, ["research-supplemental-schema"])
    assert result.exit_code == 0, result.output
    assert "market.reference.hint" in result.output


def test_sdk_network_failure_is_not_a_missing_capability(tmp_path, monkeypatch):
    _, state, objects = _runtime(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps({"failure_class": "NETWORK", "error_type": "ProxyError"}),
    ))
    with pytest.raises(ProviderError) as caught:
        AKShareHintProvider(objects, state).fetch_hints(request())
    assert caught.value.failure_class is FailureClass.NETWORK


@pytest.mark.parametrize("strategy,expected_lanes", [
    ("ENV_THEN_DIRECT", [True, False]), ("ENV_ONLY", [True, True]),
    ("DIRECT_ONLY", [False, False]),
])
def test_sdk_obeys_transport_profile_and_bounded_fallback(
    tmp_path, monkeypatch, strategy, expected_lanes,
):
    from dataclasses import replace

    _, state, objects = _runtime(tmp_path)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    definition = next(d for d in service.factory.registry.providers
                      if d.provider_id == "akshare-reference-hints")
    profile = service.factory.profiles[definition.transport_profile]
    service.factory.profiles[definition.transport_profile] = replace(
        profile, proxy_strategy=strategy, max_attempts=2,
        backoff_seconds=0, jitter_seconds=0,
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    seen = []

    def worker(*args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return SimpleNamespace(returncode=1, stdout=json.dumps({
                "failure_class": "NETWORK", "error_type": "ProxyError",
            }))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "rows": [ak_row()], "sdk_version": "test-only", "upstream": "EASTMONEY",
            "operation": "stock_zh_a_hist", "adjust": "",
        }))

    monkeypatch.setattr(subprocess, "run", worker)
    rows, _ = service.factory.create(definition.provider_id).fetch_hints(request())
    assert rows[0]["symbol"] == "600519"
    assert [("HTTPS_PROXY" in call["env"]) for call in seen] == expected_lanes
    assert all(0 < call["timeout"] <= definition.timeout_seconds for call in seen)
    for call, inherit in zip(seen, expected_lanes, strict=True):
        if not inherit:
            assert call["env"]["NO_PROXY"] == "*"
    assert __import__("os").environ["HTTPS_PROXY"] == "http://127.0.0.1:1"


@pytest.mark.parametrize("failure", ["RATE_LIMITED", "ACCESS_RESTRICTED", "DATA_QUALITY"])
def test_sdk_does_not_route_around_access_or_invalid_data(tmp_path, monkeypatch, failure):
    _, state, objects = _runtime(tmp_path)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    seen = []

    def worker(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(returncode=1, stdout=json.dumps({"failure_class": failure}))

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(ProviderError):
        service.factory.create("akshare-reference-hints").fetch_hints(request())
    assert len(seen) == 1


def test_global_news_does_not_require_an_unrelated_stock_code():
    news = SupplementalRequest(
        capability="news.global.lead", start=date(2026, 9, 1), end=date(2026, 9, 2),
    )
    assert news.symbol == ""
    with pytest.raises(ValueError, match="reference hints require"):
        SupplementalRequest(start=date(2026, 9, 1), end=date(2026, 9, 2))


def test_live_provider_fallback_consumes_verified_capture_without_private_request(
    tmp_path, monkeypatch,
):
    from astock.providers.supplemental import _HintPersistence

    _, state, objects = _runtime(tmp_path)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    definitions = [item for item in service.factory.registry.providers
                   if item.provider_id in {"akshare-reference-hints", "tushare-reference-hints"}]
    monkeypatch.setattr(
        service.factory, "definitions_for_capability", lambda capability: definitions,
    )
    persist = _HintPersistence()
    persist.provider_id = "tushare-reference-hints"
    persist.object_store, persist.state = objects, state
    snapshot = persist.persist_envelope(ts_payload(), "fixture://bounded-provider-fallback")
    records = normalize_daily([ts_row()], request(), kind="tushare")
    seen = []

    def create(provider_id):
        seen.append(provider_id)
        if provider_id == "akshare-reference-hints":
            def unavailable(_request):
                raise ProviderError("network unavailable", failure_class=FailureClass.NETWORK)
            return SimpleNamespace(fetch_hints=unavailable)
        return SimpleNamespace(fetch_hints=lambda _request: (records, snapshot))

    monkeypatch.setattr(service.factory, "create", create)
    result = service.collect(request(), live=True)
    assert seen == ["akshare-reference-hints", "tushare-reference-hints"]
    assert [item["status"] for item in result["attempts"]] == ["NETWORK", "CAPTURED"]
    assert result["captures"][0]["source_snapshot_id"] == snapshot.snapshot_id
    assert result["manual_actions"] == []
    assert not result["formal_use_allowed"]


def test_optional_self_probe_checks_its_public_capture_contract(tmp_path, monkeypatch):
    from astock.providers.self_probe import ProviderSelfProbeRunner
    from astock.providers.supplemental import _HintPersistence

    _, state, objects = _runtime(tmp_path)
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    definition = next(item for item in service.factory.registry.providers
                      if item.provider_id == "akshare-reference-hints")
    persist = _HintPersistence()
    persist.provider_id = definition.provider_id
    persist.object_store, persist.state = objects, state
    snapshot = persist.persist_envelope({"fixture": True}, "fixture://probe-contract")
    provider = SimpleNamespace(fetch_hints=lambda req: ([{"fixture": "only"}], snapshot))
    monkeypatch.setattr(service.factory, "create", lambda provider_id: provider)
    result = ProviderSelfProbeRunner(service.factory).run(definition)
    assert result.record_count == 1 and result.quality
    assert result.checked_capabilities == ("market.reference.hint",)
    provider.fetch_hints = lambda req: None
    with pytest.raises(ValueError, match="must contain rows and a snapshot"):
        ProviderSelfProbeRunner(service.factory).run(definition)
