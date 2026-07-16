from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.state import StateStore
from astock.schemas import CollectionCheckpoint, CollectionTerminalCondition

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


def test_collection_checkpoint_round_trips_every_required_cursor_level(
    state: StateStore,
) -> None:
    checkpoint = CollectionCheckpoint(
        author="mr-dang-77",
        content_type="answers",
        listing_page=3,
        listing_cursor="offset:40",
        content_id="answer-123",
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
    recovered = state.get_collection_checkpoint("mr-dang-77", "answers", "answer-123")
    assert recovered is not None
    assert recovered.comment_page == 3
    assert recovered.comment_cursor == "comment:40"
    with state.connect() as connection:
        rows = connection.execute(
            "SELECT checkpoint_id FROM checkpoint WHERE scope_type='author-collection'"
        ).fetchall()
    assert [row["checkpoint_id"] for row in rows] == [checkpoint_id]


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
