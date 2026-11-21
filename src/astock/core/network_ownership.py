"""One process-owned network capacity pool in the existing checkpoint table.

Canonical Web claims retain their immutable generations. Ordinary provider HTTP
leases use the same pool and disappear after terminal release; this is not a job
scheduler and does not control tools executed by an external host.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.request_budget import RequestBudgetExhausted, current_request_budget
from astock.core.state import StateStore
from astock.storage_retention import process_identity

NETWORK_SCOPE = "owned-network-acquisition"


class NetworkLimits(Protocol):
    @property
    def max_owned_concurrency(self) -> int: ...

    @property
    def max_owned_per_provider(self) -> int: ...

    @property
    def max_owned_per_domain(self) -> int: ...


@dataclass(frozen=True)
class OwnedNetworkLimits:
    max_owned_concurrency: int = 4
    max_owned_per_provider: int = 2
    max_owned_per_domain: int = 2


def load_network_limits(root: Path | None = None) -> OwnedNetworkLimits:
    root = root or Path(__file__).resolve().parents[3]
    raw = yaml.safe_load((root / "configs" / "web_tools.yaml").read_text(encoding="utf-8"))
    values = {
        key: raw[key] for key in (
            "max_owned_concurrency", "max_owned_per_provider", "max_owned_per_domain",
        )
    }
    if any(type(value) is not int or not 1 <= value <= 8 for value in values.values()):
        raise ValueError("Owned network limits must be positive bounded integers")
    return OwnedNetworkLimits(**values)


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


class OwnedNetworkRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _owner_gone(cursor: dict[str, Any]) -> bool:
        identity = process_identity(cursor.get("owner_pid", 0))
        created = cursor.get("owner_create_time")
        return identity.status == "DEAD" or (
            identity.status == "ALIVE" and identity.creation_time is not None
            and isinstance(created, (float, int))
            and abs(identity.creation_time - created) > 0.001
        )

    def acquisition(self, key: str, now: datetime) -> dict[str, Any] | None:
        with self.state.transaction() as connection:
            cursor = _read(connection, NETWORK_SCOPE, key)
            return None if cursor is None else self._reconcile_acquisition(connection, cursor, now)

    def _reconcile_acquisition(
        self, connection: sqlite3.Connection, cursor: dict[str, Any], now: datetime,
    ) -> dict[str, Any]:
        if cursor["status"] not in {"RUNNING", "UNKNOWN"} or cursor.get("lease_released"):
            return cursor
        gone = self._owner_gone(cursor)
        if gone or (
            cursor["status"] == "RUNNING"
            and datetime.fromisoformat(cursor["lease_expires_at"]) <= now
        ):
            cursor.update(
                status="UNKNOWN" if cursor["dispatch_attempted"] else "NOT_DISPATCHED",
                reason_code="OWNER_GONE" if gone else "LEASE_EXPIRED",
                lease_released=gone,
            )
            _write(connection, NETWORK_SCOPE, cursor["acquisition_id"], cursor, now)
            if gone and cursor["identity"].get("schema_version") == "owned-http-dispatch-v1":
                connection.execute(
                    "DELETE FROM checkpoint WHERE scope_type=? AND scope_key=?",
                    (NETWORK_SCOPE, cursor["acquisition_id"]),
                )
        return cursor

    def claim_acquisition(
        self, identity: dict[str, Any], *, provider: str, domain: str,
        policy: NetworkLimits, now: datetime, deadline_at: datetime,
        fresh_after: datetime, allow_start: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        with self.state.transaction() as connection:
            return self._claim_acquisition(
                connection, identity, provider=provider, domain=domain, policy=policy,
                now=now, deadline_at=deadline_at, fresh_after=fresh_after, allow_start=allow_start,
            )

    def _claim_acquisition(
        self, connection: sqlite3.Connection, identity: dict[str, Any], *,
        provider: str, domain: str, policy: NetworkLimits, now: datetime,
        deadline_at: datetime, fresh_after: datetime, allow_start: bool,
    ) -> tuple[dict[str, Any], bool]:
        key = content_hash(identity)
        old = _read(connection, NETWORK_SCOPE, key)
        if old is not None:
            old = self._reconcile_acquisition(connection, old, now)
            if old["identity"] != identity:
                raise ValueError("Shared acquisition identity mismatch")
            if old["status"] in {"RUNNING", "UNKNOWN"} or (
                old["status"] == "NOT_DISPATCHED" and not old.get("result_hash")
            ):
                return old, False
            if old["status"] == "SUCCEEDED" and (
                datetime.fromisoformat(old["observed_at"]) >= fresh_after
            ):
                return old, False
        if not allow_start:
            return {"acquisition_id": key, "status": "UNAVAILABLE"}, False
        if deadline_at <= now:
            return {"acquisition_id": key, "status": "BUDGET_EXHAUSTED"}, False
        reason = self._capacity_reason(connection, provider, domain, policy, now)
        if reason:
            return {"acquisition_id": key, "status": "RATE_LIMITED",
                    "reason_code": reason}, False
        owner = process_identity(os.getpid())
        if owner.status != "ALIVE" or owner.creation_time is None:
            raise RuntimeError("Owned acquisition requires an observable process identity")
        cursor: dict[str, Any] = {
            "acquisition_id": key, "identity": identity, "provider": provider,
            "domain": domain, "status": "RUNNING",
            "generation": 1 if old is None else old["generation"] + 1,
            "owner_pid": os.getpid(), "owner_create_time": owner.creation_time,
            "lease_expires_at": deadline_at.isoformat(), "lease_released": False,
            "dispatch_attempted": False, "started_at": now.isoformat(),
        }
        _write(connection, NETWORK_SCOPE, key, cursor, now)
        return cursor, True

    def _capacity_reason(
        self, connection: sqlite3.Connection, provider: str, domain: str,
        policy: NetworkLimits, now: datetime, *, exclude: str | None = None,
    ) -> str | None:
        rows = connection.execute(
            "SELECT cursor_json FROM checkpoint WHERE scope_type=? AND status IN (?,?)",
            (NETWORK_SCOPE, "RUNNING", "UNKNOWN"),
        ).fetchall()
        active: list[dict[str, Any]] = []
        for row in rows:
            cursor = self._reconcile_acquisition(connection, json.loads(row["cursor_json"]), now)
            if cursor["acquisition_id"] == exclude or cursor.get("lease_released"):
                continue
            if self._owner_gone(cursor):
                continue
            active.append(cursor)
        if len(active) >= policy.max_owned_concurrency:
            return "OWNED_GLOBAL_CONCURRENCY"
        if sum(item["provider"] == provider for item in active) >= policy.max_owned_per_provider:
            return "OWNED_PROVIDER_CONCURRENCY"
        if sum(item["domain"] == domain for item in active) >= policy.max_owned_per_domain:
            return "OWNED_DOMAIN_CONCURRENCY"
        return None

    def start_acquisition(self, key: str, generation: int, now: datetime) -> None:
        with self.state.transaction() as connection:
            cursor = self._owned_acquisition(connection, key, generation)
            if datetime.fromisoformat(cursor["lease_expires_at"]) <= now:
                raise TimeoutError("Owned acquisition lease expired before dispatch")
            cursor["dispatch_attempted"] = True
            _write(connection, NETWORK_SCOPE, key, cursor, now)

    def _owned_acquisition(
        self, connection: sqlite3.Connection, key: str, generation: int,
    ) -> dict[str, Any]:
        cursor = _read(connection, NETWORK_SCOPE, key)
        owner = process_identity(os.getpid())
        if (
            cursor is None or cursor["generation"] != generation
            or cursor["owner_pid"] != os.getpid()
            or owner.status != "ALIVE" or owner.creation_time is None
            or abs(cursor["owner_create_time"] - owner.creation_time) > 0.001
            or cursor.get("lease_released")
        ):
            raise ValueError("Shared acquisition has another owner or generation")
        return cursor

    def move_acquisition_domain(
        self, key: str, generation: int, domain: str, *, policy: NetworkLimits, now: datetime,
    ) -> None:
        with self.state.transaction() as connection:
            cursor = self._owned_acquisition(connection, key, generation)
            if datetime.fromisoformat(cursor["lease_expires_at"]) <= now:
                raise TimeoutError("Owned acquisition lease expired before redirect")
            reason = self._capacity_reason(
                connection, cursor["provider"], domain, policy, now, exclude=key,
            )
            if reason:
                raise ValueError(reason)
            cursor["domain"] = domain
            _write(connection, NETWORK_SCOPE, key, cursor, now)

    def finish_acquisition(
        self, key: str, generation: int, *, status: str, now: datetime,
        result_hash: str | None = None, observed_at: datetime | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN", "NOT_DISPATCHED"}:
            raise ValueError("Invalid shared acquisition terminal status")
        with self.state.transaction() as connection:
            cursor = _read(connection, NETWORK_SCOPE, key)
            if cursor is None or cursor["generation"] != generation:
                raise ValueError("Shared result belongs to another acquisition generation")
            if cursor.get("result_hash") not in {None, result_hash}:
                raise ValueError("Shared acquisition has a conflicting frozen result")
            cursor.update(
                status=status, result_hash=result_hash, reason_code=reason_code,
                observed_at=(observed_at or now).isoformat(), lease_released=True,
            )
            _write(connection, NETWORK_SCOPE, key, cursor, now, object_hash=result_hash)
            return cursor


    def release_transient(self, key: str, generation: int) -> None:
        with self.state.transaction() as connection:
            cursor = self._owned_acquisition(connection, key, generation)
            if cursor["identity"].get("schema_version") != "owned-http-dispatch-v1":
                raise ValueError("Only a transient HTTP lease may be removed")
            connection.execute(
                "DELETE FROM checkpoint WHERE scope_type=? AND scope_key=?",
                (NETWORK_SCOPE, key),
            )


@dataclass
class _ActiveNetworkLease:
    repository: OwnedNetworkRepository
    acquisition: dict[str, Any]
    limits: NetworkLimits

    def before_hop(self, url: str) -> None:
        self.repository.move_acquisition_domain(
            self.acquisition["acquisition_id"], self.acquisition["generation"],
            urlsplit(url).hostname or "", policy=self.limits, now=datetime.now(UTC),
        )


_ACTIVE_NETWORK: ContextVar[_ActiveNetworkLease | None] = ContextVar(
    "owned_network_lease", default=None,
)


def check_owned_network_hop(url: str) -> None:
    active = _ACTIVE_NETWORK.get()
    if active is not None:
        active.before_hop(url)


@contextmanager
def owned_network_request(
    state: StateStore, provider: str, url: str, *, timeout_seconds: float,
    limits: NetworkLimits | None = None,
) -> Iterator[None]:
    """One existing provider call owns one slot across its bounded retries/redirects."""
    limits = limits or load_network_limits()
    budget = current_request_budget()
    if budget is not None and (
        budget.remaining_seconds() <= 0 or budget.remaining_attempts() == 0
    ):
        raise RequestBudgetExhausted("Owned network request budget exhausted before claim")
    remaining = timeout_seconds if budget is None else min(
        timeout_seconds, budget.remaining_seconds(),
    )
    now = datetime.now(UTC)
    repository = OwnedNetworkRepository(state)
    acquisition, acquired = repository.claim_acquisition(
        {"schema_version": "owned-http-dispatch-v1", "dispatch_id": uuid4().hex},
        provider=provider, domain=urlsplit(url).hostname or "", policy=limits,
        now=now, deadline_at=now + timedelta(seconds=remaining), fresh_after=now,
    )
    if not acquired:
        raise ProviderError(
            "Owned network capacity is occupied", failure_class=FailureClass.RATE_LIMITED,
            retryable=False, details={"reason_code": acquisition.get("reason_code")},
        )
    token = _ACTIVE_NETWORK.set(_ActiveNetworkLease(repository, acquisition, limits))
    try:
        repository.start_acquisition(
            acquisition["acquisition_id"], acquisition["generation"], datetime.now(UTC),
        )
        yield
    finally:
        _ACTIVE_NETWORK.reset(token)
        repository.release_transient(acquisition["acquisition_id"], acquisition["generation"])
