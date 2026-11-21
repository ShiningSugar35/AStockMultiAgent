"""Offline target-author chain derivation from preserved comment evidence."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping

from astock.core.hashing import content_hash
from astock.schemas import (
    DistillationClassRuleSet,
    ZhihuAuthorParticipationChain,
    ZhihuCommentNode,
)

_PARTICIPATION_RULE_VERSION = "target-author-reply-ancestor-chain-v2"
_HTML_TAG = re.compile(r"<[^>]+>")
_ZERO_WIDTH = re.compile("[\u200b-\u200d\ufeff]")
_WHITESPACE = re.compile(r"\s+")


def derive_author_participation_chains(
    comments: list[ZhihuCommentNode],
) -> list[ZhihuAuthorParticipationChain]:
    by_root: dict[tuple[str, str, str], list[ZhihuCommentNode]] = {}
    for comment in comments:
        key = (comment.content_type.value, comment.content_id, comment.root_comment_id)
        by_root.setdefault(key, []).append(comment)
    chains: list[ZhihuAuthorParticipationChain] = []
    for (_, _, root_id), root_comments in sorted(by_root.items()):
        targets = [
            item
            for item in root_comments
            if item.is_target_author and item.parent_comment_id is not None
        ]
        if not targets:
            continue
        by_id = {item.comment_id: item for item in root_comments}
        selected_ids: set[str] = set()
        for target in targets:
            cursor: ZhihuCommentNode | None = target
            visited: set[str] = set()
            while cursor is not None and cursor.comment_id not in visited:
                visited.add(cursor.comment_id)
                selected_ids.add(cursor.comment_id)
                ancestor_id = cursor.reply_to_comment_id or cursor.parent_comment_id
                cursor = by_id.get(ancestor_id) if ancestor_id else None
        selected = [item for item in root_comments if item.comment_id in selected_ids]
        selected.sort(
            key=lambda item: (
                item.published_at or item.collected_at,
                item.comment_id,
            )
        )
        target_ids = sorted(item.comment_id for item in targets)
        context_ids = [item.comment_id for item in selected]
        snapshot_ids = sorted({item.raw_source_snapshot_id for item in selected})
        identity = {
            "author_source_id": targets[0].author_source_id,
            "content_type": targets[0].content_type.value,
            "content_id": targets[0].content_id,
            "root_comment_id": root_id,
            "target_author_comment_ids": target_ids,
            "ordered_context_comment_ids": context_ids,
            "source_snapshot_ids": snapshot_ids,
            "selection_rule_version": _PARTICIPATION_RULE_VERSION,
        }
        chains.append(
            ZhihuAuthorParticipationChain(
                chain_id=f"zhihu-participation:{content_hash(identity)}",
                author_source_id=targets[0].author_source_id,
                content_type=targets[0].content_type,
                content_id=targets[0].content_id,
                root_comment_id=root_id,
                target_author_comment_ids=target_ids,
                ordered_context_comment_ids=context_ids,
                source_snapshot_ids=snapshot_ids,
                selection_rule_version=_PARTICIPATION_RULE_VERSION,
                created_at=max(item.collected_at for item in selected),
            )
        )
    return chains


def derive_keyword_filtered_author_participation_chains(
    comments: list[ZhihuCommentNode],
    bodies_by_comment_id: Mapping[tuple[str, str, str], str],
    rules: DistillationClassRuleSet,
    *,
    terminal_root_ids: set[tuple[str, str, str]] | None = None,
) -> list[ZhihuAuthorParticipationChain]:
    """Keep complete target-author reply paths with any configured chain keyword."""

    filtered: list[ZhihuAuthorParticipationChain] = []
    for base in derive_author_participation_chains(comments):
        root_key = (base.content_type.value, base.content_id, base.root_comment_id)
        if terminal_root_ids is not None and root_key not in terminal_root_ids:
            continue
        normalized_bodies = [
            _normalize_comment_text(
                bodies_by_comment_id.get(
                    (base.content_type.value, base.content_id, comment_id),
                    "",
                )
            )
            for comment_id in base.ordered_context_comment_ids
        ]
        matched_terms = sorted(
            {
                term.strip()
                for terms in rules.content_class_terms.values()
                for term in terms
                if term.strip()
                and any(term.strip().casefold() in body for body in normalized_bodies)
            },
            key=lambda item: (item.casefold(), item),
        )
        if not matched_terms:
            continue
        selection_rule_version = (
            f"{_PARTICIPATION_RULE_VERSION}+{rules.comment_chain_filter_version}"
        )
        keyword_filter_rule_version = f"{rules.comment_chain_filter_version}@{rules.rule_version}"
        identity = {
            "author_source_id": base.author_source_id,
            "content_type": base.content_type.value,
            "content_id": base.content_id,
            "root_comment_id": base.root_comment_id,
            "target_author_comment_ids": base.target_author_comment_ids,
            "ordered_context_comment_ids": base.ordered_context_comment_ids,
            "source_snapshot_ids": base.source_snapshot_ids,
            "selection_rule_version": selection_rule_version,
            "keyword_filter_rule_version": keyword_filter_rule_version,
            "matched_keyword_terms": matched_terms,
        }
        filtered.append(
            base.model_copy(
                update={
                    "chain_id": f"zhihu-participation:{content_hash(identity)}",
                    "selection_rule_version": selection_rule_version,
                    "keyword_filter_rule_version": keyword_filter_rule_version,
                    "matched_keyword_terms": matched_terms,
                }
            )
        )
    return filtered


def _normalize_comment_text(value: str) -> str:
    value = _HTML_TAG.sub(" ", html.unescape(value))
    value = unicodedata.normalize("NFKC", value)
    value = _ZERO_WIDTH.sub("", value)
    return _WHITESPACE.sub(" ", value).strip().casefold()


__all__ = [
    "derive_author_participation_chains",
    "derive_keyword_filtered_author_participation_chains",
]
