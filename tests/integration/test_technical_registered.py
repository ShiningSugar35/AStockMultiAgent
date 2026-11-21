"""Recorded canonical-release -> technical feature -> existing artifact registry, offline."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.schemas.market import Market
from astock.schemas.reference_data import ReferenceDatasetKind
from astock.technical.cli import register_commands
from astock.technical.policy import load_technical_analysis_policy
from astock.technical.registered import analyze_registered_daily

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source(tmp_path: Path):
    metadata = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    metadata.initialize()
    state = StateStore(metadata.path)
    objects = ObjectStore(tmp_path / "objects/sha256")
    parquet = ReferenceParquetStore(tmp_path / "data/parquet")
    reference = MarketReferenceService(state, objects, parquet, ROOT / "tests/fixtures/reference")
    report = reference.sync_daily("600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22))
    row = state.get_market_reference_release(
        ReferenceDatasetKind.DAILY_UNADJUSTED.value,
        "XSHG:600519",
    )
    assert row is not None and report.release_id
    return state, objects, parquet, reference, row


def _call(source, *, register=False):
    state, objects, parquet, _, row = source
    return analyze_registered_daily(
        state,
        objects,
        row["release_id"],
        policy=load_technical_analysis_policy(ROOT / "configs/technical_analysis.yaml"),
        calendar_path=ROOT / "configs/official_trading_calendar.yaml",
        as_of=datetime.fromisoformat(row["available_to_system_at"]),
        parquet_root=parquet.root,
        register=register,
    )


def _counts(state):
    with closing(state.connect()) as connection:
        return tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM artifact_registry),"
                "(SELECT COUNT(*) FROM source_snapshot_index),"
                "(SELECT COUNT(*) FROM market_reference_release)"
            ).fetchone()
        )


def test_canonical_parent_is_verified_once_and_readonly_does_not_copy_facts(source):
    state, objects, parquet, _, _ = source
    before = _counts(state)
    files = sorted(
        str(p) for root in (objects.root, parquet.root) for p in root.rglob("*") if p.is_file()
    )
    original = CanonicalRegimeReferenceViews._daily_rows
    calls = []

    def counted(view, release_id, **kwargs):
        calls.append(release_id)
        return original(view, release_id, **kwargs)

    with patch.object(CanonicalRegimeReferenceViews, "_daily_rows", counted):
        snapshot = _call(source)
    assert len(calls) == 1
    assert snapshot.instrument_id == "XSHG:600519"
    assert snapshot.included_bar_count == 3
    assert "INDICATOR_HISTORY_PARTIAL" in snapshot.caution_codes
    assert not snapshot.recommendation_allowed
    assert _counts(state) == before
    assert files == sorted(
        str(p) for root in (objects.root, parquet.root) for p in root.rglob("*") if p.is_file()
    )


def test_register_reuses_same_derived_artifact_and_never_duplicates_market_facts(source):
    state, _, _, _, _ = source
    before = _counts(state)
    first = _call(source, register=True)
    second = _call(source, register=True)
    assert first == second
    assert _counts(state) == (before[0] + 1, before[1], before[2])
    record = state.artifact_record(
        "TechnicalIndicatorSnapshot:" + first.snapshot_id.removeprefix("technical:")
    )
    assert record and record["type"] == "TechnicalIndicatorSnapshot"


def test_corrupted_parent_never_produces_indicator_snapshot(source):
    _, _, parquet, reference, _ = source
    descriptor = reference.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")["release"][
        "canonical_files"
    ][0]
    target = parquet.root / descriptor["path"]
    target.write_bytes(target.read_bytes() + b"corrupt")
    with pytest.raises(ValueError):
        _call(source)


def test_cli_is_actual_wired_callable_and_failures_hide_input_details(source):
    state, objects, parquet, _, row = source
    app = typer.Typer()
    paths = SimpleNamespace(root=ROOT, parquet=parquet.root)
    register_commands(app, lambda: (paths, state, objects), lambda v: typer.echo(json.dumps(v)))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "research-technical-analyze",
            row["release_id"],
            "--as-of",
            row["available_to_system_at"],
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "RESEARCH_ONLY"
    failed = runner.invoke(app, ["research-technical-analyze", "private-secret-not-an-id"])
    assert failed.exit_code == 2
    assert "private-secret" not in failed.output and "Traceback" not in failed.output
    assert json.loads(failed.output)["status"] == "UNAVAILABLE"


def test_root_cli_registration_does_not_change_frozen_entry_modules():
    from astock.cli import app

    result = CliRunner().invoke(app, ["research-technical-schema"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema["properties"]["recommendation_allowed"]["const"] is False


def test_large_release_verification_and_tail_use_bounded_parquet_batches(source):
    from datetime import timedelta

    import pyarrow.parquet as pq

    from astock.core.hashing import content_hash
    from astock.technical.registered import load_daily_release

    state, objects, parquet, reference, row = source
    view = CanonicalRegimeReferenceViews(state, objects, parquet.root)
    _, manifest, original = view._daily_rows(row["release_id"])
    template = tuple(original.values())[-1]
    rows = []
    count = 2051
    for index in range(count):
        offset = timedelta(days=count - 1 - index)
        rows.append(
            template.model_copy(
                update={
                    "observation_id": content_hash({"streaming_index": index}),
                    "session_date": template.session_date - offset,
                    "session_close_at": template.session_close_at - offset,
                }
            )
        )
    report = reference._release(
        command="recorded-streaming-test",
        dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key=manifest.scope_key,
        provider_id=manifest.provider_id,
        raw_snapshot_ids=list(manifest.raw_snapshot_ids),
        records=rows,
        requested_start=rows[0].session_date,
        requested_end=rows[-1].session_date,
        available_at=manifest.available_to_system_at,
        complete=True,
        reasons=[],
    )
    with patch.object(pq.ParquetFile, "read", side_effect=AssertionError("unbounded Parquet read")):
        returned, tail, _, _ = load_daily_release(
            state,
            objects,
            report.release_id,
            limit=17,
            parquet_root=parquet.root,
        )
    assert returned.coverage.record_count == count
    assert len(tail) == 17
    assert tuple(x.observation_id for x in tail) == tuple(x.observation_id for x in rows[-17:])
    # Reject a parent larger than the configured scan budget before any row
    # decoding, even though the requested indicator tail contains only 17 bars.
    with patch.object(
        CanonicalRegimeReferenceViews,
        "_daily_rows",
        side_effect=AssertionError("must reject before scanning parent"),
    ):
        with pytest.raises(ValueError, match="row budget"):
            load_daily_release(
                state,
                objects,
                report.release_id,
                limit=17,
                parquet_root=parquet.root,
                max_release_rows=1000,
            )
