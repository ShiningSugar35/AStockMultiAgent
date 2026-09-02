"""Tests for macro-economic authority recorded-first providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.pit import PointInTimeRepository
from astock.providers.macro_authority import (
    MacroAuthorityReleaseError,
    MofFiscalPolicyReleaseProvider,
    NbsStatisticalReleaseProvider,
    NdrcPricingPolicyReleaseProvider,
    PbocMonetaryPolicyReleaseProvider,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "macro"


def test_nbs_gdp_recorded_fixture_is_valid(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = NbsStatisticalReleaseProvider(object_store, state, FIXTURE_ROOT)
    payload, snapshot = provider.fetch_indicator("gdp")

    assert payload["indicator_code"] == "gdp"
    assert payload["indicator_name"] == "国内生产总值"
    assert payload["observation_period"] == "2026-Q2"
    assert payload["publication_date"] == "2026-07-15"
    assert payload["revision_version"] == 1
    assert payload["available_to_system_at"] == "2026-07-15T10:00:00+08:00"
    assert isinstance(payload["data_points"], list)
    assert len(payload["data_points"]) == 2
    assert object_store.verify(snapshot.object_sha256)


def test_macro_revision_chain_is_pit_linked(
    state: StateStore,
    object_store: ObjectStore,
    tmp_path: Path,
) -> None:
    payload = json.loads((FIXTURE_ROOT / "nbs_gdp.json").read_text(encoding="utf-8"))
    fixture = tmp_path / "nbs_gdp.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    provider = NbsStatisticalReleaseProvider(object_store, state, tmp_path)
    _, first_snapshot = provider.fetch_indicator("gdp")

    payload["revision_version"] = 2
    payload["publication_date"] = "2026-07-20"
    payload["available_to_system_at"] = "2026-07-20T10:00:00+08:00"
    payload["revision_history"].append(
        {"version": 2, "date": "2026-07-20", "description": "修订核算"}
    )
    payload["data_points"][0]["value"] = 321000.0
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _, second_snapshot = provider.fetch_indicator("gdp")

    repository = PointInTimeRepository(state)
    first = repository.get_by_source("macro:nbs-statistical-release:gdp:2026-Q2:v1")
    second = repository.get_by_source("macro:nbs-statistical-release:gdp:2026-Q2:v2")
    assert first is not None
    assert second is not None
    assert first.source_snapshot_id == first_snapshot.snapshot_id
    assert second.source_snapshot_id == second_snapshot.snapshot_id
    assert second.supersedes_source_id == first.source_id
    assert second.revised_at == second_snapshot.available_to_system_at
    assert first.available_to_system_at < second.available_to_system_at


def test_pboc_m2_recorded_fixture_is_valid(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = PbocMonetaryPolicyReleaseProvider(object_store, state, FIXTURE_ROOT)
    payload, snapshot = provider.fetch_indicator("m2")

    assert payload["indicator_code"] == "m2"
    assert payload["indicator_name"] == "广义货币供应量M2"
    assert payload["observation_period"] == "2026-06"
    assert payload["publication_date"] == "2026-07-12"
    assert payload["revision_version"] == 1
    assert isinstance(payload["data_points"], list)
    assert len(payload["data_points"]) == 2
    assert object_store.verify(snapshot.object_sha256)


def test_mof_fiscal_revenue_recorded_fixture_is_valid(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = MofFiscalPolicyReleaseProvider(object_store, state, FIXTURE_ROOT)
    payload, snapshot = provider.fetch_indicator("fiscal_revenue")

    assert payload["indicator_code"] == "fiscal_revenue"
    assert payload["indicator_name"] == "全国一般公共预算收入"
    assert payload["observation_period"] == "2026-06"
    assert payload["publication_date"] == "2026-07-18"
    assert payload["revision_version"] == 1
    assert isinstance(payload["data_points"], list)
    assert len(payload["data_points"]) == 2
    assert object_store.verify(snapshot.object_sha256)


def test_ndrc_refined_oil_price_recorded_fixture_is_valid(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = NdrcPricingPolicyReleaseProvider(object_store, state, FIXTURE_ROOT)
    payload, snapshot = provider.fetch_indicator("refined_oil_price")

    assert payload["indicator_code"] == "refined_oil_price"
    assert payload["indicator_name"] == "成品油价格调整"
    assert payload["observation_period"] == "2026-07-01"
    assert payload["publication_date"] == "2026-06-30"
    assert payload["revision_version"] == 1
    assert isinstance(payload["data_points"], list)
    assert len(payload["data_points"]) == 2
    assert object_store.verify(snapshot.object_sha256)


def test_nbs_live_fetch_raises_not_implemented(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = NbsStatisticalReleaseProvider(object_store, state, FIXTURE_ROOT)
    with pytest.raises(MacroAuthorityReleaseError, match="not yet implemented"):
        provider.fetch_indicator("gdp", live=True)


def test_nbs_missing_fixture_raises_error(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = NbsStatisticalReleaseProvider(object_store, state, FIXTURE_ROOT)
    with pytest.raises(MacroAuthorityReleaseError, match="Missing recorded NBS fixture"):
        provider.fetch_indicator("nonexistent_indicator")


def test_macro_release_validates_schema_version(
    state: StateStore,
    object_store: ObjectStore,
    tmp_path: Path,
) -> None:
    bad_fixture = tmp_path / "nbs_bad_schema.json"
    bad_fixture.write_text(
        '{"schema_version": "wrong", "_astock_source": "nbs-statistical-release", '
        '"indicator_code": "gdp", "observation_period": "2026-Q2", '
        '"publication_date": "2026-07-15", "revision_version": 1, '
        '"available_to_system_at": "2026-07-15T10:00:00+08:00", "data_points": []}',
        encoding="utf-8",
    )
    provider = NbsStatisticalReleaseProvider(object_store, state, tmp_path)
    with pytest.raises(MacroAuthorityReleaseError, match="schema version"):
        provider.fetch_indicator("bad_schema")


def test_macro_release_validates_source_field(
    state: StateStore,
    object_store: ObjectStore,
    tmp_path: Path,
) -> None:
    bad_fixture = tmp_path / "nbs_bad_source.json"
    bad_fixture.write_text(
        '{"schema_version": "macro-release-v1", "_astock_source": "wrong-source", '
        '"indicator_code": "gdp", "observation_period": "2026-Q2", '
        '"publication_date": "2026-07-15", "revision_version": 1, '
        '"available_to_system_at": "2026-07-15T10:00:00+08:00", "data_points": []}',
        encoding="utf-8",
    )
    provider = NbsStatisticalReleaseProvider(object_store, state, tmp_path)
    with pytest.raises(MacroAuthorityReleaseError, match="source mismatch"):
        provider.fetch_indicator("bad_source")


def test_macro_release_validates_required_fields(
    state: StateStore,
    object_store: ObjectStore,
    tmp_path: Path,
) -> None:
    bad_fixture = tmp_path / "nbs_missing_fields.json"
    bad_fixture.write_text(
        '{"schema_version": "macro-release-v1", "_astock_source": "nbs-statistical-release"}',
        encoding="utf-8",
    )
    provider = NbsStatisticalReleaseProvider(object_store, state, tmp_path)
    with pytest.raises(MacroAuthorityReleaseError, match="indicator_code"):
        provider.fetch_indicator("missing_fields")


def test_macro_release_validates_data_points_not_empty(
    state: StateStore,
    object_store: ObjectStore,
    tmp_path: Path,
) -> None:
    bad_fixture = tmp_path / "nbs_empty_data.json"
    bad_fixture.write_text(
        '{"schema_version": "macro-release-v1", "_astock_source": "nbs-statistical-release", '
        '"_astock_request": {"purpose": "MACRO_ECONOMIC_RELEASE", "indicator_code": "gdp"}, '
        '"indicator_code": "gdp", "observation_period": "2026-Q2", '
        '"publication_date": "2026-07-15", "revision_version": 1, '
        '"available_to_system_at": "2026-07-15T10:00:00+08:00", '
        '"source_url": "https://www.stats.gov.cn/test", "data_points": []}',
        encoding="utf-8",
    )
    provider = NbsStatisticalReleaseProvider(object_store, state, tmp_path)
    with pytest.raises(MacroAuthorityReleaseError, match="empty data_points"):
        provider.fetch_indicator("empty_data")
