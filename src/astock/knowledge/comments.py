"""Deterministic comment-page ingestion and target-author chain derivation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.adapter import ZhihuResponseAdapter
from astock.knowledge.repository import CommentRegistration, KnowledgeRepository
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import PersistedZhihuResponse
from astock.schemas import (
    CollectionCheckpoint,
    CollectionTerminalCondition,
    KnowledgeSourceDefinition,
    ZhihuAuthorParticipationChain,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContentType,
)

_PARTICIPATION_RULE_VERSION = "target-author-ancestor-chain-v1"


@dataclass(frozen=True, slots=True)
class ZhihuCommentIngestExecution:
    page: ZhihuCommentPage
    comment_records: tuple[ZhihuCommentNode, ...]
    registrations: tuple[CommentRegistration, ...]
    parquet_files: tuple[Path, ...]
    participation_chains: tuple[ZhihuAuthorParticipationChain, ...]


class ZhihuCommentService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        parquet_store: ParquetKnowledgeStore,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.repository = KnowledgeRepository(state)
        self.adapter = ZhihuResponseAdapter(object_store)

    def ingest_page(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        content_id: str,
        *,
        parent_comment_id: str | None,
        comment_page: int,
        request_cursor: str | None,
        response: PersistedZhihuResponse,
    ) -> ZhihuCommentIngestExecution:
        page, parsed = self.adapter.parse_comment_page(
            source,
            content_type,
            content_id,
            parent_comment_id=parent_comment_id,
            comment_page=comment_page,
            request_cursor=request_cursor,
            response=response,
        )
        registrations: list[CommentRegistration] = []
        files: list[Path] = []
        stored_records: list[ZhihuCommentNode] = []
        for record in parsed:
            registration = self.repository.register_comment(record)
            registrations.append(registration)
            stored_records.append(registration.record)
            files.append(self.parquet_store.write_comment(registration.record))
        page = self.repository.register_comment_page(page)
        checkpoint = _next_comment_checkpoint(page)
        self.state.set_collection_checkpoint(
            checkpoint,
            status=("SUCCEEDED" if checkpoint.terminal_condition else "RUNNING"),
            object_hash=page.raw_object_sha256,
        )
        page_object = self.object_store.put_json(page.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"ZhihuCommentPage:{page.page_id}",
            artifact_type="ZhihuCommentPage",
            schema_version=page.schema_version,
            object_hash=page_object.sha256,
            input_hashes=[page.source_snapshot_id],
        )
        all_comments = self.repository.latest_comments(
            source.source_id,
            content_type,
            content_id,
        )
        chains = derive_author_participation_chains(all_comments)
        for chain in chains:
            chain_object = self.object_store.put_json(chain.model_dump(mode="json"))
            self.repository.register_participation_chain(
                chain,
                object_hash=chain_object.sha256,
            )
            self.state.register_artifact(
                artifact_id=f"ZhihuAuthorParticipationChain:{chain.chain_id}",
                artifact_type="ZhihuAuthorParticipationChain",
                schema_version=chain.schema_version,
                object_hash=chain_object.sha256,
                input_hashes=chain.source_snapshot_ids,
            )
        return ZhihuCommentIngestExecution(
            page=page,
            comment_records=tuple(stored_records),
            registrations=tuple(registrations),
            parquet_files=tuple(files),
            participation_chains=tuple(chains),
        )


def derive_author_participation_chains(
    comments: list[ZhihuCommentNode],
) -> list[ZhihuAuthorParticipationChain]:
    by_root: dict[str, list[ZhihuCommentNode]] = {}
    for comment in comments:
        by_root.setdefault(comment.root_comment_id, []).append(comment)
    chains: list[ZhihuAuthorParticipationChain] = []
    for root_id, root_comments in sorted(by_root.items()):
        targets = [item for item in root_comments if item.is_target_author]
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


def _next_comment_checkpoint(page: ZhihuCommentPage) -> CollectionCheckpoint:
    terminal: CollectionTerminalCondition | None = None
    if page.is_end:
        terminal = (
            CollectionTerminalCondition.CONFIRMED_EMPTY
            if not page.comment_ids
            else CollectionTerminalCondition.PAGINATION_COMPLETE
        )
    return CollectionCheckpoint(
        author=page.author_source_id,
        content_type=page.content_type.value,
        listing_page=0,
        content_id=page.content_id,
        comment_parent_id=page.parent_comment_id,
        comment_page=page.comment_page if page.is_end else page.comment_page + 1,
        comment_cursor=page.request_url if page.is_end else page.next_cursor,
        nested_reply_cursor=(
            (page.request_url if page.is_end else page.next_cursor)
            if page.parent_comment_id
            else None
        ),
        terminal_condition=terminal,
    )


__all__ = [
    "ZhihuCommentIngestExecution",
    "ZhihuCommentService",
    "derive_author_participation_chains",
]
