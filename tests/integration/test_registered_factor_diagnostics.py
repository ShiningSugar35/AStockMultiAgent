"""Recorded native inputs exercise the public factor consumer; no market/OOS claim."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.candidates.discovery_cli import app
from astock.candidates.registered_factor_diagnostics import (
    FactorFreezeRequest,
    FactorMember,
    FactorReportRequest,
    evaluate_registered_factor_panels,
    freeze_factor_panel,
    load_factor_panel,
)
from astock.candidates.registered_features import SupplementalFeatureRequest
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_sources import FinancialSourceParquetStore, FinancialSourceService
from astock.market_data.official_calendar import load_official_trading_calendar
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.research.debate_evaluation import load_quality_policy
from astock.schemas import FetchStatus, SourceSnapshot
from astock.schemas.financial import FinancialIndustryProfile
from astock.schemas.market import Market
from astock.schemas.paper import ReplayFeeSchedule
from astock.schemas.reference_data import (
    CorporateActionObservation,
    CorporateActionStatus,
    DailyBarObservation,
    ReferenceDatasetKind,
)
from tests.unit.test_registered_discovery_features import CAPTURED, valuation_capture

ROOT = Path(__file__).resolve().parents[2]
DAYS = tuple(date(2026, 11, day) for day in range(23, 28))
EVALUATED = datetime(2026, 11, 27, 8, tzinfo=UTC)


def services(tmp_path):
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    parquet = tmp_path / "parquet"
    service = FinancialSourceService(
        state, objects, FinancialSourceParquetStore(parquet / "financial_sources"), ROOT,
    )
    reference = MarketReferenceService(
        state, objects, ReferenceParquetStore(parquet), ROOT / "tests/fixtures/reference",
    )
    return service, reference, parquet


def selection(service, monkeypatch, *, include_missing=False, fees=()):
    members = []
    for company, pb in [("600001", "2"), ("600002", "4")] + (
        [("600003", None)] if include_missing else []
    ):
        capture = valuation_capture(service.state, service.objects, monkeypatch, company, pb=pb)
        members.append(FactorMember(
            company_id=company, market=Market.XSHG, period_end=date(2026, 9, 30),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="recorded-industrial",
            supplemental=SupplementalFeatureRequest(
                market=Market.XSHG, valuation_capture_id=capture.artifact_id,
            ),
        ))
    return FactorFreezeRequest(
        panel_id="recorded-panel", population_id="recorded-population",
        factor_id="VENDOR_PB", formula_version="discovery-observations-v1",
        feature_as_of=CAPTURED, benchmark_id="INDEX:000300", holding_sessions=5,
        quantiles=2, members=tuple(members), fee_artifact_ids=fees,
    )


def raw(service, value, stamp):
    ref = service.objects.put_json(value)
    snapshot = SourceSnapshot(
        snapshot_id="recorded-factor:" + ref.sha256, source_id="recorded-factor",
        object_sha256=ref.sha256, byte_size=ref.byte_size, fetched_at=stamp,
        available_to_system_at=stamp, created_at=stamp, fetch_status=FetchStatus.SUCCEEDED,
        mime="application/json",
    )
    service.state.register_snapshot(snapshot)
    return snapshot.snapshot_id


def outcomes(service, reference, request, *, action_mode="complete"):
    """Full contract fixture whose raw data is explicitly synthetic."""
    identities = [(item.market, item.company_id) for item in request.members]
    identities.append((Market.INDEX, "000300"))
    for ordinal, (market, code) in enumerate(identities):
        close = Decimal("100") if market is Market.INDEX else Decimal("11") - 2 * ordinal
        opening = Decimal("100") if market is Market.INDEX else Decimal("10")
        snapshot = raw(service, {"synthetic": "daily", "symbol": code}, EVALUATED)
        rows = [
            DailyBarObservation(
                observation_id=content_hash({"symbol": code, "day": day.isoformat()}),
                instrument_id=f"{market.value}:{code}", market=market, symbol=code,
                session_date=day,
                session_close_at=datetime(day.year, day.month, day.day, 7, tzinfo=UTC),
                open=opening, high=max(opening, close), low=min(opening, close),
                close=close, previous_close=opening, volume=Decimal(100000),
                source_snapshot_id=snapshot, available_to_system_at=EVALUATED, created_at=EVALUATED,
            ) for day in DAYS
        ]
        reference._release(
            command="recorded-factor-proof", dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
            scope_key=f"{market.value}:{code}", provider_id="recorded-factor",
            raw_snapshot_ids=[snapshot], records=rows, requested_start=DAYS[0],
            requested_end=DAYS[-1], available_at=EVALUATED, complete=True, reasons=[],
        )
        if market is Market.INDEX or action_mode == "unknown":
            continue
        action_raw = raw(service, {"synthetic": "prior official action", "symbol": code}, EVALUATED)
        event = CorporateActionObservation(
            observation_id=content_hash({"action": "prior", "symbol": code}),
            instrument_id=f"{market.value}:{code}", market=market, symbol=code,
            action_type="CASH_DIVIDEND", announcement_date=date(2026, 5, 1),
            ex_date=DAYS[2] if action_mode == "in_window" else date(2026, 5, 20),
            status=CorporateActionStatus.TERMS_VERIFIED, structured_terms={"cash_per_share": "1"},
            official_document_snapshot_id=action_raw,
            official_document_url="https://example.invalid/recorded-action",
            official_announcement_id="recorded-action:" + code,
            source_snapshot_id=action_raw, available_to_system_at=EVALUATED, created_at=EVALUATED,
        )
        reference._release(
            command="recorded-factor-proof", dataset_kind=ReferenceDatasetKind.CORPORATE_ACTION,
            scope_key=f"{market.value}:{code}", provider_id="recorded-factor",
            raw_snapshot_ids=[action_raw], records=[event], requested_start=date(2026, 5, 1),
            requested_end=DAYS[-1], available_at=EVALUATED,
            complete=action_mode != "partial", reasons=[],
        )


def freeze(service, request):
    return freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: CAPTURED,
    )


def report(service, parquet, identity, *, at=EVALUATED):
    return evaluate_registered_factor_panels(
        service.state, service.objects, parquet,
        load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml"),
        load_quality_policy(ROOT / "configs/research_quality.yaml"),
        FactorReportRequest(panel_artifact_ids=(identity,), evaluated_at=at),
    )


def test_actual_feature_capture_canonical_report_and_missing_values_are_read_only(
    tmp_path, monkeypatch,
):
    service, reference, parquet = services(tmp_path)
    request = selection(service, monkeypatch, include_missing=True)
    frozen = freeze(service, request)
    outcomes(service, reference, request)
    with closing(service.state.connect()) as connection:
        before = connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0]
    result = report(service, parquet, frozen["artifact_id"])
    assert result["frozen_input_count"] == 3
    assert result["evaluated_count"] == 2 and result["excluded"]["MISSING_FEATURE"] == 1
    assert result["mean_date_rank_ic"] == pytest.approx(-1)
    assert result["cost_basis"] == "UNKNOWN"
    assert all(row["net_return"] is None for row in result["dates"][0]["quantiles"])
    assert all(item["lineage"]["corporate_actions"]["object_hash"] for item in result["members"])
    assert not result["persistent_writes"] and not result["market_validation_claim"]
    assert not result["experiment_promotion_allowed"]
    assert not result["blind_tranche_mutation_allowed"]
    with closing(service.state.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == before


def test_registered_effective_fee_model_uses_one_buy_and_sell_on_declared_lot(
    tmp_path, monkeypatch,
):
    service, reference, parquet = services(tmp_path)
    fees = ReplayFeeSchedule(
        rule_version="recorded-factor-fees", effective_from=date(2020, 1, 1),
        applicable_markets=[Market.XSHG], commission_rate=Decimal(".0003"),
        minimum_commission_fen=500, stamp_tax_sell_rate=Decimal(".0005"),
        transfer_fee_rate=Decimal(".00001"), source_urls=["https://example.invalid/fee"],
        created_at=CAPTURED - timedelta(days=1),
    )
    value = service.objects.put_json(fees.model_dump(mode="json"))
    identity = "ReplayFeeSchedule:factor-recorded"
    service.state.register_artifact(
        artifact_id=identity, artifact_type="ReplayFeeSchedule", schema_version=fees.schema_version,
        object_hash=value.sha256, input_hashes=[],
    )
    request = selection(service, monkeypatch, fees=(identity,))
    frozen = freeze(service, request)
    outcomes(service, reference, request)
    result = report(service, parquet, frozen["artifact_id"])
    low_pb = result["dates"][0]["quantiles"][0]
    assert low_pb["gross_return"] == pytest.approx(.1)
    assert low_pb["net_return"] == pytest.approx((110000 - 556) / (100000 + 501) - 1)
    assert "NO_FILL_OR_SLIPPAGE_MODEL" in result["cost_basis"]


@pytest.mark.parametrize("mode", ["unknown", "partial", "in_window"])
def test_unqualified_action_coverage_never_enters_factor_ic(tmp_path, monkeypatch, mode):
    service, reference, parquet = services(tmp_path)
    request = selection(service, monkeypatch)
    frozen = freeze(service, request)
    outcomes(service, reference, request, action_mode=mode)
    result = report(service, parquet, frozen["artifact_id"])
    assert result["status"] == "NOT_EVALUABLE" and result["evaluated_count"] == 0
    assert result["unavailable_outcomes"] == {"PRICE_ONLY_UNQUALIFIED": 2}
    assert result["mean_date_rank_ic"] is None


def test_signal_is_prospective_and_same_panel_cannot_be_reselected(tmp_path, monkeypatch):
    service, _, parquet = services(tmp_path)
    request = selection(service, monkeypatch)
    frozen = freeze(service, request)
    replay = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: CAPTURED + timedelta(days=20),
    )
    assert replay["idempotent_replay"] and replay["object_hash"] == frozen["object_hash"]
    assert replay["signal_at"] == frozen["signal_at"]
    with pytest.raises(ValueError, match="another selection"):
        freeze(service, request.model_copy(update={"population_id": "changed"}))
    future = request.model_copy(update={
        "panel_id": "future", "feature_as_of": CAPTURED + timedelta(days=1),
    })
    with pytest.raises(ValueError, match="cutoff"):
        freeze(service, future)
    result = report(service, parquet, frozen["artifact_id"], at=CAPTURED + timedelta(days=1))
    assert result["unavailable_outcomes"] == {"IMMATURE": 2}


def test_frozen_source_corruption_is_not_hidden_by_cached_feature_value(tmp_path, monkeypatch):
    service, _, _ = services(tmp_path)
    frozen = freeze(service, selection(service, monkeypatch))
    panel, _ = load_factor_panel(service.state, service.objects, frozen["artifact_id"])
    assert panel.source_hashes
    service.objects.path_for(panel.source_hashes[0]).write_bytes(b"corrupt recorded source")
    with pytest.raises(ValueError, match="source object"):
        load_factor_panel(service.state, service.objects, frozen["artifact_id"])


def test_public_cli_schema_and_uninitialized_report_do_not_create_state(tmp_path, monkeypatch):
    schema = CliRunner().invoke(app, ["schema"])
    assert schema.exit_code == 0
    assert {"factor_freeze", "factor_report"} <= json.loads(schema.stdout).keys()
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(ROOT))
    runtime = tmp_path / "uninitialized"
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    request_file = tmp_path / "report.json"
    request_file.write_text(FactorReportRequest(
        panel_artifact_ids=("DiscoveryFactorPanel:missing",), evaluated_at=EVALUATED,
    ).model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(app, ["factor-report", str(request_file)])
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "NOT_READY"
    assert not runtime.exists()


def test_duplicate_daily_panels_and_unregistered_json_do_not_inflate_sample(tmp_path, monkeypatch):
    service, _, parquet = services(tmp_path)
    request = selection(service, monkeypatch)
    a = freeze(service, request)
    b = freeze(service, request.model_copy(update={"panel_id": "second-name"}))
    with pytest.raises(ValueError, match="one daily"):
        evaluate_registered_factor_panels(
            service.state, service.objects, parquet,
            load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml"),
            load_quality_policy(ROOT / "configs/research_quality.yaml"),
            FactorReportRequest(panel_artifact_ids=(a["artifact_id"], b["artifact_id"]),
                                evaluated_at=EVALUATED),
        )
    with pytest.raises(ValueError, match="type mismatch"):
        report(service, parquet, "caller-supplied-good-results")


def test_public_freeze_and_report_commands_use_registered_readers(tmp_path, monkeypatch):
    import astock.candidates.discovery_cli as cli
    import astock.candidates.registered_factor_diagnostics as factors
    from astock.settings import ProjectPaths

    service, reference, parquet = services(tmp_path)
    request = selection(service, monkeypatch)
    paths = ProjectPaths(
        ROOT, tmp_path, service.objects.root, parquet, tmp_path / "manifests", service.state.path,
    )
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(ROOT))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(CAPTURED.timestamp(), tz)

    monkeypatch.setattr(factors, "datetime", Clock)
    flags = []

    def factory(*, read_only=False):
        flags.append(read_only)
        return (
            paths, StateStore(service.state.path, ROOT / "migrations", read_only=read_only),
            service.objects,
        )

    monkeypatch.setattr(cli, "_services", factory)
    request_file = tmp_path / "freeze.json"
    request_file.write_text(request.model_dump_json(), encoding="utf-8")
    frozen = CliRunner().invoke(app, ["factor-freeze", str(request_file)])
    assert frozen.exit_code == 0, frozen.stdout
    identity = json.loads(frozen.stdout)["artifact_id"]
    outcomes(service, reference, request)
    request_file.write_text(FactorReportRequest(
        panel_artifact_ids=(identity,), evaluated_at=EVALUATED,
    ).model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(app, ["factor-report", str(request_file)])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["mean_date_rank_ic"] == pytest.approx(-1)
    assert flags == [False, True]


@pytest.mark.parametrize(
    "corrupt_dependency", [None, "official_original", "provider_raw", "official_admission",
                          "instrument_manifest"],
)
def test_certified_financial_dependency_uses_byte_hash_not_semantic_hash(
    tmp_path, monkeypatch, corrupt_dependency,
):
    from astock.candidates.registered_features import build_registered_features
    from astock.schemas.financial import FinancialPeriodType
    from tests.integration.test_financial_sources import (
        PERIOD_END,
        _capture_existing_annual,
        _install_tushare_financial_mock,
        _service,
    )

    service = _service(tmp_path)
    capture = _capture_existing_annual(service)
    client, calls, _ = _install_tushare_financial_mock(service, monkeypatch)
    monkeypatch.setattr(service.official, "_live_candidates", lambda *args, **kwargs: [])
    try:
        release = service.sync(
            "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL, live=True,
        )
    finally:
        client.close()
    assert release.release_id is not None
    assert calls == ["balancesheet", "income", "cashflow"]
    row = service.repository.get("000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value)
    assert row is not None
    manifest = service._verified_manifest(row)
    boundary = manifest.available_to_system_at + timedelta(seconds=1)
    packet = build_registered_features(
        service, company_id="000001", period_end=PERIOD_END, as_of=boundary,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL, business_group_id="fixture",
    )
    assert len(packet["releases"]) == 1
    request = FactorFreezeRequest(
        panel_id="actual-financial-parent", population_id="fixture", factor_id="REVENUE_TTM",
        formula_version=packet["formula_version"], feature_as_of=boundary,
        benchmark_id="INDEX:000300", holding_sessions=5,
        members=(FactorMember(
            company_id="000001", market=Market.XSHE, period_end=PERIOD_END,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="fixture",
        ),),
    )
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: boundary,
    )
    panel, _ = load_factor_panel(service.state, service.objects, frozen["artifact_id"])
    assert str(row["manifest_object_hash"]) in panel.source_hashes
    assert panel.members[0].value is not None
    assert all(service.objects.verify(item) for item in panel.source_hashes)
    snapshots = {
        "official_original": service.state.get_snapshot(capture.snapshot_id),
        "official_admission": service.state.get_snapshot(capture.admission_snapshot_id),
        "provider_raw": service.state.get_snapshot(manifest.raw_snapshot_ids[0]),
    }
    assert all(snapshot is not None for snapshot in snapshots.values())
    dependencies = {
        name: snapshot.object_sha256 for name, snapshot in snapshots.items()
        if snapshot is not None
    } | {"instrument_manifest": manifest.instrument_manifest_object_hash}
    assert set(dependencies.values()) <= set(panel.source_hashes)
    if corrupt_dependency is not None:
        service.objects.path_for(dependencies[corrupt_dependency]).write_bytes(b"corrupted parent")
        assert service.objects.verify(str(row["manifest_object_hash"]))
        with pytest.raises(ValueError, match="source object"):
            load_factor_panel(service.state, service.objects, frozen["artifact_id"])


def test_forecast_factor_freeze_keeps_underlying_response_bytes(tmp_path):
    from tests.unit.test_sampled_forecasts import capture
    from tests.unit.test_sampled_forecasts import report as forecast_report

    service, _, _ = services(tmp_path)
    forecast, calls = capture(service.objects, service.state, [
        {**forecast_report("FY26", "A", "2"), "predictNextYearEps": "3"},
    ])
    assert len(calls) == 1
    boundary = forecast.available_to_system_at + timedelta(seconds=1)
    request = FactorFreezeRequest(
        panel_id="forecast-parent", population_id="recorded-population",
        factor_id="SAMPLED_EPS_NEXT_YEAR_GROWTH", formula_version="discovery-observations-v1",
        feature_as_of=boundary, benchmark_id="INDEX:000300", holding_sessions=5,
        members=(FactorMember(
            company_id="600519", market=Market.XSHG, period_end=date(2026, 9, 30),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="recorded-industrial",
            supplemental=SupplementalFeatureRequest(
                market=Market.XSHG, forecast_capture_ids=(forecast.capture_id,),
                forecast_fiscal_year=2026,
            ),
        ),),
    )
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: boundary,
    )
    panel, _ = load_factor_panel(service.state, service.objects, frozen["artifact_id"])
    assert panel.members[0].value == Decimal(".5")
    snapshot = service.state.get_snapshot(forecast.source_snapshot_ids[0])
    assert snapshot is not None and snapshot.object_sha256 in panel.source_hashes
    service.objects.path_for(snapshot.object_sha256).write_bytes(b"corrupt forecast response")
    record = service.state.artifact_record(forecast.capture_id)
    assert record is not None and service.objects.verify(record["object_hash"])
    with pytest.raises(ValueError, match="source object"):
        load_factor_panel(service.state, service.objects, frozen["artifact_id"])


def test_current_queue_excludes_superseded_financial_member_but_preserves_frozen_history(
    tmp_path, monkeypatch,
):
    from astock.candidates.registered_factor_diagnostics import load_current_factor_members
    from astock.candidates.registered_features import build_registered_features
    from astock.financial_sources.repository import _release_identity
    from astock.schemas.financial import FinancialPeriodType
    from tests.integration.test_financial_sources import (
        PERIOD_END,
        _capture_existing_annual,
        _install_tushare_financial_mock,
        _service,
    )

    service = _service(tmp_path)
    _capture_existing_annual(service)
    client, _, _ = _install_tushare_financial_mock(service, monkeypatch)
    monkeypatch.setattr(service.official, "_live_candidates", lambda *args, **kwargs: [])
    try:
        release = service.sync(
            "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL, live=True,
        )
    finally:
        client.close()
    assert release.release_id
    row = service.repository.get("000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value)
    assert row is not None
    manifest = service._verified_manifest(row)
    boundary = manifest.available_to_system_at + timedelta(seconds=1)
    packet = build_registered_features(
        service, company_id="000001", period_end=PERIOD_END, as_of=boundary,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL, business_group_id="fixture",
    )
    request = FactorFreezeRequest(
        panel_id="financial-current-head", population_id="recorded", factor_id="REVENUE_TTM",
        formula_version=packet["formula_version"], feature_as_of=boundary,
        benchmark_id="INDEX:000300", holding_sessions=5,
        members=(FactorMember(
            company_id="000001", market=Market.XSHE, period_end=PERIOD_END,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="fixture",
        ),),
    )
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: boundary,
    )
    panel, digest, members, excluded = load_current_factor_members(
        service.state, service.objects, frozen["artifact_id"], as_of=boundary, max_age_hours=24,
    )
    assert len(members) == 1 and excluded == ()
    selected_facts = set(members[0].feature["source_fact_ids"])
    assert all(selected_facts.intersection(item["fact_ids"])
               for item in members[0].financial_releases)
    later = boundary + timedelta(minutes=1)
    # A real registry revision with verified existing bytes; no rewrite of the frozen panel.
    revised = manifest.model_copy(update={
        "previous_release_id": manifest.release_id, "supersedes_release_id": manifest.release_id,
        "available_to_system_at": later,
    })
    revised = revised.model_copy(update={"release_id": content_hash(_release_identity(revised))})
    revision = service.objects.put_json(revised.model_dump(mode="json"))
    service.repository.publish(revised, revision.sha256)
    _, _, eligible, excluded = load_current_factor_members(
        service.state, service.objects, frozen["artifact_id"], as_of=later, max_age_hours=24,
    )
    assert eligible == () and excluded == ("XSHE:000001",)
    historical, old_digest, old_eligible, _ = load_current_factor_members(
        service.state, service.objects, frozen["artifact_id"], as_of=boundary, max_age_hours=24,
    )
    assert historical == panel and old_digest == digest and old_eligible == members


@pytest.mark.parametrize("stale_boundary", ["feature_as_of", "current_capture"])
def test_current_priority_cannot_refresh_old_valuation_with_new_freeze(
    tmp_path, monkeypatch, stale_boundary,
):
    from astock.candidates.registered_factor_diagnostics import load_current_factor_members

    service, _, _ = services(tmp_path)
    request = selection(service, monkeypatch)
    current = CAPTURED + timedelta(days=2)
    if stale_boundary == "current_capture":
        request = request.model_copy(update={"feature_as_of": current})
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: current,
    )
    # A newly frozen wrapper is valid historical evidence, but cannot refresh
    # the earlier feature cutoff or the actual registered daily_basic capture.
    panel, _ = load_factor_panel(service.state, service.objects, frozen["artifact_id"])
    assert panel.signal_at == current
    assert all(item.value is not None for item in panel.members)
    with pytest.raises(ValueError, match="freshness"):
        load_current_factor_members(
            service.state, service.objects, frozen["artifact_id"],
            as_of=current, max_age_hours=24,
        )


def test_current_forecast_revision_keeps_older_baseline_without_refreshing_current_capture(
    tmp_path, monkeypatch,
):
    import astock.providers.forecasts as forecasts
    from astock.candidates.registered_factor_diagnostics import load_current_factor_members
    from tests.unit.test_sampled_forecasts import capture
    from tests.unit.test_sampled_forecasts import report as forecast_report

    service, _, _ = services(tmp_path)
    old_time = datetime(2026, 11, 20, 10, tzinfo=UTC)
    current_time = old_time + timedelta(days=3)

    class Clock(datetime):
        value = old_time

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.value.timestamp(), tz or UTC)

    monkeypatch.setattr(forecasts, "datetime", Clock)
    prior, _ = capture(service.objects, service.state, [forecast_report("OLD", "A", "2")])
    Clock.value = current_time
    latest, _ = capture(service.objects, service.state, [
        forecast_report("NEW", "A", "3", "2026-11-03"),
    ])
    boundary = current_time + timedelta(seconds=1)
    request = FactorFreezeRequest(
        panel_id="current-forecast-revision", population_id="recorded",
        factor_id="SAMPLED_EPS_OBSERVED_REVISION", formula_version="discovery-observations-v1",
        feature_as_of=boundary, benchmark_id="INDEX:000300", holding_sessions=5,
        members=(FactorMember(
            company_id="600519", market=Market.XSHG, period_end=date(2026, 9, 30),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="recorded",
            supplemental=SupplementalFeatureRequest(
                market=Market.XSHG, forecast_capture_ids=(latest.capture_id,),
                prior_forecast_capture_ids=(prior.capture_id,), forecast_fiscal_year=2026,
                prior_forecast_as_of=prior.available_to_system_at,
            ),
        ),),
    )
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: boundary,
    )
    _, _, current, excluded = load_current_factor_members(
        service.state, service.objects, frozen["artifact_id"], as_of=boundary, max_age_hours=24,
    )
    assert excluded == () and current[0].value == Decimal(".5")
    assert current[0].feature["inputs"]["prior_captures"][0]["available_to_system_at"]


def _technical_priority_panel(service, reference, *, latest: date, observed: datetime):
    calendar = load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml")
    sessions = calendar.open_dates(Market.XSHG, latest - timedelta(days=80), latest)
    assert sessions is not None
    dates = sessions[-40:]
    snapshot = raw(service, {"technical-latest": latest.isoformat()}, observed)
    rows = []
    for index, day in enumerate(dates):
        price = Decimal(10) + Decimal(index) / 10
        rows.append(DailyBarObservation(
            observation_id=content_hash({"technical": day.isoformat(), "last": latest.isoformat()}),
            instrument_id="XSHG:600001", market=Market.XSHG, symbol="600001",
            session_date=day,
            session_close_at=datetime(day.year, day.month, day.day, 7, tzinfo=UTC),
            open=price, high=price + Decimal(".1"), low=price - Decimal(".1"), close=price,
            previous_close=price - Decimal(".1"), volume=Decimal(100000),
            source_snapshot_id=snapshot, available_to_system_at=observed, created_at=observed,
        ))
    release = reference._release(
        command="recorded-current-technical", dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key="XSHG:600001", provider_id="recorded-factor", raw_snapshot_ids=[snapshot],
        records=rows, requested_start=dates[0], requested_end=dates[-1],
        available_at=observed, complete=True, reasons=[],
    )
    assert release.release_id is not None
    request = FactorFreezeRequest(
        panel_id="technical-current", population_id="recorded", factor_id="TECHNICAL_EMA_TREND",
        formula_version="discovery-observations-v1", feature_as_of=observed,
        benchmark_id="INDEX:000300", holding_sessions=5,
        members=(FactorMember(
            company_id="600001", market=Market.XSHG, period_end=date(2026, 9, 30),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="recorded",
            supplemental=SupplementalFeatureRequest(
                market=Market.XSHG, technical_daily_release_id=release.release_id,
            ),
        ),),
    )
    frozen = freeze_factor_panel(
        service, request, load_quality_policy(ROOT / "configs/research_quality.yaml"),
        clock=lambda: observed,
    )
    return frozen["artifact_id"], calendar


@pytest.mark.parametrize("latest,calendar_available,eligible", [
    (date(2026, 11, 20), True, True),
    (date(2026, 7, 31), True, False),
    (date(2026, 11, 20), False, False),
])
def test_technical_currentness_uses_real_sessions_not_new_capture_time(
    tmp_path, latest, calendar_available, eligible,
):
    from astock.candidates.registered_factor_diagnostics import load_current_factor_members

    service, reference, _ = services(tmp_path)
    sunday = datetime(2026, 11, 22, 6, tzinfo=UTC)
    if latest == date(2026, 7, 31):
        # The actual producer rejects stale coverage before a panel can be frozen.
        with pytest.raises(
            ValueError, match="technical daily-bar coverage mismatches official session dates",
        ):
            _technical_priority_panel(service, reference, latest=latest, observed=sunday)
        return
    identity, calendar = _technical_priority_panel(
        service, reference, latest=latest, observed=sunday,
    )
    panel, _ = load_factor_panel(service.state, service.objects, identity)
    assert panel.members[0].value is not None
    assert panel.members[0].feature["inputs"]["as_of"] == sunday.isoformat()
    selected_calendar = calendar if calendar_available else None
    if eligible:
        _, _, members, _ = load_current_factor_members(
            service.state, service.objects, identity, as_of=sunday, max_age_hours=24,
            calendar=selected_calendar,
        )
        assert len(members) == 1
    else:
        with pytest.raises(ValueError, match="freshness|calendar"):
            load_current_factor_members(
                service.state, service.objects, identity, as_of=sunday, max_age_hours=24,
                calendar=selected_calendar,
            )
