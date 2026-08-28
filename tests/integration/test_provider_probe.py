from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers import ProviderProbeService, RawProbeResponse, load_provider_registry
from astock.providers.financial_base import FinancialRawCaptureError
from astock.providers.probe import _parse_datetime
from astock.schemas import (
    ProviderHealthStatus,
    ProviderProbeFailureCode,
    ProviderProbeMode,
    ProviderProbeReport,
    ProviderRegistry,
)


def _service(tmp_path: Path) -> ProviderProbeService:
    state = StateStore(tmp_path / "state.sqlite", Path("migrations"))
    state.migrate()
    return ProviderProbeService(
        project_root=Path.cwd(),
        registry=load_provider_registry(Path("configs/provider_registry.yaml")),
        state=state,
        objects=ObjectStore(tmp_path / "objects"),
    )


def test_recorded_probe_persists_verified_artifact_and_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.probe("eastmoney-5m")
    second = service.probe("eastmoney-5m")

    assert first == second
    assert first.status == ProviderHealthStatus.HEALTHY
    assert first.probe_mode == "RECORDED"
    assert first.last_probe_at is not None
    assert service.objects.verify(str(first.report_object_hash))
    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 1
        artifact = connection.execute(
            "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
            (first.report_artifact_id,),
        ).fetchone()
    assert artifact == ("ProviderProbeReport", first.report_object_hash)


def test_cninfo_probe_is_honest_about_unchecked_download_capability(tmp_path: Path) -> None:
    service = _service(tmp_path)
    recorded = service.probe("cninfo-disclosures")

    assert recorded.status == ProviderHealthStatus.DEGRADED
    assert recorded.failure_code == ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
    assert recorded.checked_capabilities == ["disclosure.discover"]
    assert "disclosure.document" in recorded.capability_gaps

    live_calls = 0

    def successful_search(_provider: object) -> RawProbeResponse:
        nonlocal live_calls
        live_calls += 1
        return RawProbeResponse(200, b'{"announcements":[]}')

    service.live_transport = successful_search
    live = service.probe("cninfo-disclosures", live=True, probe_key="cninfo-search-only")
    assert live_calls == 1
    assert live.status == ProviderHealthStatus.DEGRADED
    assert live.failure_code == ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
    assert live.checked_capabilities == ["disclosure.discover"]
    assert "disclosure.document" in live.capability_gaps


def test_object_written_before_sql_failure_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    original = service.state.record_provider_probe
    writes: list[str] = []

    def fail_once(report: object, object_hash: str) -> bool:
        writes.append(object_hash)
        raise RuntimeError("simulated transaction boundary crash")

    monkeypatch.setattr(service.state, "record_provider_probe", fail_once)
    with pytest.raises(RuntimeError, match="simulated"):
        service.probe("sina-5m")
    assert service.objects.verify(writes[0])
    assert service.state.get_provider_probe_health("sina-5m") is None

    monkeypatch.setattr(service.state, "record_provider_probe", original)
    recovered = service.probe("sina-5m")
    assert recovered.status == ProviderHealthStatus.HEALTHY
    assert service.objects.verify(str(recovered.report_object_hash))
    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 1


def test_sql_transaction_rolls_back_event_artifact_and_health(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with service.state.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_provider_health BEFORE INSERT ON provider_health "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
        service.probe("cninfo-disclosures")

    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM artifact_registry WHERE type='ProviderProbeReport'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM provider_health").fetchone()[0] == 0


def test_damaged_latest_object_is_corrupt_and_probe_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    healthy = service.probe("eastmoney-5m")
    service.objects.path_for(str(healthy.report_object_hash)).write_bytes(b"tampered")

    corrupt = service.status("eastmoney-5m")
    assert corrupt.status == ProviderHealthStatus.CORRUPT
    with pytest.raises(RuntimeError, match="CORRUPT"):
        service.probe("eastmoney-5m")


def test_damaged_financial_probe_is_corrupt_and_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.probe("eastmoney-financial")
    assert result.report_object_hash is not None
    service.objects.path_for(result.report_object_hash).write_bytes(b"tampered")
    assert service.status("eastmoney-financial").status is ProviderHealthStatus.CORRUPT
    with pytest.raises(RuntimeError, match="CORRUPT"):
        service.probe("eastmoney-financial")
    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 1


def test_damaged_artifact_chain_is_corrupt_and_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    healthy = service.probe("sina-5m")
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE artifact_registry SET object_hash=? WHERE artifact_id=?",
            ("0" * 64, healthy.report_artifact_id),
        )

    assert service.status("sina-5m").status == ProviderHealthStatus.CORRUPT
    with pytest.raises(RuntimeError, match="CORRUPT"):
        service.probe("sina-5m")


def test_missing_health_or_incomplete_latest_pointer_is_corrupt(tmp_path: Path) -> None:
    missing_health = _service(tmp_path / "missing")
    missing_health.probe("eastmoney-5m")
    with missing_health.state.transaction() as connection:
        connection.execute("DELETE FROM provider_health WHERE provider_id='eastmoney-5m'")
    assert missing_health.status("eastmoney-5m").status == ProviderHealthStatus.CORRUPT

    incomplete = _service(tmp_path / "incomplete")
    incomplete.probe("sina-5m")
    with incomplete.state.transaction() as connection:
        connection.execute(
            "UPDATE provider_health SET report_artifact_id=NULL WHERE provider_id='sina-5m'"
        )
    assert incomplete.status("sina-5m").status == ProviderHealthStatus.CORRUPT


def test_live_provider_error_is_captured_as_structured_health_result(tmp_path: Path) -> None:
    service = _service(tmp_path)

    def denied(_provider: object) -> RawProbeResponse:
        raise ProviderError(
            "provider denied access",
            failure_class=FailureClass.ACCESS_RESTRICTED,
            retryable=False,
            details={"status_code": 403},
        )

    service.live_transport = denied
    result = service.probe("sina-5m", live=True, probe_key="sina-denied")

    assert result.status is ProviderHealthStatus.UNAVAILABLE
    assert result.failure_code is ProviderProbeFailureCode.HTTP_403
    assert result.last_probe_at is not None


def test_live_raw_capture_schema_drift_is_structured_degraded_health(tmp_path: Path) -> None:
    service = _service(tmp_path)

    def schema_drift(_provider: object) -> RawProbeResponse:
        raise FinancialRawCaptureError("FINANCIAL_RAW_NORMALIZATION_FAILED", [])

    service.live_transport = schema_drift
    result = service.probe(
        "eastmoney-financial",
        live=True,
        probe_key="financial-schema-drift",
    )

    assert result.status is ProviderHealthStatus.DEGRADED
    assert result.failure_code is ProviderProbeFailureCode.DATA_QUALITY
    assert result.last_probe_at is not None


def test_live_raw_capture_network_failure_is_structured_unavailable_health(tmp_path: Path) -> None:
    service = _service(tmp_path)

    def network_failure(_provider: object) -> RawProbeResponse:
        raise FinancialRawCaptureError("FINANCIAL_NETWORK_FAILED", [])

    service.live_transport = network_failure
    result = service.probe(
        "eastmoney-financial",
        live=True,
        probe_key="financial-network-failure",
    )

    assert result.status is ProviderHealthStatus.UNAVAILABLE
    assert result.failure_code is ProviderProbeFailureCode.NETWORK
    assert result.last_probe_at is not None


def test_consistently_rolled_back_health_pointer_is_corrupt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    earlier = service.probe("eastmoney-5m")
    earlier_report = ProviderProbeReport.model_validate_json(
        service.objects.get_bytes(str(earlier.report_object_hash))
    )
    service.live_transport = lambda _provider: RawProbeResponse(403, b'{"denied":true}')
    later = service.probe("eastmoney-5m", live=True, probe_key="later-unavailable")
    assert later.status == ProviderHealthStatus.UNAVAILABLE
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE provider_health SET capability_hash=?,status=?,last_probe_at=?,"
            "failure_count=?,last_error_class=?,registry_version=?,probe_mode=?,"
            "report_artifact_id=?,report_object_hash=?,failure_code=?,latest_probe_id=? "
            "WHERE provider_id=?",
            (
                earlier_report.capability_hash,
                earlier_report.status.value,
                earlier_report.completed_at.isoformat(),
                0,
                None,
                earlier_report.registry_version,
                earlier_report.probe_mode.value,
                earlier.report_artifact_id,
                earlier.report_object_hash,
                None,
                earlier_report.probe_id,
                earlier_report.provider_id,
            ),
        )

    assert service.status("eastmoney-5m").status == ProviderHealthStatus.CORRUPT
    with pytest.raises(RuntimeError, match="CORRUPT"):
        service.probe("eastmoney-5m")


def test_pure_legacy_health_without_probe_event_is_not_probed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.state.record_provider_health(
        "eastmoney-5m", status="AVAILABLE", capability_hash="legacy"
    )

    result = service.status("eastmoney-5m")
    assert result.status == ProviderHealthStatus.NOT_PROBED
    assert result.last_probe_at is None
    assert result.checked_capabilities == []


def test_registry_version_and_same_version_capability_drift_require_new_probe(
    tmp_path: Path,
) -> None:
    original = _service(tmp_path)
    original.probe("eastmoney-5m")
    payload = original.registry.model_dump(mode="python")

    version_payload = dict(payload)
    version_payload["registry_version"] = "provider-registry-v4-test"
    version_service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=ProviderRegistry.model_validate(version_payload),
        state=original.state,
        objects=original.objects,
    )
    assert version_service.status("eastmoney-5m").status == ProviderHealthStatus.NOT_PROBED

    capability_payload = original.registry.model_dump(mode="python")
    capability_payload["providers"][0]["capabilities"].append("market.experimental")
    capability_service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=ProviderRegistry.model_validate(capability_payload),
        state=original.state,
        objects=original.objects,
    )
    drift = capability_service.status("eastmoney-5m")
    assert drift.status == ProviderHealthStatus.NOT_PROBED
    assert drift.checked_capabilities == []
    assert "market.experimental" in drift.capability_gaps

    refreshed_capability = capability_service.probe("eastmoney-5m")
    assert refreshed_capability.status == ProviderHealthStatus.DEGRADED
    assert "market.experimental" in refreshed_capability.capability_gaps
    refreshed_version = version_service.probe("eastmoney-5m")
    assert refreshed_version.status == ProviderHealthStatus.HEALTHY
    assert refreshed_version.registry_version == "provider-registry-v4-test"


def test_idempotent_event_lookup_returns_requested_report_not_latest(tmp_path: Path) -> None:
    service = _service(tmp_path)
    recorded = service.probe("eastmoney-5m")
    service.live_transport = lambda _provider: RawProbeResponse(403, b'{"denied":true}')
    failed_live = service.probe("eastmoney-5m", live=True, probe_key="failed-live-v1")
    retried_recorded = service.probe("eastmoney-5m")

    assert failed_live.status == ProviderHealthStatus.UNAVAILABLE
    assert retried_recorded.status == ProviderHealthStatus.HEALTHY
    assert retried_recorded.probe_mode == "RECORDED"
    assert retried_recorded.report_object_hash == recorded.report_object_hash
    assert service.status("eastmoney-5m").probe_mode == "LIVE"


def test_live_same_key_calls_transport_once_and_returns_same_report(tmp_path: Path) -> None:
    service = _service(tmp_path)
    calls = 0

    def success(_provider: object) -> RawProbeResponse:
        nonlocal calls
        calls += 1
        return RawProbeResponse(
            200,
            b'[{"day":"2026-07-22 15:00:00","open":"1","high":"1",'
            b'"low":"1","close":"1","volume":"1"}]',
        )

    service.live_transport = success
    first = service.probe("sina-5m", live=True, probe_key="stable-live-key")
    second = service.probe("sina-5m", live=True, probe_key="stable-live-key")

    assert calls == 1
    assert first == second
    assert first.report_object_hash == second.report_object_hash


def test_concurrent_same_identity_executes_and_commits_once(tmp_path: Path) -> None:
    service = _service(tmp_path)
    second_service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=service.registry,
        state=StateStore(service.state.path, Path("migrations")),
        objects=ObjectStore(service.objects.root),
    )
    start = threading.Barrier(2)
    calls = 0
    calls_lock = threading.Lock()

    def slow_success(_provider: object) -> RawProbeResponse:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return RawProbeResponse(
            200,
            b'{"data":{"klines":["2026-07-22 15:00,1,1,1,1,1,1"]}}',
        )

    def run(selected: ProviderProbeService) -> object:
        start.wait()
        return selected.probe("eastmoney-5m", live=True, probe_key="parallel-v1")

    service.live_transport = slow_success
    second_service.live_transport = slow_success
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, [service, second_service]))

    assert calls == 1
    assert results[0] == results[1]
    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 1


def test_different_identities_rebuild_latest_by_completion_not_commit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    unavailable = ProviderProbeService(
        project_root=Path.cwd(),
        registry=service.registry,
        state=service.state,
        objects=service.objects,
        live_transport=lambda _provider: RawProbeResponse(403, b'{"denied":true}'),
    )
    service.live_transport = lambda _provider: RawProbeResponse(
        200,
        b'{"data":{"klines":["2026-07-22 15:00,1,1,1,1,1,1"]}}',
    )
    original = service.state.record_provider_probe
    healthy_waiting = threading.Event()
    unavailable_committed = threading.Event()

    def commit_out_of_order(report: ProviderProbeReport, object_hash: str) -> bool:
        if report.status == ProviderHealthStatus.HEALTHY:
            healthy_waiting.set()
            assert unavailable_committed.wait(timeout=5)
            return original(report, object_hash)
        result = original(report, object_hash)
        unavailable_committed.set()
        return result

    monkeypatch.setattr(service.state, "record_provider_probe", commit_out_of_order)
    with ThreadPoolExecutor(max_workers=2) as executor:
        earlier_future = executor.submit(
            service.probe,
            "eastmoney-5m",
            live=True,
            probe_key="healthy-earlier",
        )
        assert healthy_waiting.wait(timeout=5)
        later_future = executor.submit(
            unavailable.probe,
            "eastmoney-5m",
            live=True,
            probe_key="unavailable-later",
        )
        earlier = earlier_future.result()
        later = later_future.result()

    assert earlier.last_probe_at is not None
    assert later.last_probe_at is not None
    assert earlier.last_probe_at < later.last_probe_at
    latest = service.status("eastmoney-5m")
    assert latest.status == ProviderHealthStatus.UNAVAILABLE
    assert latest.last_probe_at == later.last_probe_at
    assert latest.report_object_hash == later.report_object_hash
    assert latest.failure_count == 1
    with sqlite3.connect(service.state.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 2


def test_same_completion_time_uses_probe_id_as_stable_latest_tie_break(tmp_path: Path) -> None:
    service = _service(tmp_path)
    provider = service.registry.providers[0]
    completed_at = datetime(2026, 7, 22, 15, 30, tzinfo=UTC)
    common = {
        "provider_id": provider.provider_id,
        "registry_version": service.registry.registry_version,
        "capability_hash": content_hash(provider),
        "probe_mode": ProviderProbeMode.LIVE,
        "started_at": completed_at,
        "completed_at": completed_at,
        "latency_ms": 0,
        "checked_capabilities": ["market.raw_5m"],
        "capability_gaps": service.registry.capability_gaps,
    }
    earlier = ProviderProbeReport(
        **common,
        probe_id="a" * 64,
        status=ProviderHealthStatus.HEALTHY,
        failure_count=0,
    )
    later = ProviderProbeReport(
        **common,
        probe_id="b" * 64,
        status=ProviderHealthStatus.UNAVAILABLE,
        failure_code=ProviderProbeFailureCode.HTTP_403,
        failure_count=1,
    )
    later_object = service.objects.put_json(later.model_dump(mode="json"))
    service.state.record_provider_probe(later, later_object.sha256)
    earlier_object = service.objects.put_json(earlier.model_dump(mode="json"))
    service.state.record_provider_probe(earlier, earlier_object.sha256)

    latest = service.status(provider.provider_id)
    assert latest.status == ProviderHealthStatus.UNAVAILABLE
    assert latest.report_artifact_id == f"provider-probe:{later.probe_id}"
    assert latest.failure_count == 1

    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE provider_health SET capability_hash=?,status=?,last_probe_at=?,"
            "failure_count=0,last_error_class=NULL,registry_version=?,probe_mode=?,"
            "report_artifact_id=?,report_object_hash=?,failure_code=NULL,latest_probe_id=? "
            "WHERE provider_id=?",
            (
                earlier.capability_hash,
                earlier.status.value,
                earlier.completed_at.isoformat(),
                earlier.registry_version,
                earlier.probe_mode.value,
                f"provider-probe:{earlier.probe_id}",
                earlier_object.sha256,
                earlier.probe_id,
                earlier.provider_id,
            ),
        )
    assert service.status(provider.provider_id).status == ProviderHealthStatus.CORRUPT


def test_probe_event_failure_marker_is_distinct_from_derived_health_streak(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.live_transport = lambda _provider: RawProbeResponse(403, b'{"denied":true}')

    first = service.probe("eastmoney-5m", live=True, probe_key="failure-one")
    second = service.probe("eastmoney-5m", live=True, probe_key="failure-two")
    replayed_first = service.probe(
        "eastmoney-5m",
        live=True,
        probe_key="failure-one",
    )

    assert first.failure_count == 1
    assert second.failure_count == 2
    assert replayed_first.failure_count == 1
    assert service.status("eastmoney-5m").failure_count == 2
    with sqlite3.connect(service.state.path) as connection:
        event_markers = connection.execute(
            "SELECT failure_count FROM provider_probe_event "
            "WHERE provider_id='eastmoney-5m' ORDER BY completed_at,probe_id"
        ).fetchall()
    assert event_markers == [(1,), (1,)]
    for result in (first, second):
        report = ProviderProbeReport.model_validate_json(
            service.objects.get_bytes(str(result.report_object_hash))
        )
        assert report.failure_count == 1


@pytest.mark.parametrize("damage", ["object", "artifact", "event"])
def test_damaged_historical_identity_replay_fails_closed_without_details(
    tmp_path: Path,
    damage: str,
) -> None:
    service = _service(tmp_path)
    recorded = service.probe("eastmoney-5m")
    service.live_transport = lambda _provider: RawProbeResponse(
        200,
        b'{"data":{"klines":["2026-07-22 15:00,1,1,1,1,1,1"]}}',
    )
    latest = service.probe("eastmoney-5m", live=True, probe_key="newer-live")
    recorded_probe_id = str(recorded.report_artifact_id).split(":", maxsplit=1)[1]
    if damage == "object":
        service.objects.path_for(str(recorded.report_object_hash)).write_bytes(b"tampered")
    else:
        with service.state.transaction() as connection:
            if damage == "artifact":
                connection.execute(
                    "UPDATE artifact_registry SET object_hash=? WHERE artifact_id=?",
                    ("0" * 64, recorded.report_artifact_id),
                )
            else:
                connection.execute(
                    "UPDATE provider_probe_event SET status='DEGRADED' WHERE probe_id=?",
                    (recorded_probe_id,),
                )

    assert service.status("eastmoney-5m").report_object_hash == latest.report_object_hash
    with pytest.raises(RuntimeError) as captured:
        service.probe("eastmoney-5m")
    assert str(captured.value) == "provider probe state is CORRUPT"
    assert str(recorded.report_object_hash) not in str(captured.value)


def test_cli_historical_corruption_has_fixed_safe_exit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = StateStore(runtime / "state.sqlite", Path("migrations"))
    state.migrate()
    objects = ObjectStore(runtime / "objects" / "sha256")
    service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=load_provider_registry(Path("configs/provider_registry.yaml")),
        state=state,
        objects=objects,
        live_transport=lambda _provider: RawProbeResponse(
            200,
            b'{"data":{"klines":["2026-07-22 15:00,1,1,1,1,1,1"]}}',
        ),
    )
    recorded = service.probe("eastmoney-5m")
    service.probe("eastmoney-5m", live=True, probe_key="newer-live")
    objects.path_for(str(recorded.report_object_hash)).write_bytes(b"private-token=secret")
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    result = CliRunner().invoke(app, ["provider-probe", "eastmoney-5m"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "failure_code": "CORRUPT_PROVIDER_STATE",
        "status": "FAILED",
    }
    assert "secret" not in result.stdout
    assert str(recorded.report_object_hash) not in result.stdout


@pytest.mark.parametrize("read_error", [FileNotFoundError, PermissionError])
def test_unreadable_recorded_override_is_stable_and_never_leaks_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_error: type[OSError],
) -> None:
    service = _service(tmp_path / "runtime")
    fixture = tmp_path / "private-token=secret" / "recorded.json"
    original = Path.read_bytes

    def fail_selected(path: Path) -> bytes:
        if path == fixture:
            raise read_error(str(fixture))
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    first = service.probe("eastmoney-5m", recorded_fixture=fixture)
    second = service.probe("eastmoney-5m", recorded_fixture=fixture)
    report = service.objects.get_bytes(str(first.report_object_hash)).decode("utf-8")

    assert first == second
    assert first.status == ProviderHealthStatus.DEGRADED
    assert first.failure_code == ProviderProbeFailureCode.MALFORMED_RESPONSE
    assert "private-token" not in report
    assert "secret" not in report
    assert str(fixture) not in report


@pytest.mark.parametrize("read_error", [FileNotFoundError, PermissionError])
def test_cli_unreadable_recorded_fixture_is_degraded_and_path_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_error: type[OSError],
) -> None:
    runtime = tmp_path / "runtime"
    registry = load_provider_registry(Path("configs/provider_registry.yaml"))
    configured_fixture = (Path.cwd() / registry.providers[0].recorded_fixture).resolve()
    original = Path.read_bytes

    def fail_selected(path: Path) -> bytes:
        if path.resolve() == configured_fixture:
            raise read_error("C:\\private-token=secret\\fixture.json")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    result = CliRunner().invoke(app, ["provider-probe", "eastmoney-5m"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "DEGRADED"
    assert payload["failure_code"] == "MALFORMED_RESPONSE"
    assert "private-token" not in result.stdout
    assert "secret" not in result.stdout
    assert str(configured_fixture) not in result.stdout


def test_parse_datetime_handles_nonempty_iso_and_invalid_values() -> None:
    parsed = _parse_datetime("2026-07-22T12:34:56+00:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-07-22T12:34:56+00:00"
    assert _parse_datetime("not-a-time") is None


def test_provider_list_status_and_legacy_probe_never_call_network_or_fake_probe_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "中文运行目录"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    runner = CliRunner()

    listed = runner.invoke(app, ["provider-list"])
    status = runner.invoke(app, ["provider-status", "eastmoney-5m"])
    legacy = runner.invoke(app, ["probe"])

    assert listed.exit_code == status.exit_code == legacy.exit_code == 0
    assert json.loads(status.stdout)["last_probe_at"] is None
    with sqlite3.connect(runtime / "state.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM provider_probe_event").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM provider_health").fetchone()[0] == 0


def test_cli_recorded_probe_and_unknown_provider_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(Path.cwd()))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    runner = CliRunner()

    success = runner.invoke(app, ["provider-probe", "cninfo-disclosures"])
    missing_live_key = runner.invoke(app, ["provider-probe", "eastmoney-5m", "--live"])
    unknown = runner.invoke(app, ["provider-status", "unknown?token=secret"])

    assert success.exit_code == 0
    assert json.loads(success.stdout)["status"] == "DEGRADED"
    assert missing_live_key.exit_code == 1
    assert json.loads(missing_live_key.stdout) == {
        "failure_code": "PROBE_KEY_REQUIRED",
        "status": "FAILED",
    }
    assert unknown.exit_code == 1
    payload = json.loads(unknown.stdout)
    assert payload == {"failure_code": "UNKNOWN_PROVIDER", "status": "FAILED"}
    assert "secret" not in unknown.stdout


def test_migration_0037_is_repeatable_on_empty_database(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", Path("migrations"))
    applied = state.migrate()
    second = state.migrate()

    assert "0037" in applied
    assert second == []


def test_migration_0037_upgrades_a_database_frozen_at_0036(tmp_path: Path) -> None:
    migration_copy = tmp_path / "migrations"
    migration_copy.mkdir()
    for source in sorted(Path("migrations").glob("*.sql")):
        if source.name[:4] <= "0036":
            shutil.copy2(source, migration_copy / source.name)
    state = StateStore(tmp_path / "state.sqlite", migration_copy)
    state.migrate()

    shutil.copy2(
        Path("migrations/0037_provider_probe_history.sql"),
        migration_copy / "0037_provider_probe_history.sql",
    )
    assert state.migrate() == ["0037"]
    with sqlite3.connect(state.path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(provider_health)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"report_object_hash", "latest_probe_id", "failure_code"} <= columns
    assert "provider_probe_event" in tables
