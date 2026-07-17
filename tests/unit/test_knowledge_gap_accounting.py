from __future__ import annotations

import pytest

from astock.core.errors import StorageError
from astock.knowledge.gaps import count_open_gap_boundaries

_AUTHOR = "zhihu:gap-accounting"


def _record_gap(state, scope_id: str, cursor: dict[str, object]) -> None:
    state.record_collection_gap(
        scope_id=scope_id,
        cursor=cursor,
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )


def test_open_gap_count_preserves_attempts_but_deduplicates_boundaries(state) -> None:
    listing_scope = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="answers",
        status="ACCESS_RESTRICTED",
    )
    for snapshot_id in ("snapshot:first", "snapshot:retry"):
        _record_gap(
            state,
            listing_scope,
            {
                "listing_page": 0,
                "listing_cursor": None,
                "source_snapshot_id": snapshot_id,
            },
        )

    comment_scope = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="comments:answers:answer-1:__root__",
        status="ACCESS_RESTRICTED",
    )
    for snapshot_id in ("snapshot:comment-first", "snapshot:comment-retry"):
        _record_gap(
            state,
            comment_scope,
            {
                "comment_page": 0,
                "comment_cursor": None,
                "source_snapshot_id": snapshot_id,
            },
        )
    _record_gap(
        state,
        comment_scope,
        {
            "comment_page": 1,
            "comment_cursor": "next-page",
            "source_snapshot_id": "snapshot:comment-next",
        },
    )

    assert count_open_gap_boundaries(state, _AUTHOR) == 3
    assert (
        count_open_gap_boundaries(state, _AUTHOR, content_type="answers") == 1
    )
    assert (
        count_open_gap_boundaries(
            state,
            _AUTHOR,
            content_type="answers",
            comment_scope_prefix="comments:answers:%",
        )
        == 3
    )
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap WHERE status='OPEN'"
        ).fetchone()[0] == 5


def test_invalid_gap_cursor_is_a_storage_error(state) -> None:
    scope_id = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="articles",
        status="PARTIAL",
    )
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO collection_gap(gap_id,scope_id,cursor_json,failure_class,"
            "retryable,status) VALUES(?,?,?,?,?,?)",
            ("gap:corrupt", scope_id, "not-json", "STORAGE", 0, "OPEN"),
        )

    with pytest.raises(StorageError, match="cursor JSON is invalid"):
        count_open_gap_boundaries(state, _AUTHOR)


def test_comment_scope_prefix_requires_a_content_type(state) -> None:
    with pytest.raises(ValueError, match="requires content_type"):
        count_open_gap_boundaries(
            state,
            _AUTHOR,
            comment_scope_prefix="comments:answers:%",
        )
