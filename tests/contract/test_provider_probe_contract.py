from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers import ProviderProbeService, RawProbeResponse, load_provider_registry
from astock.schemas import ProviderHealthStatus, ProviderProbeFailureCode


@pytest.fixture
def service(tmp_path: Path) -> ProviderProbeService:
    state = StateStore(tmp_path / "state.sqlite", Path("migrations"))
    state.migrate()
    return ProviderProbeService(
        project_root=Path.cwd(),
        registry=load_provider_registry(Path("configs/provider_registry.yaml")),
        state=state,
        objects=ObjectStore(tmp_path / "objects"),
    )


@pytest.mark.parametrize(
    ("fixture", "expected_status", "expected_code"),
    [
        ("http-401.json", ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.HTTP_401),
        ("http-403.json", ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.HTTP_403),
        ("http-429.json", ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.HTTP_429),
        ("timeout.json", ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.TIMEOUT),
        ("network.json", ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.NETWORK),
        (
            "malformed.json",
            ProviderHealthStatus.DEGRADED,
            ProviderProbeFailureCode.MALFORMED_RESPONSE,
        ),
        (
            "data-quality.json",
            ProviderHealthStatus.DEGRADED,
            ProviderProbeFailureCode.DATA_QUALITY,
        ),
    ],
)
def test_recorded_failure_taxonomy_is_stable(
    service: ProviderProbeService,
    fixture: str,
    expected_status: ProviderHealthStatus,
    expected_code: ProviderProbeFailureCode,
) -> None:
    result = service.probe(
        "eastmoney-5m",
        recorded_fixture=Path("tests/fixtures/providers") / fixture,
    )

    assert result.status == expected_status
    assert result.failure_code == expected_code


def test_live_response_metadata_is_redacted(service: ProviderProbeService) -> None:
    secret = "token=abc123; Cookie=session-secret; https://example.invalid/?key=secret"
    service.live_transport = lambda _provider: RawProbeResponse(
        status_code=200,
        content=json.dumps(
            {"data": {"klines": [f"2026-07-22 15:00,1,1,1,1,1,1,{secret}"]}}
        ).encode(),
        content_type="token=header-secret",
        latency_ms=1,
    )

    result = service.probe("eastmoney-5m", live=True, probe_key="privacy-case")
    report = service.objects.get_bytes(str(result.report_object_hash)).decode("utf-8")

    assert result.status == ProviderHealthStatus.HEALTHY
    assert "abc123" not in report
    assert "session-secret" not in report
    assert "header-secret" not in report
    assert "example.invalid" not in report


def test_live_timeout_and_invalid_json_are_classified(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", Path("migrations"))
    state.migrate()
    calls = 0

    def malformed(_provider: object) -> RawProbeResponse:
        nonlocal calls
        calls += 1
        return RawProbeResponse(200, b"not-json")

    service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=load_provider_registry(Path("configs/provider_registry.yaml")),
        state=state,
        objects=ObjectStore(tmp_path / "objects"),
        live_transport=malformed,
    )
    result = service.probe("sina-5m", live=True, probe_key="malformed-live")

    assert calls == 1
    assert result.failure_code == ProviderProbeFailureCode.MALFORMED_RESPONSE
    assert result.last_probe_at is not None


def test_probe_report_schema_rejects_inconsistent_health() -> None:
    from astock.schemas import ProviderProbeMode, ProviderProbeReport

    with pytest.raises(ValueError, match="requires failure_code"):
        ProviderProbeReport(
            probe_id="a" * 64,
            provider_id="eastmoney-5m",
            registry_version="v1",
            capability_hash="b" * 64,
            probe_mode=ProviderProbeMode.RECORDED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            latency_ms=0,
            status=ProviderHealthStatus.DEGRADED,
            checked_capabilities=["market.raw_5m"],
            capability_gaps=[],
            failure_count=1,
        )
