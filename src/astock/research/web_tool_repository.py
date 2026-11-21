"""Atomic Web operation and quota claims in the existing checkpoint table."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.network_ownership import NETWORK_SCOPE, OwnedNetworkRepository
from astock.core.state import StateStore
from astock.schemas.web_tools import WebToolPolicy, WebToolRequest
from astock.storage_retention import process_identity

SCOPE = "web-tool-operation"
BUDGET_SCOPE = "web-tool-budget"


def operation_id(request: WebToolRequest) -> str:
    payload = request.model_dump(mode="json")
    if request.continuation_dispatch_id is None:
        payload.pop("continuation_dispatch_id")  # Preserve identities of unbound legacy calls.
    return content_hash(payload)


def _read(connection: sqlite3.Connection, scope: str, key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT cursor_json FROM checkpoint WHERE scope_type=? AND scope_key=?", (scope, key)
    ).fetchone()
    return None if row is None else json.loads(row["cursor_json"])


def _write(
    connection: sqlite3.Connection, scope: str, key: str, cursor: dict[str, Any], now: datetime,
    *, object_hash: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO checkpoint(checkpoint_id,scope_type,scope_key,cursor_json,status,"
        "object_hash,committed_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(scope_type,scope_key) DO UPDATE SET cursor_json=excluded.cursor_json,"
        "status=excluded.status,object_hash=excluded.object_hash,committed_at=excluded.committed_at",
        (
            sha256_bytes(f"{scope}:{key}".encode()), scope, key,
            canonical_json_bytes(cursor).decode(), cursor["status"], object_hash, now.isoformat(),
        ),
    )


class WebToolOperationRepository(OwnedNetworkRepository):
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.state.get_checkpoint(SCOPE, key)
        return None if row is None else row["cursor"]

    def claim(
        self, request: WebToolRequest, *, source: str, host: dict[str, Any] | None,
        policy: WebToolPolicy, now: datetime,
    ) -> tuple[dict[str, Any], bool]:
        with self.state.transaction() as connection:
            return self._claim_operation(
                connection, request, source=source, host=host, policy=policy, now=now,
            )

    def _claim_operation(
        self, connection: sqlite3.Connection, request: WebToolRequest, *,
        source: str, host: dict[str, Any] | None, policy: WebToolPolicy, now: datetime,
    ) -> tuple[dict[str, Any], bool]:
        key = operation_id(request)
        old = _read(connection, SCOPE, key)
        if old is not None and not (
            old["status"] == "HOST_FAILED" and source == "deepseek-tools"
        ):
            return old, False
        cursor: dict[str, Any] = {
            "operation_id": key, "request": request.model_dump(mode="json"),
            "source": source, "status": "WAITING_HOST" if host else "PAID_RUNNING",
            "dispatch_id": uuid4().hex, "started_at": now.isoformat(),
            "host": host, "billing": "INCLUDED" if host else "RESERVED",
            "budget_keys": [], "host_failure": None if old is None else old.get("reason_code"),
            "prior_result_hash": None if old is None else old.get("result_hash"),
            "owner_pid": os.getpid(),
            "owner_create_time": process_identity(os.getpid()).creation_time,
        }
        if host is None:
            active = connection.execute(
                "SELECT scope_key,cursor_json FROM checkpoint "
                "WHERE scope_type=? AND status='PAID_RUNNING'", (SCOPE,),
            ).fetchall()
            active_count = 0
            for row in active:
                running = json.loads(row["cursor_json"])
                if self._owner_gone(running):
                    running.update(status="UNKNOWN", billing="UNKNOWN",
                                   reason_code="OWNER_EXITED_COMPLETION_UNKNOWN")
                    _write(connection, SCOPE, row["scope_key"], running, now)
                else:
                    active_count += 1
            if active_count >= policy.max_paid_concurrency:
                return {"operation_id": key, "status": "RATE_LIMITED",
                        "reason_code": "PAID_CONCURRENCY_LIMIT"}, False
            rate = _read(connection, "web-tool-dispatch-rate", "deepseek-tools")
            if rate and datetime.fromisoformat(rate["next_allowed_at"]) > now:
                return {"operation_id": key, "status": "RATE_LIMITED",
                        "reason_code": "PAID_DISPATCH_INTERVAL",
                        "retry_at": rate["next_allowed_at"]}, False
            scopes = (
                (f"request:{content_hash(request.request_id)}", policy.max_paid_per_request),
                (f"day:{now.date().isoformat()}", policy.max_paid_per_day),
                (f"month:{now.strftime('%Y-%m')}", policy.max_paid_per_month),
            )
            counters = []
            for budget_key, limit in scopes:
                counter = _read(connection, BUDGET_SCOPE, budget_key) or {
                    "status": "ACTIVE", "reserved_or_charged": 0,
                }
                if counter["reserved_or_charged"] >= limit:
                    return {
                        "operation_id": key, "status": "BUDGET_EXHAUSTED",
                        "reason_code": budget_key.split(":")[0].upper() + "_BUDGET",
                    }, False
                counters.append((budget_key, counter))
            for budget_key, counter in counters:
                counter["reserved_or_charged"] += 1
                _write(connection, BUDGET_SCOPE, budget_key, counter, now)
            cursor["budget_keys"] = [key for key, _ in scopes]
            _write(connection, "web-tool-dispatch-rate", "deepseek-tools", {
                "status": "ACTIVE",
                "next_allowed_at": (now + timedelta(
                    seconds=policy.min_paid_interval_seconds
                )).isoformat(),
            }, now)
        _write(connection, SCOPE, key, cursor, now)
        return cursor, True

    def finish(
        self, key: str, dispatch_id: str, *, status: str, billing: str,
        result_hash: str, artifact_id: str, reason_code: str, now: datetime,
        host_result_hash: str | None = None,
        content_entries: dict[str, dict[str, Any]] | None = None,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        with self.state.transaction() as connection:
            cursor = _read(connection, SCOPE, key)
            if cursor is None or cursor["dispatch_id"] != dispatch_id:
                raise ValueError("Web result belongs to another dispatch")
            if cursor.get("result_hash") == result_hash:
                self._store_content_entries(
                    connection, content_entries, result_hash, cursor["status"], now
                )
                return cursor
            recoverable = {"WAITING_HOST", "PAID_RUNNING", "SHARED_WAITING", "SHARED_UNKNOWN"}
            if cursor.get("shared_acquisition_id"):
                recoverable.add("UNKNOWN")
            if cursor["status"] not in recoverable:
                raise ValueError("Web result cannot replace a terminal or cancelled operation")
            if (
                cursor["billing"] in {"RESERVED", "UNKNOWN"} and billing == "NOT_CHARGED"
                and not cursor.get("result_hash")
            ):
                for budget_key in cursor["budget_keys"]:
                    counter = _read(connection, BUDGET_SCOPE, budget_key)
                    if counter is None or counter["reserved_or_charged"] < 1:
                        raise ValueError("Web budget reservation is inconsistent")
                    counter["reserved_or_charged"] -= 1
                    _write(connection, BUDGET_SCOPE, budget_key, counter, now)
            if cursor["source"] == "deepseek-tools" and retry_after_seconds:
                rate = _read(connection, "web-tool-dispatch-rate", "deepseek-tools")
                retry_at = now + timedelta(seconds=min(max(retry_after_seconds, 0), 3600))
                if rate is None or datetime.fromisoformat(rate["next_allowed_at"]) < retry_at:
                    _write(connection, "web-tool-dispatch-rate", "deepseek-tools", {
                        "status": "ACTIVE", "next_allowed_at": retry_at.isoformat(),
                    }, now)
            cursor.update(
                status=status, billing=billing, result_hash=result_hash, artifact_id=artifact_id,
                reason_code=reason_code, finished_at=now.isoformat(),
                host_result_hash=host_result_hash,
            )
            _write(connection, SCOPE, key, cursor, now, object_hash=result_hash)
            self._store_content_entries(connection, content_entries, result_hash, status, now)
            return cursor

    @staticmethod
    def _store_content_entries(
        connection: sqlite3.Connection, entries: dict[str, dict[str, Any]] | None,
        result_hash: str, status: str, now: datetime,
    ) -> None:
        if status != "SUCCEEDED":
            return
        for key, entry in (entries or {}).items():
            previous = _read(connection, "web-tool-content", key)
            if previous == entry:
                continue
            if previous is not None and (
                datetime.fromisoformat(previous["observed_at"])
                > datetime.fromisoformat(entry["observed_at"])
            ):
                continue
            _write(connection, "web-tool-content", key, entry, now, object_hash=result_hash)

    def reconcile_owner(self, key: str, now: datetime) -> dict[str, Any]:
        with self.state.transaction() as connection:
            cursor = _read(connection, SCOPE, key)
            if cursor is None:
                raise ValueError("unknown Web operation")
            if cursor["status"] == "PAID_RUNNING" and self._owner_gone(cursor):
                cursor.update(status="UNKNOWN", billing="UNKNOWN",
                              reason_code="OWNER_EXITED_COMPLETION_UNKNOWN")
                _write(connection, SCOPE, key, cursor, now)
            return cursor

    def cancel(self, key: str, now: datetime) -> dict[str, Any]:
        with self.state.transaction() as connection:
            cursor = _read(connection, SCOPE, key)
            if cursor is None:
                raise ValueError("unknown Web operation")
            if cursor["status"] in {"WAITING_HOST", "HOST_FAILED"}:
                cursor["status"] = "CANCELLED"
                cursor["finished_at"] = now.isoformat()
                _write(connection, SCOPE, key, cursor, now)
            elif cursor["status"] == "PAID_RUNNING":
                # Only its process owner can cancel an in-flight paid call safely.
                raise ValueError("paid operation is in flight; cancel its owning task")
            return cursor

    def claim_paid_acquisition(
        self, request: WebToolRequest, identity: dict[str, Any], *,
        policy: WebToolPolicy, now: datetime, deadline_at: datetime, fresh_after: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Publish the shared owner, its operation and fee reservation in one commit."""
        with self.state.transaction() as connection:
            old = _read(connection, SCOPE, operation_id(request))
            if old is not None and old["status"] != "HOST_FAILED":
                return old, None, False
            acquisition, acquired = self._claim_acquisition(
                connection, identity, provider="deepseek-tools", domain="api.deepseek.com",
                policy=policy, now=now, deadline_at=deadline_at, fresh_after=fresh_after,
                allow_start=True,
            )
            if "generation" not in acquisition:
                return {**acquisition, "operation_id": operation_id(request)}, None, False
            if not acquired:
                cursor = self._claim_shared_consumer(connection, request, acquisition, now=now)
                return cursor, acquisition, False
            cursor, claimed = self._claim_operation(
                connection, request, source="deepseek-tools", host=None, policy=policy, now=now,
            )
            if not claimed:
                acquisition.update(status="NOT_DISPATCHED", lease_released=True)
                _write(connection, NETWORK_SCOPE, acquisition["acquisition_id"], acquisition, now)
                return cursor, acquisition, False
            cursor.update(
                shared_acquisition_id=acquisition["acquisition_id"],
                shared_generation=acquisition["generation"], shared_owner=True,
            )
            _write(connection, SCOPE, cursor["operation_id"], cursor, now)
            return cursor, acquisition, True

    def claim_shared_consumer(
        self, request: WebToolRequest, acquisition: dict[str, Any], *, now: datetime,
    ) -> dict[str, Any]:
        with self.state.transaction() as connection:
            return self._claim_shared_consumer(connection, request, acquisition, now=now)

    def _claim_shared_consumer(
        self, connection: sqlite3.Connection, request: WebToolRequest,
        acquisition: dict[str, Any], *, now: datetime,
    ) -> dict[str, Any]:
        key = operation_id(request)
        old = _read(connection, SCOPE, key)
        if old is not None and old["status"] != "HOST_FAILED":
            return old
        cursor: dict[str, Any] = {
            "operation_id": key, "request": request.model_dump(mode="json"),
            "source": acquisition["provider"],
            "status": (
                "SHARED_UNKNOWN" if acquisition["status"] == "UNKNOWN" else "SHARED_WAITING"
            ),
            "dispatch_id": uuid4().hex, "started_at": now.isoformat(), "host": None,
            "billing": "NOT_CHARGED", "budget_keys": [],
            "host_failure": None if old is None else old.get("reason_code"),
            "prior_result_hash": None if old is None else old.get("result_hash"),
            "owner_pid": os.getpid(),
            "owner_create_time": process_identity(os.getpid()).creation_time,
            "shared_acquisition_id": acquisition["acquisition_id"],
            "shared_generation": acquisition["generation"], "shared_owner": False,
        }
        _write(connection, SCOPE, key, cursor, now)
        return cursor

    def shared_consumer_unknown(self, key: str, dispatch_id: str, now: datetime) -> dict[str, Any]:
        with self.state.transaction() as connection:
            cursor = _read(connection, SCOPE, key)
            if cursor is None or cursor["dispatch_id"] != dispatch_id:
                raise ValueError("Shared consumer belongs to another dispatch")
            if cursor["status"] in {"SHARED_WAITING", "PAID_RUNNING"}:
                cursor.update(
                    status="UNKNOWN" if cursor.get("shared_owner") else "SHARED_UNKNOWN",
                    reason_code="SHARED_COMPLETION_UNKNOWN",
                )
                _write(connection, SCOPE, key, cursor, now)
            return cursor
