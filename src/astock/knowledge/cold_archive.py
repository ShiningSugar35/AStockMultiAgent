"""Cold-archive historical knowledge pipeline rows without weakening current runtime provenance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore, utc_now_text

_ARCHIVE_SCHEMA_VERSION = "knowledge-cold-archive-v1"

# Historical pipeline state that is no longer a Research Runtime dependency after KGA publication.
# Immutable current Direct/Visual Skill registries and raw source/version tables
# deliberately stay hot.
_ARCHIVE_TABLES = (
    "author_distillation_report",
    "author_draft_generation_report",
    "book_chart_unit",
    "book_cleaning_report",
    "book_image_evidence",
    "book_image_evidence_attempt",
    "book_image_ocr",
    "book_layout_atom",
    "book_method_coverage_report",
    "book_parse_report",
    "book_source_manifest",
    "book_visual_coverage_report",
    "book_visual_run",
    "book_visual_semantic_ref",
    "distillation_review_queue",
    "knowledge_argument_relation",
    "knowledge_argument_unit",
    "knowledge_argument_unit_paragraph_ref",
    "knowledge_author_skill_coverage",
    "knowledge_distillation_run",
    "knowledge_distillation_unit",
    "knowledge_embedding_manifest",
    "knowledge_keyword_screen",
    "knowledge_llm_batch",
    "knowledge_method_rule",
    "knowledge_method_rule_au_ref",
    "knowledge_paragraph_unit",
    "knowledge_review_decision",
    "knowledge_review_decision_candidate_range",
    "knowledge_reviewed_argument_decision_ref",
    "knowledge_reviewed_argument_paragraph_ref",
    "knowledge_reviewed_argument_relation",
    "knowledge_reviewed_argument_unit",
    "knowledge_reviewed_checkpoint",
    "knowledge_reviewed_coverage_report",
    "knowledge_reviewed_embedding_manifest",
    "knowledge_reviewed_semantic_run",
    "knowledge_reviewed_shadow_bundle",
    "knowledge_reviewed_skill",
    "knowledge_reviewed_skill_au_ref",
    "knowledge_reviewed_skill_rule_ref",
    "knowledge_reviewed_visual_ref",
    "knowledge_semantic_content_item",
    "knowledge_semantic_run",
    "knowledge_viewpoint_card",
    "knowledge_viewpoint_card_au_ref",
    "private_docx_parse_report",
    "private_skill_candidate_draft",
    "private_skill_candidate_unit_ref",
    "private_skill_candidate_viewpoint_ref",
    "private_viewpoint_draft",
)

_RECOMPUTABLE_PATHS = (
    Path("data/parquet/knowledge_semantic"),
    Path("data/parquet/knowledge_distillation"),
    Path("data/parquet/knowledge_reviewed"),
    Path("knowledge_semantic_packets"),
)


@dataclass(frozen=True, slots=True)
class ColdArchiveTablePlan:
    table: str
    total_rows: int
    protected_rows: int
    archive_rows: int


@dataclass(frozen=True, slots=True)
class ColdArchivePlan:
    latest_migration: str
    source_db_size_bytes: int
    tables: tuple[ColdArchiveTablePlan, ...]

    @property
    def archive_row_count(self) -> int:
        return sum(item.archive_rows for item in self.tables)

    @property
    def protected_row_count(self) -> int:
        return sum(item.protected_rows for item in self.tables)


class KnowledgeColdArchiveService:
    """Move obsolete semantic/distillation history to verified compressed Parquet cold storage."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        runtime_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.runtime_root = runtime_root.resolve()
        self.archive_root = self.runtime_root / "archive" / "knowledge-history"

    def plan(self) -> ColdArchivePlan:
        with self.state.connect() as connection:
            archive_tables = self._existing_archive_tables(connection)
            protected = self._protected_parent_rowids(connection, archive_tables)
            latest = self._latest_migration(connection)
            plans = tuple(
                ColdArchiveTablePlan(
                    table=table,
                    total_rows=(total := self._row_count(connection, table)),
                    protected_rows=len(protected.get(table, set())),
                    archive_rows=total - len(protected.get(table, set())),
                )
                for table in archive_tables
            )
        return ColdArchivePlan(
            latest_migration=latest,
            source_db_size_bytes=self.state.path.stat().st_size,
            tables=plans,
        )

    def archive(self) -> dict[str, object]:
        plan = self.plan()
        if plan.archive_row_count == 0:
            return {
                "status": "NOOP",
                "archive_row_count": 0,
                "protected_row_count": plan.protected_row_count,
            }

        self.archive_root.mkdir(parents=True, exist_ok=True)
        staging = self.archive_root / f".staging-{uuid4().hex}"
        tables_dir = staging / "tables"
        tables_dir.mkdir(parents=True, exist_ok=False)

        try:
            with self.state.connect() as connection:
                archive_tables = tuple(item.table for item in plan.tables)
                protected = self._protected_parent_rowids(connection, archive_tables)
                current = self._plan_from_connection(connection, archive_tables, protected)
                if current != plan:
                    raise RuntimeError("knowledge cold-archive source changed after planning")
                table_files = [
                    self._export_table(
                        connection,
                        table_plan,
                        protected.get(table_plan.table, set()),
                        tables_dir,
                    )
                    for table_plan in plan.tables
                    if table_plan.archive_rows > 0
                ]
                schema_rows = self._schema_manifest(connection, archive_tables)

            identity = {
                "schema_version": _ARCHIVE_SCHEMA_VERSION,
                "source_latest_migration": plan.latest_migration,
                "tables": [
                    {
                        "table": item["table"],
                        "rows": item["rows"],
                        "sha256": item["sha256"],
                    }
                    for item in table_files
                ],
                "protected_rows": {
                    item.table: item.protected_rows
                    for item in plan.tables
                    if item.protected_rows > 0
                },
            }
            archive_digest = content_hash(identity)
            archive_id = f"knowledge-history:{archive_digest}"
            final_dir = self.archive_root / archive_digest
            manifest = {
                "schema_version": _ARCHIVE_SCHEMA_VERSION,
                "archive_id": archive_id,
                "created_at": utc_now_text(),
                "source_database": str(self.state.path),
                "source_db_size_bytes": plan.source_db_size_bytes,
                "source_latest_migration": plan.latest_migration,
                "archive_row_count": plan.archive_row_count,
                "protected_row_count": plan.protected_row_count,
                "protected_rows": {
                    item.table: item.protected_rows
                    for item in plan.tables
                    if item.protected_rows > 0
                },
                "tables": table_files,
                "sqlite_schema": schema_rows,
                "recomputable_paths": [path.as_posix() for path in _RECOMPUTABLE_PATHS],
            }
            if final_dir.exists():
                existing_path = final_dir / "manifest.json"
                if not existing_path.is_file():
                    raise RuntimeError(f"cold archive identity collision: {archive_id}")
                existing_manifest = json.loads(existing_path.read_bytes())
                if _manifest_identity(existing_manifest) != identity:
                    raise RuntimeError(f"cold archive identity collision: {archive_id}")
                manifest = existing_manifest
                manifest_bytes = canonical_json_bytes(manifest)
                shutil.rmtree(staging)
            else:
                manifest_bytes = canonical_json_bytes(manifest)
                (staging / "manifest.json").write_bytes(manifest_bytes)
                os.replace(staging, final_dir)
            manifest_object = self.objects.put_bytes(manifest_bytes)

            archive_size = _tree_size(final_dir)
            self._delete_archived_rows(
                plan,
                archive_id=archive_id,
                manifest_object_hash=manifest_object.sha256,
                archive_path=final_dir,
                archive_size_bytes=archive_size,
            )
            removed_files = self.remove_recomputable_intermediates()
            audit = self.audit(archive_id)
            if audit["status"] != "PASS":
                raise RuntimeError("knowledge cold archive failed post-commit audit")
            return {
                "status": "ARCHIVED",
                "archive_id": archive_id,
                "manifest_object_hash": manifest_object.sha256,
                "archive_path": str(final_dir),
                "archive_size_bytes": archive_size,
                "archive_row_count": plan.archive_row_count,
                "protected_row_count": plan.protected_row_count,
                "removed_recomputable_bytes": sum(
                    int(str(item["bytes"])) for item in removed_files
                ),
                "removed_recomputable_paths": removed_files,
                "audit": audit,
            }
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def audit(self, archive_id: str | None = None) -> dict[str, object]:
        with self.state.connect() as connection:
            if archive_id is None:
                rows = connection.execute(
                    "SELECT * FROM knowledge_cold_archive ORDER BY created_at, archive_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM knowledge_cold_archive WHERE archive_id=?", (archive_id,)
                ).fetchall()
        if not rows:
            return {"status": "NOT_FOUND", "archive_count": 0, "archives": []}

        reports: list[dict[str, object]] = []
        failures = 0
        for row in rows:
            record = dict(row)
            manifest_hash = str(record["manifest_object_hash"])
            try:
                manifest = json.loads(self.objects.get_bytes(manifest_hash))
                archive_dir = Path(str(record["archive_path"]))
                if not archive_dir.is_absolute():
                    archive_dir = self.runtime_root / archive_dir
                file_failures: list[str] = []
                verified_rows = 0
                for item in manifest["tables"]:
                    path = archive_dir / str(item["relative_path"])
                    if not path.is_file() or _sha256_file(path) != str(item["sha256"]):
                        file_failures.append(str(item["table"]))
                        continue
                    actual_rows = int(pq.ParquetFile(path).metadata.num_rows)
                    if actual_rows != int(item["rows"]):
                        file_failures.append(str(item["table"]))
                        continue
                    verified_rows += actual_rows
                manifest_path = archive_dir / "manifest.json"
                manifest_matches = (
                    manifest_path.is_file()
                    and manifest_path.read_bytes() == canonical_json_bytes(manifest)
                )
                if not manifest_matches:
                    file_failures.append("manifest.json")
                status = (
                    "PASS"
                    if not file_failures
                    and verified_rows == int(record["archived_row_count"])
                    else "FAIL"
                )
                if status != "PASS":
                    failures += 1
                reports.append(
                    {
                        "archive_id": record["archive_id"],
                        "status": status,
                        "verified_rows": verified_rows,
                        "expected_rows": int(record["archived_row_count"]),
                        "file_failures": file_failures,
                    }
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                failures += 1
                reports.append(
                    {
                        "archive_id": record["archive_id"],
                        "status": "FAIL",
                        "verified_rows": 0,
                        "expected_rows": int(record["archived_row_count"]),
                        "file_failures": ["manifest"],
                    }
                )
        return {
            "status": "PASS" if failures == 0 else "FAIL",
            "archive_count": len(reports),
            "archives": reports,
        }

    def restore(self, archive_id: str) -> dict[str, object]:
        """Restore archived rows into the hot SQLite database, without deleting the cold copy."""

        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_cold_archive WHERE archive_id=?", (archive_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown knowledge cold archive: {archive_id}")
        record = dict(row)
        manifest = json.loads(self.objects.get_bytes(str(record["manifest_object_hash"])))
        archive_dir = Path(str(record["archive_path"]))
        if not archive_dir.is_absolute():
            archive_dir = self.runtime_root / archive_dir

        restored_rows = 0
        with closing(self.state.connect()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in manifest["tables"]:
                    table = str(item["table"])
                    path = archive_dir / str(item["relative_path"])
                    if _sha256_file(path) != str(item["sha256"]):
                        raise RuntimeError(f"cold archive file hash mismatch: {table}")
                    with pq.ParquetFile(path) as parquet:
                        columns = [str(name) for name in parquet.schema_arrow.names]
                        placeholders = ",".join("?" for _ in columns)
                        column_sql = ",".join(_quote(name) for name in columns)
                        sql = f'INSERT INTO "{table}"({column_sql}) VALUES({placeholders})'
                        for record_batch in parquet.iter_batches(batch_size=2000):
                            data = pa.Table.from_batches([record_batch])
                            rows = list(
                                zip(
                                    *(data[name].to_pylist() for name in columns),
                                    strict=True,
                                )
                            )
                            if rows:
                                connection.executemany(sql, rows)
                                restored_rows += len(rows)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"cold archive restore violates {len(violations)} SQLite foreign keys"
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        expected = int(record["archived_row_count"])
        if restored_rows != expected:
            raise RuntimeError(f"restored row count mismatch: {restored_rows} != {expected}")
        return {
            "status": "RESTORED",
            "archive_id": archive_id,
            "restored_rows": restored_rows,
        }

    def remove_recomputable_intermediates(self) -> list[dict[str, object]]:
        removed: list[dict[str, object]] = []
        for relative in _RECOMPUTABLE_PATHS:
            path = self.runtime_root / relative
            if not path.exists():
                continue
            size = _tree_size(path) if path.is_dir() else path.stat().st_size
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append({"path": str(relative), "bytes": size})
        return removed

    def _delete_archived_rows(
        self,
        plan: ColdArchivePlan,
        *,
        archive_id: str,
        manifest_object_hash: str,
        archive_path: Path,
        archive_size_bytes: int,
    ) -> None:
        archive_tables = tuple(item.table for item in plan.tables)
        with closing(self.state.connect()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                protected = self._protected_parent_rowids(connection, archive_tables)
                current = self._plan_from_connection(connection, archive_tables, protected)
                if current != plan:
                    raise RuntimeError("knowledge cold-archive source changed before deletion")
                self._install_protected_temp(connection, protected)
                plans_by_table = {item.table: item for item in plan.tables}
                for table in self._delete_order(connection, archive_tables):
                    item = plans_by_table[table]
                    if item.archive_rows <= 0:
                        continue
                    connection.execute(
                        f'DELETE FROM "{item.table}" '
                        "WHERE rowid NOT IN ("
                        "SELECT protected_rowid FROM temp.cold_archive_protected "
                        "WHERE table_name=?"
                        ")",
                        (item.table,),
                    )
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"cold archive would violate {len(violations)} SQLite foreign keys"
                    )
                connection.execute(
                    "INSERT INTO knowledge_cold_archive("
                    "archive_id,schema_version,manifest_object_hash,archive_path,"
                    "source_latest_migration,archived_tables_json,protected_rows_json,"
                    "archived_row_count,archive_size_bytes,source_db_size_bytes,status,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        archive_id,
                        _ARCHIVE_SCHEMA_VERSION,
                        manifest_object_hash,
                        str(archive_path),
                        plan.latest_migration,
                        canonical_json_bytes([asdict(item) for item in plan.tables]).decode(
                            "utf-8"
                        ),
                        canonical_json_bytes(
                            {
                                item.table: item.protected_rows
                                for item in plan.tables
                                if item.protected_rows > 0
                            }
                        ).decode("utf-8"),
                        plan.archive_row_count,
                        archive_size_bytes,
                        plan.source_db_size_bytes,
                        "READY",
                        utc_now_text(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _export_table(
        self,
        connection: sqlite3.Connection,
        plan: ColdArchiveTablePlan,
        protected: set[int],
        tables_dir: Path,
    ) -> dict[str, object]:
        self._install_protected_temp(connection, {plan.table: protected})
        columns = connection.execute(f'PRAGMA table_info("{plan.table}")').fetchall()
        names = [str(row[1]) for row in columns]
        schema = pa.schema(
            [
                pa.field(name, _arrow_type(str(row[2])), nullable=True)
                for name, row in zip(names, columns, strict=True)
            ]
        )
        query = (
            f'SELECT {", ".join(_quote(name) for name in names)} FROM "{plan.table}" '
            "WHERE rowid NOT IN ("
            "SELECT protected_rowid FROM temp.cold_archive_protected WHERE table_name=?"
            ") ORDER BY rowid"
        )
        cursor = connection.execute(query, (plan.table,))
        final_path = tables_dir / f"{plan.table}.parquet"
        temp_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.tmp")
        writer = pq.ParquetWriter(temp_path, schema, compression="zstd", use_dictionary=True)
        written = 0
        try:
            while True:
                batch = cursor.fetchmany(4000)
                if not batch:
                    break
                data = {
                    name: [_coerce(row[index], schema.field(index).type) for row in batch]
                    for index, name in enumerate(names)
                }
                table = pa.Table.from_pydict(data, schema=schema)
                writer.write_table(table)
                written += table.num_rows
        finally:
            writer.close()
        if written != plan.archive_rows:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                "cold archive row-count mismatch for "
                f"{plan.table}: {written} != {plan.archive_rows}"
            )
        with temp_path.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        return {
            "table": plan.table,
            "rows": written,
            "protected_rows": plan.protected_rows,
            "relative_path": f"tables/{final_path.name}",
            "sha256": _sha256_file(final_path),
            "bytes": final_path.stat().st_size,
        }

    def _existing_archive_tables(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return tuple(table for table in _ARCHIVE_TABLES if table in existing)

    def _plan_from_connection(
        self,
        connection: sqlite3.Connection,
        archive_tables: tuple[str, ...],
        protected: dict[str, set[int]],
    ) -> ColdArchivePlan:
        return ColdArchivePlan(
            latest_migration=self._latest_migration(connection),
            source_db_size_bytes=self.state.path.stat().st_size,
            tables=tuple(
                ColdArchiveTablePlan(
                    table=table,
                    total_rows=(total := self._row_count(connection, table)),
                    protected_rows=len(protected.get(table, set())),
                    archive_rows=total - len(protected.get(table, set())),
                )
                for table in archive_tables
            ),
        )

    def _protected_parent_rowids(
        self,
        connection: sqlite3.Connection,
        archive_tables: tuple[str, ...],
    ) -> dict[str, set[int]]:
        archive = set(archive_tables)
        all_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        protected: dict[str, set[int]] = defaultdict(set)
        queue: deque[tuple[str, int]] = deque()

        # Any surviving table is a hard root. Its FK parents inside the archive set must remain hot.
        for child in sorted(all_tables - archive):
            groups = _fk_groups(connection, child)
            for group in groups:
                parent = str(group[0][2])
                if parent not in archive:
                    continue
                join_parts = [
                    f"p.{_quote(str(fk[4]))}=c.{_quote(str(fk[3]))}"
                    for fk in sorted(group, key=lambda item: int(item[1]))
                ]
                rows = connection.execute(
                    f'SELECT DISTINCT p.rowid FROM "{child}" AS c '
                    f'JOIN "{parent}" AS p ON {" AND ".join(join_parts)}'
                ).fetchall()
                for row in rows:
                    rowid = int(row[0])
                    if rowid not in protected[parent]:
                        protected[parent].add(rowid)
                        queue.append((parent, rowid))

        # Preserve the complete FK parent closure of any protected archive row.
        while queue:
            table, rowid = queue.popleft()
            row = connection.execute(
                f'SELECT rowid AS __rowid__, * FROM "{table}" WHERE rowid=?', (rowid,)
            ).fetchone()
            if row is None:
                continue
            for group in _fk_groups(connection, table):
                parent = str(group[0][2])
                if parent not in archive:
                    continue
                clauses: list[str] = []
                values: list[object] = []
                for fk in sorted(group, key=lambda item: int(item[1])):
                    value = row[str(fk[3])]
                    if value is None:
                        clauses = []
                        break
                    clauses.append(f'{_quote(str(fk[4]))}=?')
                    values.append(value)
                if not clauses:
                    continue
                parents = connection.execute(
                    f'SELECT rowid FROM "{parent}" WHERE {" AND ".join(clauses)}', values
                ).fetchall()
                for parent_row in parents:
                    parent_rowid = int(parent_row[0])
                    if parent_rowid not in protected[parent]:
                        protected[parent].add(parent_rowid)
                        queue.append((parent, parent_rowid))
        return dict(protected)

    @staticmethod
    def _install_protected_temp(
        connection: sqlite3.Connection,
        protected: dict[str, set[int]],
    ) -> None:
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS cold_archive_protected("
            "table_name TEXT NOT NULL, protected_rowid INTEGER NOT NULL,"
            "PRIMARY KEY(table_name, protected_rowid)) WITHOUT ROWID"
        )
        connection.execute("DELETE FROM temp.cold_archive_protected")
        rows = [
            (table, rowid)
            for table, rowids in protected.items()
            for rowid in sorted(rowids)
        ]
        if rows:
            connection.executemany(
                "INSERT INTO temp.cold_archive_protected(table_name,protected_rowid) VALUES(?,?)",
                rows,
            )

    @staticmethod
    def _delete_order(
        connection: sqlite3.Connection,
        archive_tables: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Return child-before-parent order for archived tables; ignore self references."""

        archive = set(archive_tables)
        parents_by_child: dict[str, set[str]] = {table: set() for table in archive_tables}
        indegree: dict[str, int] = {table: 0 for table in archive_tables}
        for child in archive_tables:
            for group in _fk_groups(connection, child):
                parent = str(group[0][2])
                if parent not in archive or parent == child:
                    continue
                if parent in parents_by_child[child]:
                    continue
                parents_by_child[child].add(parent)
                indegree[parent] += 1
        ready = deque(sorted(table for table, degree in indegree.items() if degree == 0))
        ordered: list[str] = []
        while ready:
            child = ready.popleft()
            ordered.append(child)
            for parent in sorted(parents_by_child[child]):
                indegree[parent] -= 1
                if indegree[parent] == 0:
                    ready.append(parent)
        if len(ordered) != len(archive_tables):
            unresolved = sorted(archive - set(ordered))
            ordered.extend(unresolved)
        return tuple(ordered)

    @staticmethod
    def _schema_manifest(
        connection: sqlite3.Connection, archive_tables: tuple[str, ...]
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for table in archive_tables:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            indexes = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            result.append(
                {
                    "table": table,
                    "create_sql": str(row[0]) if row and row[0] else None,
                    "columns": [
                        list(item) for item in connection.execute(f'PRAGMA table_info("{table}")')
                    ],
                    "indexes": [list(item) for item in indexes],
                }
            )
        return result

    @staticmethod
    def _latest_migration(connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("state database has no schema migration identity")
        return str(row[0])

    @staticmethod
    def _row_count(connection: sqlite3.Connection, table: str) -> int:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _manifest_identity(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": manifest["schema_version"],
        "source_latest_migration": manifest["source_latest_migration"],
        "tables": [
            {
                "table": item["table"],
                "rows": item["rows"],
                "sha256": item["sha256"],
            }
            for item in manifest["tables"]  # type: ignore[index]
        ],
        "protected_rows": manifest["protected_rows"],
    }


def _fk_groups(connection: sqlite3.Connection, table: str) -> list[list[sqlite3.Row]]:
    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        groups[int(row[0])].append(row)
    return list(groups.values())


def _arrow_type(declared: str) -> pa.DataType:
    upper = declared.upper()
    if "INT" in upper:
        return pa.int64()
    if any(marker in upper for marker in ("REAL", "FLOA", "DOUB")):
        return pa.float64()
    if "BLOB" in upper:
        return pa.binary()
    return pa.string()


def _coerce(value: object, data_type: pa.DataType) -> object:
    if value is None:
        return None
    if pa.types.is_integer(data_type):
        if isinstance(value, (int, float, str)):
            return int(value)
        raise TypeError(f"cannot coerce {type(value).__name__} to integer")
    if pa.types.is_floating(data_type):
        if isinstance(value, (int, float, str)):
            return float(value)
        raise TypeError(f"cannot coerce {type(value).__name__} to float")
    if pa.types.is_binary(data_type):
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


__all__ = [
    "ColdArchivePlan",
    "ColdArchiveTablePlan",
    "KnowledgeColdArchiveService",
]
