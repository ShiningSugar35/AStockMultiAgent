from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.core.errors import StorageError
from astock.knowledge.gaps import (
    count_open_gap_boundaries,
    gap_cutoff_history_available,
)

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
        count_open_gap_boundaries(
            state,
            _AUTHOR,
            excluded_scope_prefix="comments:%",
        )
        == 1
    )
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


def test_gap_count_reconstructs_open_state_at_cutoff_and_excludes_retired_scope(
    state,
) -> None:
    cutoff = datetime(2026, 7, 22, 12, tzinfo=UTC)
    answer_scope = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="answers",
        status="PARTIAL",
    )
    for suffix in ("first", "retry"):
        _record_gap(
            state,
            answer_scope,
            {"offset": 0, "source_snapshot_id": f"snapshot:{suffix}"},
        )
    late_scope = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="thoughts",
        status="PARTIAL",
    )
    _record_gap(state, late_scope, {"offset": 20})
    comment_scope = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="comments:answers:answer-1:__root__",
        status="PARTIAL",
    )
    _record_gap(state, comment_scope, {"offset": 0})

    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ((cutoff - timedelta(hours=1)).isoformat(),),
        )
        answer_gap_ids = [
            row[0]
            for row in connection.execute(
                "SELECT gap_id FROM collection_gap WHERE scope_id=? ORDER BY gap_id",
                (answer_scope,),
            ).fetchall()
        ]
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? "
            f"WHERE gap_id IN ({','.join('?' for _ in answer_gap_ids)})",
            ((cutoff - timedelta(minutes=1)).isoformat(), *answer_gap_ids),
        )
        connection.execute(
            "UPDATE collection_gap SET status='RESOLVED' WHERE scope_id=?",
            (answer_scope,),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? "
            "WHERE scope_id=? AND status='RESOLVED'",
            ((cutoff + timedelta(minutes=1)).isoformat(), answer_scope),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE scope_id=?",
            ((cutoff + timedelta(minutes=1)).isoformat(), late_scope),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE scope_id=?",
            ((cutoff - timedelta(minutes=1)).isoformat(), comment_scope),
        )

    assert gap_cutoff_history_available(state, cutoff)
    assert count_open_gap_boundaries(state, _AUTHOR, data_cutoff_at=cutoff) == 2
    assert (
        count_open_gap_boundaries(
            state,
            _AUTHOR,
            excluded_scope_prefix="comments:%",
            data_cutoff_at=cutoff,
        )
        == 1
    )
    assert (
        count_open_gap_boundaries(
            state,
            _AUTHOR,
            content_type="answers",
            data_cutoff_at=cutoff,
        )
        == 1
    )


def test_gap_count_uses_current_state_before_temporal_history_is_reliable(state) -> None:
    cutoff = datetime(2026, 7, 22, 12, tzinfo=UTC)
    scope_id = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="answers",
        status="PARTIAL",
    )
    _record_gap(state, scope_id, {"offset": 0})
    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ((cutoff + timedelta(minutes=1)).isoformat(),),
        )

    assert not gap_cutoff_history_available(state, cutoff)
    assert count_open_gap_boundaries(state, _AUTHOR, data_cutoff_at=cutoff) == 1


def test_gap_cutoff_uses_current_open_state_when_boundary_millisecond_is_ambiguous(
    state,
) -> None:
    cutoff = datetime(2026, 7, 22, 12, 0, 0, 123500, tzinfo=UTC)
    scope_id = state.upsert_collection_scope(
        author_id=_AUTHOR,
        content_type="answers",
        status="PARTIAL",
    )
    _record_gap(state, scope_id, {"offset": 0})
    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ("2026-07-22T11:59:59.999+00:00",),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE scope_id=?",
            ("2026-07-22T12:00:00.123+00:00", scope_id),
        )

    assert not gap_cutoff_history_available(state, cutoff)
    assert count_open_gap_boundaries(state, _AUTHOR, data_cutoff_at=cutoff) == 1
