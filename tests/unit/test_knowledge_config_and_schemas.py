from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.knowledge import load_knowledge_sources
from astock.schemas import (
    KnowledgeCollectionScope,
    ZhihuContainerType,
    ZhihuContentType,
    ZhihuListingPage,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_archived_source_registry_has_no_online_collection() -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    assert len(registry.sources) == 4
    assert all(not source.online_collection_required for source in registry.sources)
    assert all(not source.collection_scope.incremental_updates for source in registry.sources)
    local = next(source for source in registry.sources if source.source_id == "zhihu:hanwujideeyu")
    assert local.local_seed_sources[0].expected_sha256 == (
        "197ec18e6fabac4401f6412331e9aa50f919498d4e40cfddb481eeab9788852d"
    )


def test_columns_are_declared_only_as_unique_container_types() -> None:
    base = {
        "history_mode": "FULL_ACCESSIBLE_HISTORY",
        "content_types": ["answers"],
        "include_question_context": True,
        "include_required_comment_pages": False,
        "include_nested_replies": False,
        "derive_author_participation_chains": False,
        "incremental_updates": True,
    }
    scope = KnowledgeCollectionScope.model_validate({**base, "container_types": ["columns"]})
    assert scope.container_types == [ZhihuContainerType.COLUMNS]
    with pytest.raises(ValidationError, match="container type"):
        KnowledgeCollectionScope.model_validate({**base, "content_types": ["answers", "columns"]})
    with pytest.raises(ValidationError, match="container types must be unique"):
        KnowledgeCollectionScope.model_validate({**base, "container_types": ["columns", "columns"]})


def test_non_terminal_zhihu_page_requires_resumable_next_cursor() -> None:
    with pytest.raises(ValidationError, match="next cursor"):
        ZhihuListingPage(
            page_id="page",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            listing_page=0,
            request_url="https://www.zhihu.com/api/v4/example",
            is_end=False,
            content_ids=["1"],
            source_snapshot_id="snapshot",
            raw_object_sha256="a" * 64,
            transport=ZhihuTransport.PYTHON_HTTP,
            http_status=200,
            response_structure_version="fixture-v1",
            fetched_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
