"""Financial mapping contracts: native revisions remain hints until official certification."""
from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash, sha256_bytes
from astock.financial_sources.certification import values_equivalent
from astock.financial_sources.config import load_financial_field_mappings
from astock.providers.tushare_financial import (
    STATEMENT_ENDPOINTS,
    TushareFinancialProvider,
    normalize_financial_rows,
    validate_recorded_financial,
)
from astock.schemas import FinancialFieldCode, FinancialUnit, Market
from astock.schemas.financial_sources import FinancialProviderReportVersion

ROOT = Path(__file__).resolve().parents[2]
PERIOD = date(2026, 9, 30)
CAPTURE = "TushareStructuredCapture:" + "a" * 64
SNAPSHOT = "tushare-structured:" + "b" * 64
TARGET: dict[str, str | int] = {"symbol": "600519", "market": "XSHG", "period_end": "2026-09-30"}


def _row(**updates):
    return {
        "ts_code": "600519.SH", "ann_date": "20261031", "f_ann_date": "20261031",
        "end_date": "20260930", "report_type": "1", "comp_type": "1",
        "update_flag": "1", "total_assets": 100, "total_share": None, **updates,
    }


def _normalize(rows):
    return normalize_financial_rows(
        rows, "600519", Market.XSHG, PERIOD,
        capture_artifact_id=CAPTURE, raw_snapshot_id=SNAPSHOT,
    )


def _version(row: dict[str, object]) -> FinancialProviderReportVersion:
    return FinancialProviderReportVersion.model_validate(row["provider_report_version"])


def _fixture():
    return json.loads(
        (ROOT / "tests/fixtures/providers/tushare-financial-success.json").read_text(
            encoding="utf-8"
        )
    )["body"]


def _provider(object_store, state, monkeypatch, responses=None):
    monkeypatch.setenv("TUSHARE_TOKEN", "financial-mock-credential")
    fixture = _fixture()
    calls = []

    def transport(request):
        wire = json.loads(request.content)
        calls.append(wire)
        assert wire["params"]["report_type"] == "1"
        assert wire["params"]["ts_code"] == "600519.SH"
        endpoint = wire["api_name"]
        if responses and endpoint in responses:
            return httpx.Response(200, json=responses[endpoint])
        statement = next(key for key, value in STATEMENT_ENDPOINTS.items() if value == endpoint)
        return httpx.Response(
            200, content=fixture["captures"][statement]["raw_response"].encode(),
            headers={"content-type": "application/json"},
        )

    client = httpx.Client(transport=httpx.MockTransport(transport))
    return TushareFinancialProvider(
        object_store, state, project_root=ROOT, client=client,
    ), client, calls


def test_latest_revision_selected_without_losing_raw_identity_or_null():
    old = _row(update_flag="0", total_assets=99, total_share=8)
    latest = _row(total_assets="100.0000000001")
    selected = _normalize([old, latest, dict(latest)])[0]
    assert selected["total_assets"] == "100.0000000001"
    assert selected["total_share"] is None
    metadata = _version(selected)
    assert metadata.source_row_hash == content_hash(latest)
    assert metadata.returned_version_count == 2
    assert metadata.update_flag == "1"
    assert metadata.announced_on == date(2026, 10, 31)
    assert metadata.actual_announced_on == date(2026, 10, 31)
    assert metadata.publication_precision == "DATE_ONLY"
    assert "available_to_system_at" not in metadata.model_dump()
    assert metadata.raw_snapshot_id == SNAPSHOT


def test_official_latest_flag_has_priority_over_a_later_unmarked_announcement():
    marked = _row(update_flag="1")
    later = _row(ann_date="20261105", f_ann_date="20261105", update_flag="0", total_assets=101)
    assert _normalize([marked, later])[0]["total_assets"] == "100"


def test_same_latest_flag_uses_actual_announcement_date():
    older = _row(update_flag="1")
    newer = _row(ann_date="20261105", f_ann_date="20261105", total_assets=101)
    assert _normalize([older, newer])[0]["total_assets"] == "101"


def test_unique_latest_flag_with_missing_date_does_not_select_an_older_dated_row():
    older = _row(update_flag="0")
    marked = _row(ann_date=None, f_ann_date=None, total_assets=101)
    result = _normalize([older, marked])[0]
    assert result["total_assets"] == "101"
    assert _version(result).publication_precision == "UNKNOWN"


@pytest.mark.parametrize("flag", ["0", "1", None])
def test_competing_versions_with_unknown_dates_are_not_arbitrarily_ordered(flag):
    with pytest.raises(ValueError, match="revision date is unknown"):
        _normalize([
            _row(update_flag=flag),
            _row(ann_date=None, f_ann_date=None, update_flag=flag, total_assets=101),
        ])


def test_conflicting_latest_versions_are_not_silently_selected():
    with pytest.raises(ValueError, match="ambiguous"):
        _normalize([_row(), _row(total_assets=101)])


@pytest.mark.parametrize("report_type", [str(number) for number in range(2, 13)])
def test_parent_single_quarter_and_adjustment_types_cannot_enter_cumulative(report_type):
    with pytest.raises(ValueError, match="identity mismatch"):
        _normalize([_row(report_type=report_type)])


@pytest.mark.parametrize("updates", [
    {"ts_code": "600519.SZ"}, {"end_date": "20260630"}, {"comp_type": "9"},
    {"update_flag": "2"}, {"ann_date": "2026-10-31"}, {"f_ann_date": "20260929"},
    {"total_assets": True}, {"total_assets": "not-numeric"}, {"total_assets": "NaN"},
    {"total_assets": float("inf")},
])
def test_bad_identity_dates_and_values_fail_mapping(updates):
    with pytest.raises(ValueError):
        _normalize([_row(**updates)])


def test_absent_announcement_is_unknown_not_an_invented_timestamp():
    metadata = _version(_normalize([_row(ann_date=None, f_ann_date=None)])[0])
    assert metadata.publication_precision == "UNKNOWN"
    assert metadata.announced_on is None and metadata.actual_announced_on is None


def test_money_and_shares_use_provider_specific_units():
    mappings = load_financial_field_mappings(ROOT / "configs/financial_field_mappings.yaml")
    for mapping in mappings:
        expected = (
            FinancialUnit.SHARES if mapping.field_code is FinancialFieldCode.SHARES_OUTSTANDING
            else FinancialUnit.CNY
        )
        assert mapping.provider_unit("tushare-financial") is expected
        assert mapping.provider_field("tushare-financial")
    assert values_equivalent(
        Decimal("10000000"), FinancialUnit.CNY, Decimal("1000"), FinancialUnit.TEN_THOUSAND_CNY,
    )
    assert not values_equivalent(
        Decimal("10000000"), FinancialUnit.CNY, Decimal("1000"), FinancialUnit.SHARES,
    )


def test_native_recording_replays_all_three_statement_revisions():
    payload = _fixture()
    assert validate_recorded_financial(payload, TARGET) == (True, 5)
    for capture in payload["captures"].values():
        assert capture["source_snapshot_id"].split(":")[1] == sha256_bytes(
            capture["raw_response"].encode()
        )
    missing = copy.deepcopy(payload)
    del missing["captures"]["CASH_FLOW_STATEMENT"]
    assert validate_recorded_financial(missing, TARGET) == (False, 0)
    altered = copy.deepcopy(payload)
    altered["captures"]["BALANCE_SHEET"]["raw_response"] += " "
    assert validate_recorded_financial(altered, TARGET) == (False, 0)
    assert validate_recorded_financial(payload, {**TARGET, "symbol": "000001"}) == (False, 0)


def test_native_adapter_reuses_shared_cache_and_keeps_all_original_versions(
    object_store, state, monkeypatch,
):
    provider, client, calls = _provider(object_store, state, monkeypatch)
    try:
        first = provider.fetch("600519", Market.XSHG, PERIOD, live=True)
        second = provider.fetch("600519", Market.XSHG, PERIOD, live=True)
    finally:
        client.close()
    assert len(calls) == 3
    assert first == second
    assert set(first.tables) == set(STATEMENT_ENDPOINTS)
    balance = first.tables["BALANCE_SHEET"][0]
    assert Decimal(str(balance["total_share"])) == Decimal("1250081601")
    assert _version(balance).returned_version_count == 2
    assert _version(balance).update_flag == "1"
    assert first.tables["CASH_FLOW_STATEMENT"][0]["net_profit"] is None
    recorded = _fixture()["captures"]
    for statement, snapshot in first.snapshots_by_statement.items():
        assert state.get_snapshot(snapshot.snapshot_id) == snapshot
        assert snapshot.source_id == (
            "tushare-financial:" + first.request_hashes_by_statement[statement]
        )
        metadata = _version(first.tables[statement][0])
        assert snapshot.object_sha256 == metadata.raw_snapshot_id.split(":")[1]
        assert object_store.verify(snapshot.object_sha256)
        with state.connect() as connection:
            artifact = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (metadata.capture_artifact_id,),
            ).fetchone()
        assert artifact is not None and object_store.verify(artifact["object_hash"])
        raw_bytes = object_store.get_bytes(snapshot.object_sha256)
        assert raw_bytes == recorded[statement]["raw_response"].encode()
        raw = json.loads(raw_bytes)
        assert len(raw["data"]["items"]) == metadata.returned_version_count


def test_permission_failure_keeps_completed_raw_and_preserves_failure_class(
    object_store, state, monkeypatch,
):
    provider, client, calls = _provider(
        object_store, state, monkeypatch,
        {"income": {"code": 2002, "msg": "权限不足"}},
    )
    try:
        with pytest.raises(ProviderError) as caught:
            provider.fetch("600519", Market.XSHG, PERIOD, live=True)
    finally:
        client.close()
    assert len(calls) == 2
    assert caught.value.failure_class is FailureClass.ACCESS_RESTRICTED
    snapshots = caught.value.details["financial_snapshot_ids"]
    assert len(snapshots) == 1
    assert state.get_snapshot(snapshots[0]) is not None
    assert state.get_snapshot(caught.value.details["snapshot_id"]) is not None


def test_native_adapter_never_substitutes_recorded_fixture_for_live(
    object_store, state, monkeypatch,
):
    provider, client, calls = _provider(object_store, state, monkeypatch)
    try:
        with pytest.raises(ValueError, match="FINANCIAL_RECORDED_UNAVAILABLE"):
            provider.fetch("600519", Market.XSHG, PERIOD)
    finally:
        client.close()
    assert not calls
