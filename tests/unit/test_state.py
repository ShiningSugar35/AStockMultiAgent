from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.state import StateStore
from astock.schemas import (
    CollectionCheckpoint,
    CollectionTerminalCondition,
    FetchStatus,
    SourceSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_migration_is_idempotent_and_configures_sqlite(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    assert state.migrate() == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
        "0016",
        "0017",
        "0018",
        "0019",
        "0020",
        "0021",
        "0022",
        "0023",
        "0024",
        "0025",
        "0026",
        "0027",
        "0028",
        "0029",
        "0030",
        "0031",
        "0032",
        "0033",
        "0034",
        "0035",
        "0036",
        "0037",
        "0038",
        "0039",
        "0040",
    ]
    assert state.migrate() == []
    with state.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert state.integrity_check() == "ok"
    with state.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_audit_run'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='committee_decision_index'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shadow_study_index'"
        ).fetchone()


def test_market_reference_migration_upgrades_cleanly_from_0037(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 38):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    assert state.migrate()[-1] == "0037"

    migration = PROJECT_ROOT / "migrations" / "0038_market_reference_releases.sql"
    shutil.copy(migration, migrations / migration.name)
    assert state.migrate() == ["0038"]

    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                "legacy-manifest",
                "DatasetReleaseManifest",
                "market-reference-release-manifest-v1",
                "manifest-hash",
                '["snapshot-id","content-hash"]',
                "2026-07-22T15:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO market_reference_release(release_id,dataset_kind,scope_key,"
            "provider_id,batch_id,content_hash,previous_release_id,manifest_artifact_id,"
            "manifest_object_hash,available_to_system_at,coverage_status,pit_status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-release",
                "DAILY_UNADJUSTED",
                "XSHG:600519",
                "baostock-reference",
                "legacy-batch",
                "content-hash",
                None,
                "legacy-manifest",
                "manifest-hash",
                "2026-07-22T15:00:00+00:00",
                "COMPLETE",
                "CERTIFIED",
                "2026-07-22T15:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO market_reference_head(dataset_kind,scope_key,release_id,updated_at) "
            "VALUES(?,?,?,?)",
            (
                "DAILY_UNADJUSTED",
                "XSHG:600519",
                "legacy-release",
                "2026-07-22T15:01:00+00:00",
            ),
        )

    integrity = PROJECT_ROOT / "migrations" / "0039_market_reference_release_integrity.sql"
    shutil.copy(integrity, migrations / integrity.name)
    assert state.migrate() == ["0039"]
    assert state.migrate() == []
    with state.connect() as connection:
        row = connection.execute(
            "SELECT manifest_schema_version,raw_snapshot_ids_json,coverage_json,pit_status "
            "FROM market_reference_release WHERE release_id='legacy-release'"
        ).fetchone()
        assert tuple(row) == (
            "market-reference-release-manifest-v1",
            '["snapshot-id"]',
            '{"legacy_0038":true,"status":"COMPLETE"}',
            "UNVERIFIED",
        )
        assert connection.execute(
            "SELECT release_id FROM market_reference_head WHERE dataset_kind=? AND scope_key=?",
            ("DAILY_UNADJUSTED", "XSHG:600519"),
        ).fetchone()[0] == "legacy-release"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reference_provider_lease'"
        ).fetchone()


def test_financial_source_migration_upgrades_cleanly_from_0039(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 40):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    assert state.migrate()[-1] == "0039"

    migration = PROJECT_ROOT / "migrations" / "0040_financial_source_release.sql"
    shutil.copy(migration, migrations / migration.name)
    assert state.migrate() == ["0040"]
    assert state.migrate() == []
    with state.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(financial_source_release)"
            ).fetchall()
        }
        assert {
            "instrument_id",
            "instrument_release_id",
            "instrument_manifest_artifact_id",
            "official_index_snapshot_id",
        } <= columns
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='financial_source_head'"
        ).fetchone()


def test_checkpoint_updates_only_one_scope_row(state: StateStore) -> None:
    first = state.set_checkpoint(
        scope_type="author",
        scope_key="mr-dang-77:answers",
        cursor={"page": 1},
        status="RUNNING",
    )
    second = state.set_checkpoint(
        scope_type="author",
        scope_key="mr-dang-77:answers",
        cursor={"page": 2},
        status="SUCCEEDED",
    )
    checkpoint = state.get_checkpoint("author", "mr-dang-77:answers")
    assert first == second
    assert checkpoint is not None
    assert checkpoint["cursor"] == {"page": 2}


def test_job_attempt_lifecycle_is_auditable(state: StateStore) -> None:
    job_id = state.create_job("fixture", "input-hash")
    attempt_id = state.start_attempt(job_id)
    state.finish_attempt(attempt_id, error_class="NETWORK", retryable=True)
    state.finish_job(job_id, "RETRYABLE_FAILED")
    with state.connect() as connection:
        job = connection.execute("SELECT status FROM job WHERE job_id=?", (job_id,)).fetchone()
        attempt = connection.execute(
            "SELECT ended_at,error_class,retryable FROM job_attempt WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
    assert job["status"] == "RETRYABLE_FAILED"
    assert attempt["ended_at"] is not None
    assert attempt["error_class"] == "NETWORK"
    assert attempt["retryable"] == 1


def test_source_snapshot_round_trips_from_split_index_tables(
    state: StateStore,
) -> None:
    snapshot = SourceSnapshot(
        snapshot_id="snapshot:round-trip",
        source_id="source:test",
        object_sha256="a" * 64,
        fetched_at=datetime(2026, 7, 17, tzinfo=UTC),
        available_to_system_at=datetime(2026, 7, 17, 0, 0, 1, tzinfo=UTC),
        source_url="https://example.invalid/data",
        mime="application/json",
        byte_size=10,
        headers_hash="b" * 64,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="TEST",
    )
    state.register_snapshot(snapshot)

    restored = state.get_snapshot(snapshot.snapshot_id)
    assert restored is not None
    assert restored.model_dump(exclude={"created_at"}) == snapshot.model_dump(
        exclude={"created_at"}
    )


def test_cursor_idempotency_and_collection_interfaces(state: StateStore) -> None:
    cursor_id = state.set_cursor("zhihu", "mr-dang-77:answers", {"offset": 20})
    assert state.set_cursor("zhihu", "mr-dang-77:answers", {"offset": 40}) == cursor_id
    cursor = state.get_cursor("zhihu", "mr-dang-77:answers")
    assert cursor is not None
    assert cursor["value"] == {"offset": 40}

    assert state.register_idempotency("command-1", "collect", "artifact-1")
    assert not state.register_idempotency("command-1", "collect", "artifact-1")
    with pytest.raises(ValueError, match="collision"):
        state.register_idempotency("command-1", "collect", "artifact-2")

    scope_id = state.upsert_collection_scope(
        author_id="mr-dang-77",
        content_type="answers",
        status="RUNNING",
        last_cursor="40",
    )
    gap_id = state.record_collection_gap(
        scope_id=scope_id,
        cursor={"offset": 40},
        failure_class="RATE_LIMITED",
        retryable=True,
        status="OPEN",
    )
    with state.connect() as connection:
        gap = connection.execute(
            "SELECT retryable,status FROM collection_gap WHERE gap_id=?", (gap_id,)
        ).fetchone()
    assert gap["retryable"] == 1
    assert gap["status"] == "OPEN"


def test_gap_event_migration_preserves_rows_and_tracks_direct_state_changes(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 34):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    scope_id = state.upsert_collection_scope(
        author_id="zhihu:temporal-gap",
        content_type="answers",
        status="PARTIAL",
    )
    gap_id = state.record_collection_gap(
        scope_id=scope_id,
        cursor={"offset": 0},
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )

    migration = PROJECT_ROOT / "migrations" / "0034_knowledge_gap_state_events.sql"
    shutil.copy(migration, migrations / migration.name)
    assert state.migrate() == ["0034"]
    with state.connect() as connection:
        assert connection.execute(
            "SELECT status FROM collection_gap WHERE gap_id=?", (gap_id,)
        ).fetchone()[0] == "OPEN"
        assert connection.execute(
            "SELECT status FROM collection_gap_state_event WHERE gap_id=?", (gap_id,)
        ).fetchall()[0][0] == "OPEN"
        reliable_from = connection.execute(
            "SELECT reliable_from FROM collection_gap_temporal_meta WHERE singleton=1"
        ).fetchone()[0]
        assert reliable_from.endswith("+00:00")

    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap SET status='OPEN' WHERE gap_id=?",
            (gap_id,),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap_state_event WHERE gap_id=?", (gap_id,)
        ).fetchone()[0] == 1
        connection.execute(
            "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?",
            (gap_id,),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap_state_event WHERE gap_id=?", (gap_id,)
        ).fetchone()[0] == 2
        connection.execute(
            "INSERT OR REPLACE INTO collection_gap("
            "gap_id,scope_id,cursor_json,failure_class,retryable,status) "
            "VALUES(?,?,?,?,?,?)",
            (gap_id, scope_id, '{"offset":0}', "AUTH_REQUIRED", 0, "OPEN"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap_state_event WHERE gap_id=?", (gap_id,)
        ).fetchone()[0] == 3


def test_collection_checkpoint_round_trips_every_required_cursor_level(
    state: StateStore,
) -> None:
    checkpoint = CollectionCheckpoint(
        author="mr-dang-77",
        content_type="answers",
        listing_page=3,
        listing_cursor="offset:40",
        content_id="answer-123",
        comment_parent_id="root-456",
        comment_page=2,
        comment_cursor="comment:20",
        nested_reply_cursor="reply:8",
        terminal_condition=CollectionTerminalCondition.PARTIAL,
    )
    checkpoint_id = state.set_collection_checkpoint(
        checkpoint,
        status="RUNNING",
        object_hash="a" * 64,
    )
    state.set_collection_checkpoint(
        checkpoint.model_copy(update={"comment_page": 3, "comment_cursor": "comment:40"}),
        status="RUNNING",
        object_hash="b" * 64,
    )
    recovered = state.get_collection_checkpoint(
        "mr-dang-77",
        "answers",
        "answer-123",
        "root-456",
    )
    assert recovered is not None
    assert recovered.comment_page == 3
    assert recovered.comment_cursor == "comment:40"
    with state.connect() as connection:
        rows = connection.execute(
            "SELECT checkpoint_id FROM checkpoint WHERE scope_type='author-collection'"
        ).fetchall()
    assert [row["checkpoint_id"] for row in rows] == [checkpoint_id]


def test_root_and_child_comment_checkpoints_use_distinct_scopes(state: StateStore) -> None:
    root = CollectionCheckpoint(
        author="zhihu:test",
        content_type="answers",
        listing_page=0,
        content_id="answer-1",
        comment_page=1,
        comment_cursor="root-next",
    )
    child = root.model_copy(
        update={
            "comment_parent_id": "comment-1",
            "nested_reply_cursor": "child-next",
        }
    )
    root_id = state.set_collection_checkpoint(root, status="RUNNING")
    child_id = state.set_collection_checkpoint(child, status="RUNNING")

    assert root_id != child_id
    assert state.get_collection_checkpoint(
        "zhihu:test", "answers", "answer-1"
    ) == root
    assert state.get_collection_checkpoint(
        "zhihu:test", "answers", "answer-1", "comment-1"
    ) == child


def test_lease_lock_can_be_recovered_after_expiry(state: StateStore) -> None:
    now = datetime.now(UTC)
    assert state.acquire_lock("sync", "run-1", now + timedelta(minutes=5))
    assert not state.acquire_lock("sync", "run-2", now + timedelta(minutes=6))
    with state.transaction() as connection:
        connection.execute(
            "UPDATE lease_lock SET lease_until=? WHERE lock_key=?",
            ((now - timedelta(seconds=1)).isoformat(), "sync"),
        )
    assert state.acquire_lock("sync", "run-2", now + timedelta(minutes=6))


def test_failed_migration_rolls_back_partial_schema(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(PROJECT_ROOT / "migrations" / "0001_foundation.sql", migrations)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE should_rollback(id INTEGER);\nTHIS IS INVALID;",
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.DatabaseError):
        state.migrate()
    with state.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
        ).fetchone()
    assert exists is None


def test_private_draft_version_migration_preserves_rows_and_allows_new_version(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 21):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    now = "2026-07-17T00:00:00+00:00"
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO knowledge_distillation_run VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "run:fixture",
                "author:fixture",
                "classification-v1",
                "COMPLETE",
                "input-set",
                1,
                1,
                "{}",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_distillation_unit VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "unit:fixture",
                "run:fixture",
                "author:fixture",
                "source:fixture",
                "snapshot:fixture",
                "source-unit:fixture",
                1,
                1,
                "a" * 64,
                None,
                "KEEP_CANDIDATE",
                "classification-v1",
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO private_viewpoint_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "draft:v1",
                "run:fixture",
                "author:fixture",
                "STOCK_SELECTION",
                "unit:fixture",
                "a" * 64,
                "b" * 64,
                "SOURCE_EXCERPT_NOT_SYNTHESIZED",
                "draft-v1",
                "PENDING",
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO private_skill_candidate_draft VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate:v1",
                "run:fixture",
                "author:fixture",
                "CANDIDATE_SELECTION",
                "STOCK_SELECTION",
                "c" * 64,
                "draft-v1",
                "NOT_RUN",
                "PENDING",
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO private_skill_candidate_viewpoint_ref VALUES(?,?,?)",
            ("candidate:v1", 1, "draft:v1"),
        )
        connection.execute(
            "INSERT INTO private_skill_candidate_unit_ref VALUES(?,?,?)",
            ("candidate:v1", 1, "unit:fixture"),
        )
        connection.execute(
            "INSERT INTO author_draft_generation_report VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "report:v1",
                "run:fixture",
                "author:fixture",
                "draft-v1",
                1,
                1,
                "PENDING",
                "d" * 64,
                "{}",
                now,
            ),
        )

    source = next((PROJECT_ROOT / "migrations").glob("0021_*.sql"))
    shutil.copy(source, migrations / source.name)
    assert state.migrate() == ["0021"]
    with state.transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM private_viewpoint_draft"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM private_skill_candidate_viewpoint_ref"
        ).fetchone()[0] == 1
        connection.execute(
            "INSERT INTO private_viewpoint_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "draft:v2",
                "run:fixture",
                "author:fixture",
                "STOCK_SELECTION",
                "unit:fixture",
                "a" * 64,
                "e" * 64,
                "SOURCE_EXCERPT_NOT_SYNTHESIZED",
                "draft-v2",
                "PENDING",
                "{}",
                now,
            ),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM private_viewpoint_draft"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_generic_evidence_migration_preserves_existing_page_links(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 8):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    now = "2026-07-16T00:00:00+00:00"
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO source_snapshot_index VALUES(?,?,?,?,?,?)",
            ("snapshot:legacy", "source:legacy", "a" * 64, now, now, "SUCCEEDED"),
        )
        connection.execute(
            "INSERT INTO source_snapshot_detail VALUES(?,?,?,?,?,?)",
            ("snapshot:legacy", None, "application/pdf", 1, None, "TEST",),
        )
        connection.execute(
            "INSERT INTO source_document VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "document:legacy",
                "Legacy fixture",
                "TEST",
                "ANNOUNCEMENT",
                "[]",
                now,
                None,
                "legacy-disclosure",
                "https://example.invalid/legacy.pdf",
                "TEST",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO document_snapshot VALUES(?,?,?)",
            ("document:legacy", "snapshot:legacy", now),
        )
        connection.execute(
            "INSERT INTO document_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "page:legacy",
                "document:legacy",
                "snapshot:legacy",
                1,
                "parser-v1",
                "NATIVE_TEXT",
                "b" * 64,
                "b" * 64,
                1,
                "c" * 64,
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO evidence_record VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "evidence:legacy",
                "document:legacy",
                "snapshot:legacy",
                "page:legacy",
                1,
                "d" * 64,
                "d" * 64,
                now,
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO claim_record VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "claim:legacy",
                "subject:legacy",
                "predicate",
                now,
                "FACT",
                1.0,
                "VALIDATED",
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO claim_evidence_link VALUES(?,?,?,?,?,?,?)",
            (
                "claim:legacy",
                "evidence:legacy",
                "SUPPORT",
                1.0,
                "UNREVIEWED",
                "{}",
                now,
            ),
        )

    for version in (8, 9):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    assert state.migrate() == ["0008", "0009"]
    with state.connect() as connection:
        evidence = connection.execute(
            "SELECT source_unit_type,source_unit_index,page_id,block_id "
            "FROM evidence_record WHERE evidence_id='evidence:legacy'"
        ).fetchone()
        assert tuple(evidence) == ("PAGE", 1, "page:legacy", None)
        assert connection.execute("SELECT COUNT(*) FROM claim_evidence_link").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_checksum_ignores_only_line_end_and_eof_whitespace(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    source = PROJECT_ROOT / "migrations" / "0001_foundation.sql"
    target = migrations / source.name
    shutil.copy(source, target)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    state.migrate()
    target.write_text(target.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert state.migrate() == []
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "CREATE INDEX idx_job_status", "CREATE INDEX idx_job_status_changed"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="checksum changed"):
        state.migrate()
