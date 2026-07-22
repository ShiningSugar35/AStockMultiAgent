"""Low-frequency recovery of enumerated Zhihu articles from canonical HTML pages."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.adapter import ZhihuResponseAdapter
from astock.knowledge.config import get_knowledge_source
from astock.knowledge.repository import KnowledgeRepository
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import (
    ZhihuArticleHtmlTransport,
    ZhihuResponseTransport,
    classify_article_html_failure,
)
from astock.schemas import (
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    ZhihuContentCompleteness,
    ZhihuContentType,
)


class ZhihuArticleRecoveryExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    request_count: int
    detail_verified_count: int
    remaining_count: int
    last_content_id: str | None
    last_source_snapshot_id: str | None
    response_failure: FailureClass | None
    limit_reached: bool


class ZhihuArticleRecoveryService:
    """Recover only article IDs already enumerated by completed author listings."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        parquet_store: ParquetKnowledgeStore,
        source_registry: KnowledgeSourceRegistry,
        *,
        transport: ZhihuResponseTransport | None = None,
        request_interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_interval_seconds < 2:
            raise ValueError("request_interval_seconds must be at least 2")
        self.state = state
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.source_registry = source_registry
        self.transport = transport or ZhihuArticleHtmlTransport(object_store, state)
        self.request_interval_seconds = request_interval_seconds
        self.sleeper = sleeper
        self.repository = KnowledgeRepository(state)
        self.adapter = ZhihuResponseAdapter(object_store)

    def run(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        max_requests: int | None = None,
    ) -> ZhihuArticleRecoveryExecution:
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be positive")
        selected = list(source_ids) if source_ids is not None else [
            source.source_id
            for source in self.source_registry.sources
            if source.enabled and source.online_collection_required
        ]
        sources = [get_knowledge_source(self.source_registry, source_id) for source_id in selected]
        pending = [
            (source, record)
            for source in sources
            if ZhihuContentType.ARTICLES.value in source.collection_scope.content_types
            for record in self.repository.latest_content_records(
                source.source_id, ZhihuContentType.ARTICLES
            )
            if record.content_completeness is not ZhihuContentCompleteness.DETAIL_VERIFIED
        ]
        pending.sort(key=lambda item: (item[0].source_id, item[1].content_id))
        request_count = 0
        verified_count = 0
        last_content_id: str | None = None
        last_snapshot_id: str | None = None
        failure: FailureClass | None = None
        for source, record in pending:
            if max_requests is not None and request_count >= max_requests:
                break
            response = self.transport.fetch(
                author_source_id=source.source_id,
                content_type=ZhihuContentType.ARTICLES,
                url=record.canonical_url,
            )
            request_count += 1
            last_content_id = record.content_id
            last_snapshot_id = response.snapshot.snapshot_id
            failure = classify_article_html_failure(response)
            if failure is not None:
                break
            try:
                parsed = self.adapter.parse_article_html(
                    source,
                    record.content_id,
                    response,
                    platform_author_id=record.platform_author_id,
                )
            except ProviderError as exc:
                failure = exc.failure_class
                break
            registration = self.repository.register_content(parsed)
            self.parquet_store.write(registration.record)
            self._resolve_detail_gap(source.source_id, record.content_id, record.canonical_url)
            verified_count += 1
            if max_requests is None or request_count < max_requests:
                self.sleeper(self.request_interval_seconds)
        remaining = self._remaining_count(sources)
        limit_reached = bool(
            failure is None
            and max_requests is not None
            and request_count >= max_requests
            and remaining > 0
        )
        status = (
            "STOPPED"
            if failure is not None
            else ("PARTIAL_LIMIT_REACHED" if limit_reached else "COMPLETE")
        )
        return ZhihuArticleRecoveryExecution(
            status=status,
            request_count=request_count,
            detail_verified_count=verified_count,
            remaining_count=remaining,
            last_content_id=last_content_id,
            last_source_snapshot_id=last_snapshot_id,
            response_failure=failure,
            limit_reached=limit_reached,
        )

    def _remaining_count(self, sources: Sequence[KnowledgeSourceDefinition]) -> int:
        return sum(
            record.content_completeness is not ZhihuContentCompleteness.DETAIL_VERIFIED
            for source in sources
            for record in self.repository.latest_content_records(
                source.source_id, ZhihuContentType.ARTICLES
            )
        )

    def _resolve_detail_gap(self, source_id: str, content_id: str, url: str) -> None:
        detail_scope = f"detail:articles:{content_id}"
        with self.state.transaction() as connection:
            scope = connection.execute(
                "SELECT scope_id FROM collection_scope WHERE author_id=? AND content_type=?",
                (source_id, detail_scope),
            ).fetchone()
            if scope is None:
                return
            connection.execute(
                "UPDATE collection_scope SET status='COMPLETE',last_cursor=?,"
                "terminal_condition='PAGINATION_COMPLETE' WHERE scope_id=?",
                (url, scope["scope_id"]),
            )
            connection.execute(
                "UPDATE collection_gap SET status='RESOLVED' WHERE scope_id=? AND status='OPEN'",
                (scope["scope_id"],),
            )


__all__ = ["ZhihuArticleRecoveryExecution", "ZhihuArticleRecoveryService"]
