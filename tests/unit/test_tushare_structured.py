"""Tushare shared quotas, safe credentials, typed boundaries and point-in-time limits."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from astock.core.errors import FailureClass, ProviderError
from astock.providers.tushare_cli import register_tushare_commands
from astock.providers.tushare_structured import (
    TushareQueryRequest,
    TushareQuota,
    TushareStructuredProvider,
    _decode,
    load_tushare_access,
)

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "[REDACTED_SECRET]"
NOW = datetime(2026, 11, 21, 7, tzinfo=UTC)


def _daily(**updates: Any) -> dict[str, Any]:
    return {
        "ts_code": "600519.SH", "trade_date": "20261120", "open": 10,
        "high": 12, "low": 9, "close": 11, "vol": 2, "amount": 2.1, **updates,
    }


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = list(rows[0])
    return {"code": 0, "data": {"fields": fields, "items": [
        [row[name] for name in fields] for row in rows
    ]}}


def _request(**updates: Any) -> TushareQueryRequest:
    return TushareQueryRequest.model_validate({
        "endpoint": "daily", "params": {"ts_code": "600519.SH", "trade_date": "20261120"},
        "fields": tuple(_daily()), **updates,
    })


def _provider(objects, state, monkeypatch, payload=None, config=None):
    monkeypatch.setenv("TUSHARE_TOKEN", TOKEN)
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=payload if payload is not None else _payload([_daily()]))

    client = httpx.Client(transport=httpx.MockTransport(transport))
    provider = TushareStructuredProvider(
        objects, state, project_root=ROOT, client=client, config=config,
        clock=lambda: NOW,
    )
    return provider, client, requests


def test_success_raw_capture_cache_and_no_secret(object_store, state, monkeypatch):
    provider, client, calls = _provider(object_store, state, monkeypatch)
    try:
        first = provider.query(_request())
        second = provider.query(_request())
        schema = provider.schema("daily")
        assert schema["endpoints"]["daily"]["permission_status"] == "UNVERIFIED"
        observed = schema["endpoints"]["daily"]["observed_permission"]
        assert observed["status"] == "RESPONSE_ACCEPTED"
        assert not observed["permanent_entitlement_proven"]
        assert TOKEN not in json.dumps(schema)
    finally:
        client.close()
    assert first["record_count"] == 1 and second["cache_status"] == "HIT"
    assert len(calls) == 1
    assert TOKEN not in str(calls[0].url)
    assert json.loads(calls[0].content)["token"] == TOKEN
    assert TOKEN not in json.dumps(first)
    assert object_store.verify(first["source_object_hash"])
    assert first["semantics"]["vol"] == "LOT_100_SHARES"
    assert first["semantics"]["amount"] == "THOUSAND_CNY"
    assert not first["formal_use_allowed"] and not first["full_universe_proven"]


def test_same_fields_different_order_share_cache(object_store, state, monkeypatch):
    provider, client, calls = _provider(object_store, state, monkeypatch)
    try:
        provider.query(_request())
        provider.query(_request(fields=tuple(reversed(tuple(_daily())))))
    finally:
        client.close()
    assert len(calls) == 1


@pytest.mark.parametrize("endpoint", ["stk_mins", "income_vip", "share_float", "query"])
def test_unlisted_endpoints_rejected_before_network(endpoint):
    with pytest.raises(ValidationError):
        _request(endpoint=endpoint)


@pytest.mark.parametrize("params", [
    {"ts_code": "600519.SH,000001.SZ", "trade_date": "20261120"},
    {"ts_code": "600519.SH", "trade_date": "20261320"},
    {"ts_code": "600519.SH"},
    {"ts_code": "600519.SH", "trade_date": "20261120", "token": TOKEN},
    {"ts_code": "600519.SH", "start_date": "20261120", "end_date": "20261119"},
])
def test_market_scope_is_bounded(params):
    with pytest.raises(ValidationError):
        _request(params=params)


@pytest.mark.parametrize("params", [
    {"period": "20260930"},
    {"ts_code": "600519.SH"},
    {"ts_code": "600519.SH", "period": "20260929"},
])
def test_financial_query_requires_single_security_and_period(params):
    with pytest.raises(ValidationError):
        TushareQueryRequest(endpoint="income", params=params)


def test_macro_uses_documented_month_parameters():
    request = TushareQueryRequest(
        endpoint="cn_cpi", params={"start_m": "202601", "end_m": "202610"}
    )
    assert request.identity()["params"]["start_m"] == "202601"
    with pytest.raises(ValidationError):
        TushareQueryRequest(endpoint="cn_cpi", params={"start_q": "202601", "end_q": "202610"})


@pytest.mark.parametrize("overrides", [
    {"ts_code": "000001.SZ"}, {"trade_date": "20261119"}, {"close": "NaN"},
    {"high": 8}, {"vol": -1}, {"close": 0}, {"amount": "NaN"},
])
def test_bad_market_identity_or_numbers_rejected(object_store, state, monkeypatch, overrides):
    provider, client, _calls = _provider(
        object_store, state, monkeypatch, _payload([_daily(**overrides)])
    )
    try:
        with pytest.raises(ProviderError) as caught:
            provider.query(_request())
        assert caught.value.failure_class is FailureClass.DATA_QUALITY
        assert state.get_snapshot(caught.value.details["snapshot_id"]) is not None
    finally:
        client.close()


def test_credential_echo_never_persisted(object_store, state, monkeypatch):
    provider, client, _calls = _provider(
        object_store, state, monkeypatch, {"code": 2002, "msg": TOKEN}
    )
    try:
        with pytest.raises(ProviderError, match="echoed authentication"):
            provider.query(_request())
    finally:
        client.close()
    assert not list(object_store.root.rglob("*"))


@pytest.mark.parametrize("code,message,failure", [
    (2002, "权限不足", FailureClass.ACCESS_RESTRICTED),
    (2002, "每分钟最多访问50次", FailureClass.RATE_LIMITED),
    (2001, "token无效", FailureClass.AUTH_REQUIRED),
    (-1, "invalid query", FailureClass.INVALID_RESPONSE),
])
def test_api_failure_class_is_specific_and_retained(
    object_store, state, monkeypatch, code, message, failure
):
    provider, client, calls = _provider(
        object_store, state, monkeypatch, {"code": code, "msg": message}
    )
    try:
        with pytest.raises(ProviderError) as caught:
            provider.query(_request())
        assert caught.value.failure_class is failure
        assert state.get_snapshot(caught.value.details["snapshot_id"]) is not None
        if failure is not FailureClass.INVALID_RESPONSE:
            with pytest.raises(ProviderError):
                provider.query(_request(refresh=True))
            assert len(calls) == 1
    finally:
        client.close()


def test_http_redirect_and_boolean_success_code_rejected():
    with pytest.raises(ProviderError) as redirected:
        _decode(b"{}", 302, None)
    assert redirected.value.failure_class is FailureClass.POLICY_REJECTED
    with pytest.raises(ProviderError):
        _decode(b'{"code":false,"data":{}}', 200, None)


def test_source_quota_shared_across_instances_and_transports(state):
    config = load_tushare_access(ROOT).model_copy(update={"global_requests_per_minute": 2})
    first = TushareQuota(state, config, lambda: NOW)
    second = TushareQuota(state, config, lambda: NOW)
    with first.reserve("daily", "one"):
        pass
    with second.reserve("__mcp_control__", "tools-list"):
        pass
    with pytest.raises(ProviderError) as caught:
        with first.reserve("weekly", "three"):
            pass
    assert caught.value.failure_class is FailureClass.RATE_LIMITED


def test_financial_three_endpoints_share_one_group_quota(state):
    config = load_tushare_access(ROOT).model_copy(update={"financial_requests_per_minute": 2})
    quota = TushareQuota(state, config, lambda: NOW)
    for endpoint in ("income", "balancesheet"):
        with quota.reserve(endpoint, endpoint):
            pass
    with pytest.raises(ProviderError):
        with quota.reserve("cashflow", "third"):
            pass


def test_shanghai_midnight_resets_day_not_rolling_minute(state):
    config = load_tushare_access(ROOT)
    policy = config.endpoints["daily"].model_copy(update={"requests_per_day": 1})
    config = config.model_copy(update={"endpoints": {**config.endpoints, "daily": policy}})
    before = datetime(2026, 11, 20, 15, 59, 50, tzinfo=UTC)
    with TushareQuota(state, config, lambda: before).reserve("daily", "before"):
        pass
    after = before + timedelta(seconds=20)
    with TushareQuota(state, config, lambda: after).reserve("daily", "after"):
        pass
    value = state.get_cursor("tushare", "quota")["value"]
    assert value["day"] == "2026-11-21"
    assert value["daily"]["daily"] == 1 and len(value["recent"]) == 2


def test_cross_instance_singleflight_and_two_slots(state):
    config = load_tushare_access(ROOT)
    first, second = (TushareQuota(state, config, lambda: NOW) for _ in range(2))
    with first.reserve("daily", "same"):
        with pytest.raises(ProviderError) as duplicate:
            with second.reserve("daily", "same"):
                pass
        assert duplicate.value.failure_class is FailureClass.CONFLICT
        with second.reserve("weekly", "second"):
            with pytest.raises(ProviderError):
                with first.reserve("monthly", "third"):
                    pass
    with first.reserve("monthly", "third"):
        pass


def test_stock_basic_is_capped_at_documented_fifty_per_minute(state):
    config = load_tushare_access(ROOT)
    quota = TushareQuota(state, config, lambda: NOW)
    for index in range(50):
        with quota.reserve("stock_basic", str(index)):
            pass
    with pytest.raises(ProviderError):
        with quota.reserve("stock_basic", "overflow"):
            pass


def test_financial_restatements_and_nulls_are_preserved(object_store, state, monkeypatch):
    base = {
        "ts_code": "600519.SH", "ann_date": "20261030", "f_ann_date": "20261030",
        "end_date": "20260930", "report_type": "1", "comp_type": "1",
        "total_revenue": None, "n_income": -100.0, "update_flag": "0",
    }
    restated = {**base, "ann_date": "20261110", "f_ann_date": "20261110",
                "n_income": -120.0, "update_flag": "1"}
    provider, client, _calls = _provider(
        object_store, state, monkeypatch, _payload([base, restated])
    )
    try:
        result = provider.query(TushareQueryRequest(
            endpoint="income", params={"ts_code": "600519.SH", "period": "20260930"}
        ))
    finally:
        client.close()
    assert len(result["rows"]) == 2 and result["rows"][0]["total_revenue"] is None
    assert result["rows"][1]["update_flag"] == "1"
    assert not result["semantics"]["historical_point_in_time_verified"]
    assert "DATE_ONLY" in result["semantics"]["publication"]


def test_macro_units_and_unknown_vintage_are_explicit(object_store, state, monkeypatch):
    provider, client, _calls = _provider(
        object_store, state, monkeypatch,
        _payload([{"quarter": "2026Q3", "gdp": 100.0, "gdp_yoy": -1.0}]),
    )
    try:
        result = provider.query(TushareQueryRequest(
            endpoint="cn_gdp", params={"q": "2026Q3"}, fields=("quarter", "gdp", "gdp_yoy")
        ))
    finally:
        client.close()
    assert result["semantics"]["gdp"] == "CUMULATIVE_100_MILLION_CNY"
    assert not result["semantics"]["vintage_history_verified"]


def test_response_rows_over_budget_are_not_silently_truncated(object_store, state, monkeypatch):
    provider, client, _calls = _provider(
        object_store, state, monkeypatch, _payload([_daily(), _daily()])
    )
    try:
        with pytest.raises(ProviderError):
            provider.query(_request(max_rows=1))
    finally:
        client.close()


def test_response_bytes_bound_and_no_redirect_retry(object_store, state, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", TOKEN)
    config = load_tushare_access(ROOT).model_copy(update={"max_response_bytes": 1024})
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 1025)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = TushareStructuredProvider(
            object_store, state, client=client, config=config, project_root=ROOT
        )
        with pytest.raises(ProviderError):
            provider.query(_request())
    assert len(calls) == 1


def test_thin_cli_schema_works_without_credentials(object_store, state, monkeypatch):
    from astock.settings import ProjectPaths

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    app = typer.Typer()
    register_tushare_commands(
        app, lambda: (ProjectPaths.discover(ROOT), state, object_store),
        lambda value: typer.echo(json.dumps(value)),
    )
    result = CliRunner().invoke(app, ["tushare-schema", "--endpoint", "income"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["points_are_not_permission_proof"]
    assert payload["financial_group_requests_per_minute"] == 80

def test_credential_rotation_does_not_reuse_cached_permissions(object_store, state, monkeypatch):
    provider, client, calls = _provider(object_store, state, monkeypatch)
    try:
        first = provider.query(_request())
        monkeypatch.setenv("TUSHARE_TOKEN", "[REDACTED_SECRET_OTHER]")
        second = provider.query(_request())
        schema = provider.schema("daily")
        assert schema["endpoints"]["daily"]["permission_status"] == "UNVERIFIED"
        observed = schema["endpoints"]["daily"]["observed_permission"]
        assert observed["status"] == "RESPONSE_ACCEPTED"
        assert not observed["permanent_entitlement_proven"]
        assert TOKEN not in json.dumps(schema)
    finally:
        client.close()
    assert len(calls) == 2 and second["cache_status"] == "MISS"
    assert first["request_fingerprint"] != second["request_fingerprint"]
    assert "[REDACTED_SECRET_OTHER]" not in json.dumps(second)


def test_mcp_probe_only_lists_tools_without_data_call(object_store, state, monkeypatch):
    import io

    import astock.providers.tushare_mcp as mcp

    provider, client, _calls = _provider(object_store, state, monkeypatch)
    messages = []

    class Response(io.BytesIO):
        def __init__(self, payload):
            super().__init__(json.dumps(payload).encode())
            self.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "unit-session"}

    class Opener:
        def open(self, request, timeout):
            message = json.loads(request.data) if request.data is not None else {}
            messages.append(message.get("method", request.method))
            if message.get("method") == "initialize":
                return Response({
                    "jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}
                })
            if message.get("method") == "tools/list":
                return Response({
                    "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "tushare_query"}]}
                })
            return Response({})

    monkeypatch.setattr(mcp, "build_opener", lambda *_args: Opener())
    try:
        result = mcp.probe_tushare_mcp(provider)
    finally:
        client.close()
    assert messages == ["initialize", "notifications/initialized", "tools/list", "DELETE"]
    assert result["data_queries_executed"] == 0 and result["tool_count"] == 1
    assert not result["market_endpoint_permissions_proven"]
    assert TOKEN not in json.dumps(result)
    assert len(state.get_cursor("tushare", "quota")["value"]["recent"]) == 4


def test_mcp_sse_matching_response_and_size_bound():
    import io

    from astock.providers.tushare_mcp import _response_json

    class EventStream(io.BytesIO):
        headers = {"Content-Type": "text/event-stream"}

    event = b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
    assert _response_json(EventStream(event), 2, float("inf")) == {"tools": []}
    with pytest.raises(ValueError):
        _response_json(EventStream(event[:-2]), 2, float("inf"))

@pytest.mark.parametrize("endpoint", ["weekly", "monthly"])
def test_period_units_never_inherit_daily_scaling(endpoint):
    from astock.providers.tushare_structured import _semantics

    semantics = _semantics(endpoint)
    assert semantics["vol"] == semantics["amount"] == "SOURCE_NATIVE_UNVERIFIED"
    assert semantics["pre_close"] == "PREVIOUS_PERIOD_CLOSE"
    assert not semantics["automatic_unit_conversion"]
    assert not semantics["historical_unit_contract_verified"]


def test_recorded_two_exchanges_and_periods_do_not_generalize_historical_units():
    from decimal import Decimal

    fixture = json.loads(
        (ROOT / "tests/fixtures/providers/tushare-period-units.json").read_text(encoding="utf-8")
    )
    assert not fixture["historical_contract_proven"]
    assert not fixture["normalization_allowed_by_this_observation"]
    assert {item["symbol"] for item in fixture["results"]} == {"600519.SH", "000001.SZ"}
    assert len(fixture["results"]) == 4
    for group in fixture["results"]:
        assert group["period_count"] >= 2
        for period in group["periods"]:
            assert period["status"] == "MATCHED_SAMPLE"
            assert Decimal(period["daily_sum_shares"]) == Decimal(period["aggregate_volume_native"])
            assert abs(
                Decimal(period["daily_sum_cny"]) - Decimal(period["aggregate_amount_native"])
            ) <= 1


def test_old_semantic_contract_cache_is_not_reused(object_store, state, monkeypatch):
    from astock.core.hashing import content_hash, sha256_bytes

    provider, client, calls = _provider(object_store, state, monkeypatch)
    identity = _request().identity()
    old_fingerprint = content_hash({
        "contract": "tushare-query-v1",
        "credential_scope": sha256_bytes(TOKEN.encode()), **identity,
    })
    old_result = object_store.put_json({
        "request_fingerprint": old_fingerprint, "rows": [_daily()],
        "semantics": {"vol": "OLD_WRONG_UNITS"},
    })
    state.set_cursor("tushare", "cache:" + old_fingerprint, {
        "object_hash": old_result.sha256, "fetched_epoch": NOW.timestamp(),
    })
    try:
        result = provider.query(_request())
    finally:
        client.close()
    assert len(calls) == 1 and result["cache_status"] == "MISS"
    assert result["semantics"]["vol"] == "LOT_100_SHARES"


@pytest.mark.parametrize("status,failure", [
    (401, FailureClass.AUTH_REQUIRED), (403, FailureClass.ACCESS_RESTRICTED),
    (307, FailureClass.POLICY_REJECTED),
])
def test_mcp_auth_denial_is_distinct_from_redirect_policy(
    object_store, state, monkeypatch, status, failure
):
    import io
    from email.message import Message
    from urllib.error import HTTPError

    import astock.providers.tushare_mcp as mcp

    provider, client, _calls = _provider(object_store, state, monkeypatch)

    class Opener:
        def open(self, request, timeout):
            headers = Message()
            if status == 307:
                headers["Location"] = request.full_url + "/"
            raise HTTPError(request.full_url, status, "opaque", headers, io.BytesIO(b""))

    monkeypatch.setattr(mcp, "build_opener", lambda *_args: Opener())
    try:
        with pytest.raises(ProviderError) as caught:
            mcp.probe_tushare_mcp(provider)
        assert caught.value.failure_class is failure
        assert caught.value.details["status_code"] == status
        assert caught.value.details["canonical_trailing_slash"] is (status == 307)
        assert TOKEN not in json.dumps(caught.value.details)
    finally:
        client.close()
