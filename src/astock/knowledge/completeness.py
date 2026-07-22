"""Exact count reconciliation helpers for knowledge collection boundaries."""

from __future__ import annotations

from collections.abc import Iterable

from astock.schemas import ZhihuCommentNode


def child_reply_count_mismatches(
    comments: Iterable[ZhihuCommentNode],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Compare every latest root's declared child count with unique stored replies."""

    latest: dict[tuple[str, str], ZhihuCommentNode] = {}
    for comment in comments:
        key = (comment.content_id, comment.comment_id)
        current = latest.get(key)
        if current is None or (comment.collected_at, comment.version_id) > (
            current.collected_at,
            current.version_id,
        ):
            latest[key] = comment
    reply_ids_by_root: dict[tuple[str, str], set[str]] = {}
    for comment in latest.values():
        if comment.parent_comment_id is None:
            continue
        reply_ids_by_root.setdefault((comment.content_id, comment.root_comment_id), set()).add(
            comment.comment_id
        )
    mismatches: dict[tuple[str, str], tuple[int, int]] = {}
    for root in latest.values():
        if root.parent_comment_id is not None or root.child_comment_count == 0:
            continue
        key = (root.content_id, root.comment_id)
        actual = len(reply_ids_by_root.get(key, set()))
        if actual != root.child_comment_count:
            mismatches[key] = (root.child_comment_count, actual)
    return mismatches


__all__ = ["child_reply_count_mismatches"]
