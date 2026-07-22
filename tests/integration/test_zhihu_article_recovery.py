from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.knowledge import (
    KnowledgeRepository,
    ParquetKnowledgeStore,
    ZhihuArticleHtmlTransport,
    ZhihuArticleRecoveryService,
    load_knowledge_sources,
)
from astock.knowledge.adapter import ZhihuResponseAdapter
from astock.schemas import (
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTICLE_URL = "https://zhuanlan.zhihu.com/p/367165363"


def _fixture() -> bytes:
    return (
        PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / "zhihu_article_detail.html"
    ).read_bytes()


def _registry():
    return load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")


def _source():
    return next(item for item in _registry().sources if item.source_id == "zhihu:huang-wei-yan-30")


def _transport(state, object_store, handler) -> ZhihuArticleHtmlTransport:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return ZhihuArticleHtmlTransport(object_store, state, client=client)


def test_article_html_transport_persists_before_strict_visible_body_parse(
    state,
    object_store,
) -> None:
    transport = _transport(
        state,
        object_store,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=_fixture(),
            request=request,
        ),
    )
    response = transport.fetch(
        author_source_id=_source().source_id,
        content_type=ZhihuContentType.ARTICLES,
        url=ARTICLE_URL,
    )
    record = ZhihuResponseAdapter(object_store).parse_article_html(
        _source(),
        "367165363",
        response,
        platform_author_id="fixture-author-id",
    )

    assert response.snapshot.source_url == ARTICLE_URL
    assert object_store.verify(response.snapshot.object_sha256)
    registered_snapshot = state.get_snapshot(response.snapshot.snapshot_id)
    assert registered_snapshot is not None
    assert registered_snapshot.object_sha256 == response.snapshot.object_sha256
    assert registered_snapshot.source_url == ARTICLE_URL
    assert record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
    assert record.canonical_url == ARTICLE_URL
    assert record.title == "Synthetic fixture article"
    normalized = object_store.get_bytes(record.body_object_sha256).decode("utf-8")
    assert "Synthetic complete article paragraph" in normalized
    assert "Synthetic comment text" not in normalized
    assert "this must never enter" not in normalized


@pytest.mark.parametrize(
    "url",
    [
        "http://zhuanlan.zhihu.com/p/367165363",
        "https://www.zhihu.com/p/367165363",
        "https://zhuanlan.zhihu.com/p/not-a-number",
        "https://zhuanlan.zhihu.com/p/367165363?share=1",
        "https://user@zhuanlan.zhihu.com/p/367165363",
    ],
)
def test_article_html_transport_rejects_noncanonical_boundaries(
    state,
    object_store,
    url: str,
) -> None:
    transport = _transport(
        state,
        object_store,
        lambda request: httpx.Response(200, content=_fixture(), request=request),
    )

    with pytest.raises(ProviderError) as caught:
        transport.fetch(
            author_source_id=_source().source_id,
            content_type=ZhihuContentType.ARTICLES,
            url=url,
        )

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED


@pytest.mark.parametrize(
    "body",
    [
        b"<html><title>missing article body</title></html>",
        _fixture().replace(b"/p/367165363", b"/p/999"),
        _fixture().replace(b"Post-RichText", b"Post-RichText data-truncated=\"true\""),
    ],
)
def test_article_html_parser_rejects_missing_wrong_or_truncated_body(
    state,
    object_store,
    body: bytes,
) -> None:
    response = ZhihuArticleHtmlTransport(object_store, state).persist_imported_response(
        author_source_id=_source().source_id,
        requested_url=ARTICLE_URL,
        status_code=200,
        content_type_header="text/html; charset=utf-8",
        body=body,
    )

    with pytest.raises(ProviderError) as caught:
        ZhihuResponseAdapter(object_store).parse_article_html(
            _source(), "367165363", response
        )

    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE


def test_article_recovery_registers_verified_version_and_parquet(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    repository = KnowledgeRepository(state)
    empty = object_store.put_bytes(b"")
    metadata = {
        "author_source_id": _source().source_id,
        "content_id": "367165363",
        "content_type": ZhihuContentType.ARTICLES.value,
        "content_completeness": ZhihuContentCompleteness.LISTING_UNVERIFIED.value,
    }
    listing = ZhihuContentRecord(
        version_id=f"fixture-listing:{content_hash(metadata)}",
        author_source_id=_source().source_id,
        platform_author_id="fixture-author-id",
        content_id="367165363",
        content_type=ZhihuContentType.ARTICLES,
        canonical_url=ARTICLE_URL,
        title="Synthetic fixture article",
        collected_at=datetime.now(UTC),
        body_object_sha256=empty.sha256,
        metadata_sha256=content_hash(metadata),
        raw_source_snapshot_id="fixture-listing-snapshot",
        content_completeness=ZhihuContentCompleteness.LISTING_UNVERIFIED,
    )
    repository.register_content(listing)
    transport = _transport(
        state,
        object_store,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=_fixture(),
            request=request,
        ),
    )
    service = ZhihuArticleRecoveryService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        _registry(),
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    execution = service.run(source_ids=[_source().source_id])

    assert execution.status == "COMPLETE"
    assert execution.request_count == 1
    assert execution.detail_verified_count == 1
    assert execution.remaining_count == 0
    latest = repository.latest_content_records(
        _source().source_id, ZhihuContentType.ARTICLES
    )
    assert len(latest) == 1
    assert latest[0].content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
    parquet_files = list((tmp_path / "parquet").rglob("*.parquet"))
    assert len(parquet_files) == 1


def test_article_recovery_stops_on_access_restriction_without_false_detail(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    repository = KnowledgeRepository(state)
    empty = object_store.put_bytes(b"")
    metadata = {"content_id": "367165363", "state": "listing"}
    repository.register_content(
        ZhihuContentRecord(
            version_id=f"fixture-listing:{content_hash(metadata)}",
            author_source_id=_source().source_id,
            content_id="367165363",
            content_type=ZhihuContentType.ARTICLES,
            canonical_url=ARTICLE_URL,
            collected_at=datetime.now(UTC),
            body_object_sha256=empty.sha256,
            metadata_sha256=content_hash(metadata),
            raw_source_snapshot_id="fixture-listing-snapshot",
            content_completeness=ZhihuContentCompleteness.LISTING_UNVERIFIED,
        )
    )
    transport = _transport(
        state,
        object_store,
        lambda request: httpx.Response(
            403,
            headers={"content-type": "text/html"},
            content=b"restricted",
            request=request,
        ),
    )
    service = ZhihuArticleRecoveryService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        _registry(),
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    execution = service.run(source_ids=[_source().source_id])

    assert execution.status == "STOPPED"
    assert execution.response_failure is FailureClass.ACCESS_RESTRICTED
    assert execution.detail_verified_count == 0
    assert execution.remaining_count == 1
    assert execution.last_source_snapshot_id is not None
    assert object_store.verify(execution.last_source_snapshot_id.rsplit(":", 1)[1])
