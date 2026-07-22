"""Stable accounting for unresolved knowledge-collection boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from astock.core.errors import FailureClass, StorageError
from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore


def count_open_gap_boundaries(
    state: StateStore,
    author_source_id: str,
    *,
    content_type: str | None = None,
    comment_scope_prefix: str | None = None,
    excluded_scope_prefix: str | None = None,
    data_cutoff_at: datetime | None = None,
) -> int:
    """Count missing cursor boundaries while preserving every failed attempt row.

    A retry of the same boundary can produce another immutable failure snapshot.  That
    is useful history, but it must not make one missing page look like two missing
    pages.  Snapshot identity is therefore excluded from the boundary key.
    """

    if content_type is None and comment_scope_prefix is not None:
        raise ValueError("comment_scope_prefix requires content_type")
    with state.connect() as connection:
        if data_cutoff_at is None or not gap_cutoff_history_available(
            state, data_cutoff_at
        ):
            rows = connection.execute(
                "SELECT g.scope_id,g.cursor_json,s.content_type FROM collection_gap g "
                "JOIN collection_scope s ON s.scope_id=g.scope_id "
                "WHERE s.author_id=? AND g.status='OPEN'",
                (author_source_id,),
            ).fetchall()
        else:
            event_rows = connection.execute(
                "SELECT e.event_id,e.gap_id,e.scope_id,e.cursor_json,e.status,"
                "e.occurred_at,s.content_type FROM collection_gap_state_event e "
                "JOIN collection_scope s ON s.scope_id=e.scope_id WHERE s.author_id=?",
                (author_source_id,),
            ).fetchall()
            exclusive_cutoff = _exclusive_millisecond_cutoff(data_cutoff_at)
            latest: dict[str, tuple[datetime, int, Any]] = {}
            for row in event_rows:
                occurred_at = _parse_utc_text(str(row["occurred_at"]))
                if occurred_at >= exclusive_cutoff:
                    continue
                key = (occurred_at, int(row["event_id"]))
                previous = latest.get(str(row["gap_id"]))
                if previous is None or key > previous[:2]:
                    latest[str(row["gap_id"])] = (*key, row)
            rows = [item[2] for item in latest.values() if str(item[2]["status"]) == "OPEN"]

    rows = [
        row
        for row in rows
        if _scope_selected(
            str(row["content_type"]),
            content_type=content_type,
            comment_scope_prefix=comment_scope_prefix,
            excluded_scope_prefix=excluded_scope_prefix,
        )
    ]

    boundaries: set[tuple[str, bytes]] = set()
    for row in rows:
        cursor = _decode_cursor(str(row["cursor_json"]), str(row["scope_id"]))
        cursor.pop("source_snapshot_id", None)
        boundaries.add((str(row["scope_id"]), canonical_json_bytes(cursor)))
    return len(boundaries)


def gap_cutoff_history_available(state: StateStore, data_cutoff_at: datetime) -> bool:
    """Return whether gap state can be reconstructed at the requested cutoff.

    SQLite records trigger timestamps at millisecond precision.  If an event shares
    the cutoff millisecond, its order relative to a sub-millisecond cutoff is
    unknowable.  Treat that boundary as unavailable so callers use current state and
    surface ``GAP_CUTOFF_HISTORY_UNAVAILABLE`` instead of silently assuming no gap.
    """

    exclusive_cutoff = _exclusive_millisecond_cutoff(data_cutoff_at)
    boundary_text = exclusive_cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "+00:00"
    with state.connect() as connection:
        row = connection.execute(
            "SELECT reliable_from FROM collection_gap_temporal_meta WHERE singleton=1"
        ).fetchone()
        boundary_event = connection.execute(
            "SELECT 1 FROM collection_gap_state_event "
            "WHERE strftime('%Y-%m-%dT%H:%M:%f+00:00',occurred_at)=? LIMIT 1",
            (boundary_text,),
        ).fetchone()
    return (
        row is not None
        and _parse_utc_text(str(row["reliable_from"])) < exclusive_cutoff
        and boundary_event is None
    )


def _exclusive_millisecond_cutoff(value: datetime) -> datetime:
    utc_value = value.astimezone(UTC)
    return utc_value.replace(microsecond=(utc_value.microsecond // 1000) * 1000)


def _parse_utc_text(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _scope_selected(
    scope: str,
    *,
    content_type: str | None,
    comment_scope_prefix: str | None,
    excluded_scope_prefix: str | None,
) -> bool:
    if excluded_scope_prefix is not None and _matches_prefix(scope, excluded_scope_prefix):
        return False
    if content_type is None:
        return True
    return scope == content_type or (
        comment_scope_prefix is not None and _matches_prefix(scope, comment_scope_prefix)
    )


def _matches_prefix(value: str, sql_like_prefix: str) -> bool:
    if not sql_like_prefix.endswith("%"):
        raise ValueError("scope prefix must end with %")
    return value.startswith(sql_like_prefix[:-1])


def _decode_cursor(raw: str, scope_id: str) -> dict[str, Any]:
    try:
        cursor = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageError(
            "Collection gap cursor JSON is invalid",
            failure_class=FailureClass.STORAGE,
            details={"scope_id": scope_id},
        ) from exc
    if not isinstance(cursor, dict):
        raise StorageError(
            "Collection gap cursor must be a JSON object",
            failure_class=FailureClass.STORAGE,
            details={"scope_id": scope_id},
        )
    return cursor
