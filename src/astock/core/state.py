"""SQLite migrations and recoverable state services."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas import CollectionCheckpoint, SourceAccessDecision, SourceSnapshot

_MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{4})_.+\.sql$")


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, path: Path, migrations_dir: Path | None = None) -> None:
        self.path = path.resolve()
        self.migrations_dir = (
            migrations_dir.resolve()
            if migrations_dir is not None
            else Path(__file__).resolve().parents[3] / "migrations"
        )

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def migrate(self) -> list[str]:
        applied_now: list[str] = []
        for migration in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_PATTERN.match(migration.name)
            if match is None:
                continue
            version = match.group("version")
            sql = migration.read_text(encoding="utf-8")
            checksum = _migration_checksum(sql)
            with closing(self.connect()) as connection:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
                ).fetchone()
                if table_exists:
                    row = connection.execute(
                        "SELECT checksum FROM schema_migration WHERE version=?", (version,)
                    ).fetchone()
                    if row is not None:
                        if row["checksum"] != checksum:
                            if row["checksum"] in _legacy_format_checksums(sql):
                                connection.execute(
                                    "UPDATE schema_migration SET checksum=? WHERE version=?",
                                    (checksum, version),
                                )
                            else:
                                raise RuntimeError(f"Migration checksum changed: {migration.name}")
                        continue
                applied_at = utc_now_text().replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{sql}\n"
                    "INSERT INTO schema_migration(version, checksum, applied_at) "
                    f"VALUES ('{version}', '{checksum}', '{applied_at}');\n"
                    "COMMIT;"
                )
                try:
                    connection.executescript(script)
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                applied_now.append(version)
        return applied_now

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def integrity_check(self) -> str:
        with closing(self.connect()) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def create_job(self, job_type: str, input_hash: str, *, priority: int = 0) -> str:
        job_id = uuid4().hex
        now = utc_now_text()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO job(job_id,type,status,priority,input_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, job_type, "PENDING", priority, input_hash, now, now),
            )
        return job_id

    def start_attempt(self, job_id: str) -> str:
        attempt_id = uuid4().hex
        now = utc_now_text()
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE job SET status='RUNNING',updated_at=? WHERE job_id=?",
                (now, job_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"Unknown job: {job_id}")
            connection.execute(
                "INSERT INTO job_attempt(attempt_id,job_id,started_at,retryable) VALUES(?,?,?,0)",
                (attempt_id, job_id, now),
            )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        error_class: str | None = None,
        retryable: bool = False,
    ) -> None:
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE job_attempt SET ended_at=?,error_class=?,retryable=? "
                "WHERE attempt_id=? AND ended_at IS NULL",
                (utc_now_text(), error_class, int(retryable), attempt_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"Unknown or already-finished attempt: {attempt_id}")

    def finish_job(self, job_id: str, status: str) -> None:
        allowed = {
            "SUCCEEDED",
            "RETRYABLE_FAILED",
            "BLOCKED_MANUAL",
            "PERMANENT_FAILED",
            "CANCELLED",
        }
        if status not in allowed:
            raise ValueError(f"Invalid terminal job status: {status}")
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE job SET status=?,updated_at=? WHERE job_id=?",
                (status, utc_now_text(), job_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"Unknown job: {job_id}")

    def set_checkpoint(
        self,
        *,
        scope_type: str,
        scope_key: str,
        cursor: dict[str, Any],
        status: str,
        object_hash: str | None = None,
        job_id: str | None = None,
    ) -> str:
        checkpoint_id = sha256_bytes(f"{scope_type}:{scope_key}".encode())
        cursor_json = canonical_json_bytes(cursor).decode("utf-8")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO checkpoint(checkpoint_id,job_id,scope_type,scope_key,cursor_json,"
                "status,object_hash,committed_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_type,scope_key) DO UPDATE SET job_id=excluded.job_id,"
                "cursor_json=excluded.cursor_json,status=excluded.status,"
                "object_hash=excluded.object_hash,committed_at=excluded.committed_at",
                (
                    checkpoint_id,
                    job_id,
                    scope_type,
                    scope_key,
                    cursor_json,
                    status,
                    object_hash,
                    utc_now_text(),
                ),
            )
        return checkpoint_id

    def get_checkpoint(self, scope_type: str, scope_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM checkpoint WHERE scope_type=? AND scope_key=?",
                (scope_type, scope_key),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["cursor"] = json.loads(result.pop("cursor_json"))
        return result

    def set_collection_checkpoint(
        self,
        checkpoint: CollectionCheckpoint,
        *,
        status: str,
        object_hash: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """Commit one listing/content/comment boundary using the shared checkpoint table."""

        scope_key = self._collection_checkpoint_scope(
            checkpoint.author,
            checkpoint.content_type,
            checkpoint.content_id,
            checkpoint.comment_parent_id,
        )
        return self.set_checkpoint(
            scope_type="author-collection",
            scope_key=scope_key,
            cursor=checkpoint.model_dump(mode="json"),
            status=status,
            object_hash=object_hash,
            job_id=job_id,
        )

    def get_collection_checkpoint(
        self,
        author: str,
        content_type: str,
        content_id: str | None = None,
        comment_parent_id: str | None = None,
    ) -> CollectionCheckpoint | None:
        scope_key = self._collection_checkpoint_scope(
            author,
            content_type,
            content_id,
            comment_parent_id,
        )
        stored = self.get_checkpoint("author-collection", scope_key)
        if stored is None:
            return None
        return CollectionCheckpoint.model_validate(stored["cursor"])

    @staticmethod
    def _collection_checkpoint_scope(
        author: str,
        content_type: str,
        content_id: str | None,
        comment_parent_id: str | None = None,
    ) -> str:
        identity = {
            "author": author,
            "content_type": content_type,
            "content_id": content_id or "__listing__",
        }
        if comment_parent_id is not None:
            identity["comment_parent_id"] = comment_parent_id
        return sha256_bytes(
            canonical_json_bytes(identity)
        )

    def set_cursor(
        self,
        provider_id: str,
        scope: str,
        value: dict[str, Any],
        *,
        checkpoint_hash: str | None = None,
    ) -> str:
        cursor_id = sha256_bytes(f"{provider_id}:{scope}".encode())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO cursor_state(cursor_id,provider_id,scope,value_json,"
                "checkpoint_hash,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(provider_id,scope) DO UPDATE SET value_json=excluded.value_json,"
                "checkpoint_hash=excluded.checkpoint_hash,updated_at=excluded.updated_at",
                (
                    cursor_id,
                    provider_id,
                    scope,
                    canonical_json_bytes(value).decode("utf-8"),
                    checkpoint_hash,
                    utc_now_text(),
                ),
            )
        return cursor_id

    def get_cursor(self, provider_id: str, scope: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cursor_state WHERE provider_id=? AND scope=?",
                (provider_id, scope),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["value"] = json.loads(result.pop("value_json"))
        return result

    def register_idempotency(
        self,
        key: str,
        command_type: str,
        result_ref: str,
    ) -> bool:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT command_type,result_ref FROM idempotency_key WHERE key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["command_type"] != command_type or existing["result_ref"] != result_ref:
                    raise ValueError(f"Idempotency key collision: {key}")
                return False
            connection.execute(
                "INSERT INTO idempotency_key(key,command_type,result_ref,created_at) "
                "VALUES(?,?,?,?)",
                (key, command_type, result_ref, utc_now_text()),
            )
        return True

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM idempotency_key WHERE key=?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def upsert_collection_scope(
        self,
        *,
        author_id: str,
        content_type: str,
        status: str,
        last_cursor: str | None = None,
        terminal_condition: str | None = None,
    ) -> str:
        scope_id = sha256_bytes(f"{author_id}:{content_type}".encode())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO collection_scope(scope_id,author_id,content_type,status,"
                "last_cursor,terminal_condition) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(author_id,content_type) DO UPDATE SET status=excluded.status,"
                "last_cursor=excluded.last_cursor,terminal_condition=excluded.terminal_condition",
                (
                    scope_id,
                    author_id,
                    content_type,
                    status,
                    last_cursor,
                    terminal_condition,
                ),
            )
        return scope_id

    def record_collection_gap(
        self,
        *,
        scope_id: str,
        cursor: dict[str, Any],
        failure_class: str,
        retryable: bool,
        status: str,
    ) -> str:
        gap_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "scope_id": scope_id,
                    "cursor": cursor,
                    "failure_class": failure_class,
                }
            )
        )
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collection_gap(gap_id,scope_id,cursor_json,"
                "failure_class,retryable,status) VALUES(?,?,?,?,?,?)",
                (
                    gap_id,
                    scope_id,
                    canonical_json_bytes(cursor).decode("utf-8"),
                    failure_class,
                    int(retryable),
                    status,
                ),
            )
        return gap_id

    def acquire_lock(self, lock_key: str, owner_run_id: str, lease_until: datetime) -> bool:
        now = utc_now_text()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT owner_run_id,lease_until FROM lease_lock WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if existing and existing["owner_run_id"] != owner_run_id:
                if existing["lease_until"] > now:
                    return False
            connection.execute(
                "INSERT INTO lease_lock(lock_key,owner_run_id,lease_until) VALUES(?,?,?) "
                "ON CONFLICT(lock_key) DO UPDATE SET owner_run_id=excluded.owner_run_id,"
                "lease_until=excluded.lease_until",
                (lock_key, owner_run_id, lease_until.isoformat()),
            )
        return True

    def register_snapshot(self, snapshot: SourceSnapshot) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO source_snapshot_index(snapshot_id,source_id,object_hash,"
                "fetched_at,availability_at,fetch_status) VALUES(?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.source_id,
                    snapshot.object_sha256,
                    snapshot.fetched_at.isoformat(),
                    snapshot.available_to_system_at.isoformat(),
                    snapshot.fetch_status.value,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO source_snapshot_detail(snapshot_id,source_url,mime,"
                "byte_size,headers_hash,rights_status) VALUES(?,?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.source_url,
                    snapshot.mime,
                    snapshot.byte_size,
                    snapshot.headers_hash,
                    snapshot.rights_status,
                ),
            )

    def get_snapshot(self, snapshot_id: str) -> SourceSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT i.snapshot_id,i.source_id,i.object_hash,i.fetched_at,"
                "i.availability_at,i.fetch_status,d.source_url,d.mime,d.byte_size,"
                "d.headers_hash,d.rights_status FROM source_snapshot_index i "
                "JOIN source_snapshot_detail d ON d.snapshot_id=i.snapshot_id "
                "WHERE i.snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceSnapshot.model_validate(
            {
                "snapshot_id": row["snapshot_id"],
                "source_id": row["source_id"],
                "object_sha256": row["object_hash"],
                "fetched_at": row["fetched_at"],
                "available_to_system_at": row["availability_at"],
                "fetch_status": row["fetch_status"],
                "source_url": row["source_url"],
                "mime": row["mime"],
                "byte_size": row["byte_size"],
                "headers_hash": row["headers_hash"],
                "rights_status": row["rights_status"],
            }
        )

    def record_source_decision(self, decision: SourceAccessDecision) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO source_access_decision(decision_id,source_id,"
                "requested_capability,selected_transport,selection_reason,fallback_chain_json,"
                "request_started_at,request_finished_at,result_hash,failure_class,"
                "rate_limit_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.source_id,
                    decision.requested_capability,
                    decision.selected_transport.value,
                    decision.selection_reason,
                    json.dumps([item.value for item in decision.fallback_chain]),
                    decision.request_started_at.isoformat(),
                    decision.request_finished_at.isoformat()
                    if decision.request_finished_at
                    else None,
                    decision.result_hash,
                    decision.failure_class,
                    decision.rate_limit_state.value,
                ),
            )

    def record_provider_health(
        self,
        provider_id: str,
        *,
        status: str,
        capability_hash: str | None = None,
        failure_class: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO provider_health(provider_id,capability_hash,status,last_probe_at,"
                "failure_count,last_error_class) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(provider_id) DO UPDATE SET capability_hash=excluded.capability_hash,"
                "status=excluded.status,last_probe_at=excluded.last_probe_at,"
                "failure_count=CASE WHEN excluded.status='AVAILABLE' THEN 0 "
                "ELSE provider_health.failure_count+1 END,"
                "last_error_class=excluded.last_error_class",
                (
                    provider_id,
                    capability_hash,
                    status,
                    utc_now_text(),
                    0 if status == "AVAILABLE" else 1,
                    failure_class,
                ),
            )

    def register_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: list[str],
    ) -> None:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT type,schema_version,object_hash,input_hashes_json "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            serialized_inputs = json.dumps(input_hashes, separators=(",", ":"))
            if existing is not None:
                expected = (artifact_type, schema_version, object_hash, serialized_inputs)
                actual = tuple(existing)
                if actual != expected:
                    raise ValueError(f"Artifact identity collision: {artifact_id}")
                return
            connection.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    artifact_id,
                    artifact_type,
                    schema_version,
                    object_hash,
                    serialized_inputs,
                    utc_now_text(),
                ),
            )


def _normalized_migration_sql(sql: str) -> str:
    lines = sql.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines) + "\n"


def _migration_checksum(sql: str) -> str:
    return sha256_bytes(_normalized_migration_sql(sql).encode("utf-8"))


def _legacy_format_checksums(sql: str) -> set[str]:
    normalized = _normalized_migration_sql(sql)
    variants = {
        sql,
        f"{sql}\n",
        f"{sql}\n\n",
        normalized,
        f"{normalized}\n",
        f"{normalized}\n\n",
        normalized.replace("\n", "\r\n"),
        f"{normalized.replace(chr(10), chr(13) + chr(10))}\r\n",
    }
    return {sha256_bytes(value.encode("utf-8")) for value in variants}
