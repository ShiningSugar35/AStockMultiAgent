from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.errors import ProviderError
from astock.knowledge import load_knowledge_sources
from astock.knowledge.transport import normalize_zhihu_api_url
from astock.schemas import (
    KnowledgeIdentityStatus,
    ZhihuContentType,
    ZhihuListingPage,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_knowledge_allowlist_validates_three_online_and_one_local_source() -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    online = [source for source in registry.sources if source.online_collection_required]
    assert {source.display_name for source in online} == {
        "MR Dang",
        "黄彦臻",
        "派大星皮皮",
    }
    local = next(source for source in registry.sources if source.display_name == "寒武纪的鳄鱼")
    assert (
        local.identity_status
        is KnowledgeIdentityStatus.LOCAL_EXPORT_USER_CONFIRMED_COMPLETE
    )
    assert not local.online_collection_required
    assert local.local_seed_sources[0].expected_sha256 == (
        "197ec18e6fabac4401f6412331e9aa50f919498d4e40cfddb481eeab9788852d"
    )


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


def test_same_origin_http_zhihu_cursor_is_upgraded_to_https() -> None:
    assert normalize_zhihu_api_url(
        "http://www.zhihu.com/api/v4/members/example/pins?limit=5&offset=5"
    ) == "https://www.zhihu.com/api/v4/members/example/pins?limit=5&offset=5"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api/v4/members/example/pins",
        "https://www.zhihu.com.evil.example/api/v4/members/example/pins",
        "https://user@www.zhihu.com/api/v4/members/example/pins",
        "https://www.zhihu.com/people/example",
    ],
)
def test_zhihu_cursor_normalization_rejects_origin_or_path_escape(url: str) -> None:
    with pytest.raises(ProviderError):
        normalize_zhihu_api_url(url)
