from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from astock.core.source_resilience import (
    CircuitState,
    SourceCircuitBreaker,
    SourceFailureClass,
    classify_http_status,
    classify_source_error,
    load_source_resilience_policy,
    parse_retry_after_seconds,
)
from astock.core.source_router import SourceAccessRouter
from astock.providers.financial_base import FinancialRawCaptureError
from astock.schemas import (
    AccessTransport,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)


def _capability(
    source_id: str,
    *,
    transport: AccessTransport,
    source_class: SourceClass,
    health: str = "HEALTHY",
    freshness: Decimal = Decimal("1"),
    local: Decimal = Decimal("0"),
) -> TransportCapability:
    return TransportCapability(
        source_id=source_id,
        transport=transport,
        requested_capabilities=["news.discover"],
        available=True,
        reason=f"{source_id} ready",
        source_class=source_class,
        formal_eligible=True,
        completeness_score=Decimal("0.8"),
        local_availability_score=local,
        health_status=health,
        freshness_score=freshness,
        latency_ms=10,
        cost_efficiency_score=Decimal("1"),
        auth_ease_score=Decimal("1"),
    )


def test_source_hint_is_only_a_tie_break_not_a_safety_override() -> None:
    decision = SourceAccessRouter().decide(
        SourceAccessRequest(source_id="hinted-api", requested_capability="news.discover"),
        [
            _capability(
                "hinted-api",
                transport=AccessTransport.API,
                source_class=SourceClass.SECONDARY_STRUCTURED,
                health="DEGRADED",
                freshness=Decimal("0.2"),
            ),
            _capability(
                "local-cache",
                transport=AccessTransport.LOCAL,
                source_class=SourceClass.LOCAL_IMMUTABLE,
                local=Decimal("1"),
            ),
        ],
    )

    assert decision.selected_source_id == "local-cache"
    assert decision.selected_transport is AccessTransport.LOCAL


def test_strong_official_source_keeps_secondary_fallback_chain() -> None:
    capability = "financial.official_document"
    official = _capability(
        "official-web",
        transport=AccessTransport.BROWSER,
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        freshness=Decimal("0.5"),
    ).model_copy(update={"requested_capabilities": [capability]})
    secondary = _capability(
        "secondary-api",
        transport=AccessTransport.API,
        source_class=SourceClass.SECONDARY_STRUCTURED,
        local=Decimal("1"),
    ).model_copy(update={"requested_capabilities": [capability]})

    ranked = SourceAccessRouter().rank(
        SourceAccessRequest(requested_capability=capability, formal_use=True),
        [secondary, official],
    )

    assert [item.source_id for item in ranked] == ["official-web", "secondary-api"]

    preferred = SourceAccessRouter().decide(
        SourceAccessRequest(requested_capability=capability, formal_use=True),
        [secondary, official],
    )
    assert preferred.selected_source_id == "official-web"
    assert preferred.fallback_source_chain == ["official-web", "secondary-api"]

    decision = SourceAccessRouter().decide(
        SourceAccessRequest(requested_capability=capability, formal_use=True),
        [secondary, official.model_copy(update={"available": False})],
    )
    assert decision.selected_source_id == "secondary-api"
    assert decision.fallback_source_chain == ["official-web", "secondary-api"]


def test_rate_limit_opens_provider_capability_breaker_and_half_open_is_single_probe(state) -> None:
    breaker = SourceCircuitBreaker(state)

    opened = breaker.record_failure(
        "eastmoney-reference",
        "market.daily_unadjusted",
        SourceFailureClass.RATE_LIMITED,
        retry_after_seconds=600,
        at=NOW,
    )

    assert opened is CircuitState.OPEN
    assert not breaker.is_available(
        "eastmoney-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=599)
    )
    assert not breaker.claim_attempt(
        "eastmoney-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=599)
    )
    assert breaker.claim_attempt(
        "eastmoney-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=601)
    )
    assert not breaker.claim_attempt(
        "eastmoney-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=602)
    )

    breaker.record_success(
        "eastmoney-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=603)
    )
    status = breaker.status("eastmoney-reference", "market.daily_unadjusted")
    assert status["state"] == CircuitState.CLOSED.value
    assert status["failure_count"] == 0


def test_stale_half_open_probe_claim_is_recoverable_after_cooldown(state) -> None:
    breaker = SourceCircuitBreaker(state)
    breaker.record_failure(
        "eastmoney-reference",
        "market.daily_unadjusted",
        SourceFailureClass.RATE_LIMITED,
        retry_after_seconds=1,
        at=NOW,
    )

    assert breaker.claim_attempt(
        "eastmoney-reference",
        "market.daily_unadjusted",
        at=NOW + timedelta(seconds=301),
    )
    assert not breaker.claim_attempt(
        "eastmoney-reference",
        "market.daily_unadjusted",
        at=NOW + timedelta(seconds=302),
    )
    assert breaker.is_available(
        "eastmoney-reference",
        "market.daily_unadjusted",
        at=NOW + timedelta(seconds=601),
    )
    assert breaker.claim_attempt(
        "eastmoney-reference",
        "market.daily_unadjusted",
        at=NOW + timedelta(seconds=601),
    )
    assert not breaker.claim_attempt(
        "eastmoney-reference",
        "market.daily_unadjusted",
        at=NOW + timedelta(seconds=602),
    )


def test_open_breaker_with_corrupt_retry_timestamp_fails_closed(state) -> None:
    breaker = SourceCircuitBreaker(state)
    breaker.record_failure(
        "sina-reference",
        "instrument.identity",
        SourceFailureClass.AUTH_CONFIG,
        at=NOW,
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE source_circuit_breaker SET retry_after_at='not-a-time' "
            "WHERE source_id=? AND capability=?",
            ("sina-reference", "instrument.identity"),
        )

    assert not breaker.is_available(
        "sina-reference", "instrument.identity", at=NOW + timedelta(days=1)
    )
    assert not breaker.claim_attempt(
        "sina-reference", "instrument.identity", at=NOW + timedelta(days=1)
    )


def test_three_transient_failures_open_only_the_affected_capability(state) -> None:
    breaker = SourceCircuitBreaker(state)
    for offset in range(2):
        state_value = breaker.record_failure(
            "sina-reference",
            "instrument.master",
            SourceFailureClass.TRANSIENT_NETWORK,
            at=NOW + timedelta(seconds=offset),
        )
        assert state_value is CircuitState.CLOSED

    state_value = breaker.record_failure(
        "sina-reference",
        "instrument.master",
        SourceFailureClass.TRANSIENT_NETWORK,
        at=NOW + timedelta(seconds=2),
    )
    assert state_value is CircuitState.OPEN
    assert not breaker.is_available(
        "sina-reference", "instrument.master", at=NOW + timedelta(seconds=3)
    )
    assert breaker.is_available(
        "sina-reference", "market.daily_unadjusted", at=NOW + timedelta(seconds=3)
    )


def test_raw_capture_failure_codes_preserve_network_vs_schema_breaker_semantics(state) -> None:
    breaker = SourceCircuitBreaker(state)
    network = FinancialRawCaptureError("FINANCIAL_NETWORK_FAILED", [])
    schema = FinancialRawCaptureError("FINANCIAL_RAW_NORMALIZATION_FAILED", [])

    assert classify_source_error(network) is SourceFailureClass.TRANSIENT_NETWORK
    assert (
        breaker.record_failure(
            "eastmoney-financial",
            "financial.statement_values",
            classify_source_error(network),
            at=NOW,
        )
        is CircuitState.CLOSED
    )
    assert classify_source_error(schema) is SourceFailureClass.SCHEMA_DRIFT
    assert (
        breaker.record_failure(
            "sina-financial",
            "financial.statement_values",
            classify_source_error(schema),
            at=NOW,
        )
        is CircuitState.OPEN
    )


def test_raw_httpx_failures_preserve_transport_and_status_semantics() -> None:
    request = httpx.Request("GET", "https://example.test/")
    timeout = httpx.ReadTimeout("timed out", request=request)
    assert classify_source_error(timeout) is SourceFailureClass.TRANSIENT_NETWORK

    response = httpx.Response(503, request=request)
    server_error = httpx.HTTPStatusError("server error", request=request, response=response)
    assert classify_source_error(server_error) is SourceFailureClass.REMOTE_5XX


def test_source_resilience_v1_rejects_unimplemented_multi_probe_configuration(
    tmp_path: Path,
) -> None:
    source = (PROJECT_ROOT / "configs" / "source_resilience.yaml").read_text(encoding="utf-8")
    config = tmp_path / "source_resilience.yaml"
    config.write_text(
        source.replace("half_open_max_probes: 1", "half_open_max_probes: 2", 1),
        encoding="utf-8",
    )

    try:
        load_source_resilience_policy(config)
    except ValueError as exc:
        assert "exactly one half-open probe" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("multi-probe configuration must fail closed")


def test_retry_after_and_http_failure_classification() -> None:
    assert parse_retry_after_seconds("120", now=NOW) == 120
    future = (NOW + timedelta(seconds=75)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after_seconds(future, now=NOW) == 75
    assert parse_retry_after_seconds("invalid", now=NOW) is None
    assert classify_http_status(429) is SourceFailureClass.RATE_LIMITED
    assert classify_http_status(503) is SourceFailureClass.REMOTE_5XX
    assert classify_http_status(403) is SourceFailureClass.AUTH_CONFIG
    assert classify_http_status(404) is None
