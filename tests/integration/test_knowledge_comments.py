from __future__ import annotations

import json
from pathlib import Path

import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuCommentService,
    ZhihuHttpTransport,
    derive_keyword_filtered_author_participation_chains,
    load_distillation_rules,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.knowledge.completeness import child_reply_count_mismatches
from astock.schemas import (
    CollectionTerminalCondition,
    ZhihuCommentNode,
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
    "https://www.zhihu.com/api/v4/comment_v5/comment/comment-root-1/"
    "child_comment?order_by=ts&limit=20&offset="
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


def _typed_response(state, object_store, url: str, fixture: str, content_type):
    return ZhihuHttpTransport(object_store, state).persist_imported_response(
        author_source_id="zhihu:mr-dang-77",
        content_type=content_type,
        requested_url=url,
        status_code=200,
        content_type_header="application/json",
        body=_fixture(fixture),
        transport=ZhihuTransport.CHROME,
    )


def _body_response(state, object_store, url: str, body: dict[str, object]):
    return ZhihuHttpTransport(object_store, state).persist_imported_response(
        author_source_id="zhihu:mr-dang-77",
        content_type=ZhihuContentType.ANSWERS,
        requested_url=url,
        status_code=200,
        content_type_header="application/json",
        body=json.dumps(body).encode(),
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
    assert root_checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    with state.connect() as connection:
        before_child = [
            ZhihuCommentNode.model_validate_json(row["record_json"])
            for row in connection.execute(
                "SELECT record_json FROM zhihu_comment_version"
            ).fetchall()
        ]
    assert child_reply_count_mismatches(before_child) == {
        ("answer-fixture", "comment-root-1"): (3, 1)
    }

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
    assert child_checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    chain = next(
        item for item in child.participation_chains if item.root_comment_id == "comment-root-1"
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
        all_comments = [
            ZhihuCommentNode.model_validate_json(row["record_json"])
            for row in connection.execute(
                "SELECT record_json FROM zhihu_comment_version"
            ).fetchall()
        ]
        assert child_reply_count_mismatches(all_comments) == {}
        page_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0]
        assert page_count == 3
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 6
        assert (
            connection.execute("SELECT COUNT(*) FROM zhihu_author_participation_chain").fetchone()[
                0
            ]
            >= 2
        )
    rules = load_distillation_rules(
        PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
    )
    filtered = derive_keyword_filtered_author_participation_chains(
        all_comments,
        {
            (item.content_type.value, item.content_id, item.comment_id): object_store.get_bytes(
                item.body_object_sha256
            ).decode("utf-8")
            for item in all_comments
        },
        rules,
        terminal_root_ids={("answers", "answer-fixture", "comment-root-1")},
    )
    assert len(filtered) == 1
    assert filtered[0].root_comment_id == "comment-root-1"
    assert filtered[0].matched_keyword_terms == ["估值"]
    assert filtered[0].keyword_filter_rule_version == (
        f"{rules.comment_chain_filter_version}@{rules.rule_version}"
    )


def test_comment_chain_filter_requires_keyword_terminal_and_target_author_reply(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    service.ingest_page(
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
    comments = service.repository.latest_comments(
        "zhihu:mr-dang-77", ZhihuContentType.ANSWERS, "answer-fixture"
    )
    rules = load_distillation_rules(
        PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
    )
    bodies = {
        (item.content_type.value, item.content_id, item.comment_id): object_store.get_bytes(
            item.body_object_sha256
        ).decode("utf-8")
        for item in comments
    }
    assert derive_keyword_filtered_author_participation_chains(
        comments,
        bodies,
        rules,
        terminal_root_ids=set(),
    ) == []
    bodies[("answers", "answer-fixture", "comment-root-1")] = (
        "Synthetic root with no configured term."
    )
    assert derive_keyword_filtered_author_participation_chains(
        comments,
        bodies,
        rules,
        terminal_root_ids={("answers", "answer-fixture", "comment-root-1")},
    ) == []


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


def test_child_comment_page_rejects_a_reply_from_another_root(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    payload = json.loads(_fixture("zhihu_child_comments_page_1.json"))
    payload["data"][0]["reply_root_comment_id"] = "another-root"

    with pytest.raises(ProviderError) as caught:
        service.ingest_page(
            _source(),
            ZhihuContentType.ANSWERS,
            "answer-fixture",
            parent_comment_id="comment-root-1",
            comment_page=0,
            request_cursor=None,
            response=_body_response(state, object_store, CHILD_PAGE_1, payload),
        )

    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 0


def test_child_comment_page_rejects_a_next_cursor_outside_its_root_boundary(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    payload = json.loads(_fixture("zhihu_child_comments_page_1.json"))
    payload["paging"]["is_end"] = False
    payload["paging"]["next"] = (
        "https://www.zhihu.com/api/v4/comment_v5/comment/another-root/"
        "child_comment?order_by=ts&limit=20&offset=next"
    )

    with pytest.raises(ProviderError) as caught:
        service.ingest_page(
            _source(),
            ZhihuContentType.ANSWERS,
            "answer-fixture",
            parent_comment_id="comment-root-1",
            comment_page=0,
            request_cursor=None,
            response=_body_response(state, object_store, CHILD_PAGE_1, payload),
        )

    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE


def test_comment_page_rejects_a_nonterminal_cursor_that_does_not_advance(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    payload = {
        "data": [
            {
                "id": "cycle-root",
                "content": "Synthetic cycle root.",
                "author": {"id": "reader"},
                "created_time": 1780000000,
                "child_comment_count": 0,
                "child_comments": [],
            }
        ],
        "paging": {"is_end": False, "next": ROOT_PAGE_1, "totals": 1},
    }

    with pytest.raises(ProviderError) as caught:
        service.ingest_page(
            _source(),
            ZhihuContentType.ANSWERS,
            "answer-fixture",
            parent_comment_id=None,
            comment_page=0,
            request_cursor=None,
            response=_body_response(state, object_store, ROOT_PAGE_1, payload),
        )

    assert caught.value.failure_class is FailureClass.PAGINATION_CYCLE
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0] == 0


def test_comment_page_rejects_a_changed_cursor_when_it_adds_no_new_ids(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    first_payload = {
        "data": [
            {
                "id": "cycle-root",
                "content": "Synthetic cycle root.",
                "author": {"id": "reader"},
                "created_time": 1780000000,
                "child_comment_count": 0,
                "child_comments": [],
            }
        ],
        "paging": {"is_end": False, "next": ROOT_PAGE_2, "totals": 2},
    }
    service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=_body_response(state, object_store, ROOT_PAGE_1, first_payload),
    )
    repeated_payload = {
        **first_payload,
        "paging": {
            "is_end": False,
            "next": (
                "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
                "root_comment?order_by=score&limit=2&offset=3"
            ),
            "totals": 2,
        },
    }

    with pytest.raises(ProviderError) as caught:
        service.ingest_page(
            _source(),
            ZhihuContentType.ANSWERS,
            "answer-fixture",
            parent_comment_id=None,
            comment_page=1,
            request_cursor=ROOT_PAGE_2,
            response=_body_response(state, object_store, ROOT_PAGE_2, repeated_payload),
        )

    assert caught.value.failure_class is FailureClass.PAGINATION_CYCLE
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
    )
    assert checkpoint is not None
    assert checkpoint.comment_page == 1
    assert checkpoint.comment_cursor == ROOT_PAGE_2
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0] == 1


def test_comment_page_accepts_overlap_when_the_next_page_adds_a_new_id(
    state, object_store, tmp_path
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    first_payload = {
        "data": [
            {
                "id": "overlap-root-1",
                "content": "First synthetic root.",
                "author": {"id": "reader-1"},
                "created_time": 1780000000,
                "child_comment_count": 0,
                "child_comments": [],
            },
            {
                "id": "overlap-root-2",
                "content": "Second synthetic root.",
                "author": {"id": "reader-2"},
                "created_time": 1780000001,
                "child_comment_count": 0,
                "child_comments": [],
            },
        ],
        "paging": {"is_end": False, "next": ROOT_PAGE_2, "totals": 3},
    }
    service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=_body_response(state, object_store, ROOT_PAGE_1, first_payload),
    )
    final_payload = {
        "data": [
            first_payload["data"][1],
            {
                "id": "overlap-root-3",
                "content": "New synthetic root on an overlapping page.",
                "author": {"id": "reader-3"},
                "created_time": 1780000002,
                "child_comment_count": 0,
                "child_comments": [],
            },
        ],
        "paging": {"is_end": True, "next": "", "totals": 3},
    }

    execution = service.ingest_page(
        _source(),
        ZhihuContentType.ANSWERS,
        "answer-fixture",
        parent_comment_id=None,
        comment_page=1,
        request_cursor=ROOT_PAGE_2,
        response=_body_response(state, object_store, ROOT_PAGE_2, final_payload),
    )

    assert {record.comment_id for record in execution.comment_records} == {
        "overlap-root-2",
        "overlap-root-3",
    }
    assert execution.page.is_end
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("content_type", "segment"),
    [
        (ZhihuContentType.ANSWERS, "answers"),
        (ZhihuContentType.ARTICLES, "articles"),
        (ZhihuContentType.THOUGHTS, "pins"),
    ],
)
def test_observed_root_comment_types_use_independent_terminal_checkpoints(
    state,
    object_store,
    tmp_path,
    content_type: ZhihuContentType,
    segment: str,
) -> None:
    service = ZhihuCommentService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
    )
    content_id = f"fixture-{content_type.value}"
    url = (
        f"https://www.zhihu.com/api/v4/comment_v5/{segment}/{content_id}/"
        "root_comment?order_by=score&limit=20&offset="
    )

    execution = service.ingest_page(
        _source(),
        content_type,
        content_id,
        parent_comment_id=None,
        comment_page=0,
        request_cursor=None,
        response=_typed_response(
            state,
            object_store,
            url,
            "zhihu_root_comments_page_2.json",
            content_type,
        ),
    )

    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77", content_type.value, content_id
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    assert execution.comment_records
    assert all(object_store.verify(item.body_object_sha256) for item in execution.comment_records)
    with state.connect() as connection:
        manifest = connection.execute(
            "SELECT raw_object_hash,source_snapshot_id FROM zhihu_comment_page_manifest "
            "WHERE source_id=? AND content_type=? AND content_id=?",
            ("zhihu:mr-dang-77", content_type.value, content_id),
        ).fetchone()
    assert manifest is not None
    assert object_store.verify(manifest["raw_object_hash"])
    assert state.get_snapshot(manifest["source_snapshot_id"]) is not None


def test_only_observed_comment_endpoints_are_marked_verified() -> None:
    registry = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    roots = {
        item.content_types[0]: item
        for item in registry.templates
        if item.response_kind.value == "ROOT_COMMENTS"
    }
    child = next(
        item for item in registry.templates if item.response_kind.value == "CHILD_COMMENTS"
    )
    answer_detail = next(
        item for item in registry.templates if item.template_id == "zhihu-answer-detail"
    )
    assert set(roots) == {
        ZhihuContentType.ANSWERS,
        ZhihuContentType.ARTICLES,
        ZhihuContentType.THOUGHTS,
    }
    assert all(
        item.status is ZhihuEndpointTemplateStatus.VERIFIED for item in roots.values()
    )
    assert roots[ZhihuContentType.ANSWERS].path_template == (
        "/api/v4/comment_v5/answers/{content_id}/root_comment"
    )
    assert roots[ZhihuContentType.ARTICLES].path_template == (
        "/api/v4/comment_v5/articles/{content_id}/root_comment"
    )
    assert roots[ZhihuContentType.THOUGHTS].path_template == (
        "/api/v4/comment_v5/pins/{content_id}/root_comment"
    )
    assert all(
        item.default_query == {"order_by": "score", "limit": "20", "offset": ""}
        for item in roots.values()
    )
    assert answer_detail.status is ZhihuEndpointTemplateStatus.VERIFIED
    assert answer_detail.path_template == "/api/v4/answers/{content_id}"
    assert child.status is ZhihuEndpointTemplateStatus.VERIFIED
    assert child.path_template == (
        "/api/v4/comment_v5/comment/{parent_comment_id}/child_comment"
    )
    assert child.default_query == {"order_by": "ts", "limit": "20", "offset": ""}
