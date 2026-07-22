from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.market_data.reference import _parse_baostock_daily
from astock.providers import BaoStockReferenceProvider
from astock.schemas import (
    Market,
    ReferenceBatch,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferenceFileDescriptor,
    ReferencePitStatus,
    ReferenceSyncReport,
)
from astock.schemas.reference_data import ReferenceRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "reference"


def _service(tmp_path: Path) -> MarketReferenceService:
    state = StateStore(tmp_path / "中文状态" / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return MarketReferenceService(
        state,
        ObjectStore(tmp_path / "对象"),
        ReferenceParquetStore(tmp_path / "Parquet数据"),
        FIXTURES,
    )


def test_recorded_master_calendar_daily_release_is_idempotent_and_auditable(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    master = service.sync_instruments()
    calendar = service.sync_calendar(
        Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    daily = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )

    assert master.coverage.record_count == 6
    assert calendar.coverage.record_count == 3
    assert daily.coverage.record_count == 3
    assert master.release_id and calendar.release_id and daily.release_id
    assert service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    ).release_id == daily.release_id
    with sqlite3.connect(service.state.path) as connection:
        release_count = connection.execute(
            "SELECT count(*) FROM market_reference_release"
        ).fetchone()[0]
        assert release_count == 3
        assert connection.execute("SELECT count(*) FROM market_reference_head").fetchone()[0] == 3

    status = service.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")
    assert status["status"] == "AVAILABLE"
    canonical = status["release"]["canonical_files"][0]["path"]
    table = pq.ParquetFile(service.parquet.root / canonical).read()
    rows = [json.loads(item) for item in table.column("record_json").to_pylist()]
    assert {row["adjustment_mode"] for row in rows} == {"NONE"}
    assert {row["volume_unit"] for row in rows} == {"SHARE"}
    assert service.audit() == {
        "schema_version": "reference-audit-v1",
        "release_count": 3,
        "corrupt_release_ids": [],
        "reason_codes": [],
        "status": "PASS",
        "ledger_writes": 0,
    }


def test_as_of_visibility_and_damaged_manifest_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    assert report.release_id and report.manifest_object_hash
    before = service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED,
        "XSHG:600519",
        as_of=datetime(2026, 7, 22, 1, tzinfo=UTC),
    )
    assert before["status"] == "NOT_AVAILABLE"

    service.objects.path_for(report.manifest_object_hash).write_bytes(b"tampered")
    assert service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519"
    )["status"] == "CORRUPT"


def test_manifest_descriptors_and_parquet_corruption_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    status = service.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")
    descriptor = status["release"]["canonical_files"][0]
    assert descriptor["row_count"] == 3
    assert len(descriptor["sha256"]) == 64
    assert len(descriptor["schema_fingerprint"]) == 64
    assert descriptor["logical_content_hash"] == status["release"]["content_hash"]

    path = service.parquet.root / descriptor["path"]
    path.write_bytes(path.read_bytes() + b"tampered")
    assert service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519"
    )["status"] == "CORRUPT"


def test_parquet_logical_mutation_is_rejected_before_release_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    original_describe = service.parquet.describe
    mutated = False

    def mutate_before_describe(
        path: Path, *, logical_content_hash: str, created_at: datetime
    ) -> ReferenceFileDescriptor:
        nonlocal mutated
        if not mutated and "market_reference_canonical" in path.parts:
            table = pq.read_table(path)
            records = table.column("record_json").to_pylist()
            payload = json.loads(records[0])
            payload["close"] = "1"
            records[0] = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            table = table.set_column(
                table.schema.get_field_index("record_json"),
                "record_json",
                pa.array(records, type=pa.binary()),
            )
            pq.write_table(table, path, compression="zstd")
            mutated = True
        return original_describe(
            path,
            logical_content_hash=logical_content_hash,
            created_at=created_at,
        )

    monkeypatch.setattr(service.parquet, "describe", mutate_before_describe)
    with pytest.raises(ValueError, match="logical content"):
        service.sync_daily(
            "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
        )

    with service.state.connect() as connection:
        release_count = connection.execute(
            "SELECT count(*) FROM market_reference_release"
        ).fetchone()[0]
        assert release_count == 0
        assert connection.execute("SELECT count(*) FROM market_reference_head").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM artifact_registry WHERE type='DatasetReleaseManifest'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM checkpoint WHERE scope_type='market-reference'"
        ).fetchone()[0] == 0


def test_corporate_actions_link_only_official_document_and_never_touch_ledger(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    linked = service.sync_corporate_actions(
        "600519", Market.XSHG, date(2026, 1, 1), date(2026, 7, 22)
    )
    bjse = service.sync_corporate_actions(
        "920015", Market.BJSE, date(2026, 1, 1), date(2026, 7, 22)
    )

    assert linked.status == "PARTIAL"
    assert "TERMS_NOT_VERIFIED" in linked.reason_codes
    assert bjse.status == "PARTIAL"
    assert "OFFICIAL_EVIDENCE_UNAVAILABLE" in bjse.reason_codes
    linked_status = service.status(
        ReferenceDatasetKind.CORPORATE_ACTION, "XSHG:600519"
    )
    path = linked_status["release"]["canonical_files"][0]["path"]
    row = json.loads(
        pq.ParquetFile(service.parquet.root / path)
        .read()
        .column("record_json")
        .to_pylist()[0]
    )
    assert row["status"] == "OFFICIAL_DOCUMENT_LINKED"
    assert row["official_document_url"].startswith("https://static.cninfo.com.cn/")
    assert row["ledger_eligible"] is False
    with service.state.connect() as connection:
        assert connection.execute("SELECT count(*) FROM corporate_action_event").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM journal").fetchone()[0] == 0


def test_recorded_cli_reference_vertical_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "CLI运行时"))
    runner = CliRunner()

    master = runner.invoke(app, ["sync-instruments"])
    calendar = runner.invoke(
        app,
        [
            "sync-calendar",
            "--exchange",
            "XSHG",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-22",
        ],
    )
    daily = runner.invoke(
        app,
        [
            "sync-daily",
            "600519",
            "--market",
            "XSHG",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-22",
        ],
    )
    audit = runner.invoke(app, ["reference-audit"])

    assert master.exit_code == calendar.exit_code == daily.exit_code == audit.exit_code == 0
    assert json.loads(master.stdout)["coverage"]["record_count"] == 6
    assert json.loads(daily.stdout)["coverage"]["record_count"] == 3
    assert json.loads(audit.stdout)["status"] == "PASS"


def test_revision_creates_new_release_and_old_as_of_remains_reproducible(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        fixtures,
    )
    first = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    fixture = fixtures / "baostock" / "market_daily_unadjusted.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["request_finished_at"] = "2026-07-23T00:00:01Z"
    payload["rows"][-1][3] = "1495.00"
    payload["rows"][-1][5] = "1491.00"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    second = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )

    assert first.release_id != second.release_id
    historical = service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED,
        "XSHG:600519",
        as_of=datetime(2026, 7, 22, 12, tzinfo=UTC),
    )
    current = service.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")
    assert historical["release"]["release_id"] == first.release_id
    assert current["release"]["release_id"] == second.release_id
    assert current["release"]["previous_release_id"] == first.release_id


def test_migrated_v1_head_is_unverified_but_allows_v2_forward_recovery(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in sorted(PROJECT_ROOT.joinpath("migrations").glob("*.sql")):
        if source.name[:4] <= "0038":
            shutil.copy2(source, migrations / source.name)
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    parquet = ReferenceParquetStore(tmp_path / "parquet")
    provider = BaoStockReferenceProvider(objects, state, fixtures / "baostock")
    envelope, snapshot = provider.fetch(
        "market.daily_unadjusted",
        {
            "symbol": "600519",
            "market": "XSHG",
            "start": "2026-07-20",
            "end": "2026-07-22",
            "adjustflag": "3",
        },
    )
    records: list[ReferenceRecord] = list(
        _parse_baostock_daily(
            envelope,
            snapshot.snapshot_id,
            "600519",
            Market.XSHG,
            date(2026, 7, 20),
            date(2026, 7, 22),
        )
    )
    coverage = ReferenceCoverage(
        created_at=snapshot.available_to_system_at,
        requested_start=date(2026, 7, 20),
        requested_end=date(2026, 7, 22),
        actual_start=date(2026, 7, 20),
        actual_end=date(2026, 7, 22),
        record_count=len(records),
        status=ReferenceCoverageStatus.COMPLETE,
    )
    raw_snapshot_ids = [snapshot.snapshot_id]
    batch_id = content_hash(
        {
            "dataset_kind": ReferenceDatasetKind.DAILY_UNADJUSTED.value,
            "scope_key": "XSHG:600519",
            "provider_id": provider.provider_id,
            "raw_snapshot_ids": raw_snapshot_ids,
            "records": [item.model_dump(mode="json", exclude={"created_at"}) for item in records],
        }
    )
    batch = ReferenceBatch(
        created_at=snapshot.available_to_system_at,
        batch_id=batch_id,
        dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key="XSHG:600519",
        provider_id=provider.provider_id,
        raw_snapshot_ids=raw_snapshot_ids,
        records=records,
        coverage=coverage,
        pit_status=ReferencePitStatus.RECONSTRUCTED,
        available_to_system_at=snapshot.available_to_system_at,
    )
    observation_path = parquet.write_observation(batch)
    canonical_path, records_hash = parquet.write_canonical(batch)
    release_identity = {
        "dataset_kind": ReferenceDatasetKind.DAILY_UNADJUSTED.value,
        "scope_key": "XSHG:600519",
        "provider_id": provider.provider_id,
        "batch_id": batch_id,
        "content_hash": records_hash,
        "previous_release_id": None,
        "available_to_system_at": snapshot.available_to_system_at.isoformat(),
    }
    release_id = content_hash(release_identity)
    legacy_manifest = {
        "schema_version": "market-reference-release-v1",
        "created_at": snapshot.available_to_system_at.isoformat(),
        "release_id": release_id,
        "content_hash": records_hash,
        "dataset_kind": ReferenceDatasetKind.DAILY_UNADJUSTED.value,
        "scope_key": "XSHG:600519",
        "provider_id": provider.provider_id,
        "batch_id": batch_id,
        "previous_release_id": None,
        "raw_snapshot_ids": raw_snapshot_ids,
        "observation_files": [parquet.relative(observation_path)],
        "canonical_files": [parquet.relative(canonical_path)],
        "coverage": coverage.model_dump(mode="json"),
        "pit_status": ReferencePitStatus.RECONSTRUCTED.value,
        "available_to_system_at": snapshot.available_to_system_at.isoformat(),
    }
    manifest_object = objects.put_bytes(canonical_json_bytes(legacy_manifest))
    artifact_id = f"market-reference:{release_id}"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="DatasetReleaseManifest",
        schema_version="market-reference-release-v1",
        object_hash=manifest_object.sha256,
        input_hashes=[*raw_snapshot_ids, records_hash],
    )
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO market_reference_release(release_id,dataset_kind,scope_key,"
            "provider_id,batch_id,content_hash,previous_release_id,manifest_artifact_id,"
            "manifest_object_hash,available_to_system_at,coverage_status,pit_status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                release_id,
                ReferenceDatasetKind.DAILY_UNADJUSTED.value,
                "XSHG:600519",
                provider.provider_id,
                batch_id,
                records_hash,
                None,
                artifact_id,
                manifest_object.sha256,
                snapshot.available_to_system_at.isoformat(),
                coverage.status.value,
                ReferencePitStatus.RECONSTRUCTED.value,
                snapshot.available_to_system_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO market_reference_head(dataset_kind,scope_key,release_id,updated_at) "
            "VALUES(?,?,?,?)",
            (
                ReferenceDatasetKind.DAILY_UNADJUSTED.value,
                "XSHG:600519",
                release_id,
                snapshot.available_to_system_at.isoformat(),
            ),
        )
    state.set_checkpoint(
        scope_type="market-reference",
        scope_key="DAILY_UNADJUSTED:XSHG:600519",
        cursor={"release_id": release_id, "content_hash": records_hash},
        status="SUCCEEDED",
        object_hash=manifest_object.sha256,
    )

    shutil.copy2(
        PROJECT_ROOT / "migrations" / "0039_market_reference_release_integrity.sql",
        migrations / "0039_market_reference_release_integrity.sql",
    )
    assert state.migrate() == ["0039"]
    service = MarketReferenceService(state, objects, parquet, fixtures)
    assert service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519"
    )["status"] == "UNVERIFIED_LEGACY"

    fixture = fixtures / "baostock" / "market_daily_unadjusted.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["request_finished_at"] = "2026-07-22T07:05:00Z"
    payload["result_error_message"] = "v2 recovery"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    recovered = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )

    assert recovered.release_id and recovered.release_id != release_id
    current = service.status(ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600519")
    assert current["status"] == "AVAILABLE"
    assert current["release"]["previous_release_id"] == release_id
    historical = service.status(
        ReferenceDatasetKind.DAILY_UNADJUSTED,
        "XSHG:600519",
        as_of=datetime(2026, 7, 22, 7, 1, tzinfo=UTC),
    )
    assert historical["status"] == "UNVERIFIED_LEGACY"
    audit = service.audit()
    assert audit["status"] == "PASS"
    assert "LEGACY_UNVERIFIED_RELEASE" in audit["reason_codes"]


def test_sql_failure_rolls_back_release_head_artifact_and_checkpoint_but_keeps_orphans(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    with service.state.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_reference_head BEFORE INSERT ON market_reference_head "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
        service.sync_daily(
            "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
        )
    assert any(service.objects.root.rglob("*"))
    assert any(service.parquet.root.rglob("*.parquet"))
    with service.state.connect() as connection:
        release_count = connection.execute(
            "SELECT count(*) FROM market_reference_release"
        ).fetchone()[0]
        assert release_count == 0
        assert connection.execute("SELECT count(*) FROM market_reference_head").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM artifact_registry WHERE type='DatasetReleaseManifest'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM checkpoint WHERE scope_type='market-reference'"
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_reference_head")
    recovered = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    assert recovered.release_id is not None


def test_reference_provider_lease_is_cross_instance_renewable_and_fenced(
    tmp_path: Path,
) -> None:
    first = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    first.migrate()
    second = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    now = datetime.now(UTC)
    token = first.acquire_reference_provider_lease(
        "provider:test", "owner-a", now=now, lease_until=now + timedelta(seconds=30)
    )
    assert token == 1
    assert second.acquire_reference_provider_lease(
        "provider:test", "owner-b", now=now, lease_until=now + timedelta(seconds=30)
    ) is None
    assert first.renew_reference_provider_lease(
        "provider:test",
        "owner-a",
        token,
        now=now,
        lease_until=now + timedelta(seconds=45),
    )
    assert first.release_reference_provider_lease(
        "provider:test", "owner-a", token, now=now
    )
    next_token = second.acquire_reference_provider_lease(
        "provider:test", "owner-b", now=now, lease_until=now + timedelta(seconds=30)
    )
    assert next_token == 2
    assert not first.renew_reference_provider_lease(
        "provider:test",
        "owner-a",
        token,
        now=now,
        lease_until=now + timedelta(seconds=60),
    )


def test_same_logical_content_with_new_raw_provenance_creates_new_release(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        fixtures,
    )
    first = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    fixture = fixtures / "baostock" / "market_daily_unadjusted.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["request_finished_at"] = "2026-07-22T07:05:00Z"
    payload["result_error_message"] = "same facts refreshed"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    second = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )

    assert first.release_id != second.release_id
    assert first.raw_snapshot_ids != second.raw_snapshot_ids
    rows = state.list_market_reference_releases(
        ReferenceDatasetKind.DAILY_UNADJUSTED.value, "XSHG:600519"
    )
    assert len(rows) == 2
    assert len({row["content_hash"] for row in rows}) == 1


def test_dual_provider_failure_is_not_empty_or_published(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    bao = fixtures / "baostock" / "market_daily_unadjusted.json"
    bao_payload = json.loads(bao.read_text(encoding="utf-8"))
    bao_payload.update(
        {"rows": [], "row_contexts": [], "complete": False, "result_error_code": "NETWORK"}
    )
    bao.write_text(json.dumps(bao_payload, ensure_ascii=False), encoding="utf-8")
    east = fixtures / "eastmoney" / "daily_unadjusted.json"
    east.write_text(json.dumps({"rc": 1, "data": None}), encoding="utf-8")
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        fixtures,
    )

    report = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    assert report.status is ReferenceCoverageStatus.FAILED
    assert report.release_id is None
    assert state.list_market_reference_releases() == []


def test_audit_detects_checkpoint_corruption(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = service.sync_daily(
        "600519", Market.XSHG, date(2026, 7, 20), date(2026, 7, 22)
    )
    assert report.release_id
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE checkpoint SET cursor_json='{}' WHERE scope_type='market-reference'"
        )
    audit = service.audit()
    assert audit["status"] == "FAIL"
    assert audit["corrupt_release_ids"] == [report.release_id]
    assert "CHECKPOINT_INVALID" in audit["reason_codes"]


def test_corporate_action_official_document_is_linked_one_to_one(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    fixture = fixtures / "baostock" / "corporate_actions_structured_hint.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    duplicate = list(payload["rows"][0])
    duplicate[5] = "20.000"
    payload["rows"].append(duplicate)
    payload["row_contexts"].append({"report_period": "2025"})
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        fixtures,
    )

    report = service.sync_corporate_actions(
        "600519", Market.XSHG, date(2026, 1, 1), date(2026, 7, 22)
    )
    status = service.status(ReferenceDatasetKind.CORPORATE_ACTION, report.scope_key)
    descriptor = status["release"]["canonical_files"][0]
    records = [
        json.loads(raw)
        for raw in pq.ParquetFile(service.parquet.root / descriptor["path"])
        .read()
        .column("record_json")
        .to_pylist()
    ]
    assert [item["status"] for item in records].count("OFFICIAL_DOCUMENT_LINKED") == 0
    assert [item["status"] for item in records].count("DISCOVERED_STRUCTURED") == 2
    assert "OFFICIAL_MATCH_NOT_UNIQUE" in report.reason_codes
    assert all(item["ledger_eligible"] is False for item in records)


def test_reference_cli_failure_is_fixed_private_and_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    private = r"C:\Users\private\profile token=secret body=raw"

    def fail(*_args: object, **_kwargs: object) -> ReferenceSyncReport:
        raise ValueError(private)

    monkeypatch.setattr(MarketReferenceService, "sync_daily", fail)
    result = CliRunner().invoke(
        app,
        [
            "sync-daily",
            "600519",
            "--market",
            "XSHG",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-22",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "failure_code": "REFERENCE_SYNC_FAILED",
        "status": "FAILED",
    }
    assert private not in result.stdout


def test_reference_cli_failed_report_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    failed = ReferenceSyncReport(
        command="sync-daily",
        status=ReferenceCoverageStatus.FAILED,
        dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key="XSHG:600519",
        provider_id="baostock-reference",
        coverage=ReferenceCoverage(
            record_count=0,
            status=ReferenceCoverageStatus.FAILED,
            reason_codes=["BAOSTOCK_INCOMPLETE", "EASTMONEY_FALLBACK_FAILED"],
        ),
        pit_status=ReferencePitStatus.UNVERIFIED,
        reason_codes=["BAOSTOCK_INCOMPLETE", "EASTMONEY_FALLBACK_FAILED"],
    )
    monkeypatch.setattr(
        MarketReferenceService, "sync_daily", lambda *_args, **_kwargs: failed
    )
    result = CliRunner().invoke(
        app,
        [
            "sync-daily",
            "600519",
            "--market",
            "XSHG",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-22",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "FAILED"
