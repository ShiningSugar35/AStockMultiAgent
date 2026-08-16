from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.cold_archive import KnowledgeColdArchiveService
from astock.knowledge.parquet_compaction import (
    ParquetKnowledgeCompactor,
    compacted_record_path,
)


def _build_archive_fixture(tmp_path: Path) -> tuple[StateStore, ObjectStore, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = StateStore(runtime / "state.sqlite", tmp_path / "migrations")
    objects = ObjectStore(runtime / "objects" / "sha256")
    with sqlite3.connect(state.path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE schema_migration(
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migration VALUES('0057','fixture','2026-08-16T00:00:00+00:00');

            CREATE TABLE knowledge_semantic_run(
                run_id TEXT PRIMARY KEY,
                run_json TEXT NOT NULL
            );
            CREATE TABLE knowledge_semantic_content_item(
                item_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
                item_json TEXT NOT NULL
            );
            CREATE TABLE knowledge_argument_unit(
                argument_unit_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
                item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
                unit_json TEXT NOT NULL
            );
            CREATE TABLE knowledge_visual_skill_no_skill(
                run_id TEXT NOT NULL,
                argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
                PRIMARY KEY(run_id, argument_unit_id)
            );
            CREATE TABLE knowledge_cold_archive(
                archive_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                manifest_object_hash TEXT NOT NULL UNIQUE,
                archive_path TEXT NOT NULL UNIQUE,
                source_latest_migration TEXT NOT NULL,
                archived_tables_json TEXT NOT NULL,
                protected_rows_json TEXT NOT NULL,
                archived_row_count INTEGER NOT NULL,
                archive_size_bytes INTEGER NOT NULL,
                source_db_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            INSERT INTO knowledge_semantic_run VALUES('run-keep','{}');
            INSERT INTO knowledge_semantic_content_item VALUES('item-keep','run-keep','{}');
            INSERT INTO knowledge_argument_unit VALUES('au-keep','run-keep','item-keep','{}');
            INSERT INTO knowledge_visual_skill_no_skill VALUES('visual-run','au-keep');

            INSERT INTO knowledge_semantic_run VALUES('run-old','{}');
            INSERT INTO knowledge_semantic_content_item VALUES('item-old','run-old','{}');
            INSERT INTO knowledge_argument_unit VALUES('au-old','run-old','item-old','{}');
            """
        )
    return state, objects, runtime


def test_cold_archive_preserves_hot_fk_closure_and_restores(tmp_path: Path) -> None:
    state, objects, runtime = _build_archive_fixture(tmp_path)
    service = KnowledgeColdArchiveService(state, objects, runtime_root=runtime)

    plan = service.plan()
    by_table = {item.table: item for item in plan.tables}
    assert by_table["knowledge_semantic_run"].protected_rows == 1
    assert by_table["knowledge_semantic_content_item"].protected_rows == 1
    assert by_table["knowledge_argument_unit"].protected_rows == 1
    assert plan.archive_row_count == 3

    result = service.archive()
    assert result["status"] == "ARCHIVED"
    assert result["archive_row_count"] == 3
    audit = cast(dict[str, object], result["audit"])
    assert audit["status"] == "PASS"

    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_semantic_run").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_semantic_content_item").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM knowledge_argument_unit").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    restored = service.restore(str(result["archive_id"]))
    assert restored == {
        "status": "RESTORED",
        "archive_id": result["archive_id"],
        "restored_rows": 3,
    }
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_semantic_run").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_semantic_content_item").fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM knowledge_argument_unit").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _write_one_row(path: Path, version_id: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([{"version_id": version_id, "value": value}]),
        path,
        compression="zstd",
    )


def test_parquet_compactor_merges_partition_and_accepts_delta(tmp_path: Path) -> None:
    parquet_root = tmp_path / "parquet"
    partition = (
        parquet_root
        / "knowledge_comments"
        / "author=test"
        / "content_type=answers"
        / "year=2026"
    )
    _write_one_row(partition / "v1.parquet", "v1", "one")
    _write_one_row(partition / "v2.parquet", "v2", "two")
    _write_one_row(partition / "v3.parquet", "v3", "three")

    compactor = ParquetKnowledgeCompactor(parquet_root)
    first = compactor.compact_dataset("knowledge_comments")
    assert first.rows == 3
    assert first.files_before == 3
    assert first.files_after == 1
    assert compactor.audit()["status"] == "PASS"
    compact = partition / "_compact-v1.parquet"
    assert compacted_record_path(partition / "v1.parquet", "v1") == compact

    _write_one_row(partition / "v4.parquet", "v4", "four")
    second = compactor.compact_dataset("knowledge_comments")
    assert second.rows == 4
    assert second.files_before == 2
    assert second.files_after == 1
    assert int(pq.ParquetFile(compact).metadata.num_rows) == 4
    assert compactor.audit()["status"] == "PASS"


def test_parquet_compactor_unifies_additive_schema_drift(tmp_path: Path) -> None:
    parquet_root = tmp_path / "parquet"
    partition = (
        parquet_root
        / "knowledge_content"
        / "author=test"
        / "content_type=thoughts"
        / "year=2023"
    )
    partition.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"version_id": "old", "value": "one"}]),
        partition / "old.parquet",
        compression="zstd",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [{"version_id": "new", "value": "two", "content_completeness": "FULL"}]
        ),
        partition / "new.parquet",
        compression="zstd",
    )

    result = ParquetKnowledgeCompactor(parquet_root).compact_dataset("knowledge_content")

    assert result.rows == 2
    compact = pq.ParquetFile(partition / "_compact-v1.parquet").read()
    rows = {str(row["version_id"]): row for row in compact.to_pylist()}
    assert rows["old"]["content_completeness"] is None
    assert rows["new"]["content_completeness"] == "FULL"


def test_state_vacuum_returns_free_pages(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", tmp_path / "migrations")
    with state.connect() as connection:
        connection.execute("CREATE TABLE payload(value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO payload(value) VALUES(?)",
            [("x" * 4096,) for _ in range(3000)],
        )
        connection.execute("DELETE FROM payload")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = state.path.stat().st_size

    result = state.vacuum()

    assert result["before_bytes"] == before
    assert result["after_bytes"] <= before
    assert result["freelist_count"] == 0
