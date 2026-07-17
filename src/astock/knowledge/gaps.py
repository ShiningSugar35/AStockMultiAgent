"""Stable accounting for unresolved knowledge-collection boundaries."""

from __future__ import annotations

import json
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
) -> int:
    """Count missing cursor boundaries while preserving every failed attempt row.

    A retry of the same boundary can produce another immutable failure snapshot.  That
    is useful history, but it must not make one missing page look like two missing
    pages.  Snapshot identity is therefore excluded from the boundary key.
    """

    query = (
        "SELECT g.scope_id,g.cursor_json FROM collection_gap g "
        "JOIN collection_scope s ON s.scope_id=g.scope_id "
        "WHERE s.author_id=? AND g.status='OPEN'"
    )
    parameters: list[str] = [author_source_id]
    if content_type is not None:
        if comment_scope_prefix is None:
            query += " AND s.content_type=?"
            parameters.append(content_type)
        else:
            query += " AND (s.content_type=? OR s.content_type LIKE ?)"
            parameters.extend((content_type, comment_scope_prefix))
    elif comment_scope_prefix is not None:
        raise ValueError("comment_scope_prefix requires content_type")

    with state.connect() as connection:
        rows = connection.execute(query, parameters).fetchall()

    boundaries: set[tuple[str, bytes]] = set()
    for row in rows:
        cursor = _decode_cursor(str(row["cursor_json"]), str(row["scope_id"]))
        cursor.pop("source_snapshot_id", None)
        boundaries.add((str(row["scope_id"]), canonical_json_bytes(cursor)))
    return len(boundaries)


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
