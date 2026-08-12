from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService, _parse_eastmoney_instruments
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.sina_financial import _normalize_sina, _sina_rows
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.presentation import investor_view_from_run
from astock.schemas import FetchStatus, SourceSnapshot
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
)
from astock.schemas.research_runtime import (
    ResearchRunMode,
    ResearchRunPerformanceSummary,
    ResearchRunReport,
    ResearchRunStage,
    ResearchRunStatus,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _paths(tmp_path: Path) -> ProjectPaths:
    runtime = tmp_path / "runtime"
    return ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def _attempt(
    capability: AcquisitionCapability,
    status: AcquisitionAttemptStatus = AcquisitionAttemptStatus.SUCCEEDED,
) -> AcquisitionAttempt:
    return AcquisitionAttempt(
        capability=capability,
        status=status,
        provider_path=["test-provider"],
        fallback_used=False,
        record_count=1,
        latency_ms=1,
        internal_reason_codes=[],
        source_snapshot_ids=[],
        created_at=NOW,
    )


def test_eastmoney_current_master_accepts_numeric_key_dict_shape() -> None:
    payload: dict[str, object] = {
        "rc": 0,
        "_astock_request": {"market": "XSHG"},
        "data": {
            "diff": {
                "1": {"f12": "600938", "f14": "中国海油"},
                "0": {"f12": "600519", "f14": "贵州茅台"},
            }
        },
    }

    records = _parse_eastmoney_instruments(payload, "snapshot:test", NOW, Market.XSHG)

    assert [item.symbol for item in records] == ["600519", "600938"]
    assert all(item.market is Market.XSHG for item in records)


def _reference_service(tmp_path: Path) -> MarketReferenceService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )


def _snapshot(name: str) -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=f"snapshot:{name}",
        source_id="eastmoney-reference",
        object_sha256="a" * 64,
        fetched_at=NOW,
        available_to_system_at=NOW,
        fetch_status=FetchStatus.SUCCEEDED,
        source_url="https://example.invalid/current",
        mime="application/json",
        byte_size=1,
        headers_hash="b" * 64,
        rights_status="PUBLIC_REFERENCE_DATA",
        created_at=NOW,
    )


def test_reference_retry_is_bounded_and_only_for_retryable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr("astock.market_data.reference._time.sleep", lambda _seconds: None)

    def transient() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError(
                "temporary",
                failure_class=FailureClass.NETWORK,
                retryable=True,
            )
        return "ok"

    assert service._retry_reference_call(transient, live=True) == "ok"
    assert len(calls) == 2

    calls.clear()

    def permanent() -> str:
        calls.append(1)
        raise ProviderError(
            "denied",
            failure_class=FailureClass.ACCESS_RESTRICTED,
            retryable=False,
        )

    with pytest.raises(ProviderError):
        service._retry_reference_call(permanent, live=True)
    assert len(calls) == 1


def test_exact_instrument_identity_paginates_eastmoney_after_baostock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    pages: list[int] = []
    monkeypatch.setattr(service, "_baostock_circuit_open", lambda **_kwargs: True)

    def page(market: Market, number: int, *, live: bool = False):
        assert market is Market.XSHG
        assert live
        pages.append(number)
        symbol = "600519" if number == 1 else "600938"
        return (
            {
                "rc": 0,
                "_astock_request": {"market": "XSHG"},
                "data": {"total": 200, "diff": {"0": {"f12": symbol, "f14": symbol}}},
            },
            _snapshot(str(number)),
        )

    monkeypatch.setattr(service.eastmoney, "fetch_master_page", page)
    captured: dict[str, object] = {}

    def release(**kwargs: object):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(service, "_release", release)
    service.sync_instrument_identity("600938", Market.XSHG, live=True)

    assert pages == [1, 2]
    assert captured["scope_key"] == "XSHG:600938"
    records = captured["records"]
    assert isinstance(records, list) and len(records) == 1
    assert records[0].symbol == "600938"
    assert captured["complete"] is True
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "EASTMONEY_FALLBACK_USED" in reasons


def test_corporate_action_official_lookup_runs_even_when_baostock_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _reference_service(tmp_path)
    monkeypatch.setattr(service, "_baostock_circuit_open", lambda **_kwargs: True)
    calls: list[str] = []

    def official(*_args: object):
        calls.append("official")
        return [], ["official:index"], NOW

    monkeypatch.setattr(service, "_official_actions_live", official)
    captured: dict[str, object] = {}

    def release(**kwargs: object):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(service, "_release", release)
    service.sync_corporate_actions(
        "600938",
        Market.XSHG,
        date(2026, 1, 1),
        date(2026, 8, 12),
        live=True,
    )

    assert calls == ["official"]
    assert captured["failed"] is False
    assert captured["provider_id"] == "cninfo-disclosures"
    reasons = captured["reasons"]
    assert isinstance(reasons, list)
    assert "OFFICIAL_INDEX_CAPTURED_NO_MATCH" in reasons


def test_sina_current_dict_shape_preserves_native_scope_currency_and_converts_yuan() -> None:
    payload: dict[str, object] = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_list": {
                    "20251231": {
                        "rType": "合并期末",
                        "rCurrency": "CNY",
                        "data": [
                            {
                                "item_field": "TOTASSET",
                                "item_title": "资产总计",
                                "item_value": "1098559000000.000000",
                            },
                            {
                                "item_field": "TOTLIAB",
                                "item_title": "负债合计",
                                "item_value": "293375000000.000000",
                            },
                        ],
                    }
                }
            },
        }
    }

    rows = _sina_rows(payload, "BALANCE_SHEET")
    normalized = _normalize_sina(rows, "600938", date(2025, 12, 31))

    assert normalized == [
        {
            "report_date": "2025-12-31",
            "statement_scope": "CONSOLIDATED",
            "currency": "CNY",
            "total_assets": "109855900.000000",
            "total_liabilities": "29337500.000000",
            "company_id": "600938",
            "period_end": "2025-12-31",
            "scope": "CONSOLIDATED",
        }
    ]


def test_current_acquisition_freezes_decision_time_after_acquisition_and_keeps_manual_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    times = iter([NOW, NOW + timedelta(seconds=2)])
    service = CurrentResearchAcquisitionService(paths, state, objects, clock=lambda: next(times))

    def fake_reference(capability: AcquisitionCapability, _action: object) -> AcquisitionAttempt:
        return _attempt(capability)

    def fake_financial(
        capability: AcquisitionCapability,
        _company_id: str,
        _market: Market,
        _period_end: date,
        _period_type: object,
        *,
        identity_verified: bool = True,
    ) -> AcquisitionAttempt:
        assert identity_verified
        return _attempt(capability)

    monkeypatch.setattr(service, "_reference_attempt", fake_reference)
    monkeypatch.setattr(service, "_financial_attempt", fake_financial)

    report = service.acquire("600938", Market.XSHG)

    assert report.started_at == NOW
    assert report.decision_as_of == NOW + timedelta(seconds=2)
    assert report.question_time_anchor_used is False
    assert report.decision_snapshot_frozen_after_acquisition is True
    assert report.historical_and_prospective_pit_preserved is True
    assert report.parallel_acquisition_used is True
    assert report.external_research_needs == []
    assert report.manual_actions == []


def test_financial_secondary_hints_continue_even_when_identity_is_not_yet_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    service = CurrentResearchAcquisitionService(paths, state, ObjectStore(paths.objects))

    class FailingEastMoney:
        provider_id = "eastmoney-financial"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fetch(self, *_args: object, **_kwargs: object) -> object:
            raise ValueError("schema drift")

    class WorkingSina:
        provider_id = "sina-financial"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def fetch(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                snapshots=[SimpleNamespace(snapshot_id="snapshot:sina")],
                tables={
                    "BALANCE_SHEET": [{}],
                    "INCOME_STATEMENT": [{}],
                    "CASH_FLOW_STATEMENT": [{}],
                },
            )

    monkeypatch.setattr("astock.research.acquisition.EastMoneyFinancialProvider", FailingEastMoney)
    monkeypatch.setattr("astock.research.acquisition.SinaFinancialProvider", WorkingSina)

    attempt = service._financial_attempt(
        AcquisitionCapability.FINANCIAL_ANNUAL,
        "600938",
        Market.XSHG,
        date(2025, 12, 31),
        object(),  # type: ignore[arg-type]
        identity_verified=False,
    )

    assert attempt.status is AcquisitionAttemptStatus.PARTIAL
    assert attempt.provider_path == ["eastmoney-financial", "sina-financial"]
    assert attempt.fallback_used is True
    assert attempt.record_count == 3
    assert "INSTRUMENT_IDENTITY_UNVERIFIED" in attempt.internal_reason_codes


def test_investor_view_hides_internal_codes_and_execution_gap_by_default() -> None:
    report = ResearchRunReport(
        report_id="report:test",
        run_id="run:test",
        company_id="600938",
        as_of=NOW,
        mode=ResearchRunMode.LIVE,
        request_artifact_id="request:test",
        request_object_hash="1" * 64,
        status=ResearchRunStatus.NEEDS_INFO,
        current_stage=ResearchRunStage.EVIDENCE,
        checkpoints=[],
        output_artifacts={},
        needs_info_codes=[
            "CLAIM_IDS_REQUIRED",
            "EVIDENCE_PACK_REQUIRED",
            "TRADING_CLASSIFICATION_REQUIRED",
        ],
        performance=ResearchRunPerformanceSummary(
            wall_time_ms=1,
            knowledge_top_k_latency_ms=0,
            context_bytes=0,
            estimated_tokens=0,
            estimated_token_limit=0,
            cache_hit_count=0,
            provider_call_count=0,
        ),
        created_at=NOW,
    )

    view = investor_view_from_run(report)
    rendered = view.model_dump_json()

    assert "CLAIM_IDS_REQUIRED" not in rendered
    assert "EVIDENCE_PACK_REQUIRED" not in rendered
    assert "TRADING_CLASSIFICATION_REQUIRED" not in rendered
    assert "关键投资事实" in rendered
    assert view.internal_codes_exposed is False
    assert view.artifact_ids_exposed is False


def test_probe_is_lightweight_and_does_not_run_full_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    def forbidden_integrity(_self: StateStore) -> str:
        raise AssertionError("probe must not run PRAGMA integrity_check")

    monkeypatch.setattr(StateStore, "integrity_check", forbidden_integrity)
    invoked = CliRunner().invoke(app, ["probe"])

    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["state_health"]["status"] == "OK"
    assert payload["state_integrity"] == "NOT_RUN"
    assert payload["full_integrity_check_run"] is False


def test_live_research_no_longer_requires_question_time_as_of_but_recorded_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    runner = CliRunner()

    live = runner.invoke(app, ["research-plan", "600938", "--mode", "LIVE"])
    recorded = runner.invoke(app, ["research-plan", "600938"])

    assert live.exit_code == 0, live.output
    live_payload = json.loads(live.output)
    assert live_payload["company_id"] == "600938"
    assert recorded.exit_code != 0
    assert "--as-of is required for recorded or historical research" in recorded.output
