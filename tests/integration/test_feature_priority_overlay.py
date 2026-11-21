"""Actual factor capture -> existing research queue, with immutable source lineage."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from astock.candidates.cli_ext import register_candidate_input_commands
from astock.candidates.registered_factor_diagnostics import (
    FactorFreezeRequest,
    FactorMember,
    freeze_factor_panel,
    load_factor_panel,
)
from astock.candidates.registered_features import (
    SupplementalFeatureRequest,
    build_registered_features,
)
from astock.candidates.seeds import ResearchSeedService
from astock.core.hashing import content_hash
from astock.market_data.official_calendar import load_official_trading_calendar
from astock.research.debate_evaluation import load_quality_policy
from astock.schemas.financial import FinancialIndustryProfile
from astock.schemas.market import Market
from astock.schemas.reference_data import DailyBarObservation, ReferenceDatasetKind
from astock.schemas.research_seeds import FactorPriorityOverlay, ResearchSeedRequest
from tests.integration.test_registered_factor_diagnostics import ROOT, raw, services
from tests.unit.test_registered_discovery_features import CAPTURED, valuation_capture
from tests.unit.test_research_seeds import _FakeSeedProvider


def setup_queue(tmp_path, monkeypatch):
    service, reference, parquet = services(tmp_path)
    provider = _FakeSeedProvider(service.state, service.objects)
    provider.market_payloads = {
        market: provider._market_payload(market, [])
        for market in (Market.XSHG, Market.XSHE, Market.BJSE)
    }
    provider.market_payloads[Market.XSHG] = provider._market_payload(Market.XSHG, [
        ("600001", "记录甲", 10.0, 900_000_000.0, 3.0, 90_000_000_000.0, 0.0),
        ("600002", "记录乙", 10.0, 300_000_000.0, 1.0, 30_000_000_000.0, 0.0),
        ("600003", "记录丙", 10.0, 300_000_000.0, 1.0, 30_000_000_000.0, 0.0),
    ])
    data = provider.market_payloads[Market.XSHG]["data"]
    assert isinstance(data, dict) and isinstance(data["diff"], list)
    for row in data["diff"]:
        assert isinstance(row, dict)
        row.update(f9=10, f23=1)
    members = []
    for company, pb in (("600001", "2"), ("600002", "4"), ("600003", "1")):
        capture = valuation_capture(service.state, service.objects, monkeypatch, company, pb=pb)
        members.append(FactorMember(
            company_id=company, market=Market.XSHG, period_end=date(2026, 9, 30),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="recorded-comparable-group",
            supplemental=SupplementalFeatureRequest(
                market=Market.XSHG, valuation_capture_id=capture.artifact_id,
            ),
        ))
    frozen = freeze_factor_panel(
        service, FactorFreezeRequest(
            panel_id="queue-panel", population_id="recorded-priority-group", factor_id="VENDOR_PB",
            formula_version="discovery-observations-v1", feature_as_of=CAPTURED,
            benchmark_id="INDEX:000300", holding_sessions=5, members=tuple(members),
        ), load_quality_policy(ROOT / "configs/research_quality.yaml"), clock=lambda: CAPTURED,
    )
    queue = ResearchSeedService(
        project_root=ROOT, state=service.state, objects=service.objects, provider=provider,
    )
    request = ResearchSeedRequest(
        as_of=CAPTURED + timedelta(seconds=1), max_total_seeds=5, max_market_seeds=1,
        max_long_horizon_value_seeds=3, max_breadth_challenger_seeds=0,
        max_expert_seeds_per_author=0, include_existing_candidates=False, market_fetch_workers=1,
        created_at=CAPTURED,
    )
    overlay = FactorPriorityOverlay(
        panel_artifact_id=frozen["artifact_id"], direction="LOW_FIRST",
        ranking_hypothesis="Recorded comparable PB only prioritizes further research",
        maximum_priority_bonus=.1,
    )
    return service, reference, parquet, provider, queue, request, overlay


def test_registered_panel_reorders_actual_queue_without_changing_blind_slots_or_votes(
    tmp_path, monkeypatch,
):
    service, _, _, _, queue, request, overlay = setup_queue(tmp_path, monkeypatch)
    baseline = queue.generate(request)
    report = queue.generate(request.model_copy(update={"feature_overlay": overlay}))
    assert [seed.company_id for seed in baseline.seeds] == ["600001", "600002", "600003"]
    assert [seed.company_id for seed in report.seeds] == ["600001", "600003", "600002"]
    assert report.seeds[0] == baseline.seeds[0]
    assert report.market_seed_count == baseline.market_seed_count == 1
    assert report.long_horizon_value_seed_count == baseline.long_horizon_value_seed_count
    assert {seed.company_id for seed in report.seeds} == {
        seed.company_id for seed in baseline.seeds
    }
    before = {seed.company_id: seed for seed in baseline.seeds}
    for seed in report.seeds[1:]:
        assert seed.feature_priority and seed.feature_priority.independent_vote_weight == 0
        assert seed.feature_priority.comparable_count == 3
        assert seed.origins == before[seed.company_id].origins
        assert seed.source_snapshot_ids == before[seed.company_id].source_snapshot_ids
        assert not seed.recommendation_allowed and not seed.paper_ledger_write_allowed
    assert queue.audit("ResearchSeedReport:" + report.report_id)["status"] == "PASS"
    replay = queue.generate(request.model_copy(update={"feature_overlay": overlay}))
    assert replay == report
    panel, digest = load_factor_panel(service.state, service.objects, overlay.panel_artifact_id)
    assert {digest, *panel.source_hashes} <= set(report.source_object_hashes)


@pytest.mark.parametrize("failure", ["future", "expired", "corrupt"])
def test_unusable_optional_factor_keeps_existing_queue_alive(tmp_path, monkeypatch, failure):
    service, _, _, _, queue, request, overlay = setup_queue(tmp_path, monkeypatch)
    if failure == "future":
        request = request.model_copy(update={"as_of": CAPTURED - timedelta(seconds=1)})
    elif failure == "expired":
        request = request.model_copy(update={"as_of": CAPTURED + timedelta(days=2)})
    else:
        panel, _ = load_factor_panel(service.state, service.objects, overlay.panel_artifact_id)
        service.objects.path_for(panel.source_hashes[0]).write_bytes(b"corrupted dependency")
    baseline = queue.generate(request)
    report = queue.generate(request.model_copy(update={"feature_overlay": overlay}))
    assert report.seeds == baseline.seeds
    assert "FEATURE_PRIORITY_INPUT_UNAVAILABLE_BASELINE_PRESERVED" in report.warning_codes
    assert all(seed.feature_priority is None for seed in report.seeds)


def test_rank_tampering_fails_existing_report_audit(tmp_path, monkeypatch):
    _, _, _, _, queue, request, overlay = setup_queue(tmp_path, monkeypatch)
    report = queue.generate(request.model_copy(update={"feature_overlay": overlay}))
    changed = report.seeds[1].model_copy(update={"research_priority_score": .99})
    forged = report.model_copy(update={
        "report_id": "research-seeds:recorded-tamper",
        "seeds": [report.seeds[0], changed, *report.seeds[2:]],
    })
    queue._persist(forged)
    result = queue.audit("ResearchSeedReport:" + forged.report_id)
    assert result["status"] == "FAIL"
    codes = result["finding_codes"]
    assert isinstance(codes, list)
    assert "RESEARCH_SEED_FEATURE_PRIORITY_LINEAGE_DRIFT" in codes


def test_existing_public_seed_command_consumes_explicit_factor_specification(
    tmp_path, monkeypatch,
):
    import astock.candidates.cli_ext as cli

    service, _, parquet, provider, _, request, overlay = setup_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "MarketReferenceService", lambda *a, **k: SimpleNamespace(
        provider_registry=SimpleNamespace(providers=[]),
        config=SimpleNamespace(minimum_instrument_records={}),
    ))
    monkeypatch.setattr(cli, "ResearchSeedProviderRouter", lambda **kw: provider)
    app = typer.Typer()
    register_candidate_input_commands(
        app, lambda: (SimpleNamespace(root=ROOT, parquet=parquet), service.state, service.objects),
        lambda value: typer.echo(value.model_dump_json() if hasattr(value, "model_dump_json")
                                else json.dumps(value)),
    )
    source = tmp_path / "feature-overlay.json"
    source.write_text(overlay.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "research-seeds", "--as-of", request.as_of.isoformat(), "--max-market-seeds", "1",
        "--max-total-seeds", "5", "--max-long-horizon-value-seeds", "3",
        "--feature-overlay", str(source),
    ])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert any(seed["feature_priority"] for seed in report["seeds"])
    blind = next(seed for seed in report["seeds"] if "MARKET" in seed["origins"])
    assert blind["feature_priority"] is None
    source.write_bytes(b"x" * 8193)
    failed = CliRunner().invoke(app, ["research-seeds", "--feature-overlay", str(source)])
    assert failed.exit_code != 0 and "8192" in failed.output


def _daily_technical_source(service, reference, count):
    calendar = load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml")
    dates = calendar.open_dates(
        Market.XSHG, CAPTURED.date() - timedelta(days=120), CAPTURED.date(),
    )
    assert dates is not None
    dates = dates[-count:]
    snapshot = raw(service, {"recorded_technical": count}, CAPTURED)
    rows = []
    for index, day in enumerate(dates):
        close = Decimal(10) + Decimal(index) / 10
        rows.append(DailyBarObservation(
            observation_id=content_hash({"technical_fixture": index, "date": day.isoformat()}),
            instrument_id="XSHG:600001", market=Market.XSHG, symbol="600001",
            session_date=day, session_close_at=datetime.combine(day, datetime.min.time(),
                                                               tzinfo=CAPTURED.tzinfo)
            + timedelta(hours=7),
            open=close, high=close + Decimal(".1"), low=close - Decimal(".1"), close=close,
            previous_close=close - Decimal(".1"), volume=Decimal(100000),
            source_snapshot_id=snapshot, available_to_system_at=CAPTURED, created_at=CAPTURED,
        ))
    released = reference._release(
        command="recorded-technical-factor", dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key="XSHG:600001", provider_id="recorded-factor", raw_snapshot_ids=[snapshot],
        records=rows, requested_start=dates[0], requested_end=dates[-1],
        available_at=CAPTURED, complete=True, reasons=[],
    )
    return released.release_id


@pytest.mark.parametrize("count", [3, 40])
def test_technical_features_reuse_canonical_prices_and_keep_missing_history_unknown(
    tmp_path, count,
):
    service, reference, _ = services(tmp_path)
    release_id = _daily_technical_source(service, reference, count)
    with closing(service.state.connect()) as connection:
        before = connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0]
    supplemental = SupplementalFeatureRequest(
        market=Market.XSHG, technical_daily_release_id=release_id,
    )
    kwargs: dict[str, Any] = dict(
        company_id="600001", period_end=date(2026, 6, 30), as_of=CAPTURED,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        business_group_id="recorded-comparable", supplemental=supplemental,
    )
    packet = build_registered_features(service, **kwargs)
    rows = {row["metric_id"]: row for row in packet["features"]}
    trend = rows["TECHNICAL_EMA_TREND"]
    spread = rows["TECHNICAL_ATR_PRICE_RANGE"]
    if count == 3:
        assert trend["value"] is None and spread["value"] is None
    else:
        assert Decimal(trend["value"]) > 0
        assert 0 < Decimal(spread["value"]) < 1
        assert spread["inputs"]["price_range_not_annualized_return_volatility"]
        assert spread["inputs"]["corporate_action_coverage"] == "UNPROVEN"
    assert trend["inputs"]["source_object_hash"]
    assert trend["independent_vote_weight"] == 0
    with closing(service.state.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == before
    kwargs["company_id"] = "600002"
    with pytest.raises(ValueError, match="security"):
        build_registered_features(service, **kwargs)
    kwargs.update(company_id="600001", as_of=CAPTURED - timedelta(seconds=1))
    with pytest.raises(ValueError, match="unavailable"):
        build_registered_features(service, **kwargs)
