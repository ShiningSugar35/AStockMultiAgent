"""A request cannot multiply its transport allowance across retries, threads or restart."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.network_ownership import NETWORK_SCOPE, OwnedNetworkLimits, OwnedNetworkRepository
from astock.core.object_store import ObjectStore
from astock.core.request_budget import (
    RequestBudgetExhausted,
    RequestExecutionBudget,
    current_request_budget,
)
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.providers.http_resilience import ResilientHttpClient

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 21, tzinfo=UTC)


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    return state


def _budget(state: StateStore, maximum: int = 1) -> RequestExecutionBudget:
    return RequestExecutionBudget(
        state,
        "request-last-allowance",
        NOW + timedelta(seconds=60),
        maximum,
        clock=lambda: NOW,
    )


def test_two_workers_cannot_both_spend_last_attempt(tmp_path: Path) -> None:
    state = _state(tmp_path)
    budgets = [_budget(state), _budget(state)]
    ready = Barrier(2)

    def reserve(budget: RequestExecutionBudget) -> str:
        ready.wait(timeout=5)
        try:
            budget.reserve_attempt()
            return "RESERVED"
        except RequestBudgetExhausted:
            return "EXHAUSTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, budgets)) == ["EXHAUSTED", "RESERVED"]
    restarted = RequestExecutionBudget(
        state,
        "request-last-allowance",
        NOW + timedelta(hours=1),
        999,
        clock=lambda: NOW,
    )
    assert restarted.deadline_at == NOW + timedelta(seconds=60)
    assert restarted.max_attempts == 1
    with pytest.raises(RequestBudgetExhausted):
        restarted.reserve_attempt()


def test_elapsed_deadline_blocks_dispatch_and_binding_does_not_leak(tmp_path: Path) -> None:
    state = _state(tmp_path)
    budget = RequestExecutionBudget(
        state,
        "expired-request",
        NOW - timedelta(seconds=1),
        2,
        clock=lambda: NOW,
    )
    assert current_request_budget() is None
    with budget.bind():
        assert current_request_budget() is budget
        with pytest.raises(RequestBudgetExhausted):
            budget.reserve_attempt()
    assert current_request_budget() is None
    checkpoint = state.get_checkpoint("request-transport-budget", budget.key)
    assert checkpoint is not None
    assert checkpoint["cursor"]["attempts_reserved"] == 0


def test_http_retries_share_one_budget_without_wrapper_double_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    calls: list[str] = []
    actual_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, request=request)

    monkeypatch.setattr(
        "astock.providers.http_resilience.httpx.Client",
        lambda **_kwargs: actual_client(transport=httpx.MockTransport(respond)),
    )
    client = ResilientHttpClient(
        timeout_seconds=2,
        follow_redirects=False,
        headers={},
        lane_trust_env=(False,),
        max_attempts=5,
        backoff_seconds=0,
        jitter_seconds=0,
        retry_status_codes=(503,),
    )
    provider = HttpProviderBase(ObjectStore(tmp_path / "objects"), state, client=client)
    with _budget(state).bind(), pytest.raises(RequestBudgetExhausted):
        provider._get("https://example.invalid/resource", params={})
    client.close()
    assert calls == ["/resource"]


@pytest.mark.parametrize(
    "retry_after,expected_calls,expected_sleep",
    [
        (1, 2, [1.0]),
        (120, 1, []),
    ],
)
def test_retry_after_respects_request_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: int,
    expected_calls: int,
    expected_sleep: list[float],
) -> None:
    state = _state(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []
    actual_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429 if len(calls) == 1 else 200,
            headers={"Retry-After": str(retry_after)},
            request=request,
        )

    monkeypatch.setattr(
        "astock.providers.http_resilience.httpx.Client",
        lambda **_kwargs: actual_client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr("astock.providers.http_resilience.time.sleep", sleeps.append)
    client = ResilientHttpClient(
        timeout_seconds=2,
        follow_redirects=False,
        headers={},
        lane_trust_env=(False,),
        max_attempts=3,
        backoff_seconds=0,
        jitter_seconds=0,
        retry_status_codes=(429,),
        elapsed_budget_seconds=30,
    )
    with _budget(state, 2).bind():
        response = client.get("https://example.invalid/resource")
    client.close()
    assert len(calls) == expected_calls
    assert sleeps == expected_sleep
    assert response.status_code == (200 if expected_calls == 2 else 429)



def _redirect_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> ResilientHttpClient:
    actual = httpx.Client

    def create(**kwargs: Any) -> httpx.Client:
        return actual(
            transport=httpx.MockTransport(handler),
            follow_redirects=kwargs["follow_redirects"], headers=kwargs["headers"],
        )

    monkeypatch.setattr("astock.providers.http_resilience.httpx.Client", create)
    return ResilientHttpClient(
        timeout_seconds=60, elapsed_budget_seconds=60, follow_redirects=True,
        headers={"Authorization": "Bearer fixture-only"}, lane_trust_env=(False,),
        max_attempts=1, backoff_seconds=0, jitter_seconds=0, retry_status_codes=(),
    )


@pytest.mark.parametrize("maximum,expire,expected", [(1, False, 1), (2, False, 2), (2, True, 1)])
def test_every_http_redirect_spends_attempt_and_rechecks_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maximum: int, expire: bool, expected: int,
) -> None:
    state = _state(tmp_path)
    clock = [NOW]
    budget = RequestExecutionBudget(
        state, "redirect-budget", NOW + timedelta(seconds=60), maximum, clock=lambda: clock[0],
    )
    requests: list[httpx.Request] = []

    def respond(item: httpx.Request) -> httpx.Response:
        requests.append(item)
        if len(requests) == 1:
            clock[0] += timedelta(seconds=61 if expire else 45)
            return httpx.Response(302, headers={"Location": "https://redirect.invalid/final"})
        assert "Authorization" not in item.headers
        assert item.extensions["timeout"]["read"] <= 15
        return httpx.Response(200, text="complete")

    client = _redirect_client(monkeypatch, respond)
    try:
        with budget.bind():
            if expected == 1:
                with pytest.raises(RequestBudgetExhausted):
                    client.get("https://initial.invalid/start")
            else:
                assert client.get("https://initial.invalid/start").text == "complete"
    finally:
        client.close()
    assert len(requests) == expected
    assert budget.remaining_attempts() == maximum - expected


def test_plain_httpx_provider_redirect_uses_same_physical_attempt_budget(tmp_path: Path) -> None:
    state = _state(tmp_path)
    attempts: list[str] = []

    def respond(item: httpx.Request) -> httpx.Response:
        attempts.append(item.url.path)
        return httpx.Response(302, headers={"Location": "/next"})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        provider = HttpProviderBase(ObjectStore(tmp_path / "objects"), state, client=client)
        with _budget(state).bind(), pytest.raises(RequestBudgetExhausted):
            provider._get("https://example.invalid/first", params={})
    assert attempts == ["/first"]


def test_provider_http_and_shared_web_use_one_global_pool_and_release_terminal_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    limits = OwnedNetworkLimits(1, 1, 1)
    monkeypatch.setattr("astock.core.network_ownership.load_network_limits", lambda: limits)
    repository = OwnedNetworkRepository(state)
    now = datetime.now(UTC)
    shared, acquired = repository.claim_acquisition(
        {"schema_version": "owned-web-acquisition-v1", "target": "an owned search"},
        provider="deepseek-tools", domain="api.deepseek.com", policy=limits, now=now,
        deadline_at=now + timedelta(seconds=30), fresh_after=now,
    )
    assert acquired
    repository.start_acquisition(shared["acquisition_id"], shared["generation"], now)
    sent: list[str] = []

    def respond(item: httpx.Request) -> httpx.Response:
        sent.append(item.url.path)
        return httpx.Response(200, text="provider response")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = HttpProviderBase(ObjectStore(tmp_path / "objects"), state, client=client)
        with pytest.raises(ProviderError) as blocked:
            provider._get("https://data.example/prices", params={})
        assert blocked.value.failure_class is FailureClass.RATE_LIMITED and sent == []
        repository.finish_acquisition(
            shared["acquisition_id"], shared["generation"], status="UNKNOWN",
            now=datetime.now(UTC),
        )
        assert provider._get("https://data.example/prices", params={})[0].status_code == 200
    with state.transaction() as connection:
        cursors = connection.execute(
            "SELECT cursor_json FROM checkpoint WHERE scope_type=?", (NETWORK_SCOPE,),
        ).fetchall()
    assert len(cursors) == 1  # The canonical unknown remains; transient HTTP ownership is gone.
    assert sent == ["/prices"]


def test_expired_then_dead_http_owner_reclaims_transient_row_but_keeps_canonical_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astock.storage_retention import ProcessIdentity

    state = _state(tmp_path)
    repository = OwnedNetworkRepository(state)
    limits = OwnedNetworkLimits()
    transient, _ = repository.claim_acquisition(
        {"schema_version": "owned-http-dispatch-v1", "dispatch_id": "fixture"},
        provider="fixture", domain="example.invalid", policy=limits, now=NOW,
        deadline_at=NOW + timedelta(seconds=1), fresh_after=NOW,
    )
    repository.start_acquisition(transient["acquisition_id"], transient["generation"], NOW)
    expired = repository.acquisition(transient["acquisition_id"], NOW + timedelta(seconds=2))
    assert expired is not None and expired["status"] == "UNKNOWN"
    assert state.get_checkpoint(NETWORK_SCOPE, transient["acquisition_id"]) is not None
    monkeypatch.setattr(
        "astock.core.network_ownership.process_identity", lambda _pid: ProcessIdentity("DEAD"),
    )
    repository.acquisition(transient["acquisition_id"], NOW + timedelta(seconds=3))
    assert state.get_checkpoint(NETWORK_SCOPE, transient["acquisition_id"]) is None


def test_provider_transport_exception_releases_the_exact_network_owner(tmp_path: Path) -> None:
    state = _state(tmp_path)

    def fail(item: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout", request=item)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        provider = HttpProviderBase(ObjectStore(tmp_path / "objects"), state, client=client)
        provider.provider_id = "fixture-http-timeout"
        with pytest.raises(ProviderError, match="fixture-http-timeout timed out") as caught:
            provider._get("https://data.example/failed", params={})
        assert caught.value.failure_class is FailureClass.TIMEOUT
    with state.transaction() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM checkpoint WHERE scope_type=?", (NETWORK_SCOPE,),
        ).fetchone()[0]
    assert count == 0
