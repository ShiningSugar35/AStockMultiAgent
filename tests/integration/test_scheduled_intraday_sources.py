"""Recorded HTTP -> canonical intraday prices -> read-only scheduled input audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import ScheduledDomain, ScheduledWindow
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditRequest,
    ScheduledInputCoverageService,
    ScheduledSourceScope,
    load_input_audit_policy,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.market_data.sync import MarketSyncService
from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.sina import Sina5mProvider
from astock.schemas import BarRequest, Frequency, Market

AT = datetime(2026, 7, 22, 7, 0, 10, tzinfo=UTC)


class CaptureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz else AT.replace(tzinfo=None)


@pytest.fixture(params=[Frequency.M5, Frequency.H1])
def captured(tmp_path: Path, request: pytest.FixtureRequest):
    frequency = request.param
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(tmp_path / "objects/sha256")
    market = CanonicalMarketStore(tmp_path / "data/parquet", tmp_path / "manifests")
    shanghai = ZoneInfo("Asia/Shanghai")
    starts = [
        datetime(2026, 7, 22, 9, 30, tzinfo=shanghai),
        datetime(2026, 7, 22, 13, 0, tzinfo=shanghai),
    ]
    minutes = 5 if frequency is Frequency.M5 else 60
    labels = [
        start + timedelta(minutes=minutes * index)
        for start in starts
        for index in range(1, 120 // minutes + 1)
    ]
    east = {
        "data": {
            "code": "600519",
            "market": 1,
            "klines": [f"{label:%Y-%m-%d %H:%M},10,10,10.01,9.99,100,100000" for label in labels],
        }
    }
    sina = [
        {
            "day": f"{label:%Y-%m-%d %H:%M:%S}",
            "open": "10",
            "close": "10",
            "high": "10.01",
            "low": "9.99",
            "volume": "10000",
        }
        for label in labels
    ]
    bar_request = BarRequest(
        symbol="600519",
        market=Market.XSHG,
        frequency=frequency,
        requested_start=starts[0],
        requested_end=AT,
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=east))) as e:
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=sina))
        ) as s:
            with patch("astock.providers.base.datetime", CaptureClock):
                sync = MarketSyncService(
                    [
                        EastMoney5mProvider(objects, state, client=e),
                        Sina5mProvider(objects, state, client=s),
                    ],
                    state,
                    ParquetMarketStore(tmp_path / "data/parquet", "market_observation"),
                    market,
                ).sync_intraday(bar_request)
    assert sync.canonical_report.quality_status.value == "PASS"
    bar = market.read_bars(bar_request)[-1]
    locator = (
        f"intraday-observation:XSHG:600519:{frequency.value}:"
        f"{sync.canonical_manifest['content_hash']}:{bar.observation_id}"
    )
    audit = ScheduledInputCoverageService(store, load_input_audit_policy())
    audit_request = ScheduledInputAuditRequest(
        run_id="intraday-source-test",
        domain=ScheduledDomain.WATCHLIST,
        window=ScheduledWindow.INTRADAY,
        scope=ScheduledSourceScope(
            instrument_id="XSHG:600519",
            company_query="issuer",
            industry_query="industry",
            policy_families=(("NBS", "test"),),
        ),
        interval_start=AT - timedelta(hours=1),
        interval_end=AT - timedelta(seconds=1),
        as_of=AT,
        market_references=(locator,),
    )
    return audit, audit_request, market, bar_request


def test_canonical_intraday_source_is_verified_without_network_or_economic_writes(captured):
    audit, request, _market, _bar_request = captured

    def durable_rows():
        tables = (
            "external_account_event",
            "journal",
            "ledger_entry",
            "order_record",
            "fill",
            "position",
            "artifact_registry",
            "source_snapshot_index",
            "source_snapshot_detail",
        )
        with audit.store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in tables
            }

    before = durable_rows()
    with (
        patch.object(httpx.Client, "request", side_effect=AssertionError("audit must be offline")),
        patch.object(ObjectStore, "put_bytes", side_effect=AssertionError("audit must not write")),
    ):
        check = audit.build(request).checks[0]
    assert check.status == "CHECKED"
    assert check.checked_through == AT - timedelta(seconds=10)
    assert request.market_references[0] in check.source_revisions
    assert durable_rows() == before


def test_intraday_source_cannot_be_used_before_raw_capture(captured):
    audit, request, _market, _bar_request = captured
    request = request.model_copy(update={"as_of": AT - timedelta(seconds=1)})
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_cannot_cover_different_security(captured, monkeypatch):
    from astock.investor_orchestration.scheduled_market_views import ScheduledIntradayViews

    def forbidden_parent(*args, **kwargs):
        pytest.fail("wrong-security input must fail before canonical I/O")

    monkeypatch.setattr(ScheduledIntradayViews, "_parent", forbidden_parent)
    audit, request, _market, _bar_request = captured
    scope = request.scope.model_copy(update={"instrument_id": "XSHE:000001"})
    assert audit.build(request.model_copy(update={"scope": scope})).checks[0].status == "INVALID"


def test_intraday_source_freshness_uses_bar_close_not_report_time(captured):
    audit, request, _market, _bar_request = captured
    request = request.model_copy(update={"as_of": AT + timedelta(minutes=11)})
    assert audit.build(request).checks[0].status == "STALE"


def test_intraday_source_rejects_changed_locator_digest(captured):
    audit, request, _market, _bar_request = captured
    parts = request.market_references[0].split(":")
    parts[-2] = "f" * 64
    request = request.model_copy(update={"market_references": (":".join(parts),)})
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_rejects_corrupt_canonical_file(captured):
    audit, request, market, bar_request = captured
    manifest = market.load_manifest(bar_request)
    assert manifest is not None
    path = market.data_root / manifest["files"][0]
    path.write_bytes(b"corrupt canonical fixture")
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_rejects_corrupt_raw_object(captured):
    audit, request, market, bar_request = captured
    manifest = market.load_manifest(bar_request)
    snapshot = audit.state.get_snapshot(manifest["source_snapshot_ids"][0])
    assert snapshot is not None
    audit.objects.path_for(snapshot.object_sha256).write_bytes(b"corrupt raw fixture")
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_rejects_wrong_captured_query(captured, monkeypatch):
    audit, request, _market, _bar_request = captured
    original = audit.state.get_snapshot

    def wrong_query(identity):
        value = original(identity)
        if value is not None:
            return value.model_copy(
                update={
                    "source_url": value.source_url.replace("600519", "000001"),
                }
            )
        return value

    monkeypatch.setattr(audit.state, "get_snapshot", wrong_query)
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_single_provider_cannot_keep_a_rehashed_pass(captured):
    import json

    from astock.core.hashing import content_hash

    audit, request, market, bar_request = captured
    manifest = market.load_manifest(bar_request)
    manifest["source_snapshot_ids"] = manifest["source_snapshot_ids"][:1]
    manifest.pop("content_hash")
    manifest["content_hash"] = content_hash(manifest)
    market.manifest_path(bar_request).write_text(json.dumps(manifest), encoding="utf-8")
    parts = request.market_references[0].split(":")
    parts[-2] = manifest["content_hash"]
    request = request.model_copy(update={"market_references": (":".join(parts),)})
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_source_forming_bar_does_not_become_closed_when_read_later(captured, monkeypatch):
    from astock.investor_orchestration.scheduled_market_views import ScheduledIntradayViews

    audit, request, _market, _bar_request = captured
    original = audit.state.get_snapshot

    def earlier_capture(identity):
        value = original(identity)
        if value is not None:
            return value.model_copy(
                update={
                    "fetched_at": AT - timedelta(seconds=20),
                    "available_to_system_at": AT - timedelta(seconds=20),
                }
            )
        return value

    monkeypatch.setattr(audit.state, "get_snapshot", earlier_capture)
    with pytest.raises(ValueError, match="target bar lacks two"):
        ScheduledIntradayViews(audit.state, audit.objects).load_many(
            request.market_references,
            as_of=AT,
        )


@pytest.mark.parametrize("late_minutes,exit_code", [(0, 0), (11, 3)])
def test_intraday_cli_returns_existing_locator_and_explicit_freshness(
    captured, late_minutes, exit_code
):
    import json

    import typer
    from typer.testing import CliRunner

    from astock.investor_orchestration.source_audit_cli import register_source_audit_commands

    audit, request, _market, bar_request = captured
    app = typer.Typer()
    register_source_audit_commands(app)
    result = CliRunner().invoke(
        app,
        [
            "schedule-market-locator",
            request.scope.instrument_id,
            "--frequency",
            bar_request.frequency.value,
            "--database",
            str(audit.store.path),
            "--at",
            (AT + timedelta(minutes=late_minutes)).isoformat(),
        ],
    )
    assert result.exit_code == exit_code, result.output
    payload = json.loads(result.output)
    assert payload["locator"] == request.market_references[0]
    assert payload["status"] == ("STALE" if late_minutes else "CHECKED")
    assert payload["scope"] == "MARKET_ONLY"
    assert payload["formal_research_complete"] is False


def test_intraday_source_rehashed_parquet_price_still_must_match_raw(captured):
    import json
    from decimal import Decimal

    import pyarrow as pa
    import pyarrow.parquet as pq

    from astock.core.hashing import content_hash, sha256_bytes

    audit, request, market, bar_request = captured
    manifest = market.load_manifest(bar_request)
    relative = manifest["files"][0]
    path = market.data_root / relative
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[-1]["close"] = Decimal("12")
    rows[-1]["high"] = Decimal("12")
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    manifest["file_hashes"][relative] = sha256_bytes(path.read_bytes())
    manifest.pop("content_hash")
    manifest["content_hash"] = content_hash(manifest)
    market.manifest_path(bar_request).write_text(json.dumps(manifest), encoding="utf-8")
    parts = request.market_references[0].split(":")
    parts[-2] = manifest["content_hash"]
    request = request.model_copy(update={"market_references": (":".join(parts),)})
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_response_body_security_must_agree_with_capture_query(captured):
    import json

    from astock.core.hashing import content_hash

    audit, request, market, bar_request = captured
    manifest = market.load_manifest(bar_request)
    identity = next(
        item for item in manifest["source_snapshot_ids"] if item.startswith("eastmoney")
    )
    snapshot = audit.state.get_snapshot(identity)
    raw = json.loads(audit.objects.get_bytes(snapshot.object_sha256))
    raw["data"]["code"] = "000001"
    source = audit.objects.put_json(raw)
    updated = snapshot.model_copy(
        update={
            "snapshot_id": f"eastmoney-5m:{source.sha256}",
            "object_sha256": source.sha256,
            "byte_size": source.byte_size,
        }
    )
    audit.state.register_snapshot(updated)
    manifest["source_snapshot_ids"] = [
        updated.snapshot_id if item == identity else item
        for item in manifest["source_snapshot_ids"]
    ]
    manifest.pop("content_hash")
    manifest["content_hash"] = content_hash(manifest)
    market.manifest_path(bar_request).write_text(json.dumps(manifest), encoding="utf-8")
    parts = request.market_references[0].split(":")
    parts[-2] = manifest["content_hash"]
    request = request.model_copy(update={"market_references": (":".join(parts),)})
    assert audit.build(request).checks[0].status == "INVALID"


def test_intraday_cli_missing_database_does_not_create_a_directory(tmp_path):
    import typer
    from typer.testing import CliRunner

    from astock.investor_orchestration.source_audit_cli import register_source_audit_commands

    app = typer.Typer()
    register_source_audit_commands(app)
    database = tmp_path / "not-initialized/state.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "schedule-market-locator",
            "XSHG:600519",
            "--database",
            str(database),
        ],
    )
    assert result.exit_code != 0
    assert not database.parent.exists()
