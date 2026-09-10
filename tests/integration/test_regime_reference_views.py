"""Real recorded providers/captures to canonical read-through regime inputs.

These tests execute the existing parser, immutable objects, release publisher and
Parquet verifier. Recorded transport remains offline; no live qualification or
full-market denominator is inferred from these small fixtures.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.macro import MacroReleaseSpec, OfficialMacroCaptureService
from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.schemas.market import Market
from astock.schemas.reference_data import ReferenceDatasetKind

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def context(tmp_path: Path):
    metadata = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    metadata.initialize()
    state = StateStore(metadata.path)
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    parquet = ReferenceParquetStore(tmp_path / "data" / "parquet")
    service = MarketReferenceService(
        state, objects, parquet, ROOT / "tests" / "fixtures" / "reference"
    )
    report = service.sync_daily("600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22))
    assert report.release_id and report.manifest_object_hash
    release = state.get_market_reference_release(
        ReferenceDatasetKind.DAILY_UNADJUSTED.value, "XSHG:600519"
    )
    assert release is not None
    at = datetime.fromisoformat(release["available_to_system_at"]) + timedelta(seconds=1)
    views = CanonicalRegimeReferenceViews(state, objects, parquet.root)
    return metadata, state, objects, parquet, service, views, at, report


def test_canonical_daily_provider_release_is_read_without_copying_fact_rows(context) -> None:
    _, state, objects, parquet, _, views, at, report = context
    with closing(state.connect()) as connection:
        before = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM artifact_registry),"
                "(SELECT COUNT(*) FROM source_snapshot_index),"
                "(SELECT COUNT(*) FROM market_reference_release)"
            ).fetchone()
        )
    before_files = {
        str(path)
        for root in (objects.root, parquet.root)
        for path in root.rglob("*")
        if path.is_file()
    }
    locators = views.daily_locators("XSHG:600519", as_of=at)
    assert len(locators) == 3
    values = views.load_many(locators)
    assert [value[1]["session_date"] for value in values.values()] == [
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
    ]
    assert all(value[0]["type"] == "DailyBarObservation" for value in values.values())
    assert all(value[0]["object_hash"] == report.manifest_object_hash for value in values.values())
    assert all(value[1]["instrument_id"] == "XSHG:600519" for value in values.values())
    assert all(value[1]["adjustment_mode"] == "NONE" for value in values.values())
    assert all(state.artifact_record(locator) is None for locator in locators)
    with closing(state.connect()) as connection:
        after = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM artifact_registry),"
                "(SELECT COUNT(*) FROM source_snapshot_index),"
                "(SELECT COUNT(*) FROM market_reference_release)"
            ).fetchone()
        )
    after_files = {
        str(path)
        for root in (objects.root, parquet.root)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert before_files == after_files


def test_historical_locator_selection_does_not_use_the_current_head(context) -> None:
    _, _, _, _, _, views, at, _ = context
    assert views.daily_locators("XSHG:600519", as_of=datetime(2026, 7, 22, 1, tzinfo=UTC)) == ()
    assert len(views.daily_locators("XSHG:600519", as_of=at)) == 3
    assert views.daily_locators("XSHE:600519", as_of=at) == ()


def test_parent_is_verified_once_per_batch_and_again_on_next_invocation(context) -> None:
    _, _, _, _, _, views, at, _ = context
    locators = views.daily_locators("XSHG:600519", as_of=at)
    with patch.object(views, "_daily_rows", wraps=views._daily_rows) as verified:
        first = views.load_many(locators)
        assert verified.call_count == 1
        second = views.load_many(locators)
        assert verified.call_count == 2
    assert first == second, "read projections must not insert a new wall-clock timestamp"


def test_corrupted_parent_parquet_is_not_hidden_by_a_prior_read(context) -> None:
    _, _, _, parquet, service, views, at, _ = context
    locators = views.daily_locators("XSHG:600519", as_of=at)
    views.load_many(locators)
    descriptor = service.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")["release"][
        "canonical_files"
    ][0]
    path = parquet.root / descriptor["path"]
    path.write_bytes(path.read_bytes() + b"recorded corruption")
    with pytest.raises(ValueError, match="Parquet|manifest|invalid"):
        views.load_many(locators)


def test_unknown_or_altered_row_identity_is_not_admitted(context) -> None:
    _, _, _, _, _, views, at, _ = context
    locator = views.daily_locators("XSHG:600519", as_of=at)[0]
    with pytest.raises(ValueError, match="absent"):
        views.load(locator.rsplit(":", 1)[0] + ":" + "0" * 64)
    with pytest.raises(ValueError, match="locator"):
        views.load("reference-observation:../../outside:invalid")
    with pytest.raises(ValueError, match="timezone-aware"):
        views.daily_locators("XSHG:600519", as_of=datetime(2026, 1, 1))


def test_recorded_macro_capture_is_read_without_registering_duplicate_observations(context) -> None:
    metadata, state, _, _, _, views, _, _ = context
    at = datetime(2026, 8, 1, tzinfo=UTC)
    spec = MacroReleaseSpec(
        authority="NBS",
        release_family="recorded-regime-view",
        url="https://www.stats.gov.cn/recorded-view",
        parser="regex-v1",
        regex_observations=(
            {
                "series_key": "recorded_pmi",
                "pattern": r"PMI=(?P<value>[0-9.]+)",
                "period": "2026-07",
                "unit": "index",
                "min_value": 0,
                "max_value": 100,
            },
        ),
    )
    capture = OfficialMacroCaptureService(metadata)
    release = capture.capture(spec, recorded_content=b"<html>PMI=50.1</html>", captured_at=at)
    assert release.capture_mode == "RECORDED" and len(release.observations) == 1
    assert (
        views.macro_locators(
            authority="NBS", series_key="recorded_pmi", as_of=at - timedelta(seconds=1)
        )
        == ()
    )
    locators = views.macro_locators(authority="NBS", series_key="recorded_pmi", as_of=at)
    assert len(locators) == 1
    record, payload = views.load(locators[0])
    assert Decimal(payload["value"]) == Decimal("50.1")
    assert record["object_hash"] == release.source_hash
    assert state.artifact_record(locators[0]) is None
    assert views.load_many(locators)[locators[0]] == (record, payload)
    with pytest.raises(ValueError, match="metadata changed"):
        views.load(locators[0].rsplit(":", 1)[0] + ":" + "0" * 64)
