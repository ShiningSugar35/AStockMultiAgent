"""One durable transport-attempt/deadline budget, shared by a research request.

Costs remain in the existing Web tool quota repository. This budget counts actual
transport starts, not router visits; an interrupted attempt is never refunded.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.state import StateStore

_ACTIVE: ContextVar[RequestExecutionBudget | None] = ContextVar(
    "research_request_budget", default=None
)
_SCOPE = "request-transport-budget"


class RequestBudgetExhausted(TimeoutError):
    """No new transport may start; already captured evidence remains usable."""


class RequestExecutionBudget:
    def __init__(
        self,
        state: StateStore,
        request_id: str,
        deadline_at: datetime,
        max_attempts: int,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not request_id.strip() or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("request budget requires an identity and positive attempt limit")
        if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
            raise ValueError("request budget deadline must be timezone-aware")
        self.state = state
        self.clock = clock or (lambda: datetime.now(UTC))
        self.key = sha256_bytes(request_id.encode("utf-8"))
        self.max_attempts = max_attempts
        self.deadline_at = deadline_at
        self._monotonic_deadline = time.monotonic() + max(
            0.0, (deadline_at - self.clock()).total_seconds()
        )
        with state.transaction() as connection:
            row = connection.execute(
                "SELECT cursor_json FROM checkpoint WHERE scope_type=? AND scope_key=?",
                (_SCOPE, self.key),
            ).fetchone()
            if row is None:
                cursor = {
                    "attempts_reserved": 0,
                    "max_attempts": max_attempts,
                    "deadline_at": deadline_at.isoformat(),
                    "status": "ACTIVE",
                }
            else:
                cursor = json.loads(row["cursor_json"])
                self.deadline_at = min(deadline_at, datetime.fromisoformat(cursor["deadline_at"]))
                self.max_attempts = min(max_attempts, cursor["max_attempts"])
                cursor.update(
                    max_attempts=self.max_attempts, deadline_at=self.deadline_at.isoformat()
                )
            connection.execute(
                "INSERT INTO checkpoint(checkpoint_id,scope_type,scope_key,cursor_json,status,"
                "committed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(scope_type,scope_key) DO UPDATE SET "
                "cursor_json=excluded.cursor_json,committed_at=excluded.committed_at",
                (
                    sha256_bytes(f"{_SCOPE}:{self.key}".encode()),
                    _SCOPE,
                    self.key,
                    canonical_json_bytes(cursor).decode(),
                    "ACTIVE",
                    self.clock().isoformat(),
                ),
            )
        self._monotonic_deadline = min(
            self._monotonic_deadline,
            time.monotonic() + max(0.0, (self.deadline_at - self.clock()).total_seconds()),
        )

    def remaining_seconds(self) -> float:
        return max(
            0.0,
            min(
                self._monotonic_deadline - time.monotonic(),
                (self.deadline_at - self.clock()).total_seconds(),
            ),
        )

    def remaining_attempts(self) -> int:
        checkpoint = self.state.get_checkpoint(_SCOPE, self.key)
        if checkpoint is None:
            raise ValueError("request budget registration disappeared")
        cursor = checkpoint["cursor"]
        return max(0, int(cursor["max_attempts"]) - int(cursor["attempts_reserved"]))

    def reserve_attempt(self) -> int:
        """Atomically consume one physical HTTP/SDK attempt before dispatch."""
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT cursor_json FROM checkpoint WHERE scope_type=? AND scope_key=?",
                (_SCOPE, self.key),
            ).fetchone()
            if row is None:
                raise ValueError("request budget registration disappeared")
            cursor = json.loads(row["cursor_json"])
            if self.remaining_seconds() <= 0 or (
                datetime.fromisoformat(cursor["deadline_at"]) <= self.clock()
            ):
                raise RequestBudgetExhausted("request elapsed budget exhausted")
            if cursor["attempts_reserved"] >= cursor["max_attempts"]:
                raise RequestBudgetExhausted("request transport attempt budget exhausted")
            cursor["attempts_reserved"] += 1
            connection.execute(
                "UPDATE checkpoint SET cursor_json=?,committed_at=? "
                "WHERE scope_type=? AND scope_key=?",
                (canonical_json_bytes(cursor).decode(), self.clock().isoformat(), _SCOPE, self.key),
            )
            return int(cursor["attempts_reserved"])

    @contextmanager
    def bind(self) -> Iterator[RequestExecutionBudget]:
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)


def current_request_budget() -> RequestExecutionBudget | None:
    return _ACTIVE.get()
