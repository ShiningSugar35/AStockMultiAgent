"""SQLite migrations and recoverable state services."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.schemas import (
    CollectionCheckpoint,
    DatasetReleaseManifest,
    ProviderHealthStatus,
    ProviderProbeReport,
    SourceAccessDecision,
    SourceSnapshot,
)

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
        phase6_reconciliation: tuple[str, str] | None = None
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
                row = None
                if table_exists:
                    row = connection.execute(
                        "SELECT checksum FROM schema_migration WHERE version=?", (version,)
                    ).fetchone()
                if version == "0047" and phase6_reconciliation is not None:
                    was_applied = row is not None
                    _complete_phase6_migration_identity_reconciliation(
                        connection,
                        migration_name=migration.name,
                        migrations_dir=self.migrations_dir,
                        recorded_0044_checksum=phase6_reconciliation[0],
                        current_0044_checksum=phase6_reconciliation[1],
                        recorded_0047_checksum=(str(row["checksum"]) if row is not None else None),
                        current_0047_checksum=checksum,
                    )
                    if not was_applied:
                        applied_now.append(version)
                    phase6_reconciliation = None
                    continue
                if row is not None:
                    if row["checksum"] != checksum:
                        if row["checksum"] in _legacy_format_checksums(sql):
                            connection.execute(
                                "UPDATE schema_migration SET checksum=? WHERE version=?",
                                (checksum, version),
                            )
                        elif _can_reconcile_phase6_legacy_0044(
                            connection,
                            version=version,
                            migration_name=migration.name,
                            recorded_checksum=str(row["checksum"]),
                            current_checksum=checksum,
                            migrations_dir=self.migrations_dir,
                        ):
                            phase6_reconciliation = (str(row["checksum"]), checksum)
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
        if phase6_reconciliation is not None:
            raise RuntimeError("Phase 6 migration recovery 0047 was not available")
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

    def quick_read_only_health(self) -> dict[str, object]:
        """Return bounded SQLite health metadata without migrations or a full DB scan."""

        if not self.path.is_file():
            return {
                "status": "NOT_INITIALIZED",
                "database_exists": False,
                "latest_migration": None,
                "migration_count": 0,
                "full_integrity_check_run": False,
            }
        uri = f"file:{self.path.as_posix()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("SELECT 1").fetchone()
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
                ).fetchone()
                if not table_exists:
                    return {
                        "status": "UNINITIALIZED_SCHEMA",
                        "database_exists": True,
                        "latest_migration": None,
                        "migration_count": 0,
                        "full_integrity_check_run": False,
                    }
                row = connection.execute(
                    "SELECT COUNT(*) AS migration_count, MAX(version) AS latest_migration "
                    "FROM schema_migration"
                ).fetchone()
                return {
                    "status": "OK",
                    "database_exists": True,
                    "latest_migration": str(row["latest_migration"]) if row else None,
                    "migration_count": int(row["migration_count"]) if row else 0,
                    "full_integrity_check_run": False,
                }
        except sqlite3.Error as exc:
            return {
                "status": "UNAVAILABLE",
                "database_exists": True,
                "latest_migration": None,
                "migration_count": 0,
                "full_integrity_check_run": False,
                "error_class": type(exc).__name__,
            }

    def read_only_integrity_check(self) -> str:
        """Run the expensive SQLite integrity check without mutating journal/schema state."""

        if not self.path.is_file():
            return "not_initialized"
        uri = f"file:{self.path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def vacuum(self) -> dict[str, int]:
        """Checkpoint WAL and rewrite SQLite once to return free pages to the filesystem."""

        before = self.path.stat().st_size if self.path.exists() else 0
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        after = self.path.stat().st_size if self.path.exists() else 0
        return {
            "before_bytes": before,
            "after_bytes": after,
            "reclaimed_bytes": max(0, before - after),
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
        }

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

    def artifact_record(self, artifact_id: str) -> dict[str, Any] | None:
        """Return one registered artifact without exposing a raw database connection."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT artifact_id,type,schema_version,object_hash,input_hashes_json,created_at "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["input_hashes"] = json.loads(result.pop("input_hashes_json"))
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

    def release_lock(self, lock_key: str, owner_run_id: str) -> bool:
        with self.transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM lease_lock WHERE lock_key=? AND owner_run_id=?",
                (lock_key, owner_run_id),
            ).rowcount
        return deleted == 1

    def acquire_reference_provider_lease(
        self,
        lock_key: str,
        owner_run_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> int | None:
        """Acquire a cross-process provider lease and return its fencing token."""

        now_text = now.astimezone(UTC).isoformat()
        lease_text = lease_until.astimezone(UTC).isoformat()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT owner_run_id,fencing_token,lease_until "
                "FROM reference_provider_lease WHERE lock_key=?",
                (lock_key,),
            ).fetchone()
            if (
                existing is not None
                and existing["owner_run_id"] is not None
                and existing["owner_run_id"] != owner_run_id
                and str(existing["lease_until"]) > now_text
            ):
                return None
            token = (int(existing["fencing_token"]) if existing is not None else 0) + 1
            connection.execute(
                "INSERT INTO reference_provider_lease(lock_key,owner_run_id,fencing_token,"
                "lease_until) VALUES(?,?,?,?) ON CONFLICT(lock_key) DO UPDATE SET "
                "owner_run_id=excluded.owner_run_id,fencing_token=excluded.fencing_token,"
                "lease_until=excluded.lease_until",
                (lock_key, owner_run_id, token, lease_text),
            )
        return token

    def renew_reference_provider_lease(
        self,
        lock_key: str,
        owner_run_id: str,
        fencing_token: int,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Renew only the still-current, unexpired fenced lease."""

        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE reference_provider_lease SET lease_until=? WHERE lock_key=? "
                "AND owner_run_id=? AND fencing_token=? AND lease_until>?",
                (
                    lease_until.astimezone(UTC).isoformat(),
                    lock_key,
                    owner_run_id,
                    fencing_token,
                    now.astimezone(UTC).isoformat(),
                ),
            ).rowcount
        return updated == 1

    def release_reference_provider_lease(
        self,
        lock_key: str,
        owner_run_id: str,
        fencing_token: int,
        *,
        now: datetime,
    ) -> bool:
        """Release a lease without deleting its monotonic fencing counter."""

        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE reference_provider_lease SET owner_run_id=NULL,lease_until=? "
                "WHERE lock_key=? AND owner_run_id=? AND fencing_token=?",
                (
                    now.astimezone(UTC).isoformat(),
                    lock_key,
                    owner_run_id,
                    fencing_token,
                ),
            ).rowcount
        return updated == 1

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
                "created_at": row["fetched_at"],
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

    def latest_snapshot_for_source(self, source_id: str) -> SourceSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM source_snapshot_index WHERE source_id=? "
                "ORDER BY fetched_at DESC,snapshot_id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return self.get_snapshot(str(row["snapshot_id"])) if row is not None else None

    def record_source_decision(self, decision: SourceAccessDecision) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO source_access_decision(decision_id,source_id,"
                "selected_source_id,requested_capability,selected_transport,selection_reason,"
                "fallback_chain_json,fallback_source_chain_json,request_started_at,"
                "request_finished_at,result_hash,failure_class,rate_limit_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.source_id,
                    decision.selected_source_id,
                    decision.requested_capability,
                    decision.selected_transport.value,
                    decision.selection_reason,
                    json.dumps([item.value for item in decision.fallback_chain]),
                    json.dumps(decision.fallback_source_chain),
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
                "last_error_class=excluded.last_error_class "
                "WHERE provider_health.latest_probe_id IS NULL",
                (
                    provider_id,
                    capability_hash,
                    status,
                    utc_now_text(),
                    0 if status == "AVAILABLE" else 1,
                    failure_class,
                ),
            )

    def get_provider_probe_health(self, provider_id: str) -> dict[str, Any] | None:
        """Return the latest durable probe pointer without mutating provider state."""

        row, _ = self.get_provider_probe_health_snapshot(provider_id)
        return row

    def get_provider_probe_health_snapshot(
        self,
        provider_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Read the health pointer and deterministic event head in one SQLite snapshot."""

        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    "SELECT provider_id,capability_hash,status,last_probe_at,failure_count,"
                    "last_error_class,"
                    "registry_version,probe_mode,report_artifact_id,report_object_hash,"
                    "failure_code,latest_probe_id FROM provider_health WHERE provider_id=?",
                    (provider_id,),
                ).fetchone()
                events = connection.execute(
                    "SELECT probe_id,status FROM provider_probe_event WHERE provider_id=? "
                    "ORDER BY completed_at DESC,probe_id DESC",
                    (provider_id,),
                ).fetchall()
            finally:
                connection.rollback()
        head = (
            {
                "latest_probe_id": str(events[0]["probe_id"]),
                "failure_count": _consecutive_provider_failures(events),
            }
            if events
            else None
        )
        return (dict(row) if row is not None else None), head

    def get_provider_probe_head(self, provider_id: str) -> dict[str, Any] | None:
        """Return the true deterministic event head, independent of provider_health."""

        _, head = self.get_provider_probe_health_snapshot(provider_id)
        return head

    def get_provider_probe_event(self, probe_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT e.*,a.type AS artifact_type,a.schema_version AS artifact_schema_version,"
                "a.object_hash AS artifact_object_hash,"
                "a.input_hashes_json AS artifact_input_hashes_json "
                "FROM provider_probe_event e LEFT JOIN artifact_registry a "
                "ON a.artifact_id=e.report_artifact_id WHERE e.probe_id=?",
                (probe_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def provider_has_probe_events(self, provider_id: str) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM provider_probe_event WHERE provider_id=? LIMIT 1",
                (provider_id,),
            ).fetchone()
        return row is not None

    def provider_probe_consecutive_failures(
        self,
        provider_id: str,
        probe_id: str,
    ) -> int | None:
        """Return the deterministic failure streak ending at one immutable event."""

        with closing(self.connect()) as connection:
            target = connection.execute(
                "SELECT completed_at FROM provider_probe_event "
                "WHERE provider_id=? AND probe_id=?",
                (provider_id, probe_id),
            ).fetchone()
            if target is None:
                return None
            rows = connection.execute(
                "SELECT status FROM provider_probe_event WHERE provider_id=? AND "
                "(completed_at < ? OR (completed_at = ? AND probe_id <= ?)) "
                "ORDER BY completed_at DESC,probe_id DESC",
                (provider_id, target["completed_at"], target["completed_at"], probe_id),
            ).fetchall()
        return _consecutive_provider_failures(rows)

    def record_provider_probe(self, report: ProviderProbeReport, object_hash: str) -> bool:
        """Atomically register a verified report, immutable event, and latest health pointer."""

        if report.status in {ProviderHealthStatus.NOT_PROBED, ProviderHealthStatus.CORRUPT}:
            raise ValueError("non-terminal provider status cannot be persisted")
        artifact_id = f"provider-probe:{report.probe_id}"
        serialized_inputs = json.dumps([report.capability_hash], separators=(",", ":"))
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT provider_id,registry_version,capability_hash,probe_mode,status,"
                "completed_at,report_artifact_id,report_object_hash,failure_code,failure_count "
                "FROM provider_probe_event WHERE probe_id=?",
                (report.probe_id,),
            ).fetchone()
            event_values = (
                report.provider_id,
                report.registry_version,
                report.capability_hash,
                report.probe_mode.value,
                report.status.value,
                report.completed_at.isoformat(),
                artifact_id,
                object_hash,
                report.failure_code.value if report.failure_code else None,
                report.failure_count,
            )
            if existing is not None:
                if tuple(existing) != event_values:
                    raise ValueError(f"Provider probe identity collision: {report.probe_id}")
                return False

            artifact = connection.execute(
                "SELECT type,schema_version,object_hash,input_hashes_json "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            artifact_values = (
                "ProviderProbeReport",
                report.schema_version,
                object_hash,
                serialized_inputs,
            )
            if artifact is not None and tuple(artifact) != artifact_values:
                raise ValueError(f"Artifact identity collision: {artifact_id}")
            if artifact is None:
                connection.execute(
                    "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                    "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                    (*((artifact_id,) + artifact_values), utc_now_text()),
                )
            connection.execute(
                "INSERT INTO provider_probe_event(probe_id,provider_id,registry_version,"
                "capability_hash,probe_mode,status,completed_at,report_artifact_id,"
                "report_object_hash,failure_code,failure_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (report.probe_id, *event_values),
            )
            ordered_events = connection.execute(
                "SELECT probe_id,registry_version,capability_hash,probe_mode,status,"
                "completed_at,report_artifact_id,report_object_hash,failure_code "
                "FROM provider_probe_event WHERE provider_id=? "
                "ORDER BY completed_at DESC,probe_id DESC",
                (report.provider_id,),
            ).fetchall()
            latest = ordered_events[0]
            consecutive_failures = _consecutive_provider_failures(ordered_events)
            connection.execute(
                "INSERT INTO provider_health(provider_id,capability_hash,status,last_probe_at,"
                "failure_count,last_error_class,registry_version,probe_mode,report_artifact_id,"
                "report_object_hash,failure_code,latest_probe_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider_id) DO UPDATE SET "
                "capability_hash=excluded.capability_hash,status=excluded.status,"
                "last_probe_at=excluded.last_probe_at,failure_count=excluded.failure_count,"
                "last_error_class=excluded.last_error_class,"
                "registry_version=excluded.registry_version,probe_mode=excluded.probe_mode,"
                "report_artifact_id=excluded.report_artifact_id,"
                "report_object_hash=excluded.report_object_hash,"
                "failure_code=excluded.failure_code,latest_probe_id=excluded.latest_probe_id",
                (
                    report.provider_id,
                    latest["capability_hash"],
                    latest["status"],
                    latest["completed_at"],
                    consecutive_failures,
                    latest["failure_code"],
                    latest["registry_version"],
                    latest["probe_mode"],
                    latest["report_artifact_id"],
                    latest["report_object_hash"],
                    latest["failure_code"],
                    latest["probe_id"],
                ),
            )
        return True

    def register_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: list[str],
    ) -> None:
        self.register_artifacts(
            [
                (
                    artifact_id,
                    artifact_type,
                    schema_version,
                    object_hash,
                    input_hashes,
                )
            ]
        )

    def register_artifacts(
        self,
        artifacts: Iterable[tuple[str, str, str, str, list[str]]],
    ) -> None:
        """Idempotently register multiple artifacts in one durable transaction."""

        with self.transaction() as connection:
            created_at = utc_now_text()
            for artifact_id, artifact_type, schema_version, object_hash, input_hashes in artifacts:
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
                    continue
                connection.execute(
                    "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                    "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        artifact_id,
                        artifact_type,
                        schema_version,
                        object_hash,
                        serialized_inputs,
                        created_at,
                    ),
                )

    def publish_market_reference_release(
        self,
        manifest: DatasetReleaseManifest,
        manifest_object_hash: str,
    ) -> bool:
        """Atomically register one immutable release and advance its canonical head."""

        release_identity = {
            "dataset_kind": manifest.dataset_kind.value,
            "scope_key": manifest.scope_key,
            "provider_id": manifest.provider_id,
            "batch_id": manifest.batch_id,
            "content_hash": manifest.content_hash,
            "previous_release_id": manifest.previous_release_id,
            "available_to_system_at": manifest.available_to_system_at.isoformat(),
        }
        if manifest.release_id != content_hash(release_identity):
            raise ValueError("market-reference release identity mismatch")
        artifact_id = f"market-reference:{manifest.release_id}"
        inputs = json.dumps(
            [*manifest.raw_snapshot_ids, manifest.content_hash], separators=(",", ":")
        )
        raw_snapshot_ids_json = canonical_json_bytes(manifest.raw_snapshot_ids).decode("utf-8")
        observation_files_json = canonical_json_bytes(manifest.observation_files).decode("utf-8")
        canonical_files_json = canonical_json_bytes(manifest.canonical_files).decode("utf-8")
        coverage_json = canonical_json_bytes(manifest.coverage).decode("utf-8")
        kind = manifest.dataset_kind.value
        now = utc_now_text()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT dataset_kind,scope_key,provider_id,batch_id,content_hash,"
                "previous_release_id,manifest_artifact_id,manifest_object_hash,"
                "manifest_schema_version,raw_snapshot_ids_json,observation_files_json,"
                "canonical_files_json,coverage_json,"
                "available_to_system_at,coverage_status,pit_status "
                "FROM market_reference_release WHERE release_id=?",
                (manifest.release_id,),
            ).fetchone()
            expected_release = (
                kind,
                manifest.scope_key,
                manifest.provider_id,
                manifest.batch_id,
                manifest.content_hash,
                manifest.previous_release_id,
                artifact_id,
                manifest_object_hash,
                manifest.schema_version,
                raw_snapshot_ids_json,
                observation_files_json,
                canonical_files_json,
                coverage_json,
                manifest.available_to_system_at.isoformat(),
                manifest.coverage.status.value,
                manifest.pit_status.value,
            )
            if existing is not None:
                if tuple(existing) != expected_release:
                    raise ValueError(f"Market-reference identity collision: {manifest.release_id}")
                return False

            for snapshot_id in manifest.raw_snapshot_ids:
                if connection.execute(
                    "SELECT 1 FROM source_snapshot_index WHERE snapshot_id=?", (snapshot_id,)
                ).fetchone() is None:
                    raise ValueError(f"Unknown market-reference snapshot: {snapshot_id}")

            head = connection.execute(
                "SELECT h.release_id,r.available_to_system_at FROM market_reference_head h "
                "JOIN market_reference_release r ON r.dataset_kind=h.dataset_kind "
                "AND r.scope_key=h.scope_key AND r.release_id=h.release_id "
                "WHERE h.dataset_kind=? AND h.scope_key=?",
                (kind, manifest.scope_key),
            ).fetchone()
            current_head = str(head["release_id"]) if head is not None else None
            if current_head != manifest.previous_release_id:
                raise ValueError("market-reference previous head mismatch")
            if (
                head is not None
                and str(head["available_to_system_at"])
                > manifest.available_to_system_at.isoformat()
            ):
                raise ValueError("market-reference head availability cannot move backwards")

            artifact = connection.execute(
                "SELECT type,schema_version,object_hash,input_hashes_json "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            expected_artifact = (
                "DatasetReleaseManifest",
                manifest.schema_version,
                manifest_object_hash,
                inputs,
            )
            if artifact is not None and tuple(artifact) != expected_artifact:
                raise ValueError(f"Artifact identity collision: {artifact_id}")
            if artifact is None:
                connection.execute(
                    "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                    "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                    (artifact_id, *expected_artifact, now),
                )
            connection.execute(
                "INSERT INTO market_reference_release(release_id,dataset_kind,scope_key,"
                "provider_id,batch_id,content_hash,previous_release_id,manifest_artifact_id,"
                "manifest_object_hash,manifest_schema_version,raw_snapshot_ids_json,"
                "observation_files_json,canonical_files_json,coverage_json,"
                "available_to_system_at,coverage_status,pit_status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (manifest.release_id, *expected_release[:6], *expected_release[6:], now),
            )
            connection.execute(
                "INSERT INTO market_reference_head(dataset_kind,scope_key,release_id,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(dataset_kind,scope_key) DO UPDATE SET "
                "release_id=excluded.release_id,updated_at=excluded.updated_at",
                (kind, manifest.scope_key, manifest.release_id, now),
            )
            checkpoint_scope = f"{kind}:{manifest.scope_key}"
            connection.execute(
                "INSERT INTO checkpoint(checkpoint_id,job_id,scope_type,scope_key,cursor_json,"
                "status,object_hash,committed_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_type,scope_key) DO UPDATE SET "
                "cursor_json=excluded.cursor_json,status=excluded.status,"
                "object_hash=excluded.object_hash,committed_at=excluded.committed_at",
                (
                    sha256_bytes(f"market-reference:{checkpoint_scope}".encode()),
                    None,
                    "market-reference",
                    checkpoint_scope,
                    canonical_json_bytes(
                        {"release_id": manifest.release_id, "content_hash": manifest.content_hash}
                    ).decode("utf-8"),
                    "SUCCEEDED",
                    manifest_object_hash,
                    now,
                ),
            )
        return True

    def get_market_reference_release(
        self,
        dataset_kind: str,
        scope_key: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read the canonical head, or the latest release visible at ``as_of``."""

        with closing(self.connect()) as connection:
            if as_of is None:
                row = connection.execute(
                    "SELECT r.*,a.type AS artifact_type,"
                    "a.schema_version AS artifact_schema_version,"
                    "a.object_hash AS artifact_object_hash,a.input_hashes_json "
                    "FROM market_reference_head h JOIN market_reference_release r "
                    "ON r.dataset_kind=h.dataset_kind AND r.scope_key=h.scope_key "
                    "AND r.release_id=h.release_id JOIN artifact_registry a "
                    "ON a.artifact_id=r.manifest_artifact_id "
                    "WHERE h.dataset_kind=? AND h.scope_key=?",
                    (dataset_kind, scope_key),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT r.*,a.type AS artifact_type,"
                    "a.schema_version AS artifact_schema_version,"
                    "a.object_hash AS artifact_object_hash,a.input_hashes_json "
                    "FROM market_reference_release r JOIN artifact_registry a "
                    "ON a.artifact_id=r.manifest_artifact_id "
                    "WHERE r.dataset_kind=? AND r.scope_key=? "
                    "AND r.available_to_system_at<=? "
                    "ORDER BY r.available_to_system_at DESC,r.release_id DESC LIMIT 1",
                    (dataset_kind, scope_key, as_of.astimezone(UTC).isoformat()),
                ).fetchone()
        return dict(row) if row is not None else None

    def list_market_reference_releases(
        self, dataset_kind: str | None = None, scope_key: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if dataset_kind is not None:
            clauses.append("r.dataset_kind=?")
            params.append(dataset_kind)
        if scope_key is not None:
            clauses.append("r.scope_key=?")
            params.append(scope_key)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT r.*,a.type AS artifact_type,"
                "a.schema_version AS artifact_schema_version,"
                "a.object_hash AS artifact_object_hash,a.input_hashes_json "
                "FROM market_reference_release r JOIN artifact_registry a "
                "ON a.artifact_id=r.manifest_artifact_id"
                + where
                + " ORDER BY r.available_to_system_at DESC,r.release_id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def _consecutive_provider_failures(rows: Iterable[sqlite3.Row]) -> int:
    count = 0
    for row in rows:
        if str(row["status"]) == ProviderHealthStatus.HEALTHY.value:
            break
        count += 1
    return count


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


def _can_reconcile_phase6_legacy_0044(
    connection: sqlite3.Connection,
    *,
    version: str,
    migration_name: str,
    recorded_checksum: str,
    current_checksum: str,
    migrations_dir: Path,
) -> bool:
    if version != "0044" or migration_name != "0044_phase6_decision_close.sql":
        return False
    legacy = migrations_dir / "compat" / "0044_phase6_decision_close_legacy.sql"
    current = migrations_dir / "0044_phase6_decision_close.sql"
    recovery = migrations_dir / "0047_phase6_migration_identity_recovery.sql"
    if not legacy.is_file() or not current.is_file() or not recovery.is_file():
        return False
    if recorded_checksum != _migration_checksum(legacy.read_text(encoding="utf-8")):
        return False
    if current_checksum != _migration_checksum(current.read_text(encoding="utf-8")):
        return False
    return _phase6_legacy_0044_schema_is_materialized(connection)


def _phase6_legacy_0044_schema_is_materialized(connection: sqlite3.Connection) -> bool:
    return (
        _sqlite_table_columns(connection, "committee_trade_protocol_index")
        == (
            "protocol_id",
            "decision_id",
            "company_id",
            "verdict",
            "protocol_status",
            "strategy_id",
            "effective_from",
            "requires_user_confirmation",
            "broker_execution_allowed",
            "paper_simulation_allowed",
            "ledger_write_allowed",
            "object_hash",
            "input_hash",
            "created_at",
        )
        and _sqlite_table_columns(connection, "committee_trade_protocol_index_legacy")
        == (
            "protocol_id",
            "decision_id",
            "company_id",
            "verdict",
            "protocol_status",
            "strategy_id",
            "effective_from",
            "requires_user_confirmation",
            "broker_execution_allowed",
            "ledger_write_allowed",
            "object_hash",
            "input_hash",
            "created_at",
        )
        and _sqlite_table_columns(connection, "paper_confirmation_key_binding")
        == (
            "confirmation_id",
            "key_id",
            "public_key_object_hash",
            "created_at",
        )
        and bool(_sqlite_table_columns(connection, "paper_execution_request_index"))
        and bool(_sqlite_table_columns(connection, "phase6_run_index"))
    )


def _complete_phase6_migration_identity_reconciliation(
    connection: sqlite3.Connection,
    *,
    migration_name: str,
    migrations_dir: Path,
    recorded_0044_checksum: str,
    current_0044_checksum: str,
    recorded_0047_checksum: str | None,
    current_0047_checksum: str,
) -> None:
    if migration_name != "0047_phase6_migration_identity_recovery.sql":
        raise RuntimeError("Phase 6 migration identity recovery file changed")
    legacy = migrations_dir / "compat" / "0044_phase6_decision_close_legacy.sql"
    current_0044 = migrations_dir / "0044_phase6_decision_close.sql"
    current_0047 = migrations_dir / "0047_phase6_migration_identity_recovery.sql"
    if not legacy.is_file() or not current_0044.is_file() or not current_0047.is_file():
        raise RuntimeError("Phase 6 migration identity recovery files are incomplete")
    if recorded_0044_checksum != _migration_checksum(legacy.read_text(encoding="utf-8")):
        raise RuntimeError("Legacy Phase 6 migration checksum is not recognized")
    if current_0044_checksum != _migration_checksum(current_0044.read_text(encoding="utf-8")):
        raise RuntimeError("Current Phase 6 migration checksum changed during reconciliation")
    if current_0047_checksum != _migration_checksum(current_0047.read_text(encoding="utf-8")):
        raise RuntimeError("Current Phase 6 recovery checksum changed during reconciliation")
    if recorded_0047_checksum is not None and recorded_0047_checksum != current_0047_checksum:
        raise RuntimeError(
            "Migration checksum changed: 0047_phase6_migration_identity_recovery.sql"
        )
    if not _phase6_legacy_0044_schema_is_materialized(connection):
        raise RuntimeError(
            "Legacy Phase 6 migration schema does not match the audited 0044 contract"
        )

    connection.execute("BEGIN IMMEDIATE")
    try:
        if recorded_0047_checksum is None:
            connection.execute(
                "INSERT INTO committee_trade_protocol_index_legacy("
                "protocol_id,decision_id,company_id,verdict,protocol_status,strategy_id,"
                "effective_from,requires_user_confirmation,broker_execution_allowed,"
                "ledger_write_allowed,object_hash,input_hash,created_at) "
                "SELECT protocol_id,decision_id,company_id,verdict,protocol_status,strategy_id,"
                "effective_from,requires_user_confirmation,broker_execution_allowed,"
                "ledger_write_allowed,object_hash,input_hash,created_at "
                "FROM committee_trade_protocol_index "
                "WHERE paper_simulation_allowed=0 AND ledger_write_allowed=0"
            )
            connection.execute(
                "DELETE FROM committee_trade_protocol_index "
                "WHERE paper_simulation_allowed=0 AND ledger_write_allowed=0"
            )
            connection.execute(
                "INSERT INTO schema_migration(version,checksum,applied_at) VALUES(?,?,?)",
                ("0047", current_0047_checksum, utc_now_text()),
            )
        connection.execute(
            "UPDATE schema_migration SET checksum=? WHERE version='0044'",
            (current_0044_checksum,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _sqlite_table_columns(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    if not re.fullmatch(r"[a-z0-9_]+", table_name):
        raise ValueError("unsafe SQLite table name")
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return tuple(str(row["name"]) for row in rows)
