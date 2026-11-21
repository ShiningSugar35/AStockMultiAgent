from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from astock.financial_sources.certification import financial_amount_cny
from astock.investor_orchestration.macro import (
    ImmutableRawObjectStore,
    MacroReleaseSpec,
    OfficialMacroCaptureService,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.providers.tushare_reference import (
    TushareReferenceProvider,
    load_metric_capture,
    normalize_daily,
    normalize_instruments,
    normalize_tushare_metrics,
)
from astock.providers.tushare_structured import DEFAULT_FIELDS
from astock.schemas import FinancialUnit, Market, SourceSnapshot

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 21, 7, tzinfo=UTC)


def snapshot():
    return SourceSnapshot(
        snapshot_id="tushare-reference:" + "a" * 64, source_id="tushare-reference",
        object_sha256="a" * 64, fetched_at=NOW, available_to_system_at=NOW,
        source_url="https://api.tushare.pro", mime="application/json", byte_size=1,
    )


def daily(**overrides):
    return {"ts_code": "600519.SH", "trade_date": "20261120", "open": 10, "high": 12,
            "low": 9, "close": 11, "pre_close": 10, "vol": "2.01", "amount": "2.1",
            **overrides}


def test_daily_hands_thousand_yuan_and_actual_visibility_are_preserved():
    item = normalize_daily(
        [daily()], snapshot(), "600519", Market.XSHG, date(2026, 11, 20), date(2026, 11, 20)
    )[0]
    assert item.volume == Decimal(201)
    assert item.amount == Decimal(2100)
    assert item.available_to_system_at == NOW
    assert item.session_close_at < item.available_to_system_at


@pytest.mark.parametrize("rows", [[daily(), daily()], [daily(ts_code="000001.SZ")],
                                 [daily(vol="-1")], [daily(high=8)]])
def test_daily_rejects_duplicate_foreign_or_invalid_bars(rows):
    with pytest.raises(ValueError):
        normalize_daily(rows, snapshot(), "600519", Market.XSHG,
                        date(2026, 11, 20), date(2026, 11, 20))


def test_identity_does_not_invent_historical_listing_status():
    row = {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台",
           "exchange": "SSE", "curr_type": "CNY", "list_status": "L",
           "list_date": "20010827", "delist_date": None}
    item = normalize_instruments([row], snapshot(), Market.XSHG)[0]
    assert item.instrument_id == "XSHG:600519"
    assert item.status_date == NOW.date()
    with pytest.raises(ValueError):
        normalize_instruments([{**row, "curr_type": "USD"}], snapshot(), Market.XSHG)


def test_valuation_capture_roundtrip_checks_raw_units_identity_and_cutoff(
    object_store, state, monkeypatch,
):
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-reference-token")
    row: dict[str, object] = {key: None for key in DEFAULT_FIELDS["daily_basic"]}
    row.update(ts_code="600519.SH", trade_date="20261120", close="10",
               total_share="2", total_mv="20", pe_ttm=None, turnover_rate="1.5")
    payload = {"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    ) as client:
        provider = TushareReferenceProvider(object_store, state, project_root=ROOT, client=client,
                                           clock=lambda: NOW)
        captured = provider.valuation_metrics("600519", Market.XSHG, date(2026, 11, 20))
    artifact_id = captured.artifact_id
    read = load_metric_capture(
        state, object_store, artifact_id, subject_id="XSHG:600519", as_of=NOW
    )
    values = {item.metric: item for item in read.metrics}
    assert values["total_share"].value == Decimal(20000)
    assert values["total_mv"].value == Decimal(200000)
    assert values["turnover_rate"].unit == "PERCENT"
    assert values["pe_ttm"].value is None
    assert not read.formal_use_allowed and not read.historical_vintage_verified
    with pytest.raises(ValueError, match="cutoff"):
        load_metric_capture(state, object_store, artifact_id, subject_id="XSHG:600519",
                            as_of=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="subject"):
        load_metric_capture(state, object_store, artifact_id, subject_id="XSHE:600519", as_of=NOW)
    object_store.path_for(read.source_object_hash).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="lineage"):
        load_metric_capture(state, object_store, artifact_id, subject_id="XSHG:600519", as_of=NOW)


def test_macro_period_basis_and_unknown_fields():
    values = {item.metric: item for item in normalize_tushare_metrics(
        "cn_gdp", {"quarter": "2026Q3", "gdp": "100", "gdp_yoy": "5"}
    )}
    assert values["gdp"].value == Decimal("10000000000")
    assert values["gdp"].period_basis == "YTD"
    assert values["gdp_yoy"].period_basis == "QUARTER_YOY"
    with pytest.raises(ValueError, match="semantics"):
        normalize_tushare_metrics("daily_basic", {"unknown_amount": 1})


@pytest.mark.parametrize("unit", [FinancialUnit.SHARES, FinancialUnit.PERCENT, FinancialUnit.RATIO])
def test_non_monetary_values_cannot_become_cny(unit):
    with pytest.raises(ValueError, match="CNY"):
        financial_amount_cny(Decimal(1), unit)
    assert financial_amount_cny(Decimal(2), FinancialUnit.TEN_THOUSAND_CNY) == Decimal(20000)


def test_macro_comparison_uses_official_period_units_and_detects_corrupt_original(
    object_store, state, monkeypatch,
):
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-macro-token")
    row: dict[str, object] = {key: None for key in DEFAULT_FIELDS["cn_gdp"]}
    row.update(quarter="2026Q3", gdp="100", gdp_yoy="5")
    payload = {"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    ) as client:
        provider = TushareReferenceProvider(object_store, state, project_root=ROOT, client=client,
                                           clock=lambda: NOW)
        captured = provider.macro_metrics("cn_gdp", "2026Q3")
    artifact_id = captured.artifact_id
    store = InvestorOrchestrationStore(state.path)
    store.initialize()
    official = OfficialMacroCaptureService(
        store, config_path=ROOT / "configs/macro_current_releases_v1.yaml",
        object_store=ImmutableRawObjectStore(object_store.root),
    )
    spec = MacroReleaseSpec(
        authority="NBS", release_family="test-gdp", url="https://www.stats.gov.cn/test-gdp",
        parser="regex-v1", regex_observations=({
            "series_key": "gdp_ytd", "unit": "hundred_million_cny",
            "pattern": r"period=(?P<period>[0-9Q-]+); value=(?P<value>[0-9.]+)",
        },),
    )
    official.config["release_specs"].append(spec.model_dump())
    original = official.capture(
        spec, recorded_content=b"period=2026-Q3; value=99",
        recorded_headers={"content-type": "text/plain; charset=utf-8",
                          "last-modified": "Fri, 20 Nov 2026 01:00:00 GMT"},
        captured_at=NOW - timedelta(minutes=1),
    )
    result = official.compare_tushare(
        capture_artifact_id=artifact_id, metric="gdp", release_family="test-gdp",
        as_of=NOW, state=state, objects=object_store,
    )
    assert result["status"] == "COMPARABLE" and result["difference"] == "100000000"
    assert result["period_basis"] == "YTD" and not result["official_authority_replaced"]
    with pytest.raises(ValueError, match="semantics"):
        official.compare_tushare(
            capture_artifact_id=artifact_id, metric="pi", release_family="test-gdp",
            as_of=NOW, state=state, objects=object_store,
        )
    object_store.path_for(original.source_hash).write_bytes(b"corrupt official")
    with pytest.raises(ValueError, match="lineage"):
        official.compare_tushare(
            capture_artifact_id=artifact_id, metric="gdp", release_family="test-gdp",
            as_of=NOW, state=state, objects=object_store,
        )
