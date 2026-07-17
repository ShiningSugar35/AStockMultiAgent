from __future__ import annotations

from pathlib import Path

from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuCommentService,
    ZhihuHttpTransport,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.schemas import (
    CollectionTerminalCondition,
    ZhihuContentType,
    ZhihuEndpointTemplateStatus,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_PAGE_1 = (
    "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
    "root_comment?order_by=score&limit=2&offset="
)
ROOT_PAGE_2 = (
    "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
    "root_comment?order_by=score&limit=2&offset=2"
)
CHILD_PAGE_1 = (
    "https://www.zhihu.com/api/v4/synthetic-fixture/child-comments/"
    "comment-root-1?limit=20&offset="
)


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


def _source():
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    return next(item for item in registry.sources if item.source_id == "zhihu:mr-dang-77")


def _response(state, object_store, url: str, fixture: str):
    return ZhihuHttpTransport(object_store, state).persist_imported_response(
        author_source_id="zhihu:mr-dang-77",
        content_type=ZhihuContentType.ANSWERS,
        requested_url=url,
        status_code=200,
        content_type_header="application/json",
        body=_fixture(fixture),
        transport=ZhihuTransport.CHROME,
    )


def test_comment_pages_are_versioned_checkpointed_and_distilled_to_author_chains(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    first = service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=_response(
            state,
            object_store,
            ROOT_PAGE_1,
            "zhihu_root_comments_page_1.json",
        ),
    )

    assert len(first.comment_records) == 3
    assert [item.status for item in first.registrations] == ["NEW", "NEW", "NEW"]
    assert len(first.parquet_files) == 3
    assert all(path.is_file() for path in first.parquet_files)
    root_checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77", "answers", "answer-fixture"
    )
    assert root_checkpoint is not None
    assert root_checkpoint.comment_page == 1
    assert root_checkpoint.comment_cursor == ROOT_PAGE_2
    assert root_checkpoint.terminal_condition is None

    second = service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=1,
        request_cursor=ROOT_PAGE_2,
        response=_response(
            state,
            object_store,
            ROOT_PAGE_2,
            "zhihu_root_comments_page_2.json",
        ),
    )
    assert len(second.comment_records) == 1
    root_checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77", "answers", "answer-fixture"
    )
    assert root_checkpoint is not None
    assert (
        root_checkpoint.terminal_condition
        is CollectionTerminalCondition.PAGINATION_COMPLETE
    )

    child = service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id="comment-root-1",
        comment_page=0,
        request_cursor=None,
        response=_response(
            state,
            object_store,
            CHILD_PAGE_1,
            "zhihu_child_comments_page_1.json",
        ),
    )
    child_checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
        "comment-root-1",
    )
    assert child_checkpoint is not None
    assert (
        child_checkpoint.terminal_condition
        is CollectionTerminalCondition.PAGINATION_COMPLETE
    )
    chain = next(
        item
        for item in child.participation_chains
        if item.root_comment_id == "comment-root-1"
    )
    assert chain.target_author_comment_ids == ["comment-author-1", "comment-author-2"]
    assert chain.ordered_context_comment_ids == [
        "comment-root-1",
        "comment-author-1",
        "comment-reader-followup",
        "comment-author-2",
    ]
    assert all(object_store.verify(item.body_object_sha256) for item in child.comment_records)
    with state.connect() as connection:
        page_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0]
        assert page_count == 3
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM zhihu_author_participation_chain"
        ).fetchone()[0] >= 2


def test_repeated_comment_page_is_idempotent(state, object_store, tmp_path) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    response = _response(
        state,
        object_store,
        ROOT_PAGE_1,
        "zhihu_root_comments_page_1.json",
    )
    service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=response,
    )
    repeated = service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=response,
    )

    assert {item.status for item in repeated.registrations} == {"DUPLICATE"}
    with state.connect() as connection:
        page_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0]
        assert page_count == 1
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 3


def test_only_observed_comment_endpoint_is_marked_verified() -> None:
    registry = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    root = next(
        item for item in registry.templates if item.response_kind.value == "ROOT_COMMENTS"
    )
    child = next(
        item for item in registry.templates if item.response_kind.value == "CHILD_COMMENTS"
    )
    assert root.status is ZhihuEndpointTemplateStatus.VERIFIED
    assert root.path_template == "/api/v4/comment_v5/answers/{content_id}/root_comment"
    assert child.status is ZhihuEndpointTemplateStatus.PENDING_OBSERVATION
    assert child.path_template is None
