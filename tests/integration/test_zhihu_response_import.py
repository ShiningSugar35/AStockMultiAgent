from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuResponseImportService,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.schemas import (
    ZhihuBrowserResponseEnvelope,
    ZhihuContentCompleteness,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuEndpointTemplateStatus,
    ZhihuImportStatus,
    ZhihuResponseKind,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=0&sort_by=created"
)
PAGE_2_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=2&sort_by=created"
)
COMMENT_URL = (
    "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
    "root_comment?order_by=score&limit=20&offset="
)
DETAIL_URL = "https://www.zhihu.com/api/v4/answers/101?include=content"
THOUGHT_DETAIL_URL = "https://www.zhihu.com/api/v4/pins/2055972101819986793?include=content"
ARTICLE_DETAIL_URL = "https://zhuanlan.zhihu.com/p/367165363"


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


def _endpoint_registry() -> ZhihuEndpointTemplateRegistry:
    return load_zhihu_endpoint_templates(PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml")


def _verified_detail_registry() -> ZhihuEndpointTemplateRegistry:
    registry = _endpoint_registry()
    templates = [
        (
            item.model_copy(
                update={
                    "path_template": "/api/v4/answers/{content_id}",
                    "status": ZhihuEndpointTemplateStatus.VERIFIED,
                    "observation_evidence": "SYNTHETIC_VERIFIED_DETAIL_TEST",
                }
            )
            if item.template_id == "zhihu-answer-detail"
            else item
        )
        for item in registry.templates
    ]
    return ZhihuEndpointTemplateRegistry(templates=templates)


def _envelope(body: bytes, **updates: object) -> ZhihuBrowserResponseEnvelope:
    values: dict[str, object] = {
        "author_source_id": "zhihu:mr-dang-77",
        "response_kind": ZhihuResponseKind.LISTING,
        "content_type": ZhihuContentType.ANSWERS,
        "listing_page": 0,
        "requested_url": LISTING_URL,
        "status_code": 200,
        "response_mime": "application/json",
        "body_base64": base64.b64encode(body).decode("ascii"),
        "transport": ZhihuTransport.CHROME,
        "captured_at": datetime.now(UTC),
    }
    values.update(updates)
    return ZhihuBrowserResponseEnvelope.model_validate(values)


def _comment_envelope(
    body: bytes,
    **updates: object,
) -> ZhihuBrowserResponseEnvelope:
    values: dict[str, object] = {
        "author_source_id": "zhihu:mr-dang-77",
        "response_kind": ZhihuResponseKind.ROOT_COMMENTS,
        "content_type": ZhihuContentType.ANSWERS,
        "content_id": "answer-fixture",
        "comment_page": 0,
        "requested_url": COMMENT_URL,
        "status_code": 200,
        "response_mime": "application/json",
        "body_base64": base64.b64encode(body).decode("ascii"),
        "transport": ZhihuTransport.CHROME,
        "captured_at": datetime.now(UTC),
    }
    values.update(updates)
    return ZhihuBrowserResponseEnvelope.model_validate(values)


def _detail_envelope(
    body: bytes,
    **updates: object,
) -> ZhihuBrowserResponseEnvelope:
    values: dict[str, object] = {
        "author_source_id": "zhihu:mr-dang-77",
        "response_kind": ZhihuResponseKind.CONTENT_DETAIL,
        "content_type": ZhihuContentType.ANSWERS,
        "content_id": "101",
        "requested_url": DETAIL_URL,
        "status_code": 200,
        "response_mime": "application/json",
        "body_base64": base64.b64encode(body).decode("ascii"),
        "transport": ZhihuTransport.CHROME,
        "captured_at": datetime.now(UTC),
    }
    values.update(updates)
    return ZhihuBrowserResponseEnvelope.model_validate(values)


def test_browser_response_import_is_immutable_idempotent_and_credential_free(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    marker = b'{"data":[],"paging":{"is_end":true},"private_marker":"not-output"}'
    envelope_path = runtime / "response.json"
    envelope_path.write_text(_envelope(marker).model_dump_json(), encoding="utf-8")
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    first = service.import_file(envelope_path, registry)
    repeated = service.import_file(envelope_path, registry)

    assert first.record == repeated.record
    assert first.record.import_status is ZhihuImportStatus.PENDING
    assert first.record.body_byte_size == len(marker)
    assert object_store.get_bytes(first.record.raw_object_sha256) == marker
    assert service.repository.pending_import_count("zhihu:mr-dang-77") == 1
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_imported_response").fetchone()[0] == 1
        decisions = connection.execute(
            "SELECT selected_transport FROM source_access_decision "
            "WHERE requested_capability='zhihu-response-import'"
        ).fetchall()
    assert {row[0] for row in decisions} == {"BROWSER"}


def test_imported_listing_pages_replay_through_normal_checkpoint_pipeline(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    first_path = runtime / "page-1.json"
    first_path.write_text(
        _envelope(_fixture("zhihu_answers_page_1.json")).model_dump_json(),
        encoding="utf-8",
    )
    first_import = service.import_file(first_path, registry)

    first_replay = service.replay_listing(
        first_import.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert first_replay.sync_execution is not None
    assert first_replay.sync_execution.report.coverage_status.value == "PARTIAL"
    assert first_replay.record.import_status is ZhihuImportStatus.CONSUMED
    checkpoint = state.get_collection_checkpoint("zhihu:mr-dang-77", "answers")
    assert checkpoint is not None
    assert checkpoint.listing_page == 1
    assert checkpoint.listing_cursor == PAGE_2_URL
    assert (
        service.repository.content_version_count("zhihu:mr-dang-77", ZhihuContentType.ANSWERS) == 2
    )
    repeated = service.replay_listing(
        first_import.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )
    assert repeated.sync_execution is None

    second_path = runtime / "page-2.json"
    second_path.write_text(
        _envelope(
            _fixture("zhihu_answers_page_2.json"),
            listing_page=1,
            request_cursor=PAGE_2_URL,
            requested_url=PAGE_2_URL,
        ).model_dump_json(),
        encoding="utf-8",
    )
    second_import = service.import_file(second_path, registry)
    second_replay = service.replay_listing(
        second_import.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert second_replay.sync_execution is not None
    assert second_replay.sync_execution.report.coverage_status.value == "COMPLETE"
    assert (
        service.repository.content_version_count("zhihu:mr-dang-77", ZhihuContentType.ANSWERS) == 3
    )


def test_consumed_listing_without_manifest_can_recover_after_parser_upgrade(
    state, object_store, tmp_path
) -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, tmp_path)
    imported = service.import_envelope(
        _envelope(_fixture("zhihu_answers_metadata_only.json")),
        registry,
        _endpoint_registry(),
    )
    service.repository.mark_import_consumed(
        imported.record.envelope_id,
        datetime(2026, 7, 18, 5, 30, tzinfo=UTC),
    )

    ordinary = service.replay_listing(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )
    recovered = service.replay_listing(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
        recover_consumed=True,
    )

    assert ordinary.sync_execution is None
    assert recovered.sync_execution is not None
    assert [item.content_id for item in recovered.sync_execution.content_records] == ["101"]


def test_content_detail_replay_replaces_listing_excerpt_with_verified_full_body(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    listing_path = runtime / "listing.json"
    listing_path.write_text(
        _envelope(_fixture("zhihu_answers_page_1.json")).model_dump_json(),
        encoding="utf-8",
    )
    listing = service.import_file(listing_path, registry)
    service.replay_listing(listing.record.envelope_id, registry, _parquet_store(tmp_path))

    detail_path = runtime / "detail.json"
    detail_path.write_text(
        _detail_envelope(_fixture("zhihu_answer_detail.json")).model_dump_json(),
        encoding="utf-8",
    )
    imported = service.import_file(detail_path, registry, _verified_detail_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )

    assert replayed.response_failure is None
    assert replayed.content_record is not None
    assert replayed.content_record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
    assert b"complete detail body" in object_store.get_bytes(
        replayed.content_record.body_object_sha256
    )
    assert (
        service.repository.content_version_count("zhihu:mr-dang-77", ZhihuContentType.ANSWERS) == 3
    )


def test_content_detail_requires_observed_endpoint_and_rejects_truncation(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    path = runtime / "detail.json"
    truncated = json.loads(_fixture("zhihu_answer_detail.json"))
    truncated["is_truncated"] = True
    article_path = runtime / "article-detail.json"
    article_path.write_text(
        _detail_envelope(
            json.dumps(truncated).encode(),
            content_type=ZhihuContentType.ARTICLES,
            requested_url="https://www.zhihu.com/api/v4/articles/101",
        ).model_dump_json(),
        encoding="utf-8",
    )
    path.write_text(
        _detail_envelope(json.dumps(truncated).encode()).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(ProviderError) as unobserved:
        service.import_file(article_path, registry, _endpoint_registry())
    assert unobserved.value.failure_class is FailureClass.POLICY_REJECTED

    imported = service.import_file(path, registry, _endpoint_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )
    assert replayed.content_record is None
    assert replayed.response_failure is FailureClass.INVALID_RESPONSE
    assert object_store.get_bytes(imported.record.raw_object_sha256)
    with state.connect() as connection:
        gap = connection.execute("SELECT failure_class,status FROM collection_gap").fetchone()
    assert tuple(gap) == (FailureClass.INVALID_RESPONSE.value, "OPEN")


def test_thought_detail_replay_preserves_structured_and_html_full_bodies(
    state, object_store, tmp_path
) -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, tmp_path)
    envelope = _detail_envelope(
        _fixture("zhihu_thought_detail.json"),
        author_source_id="zhihu:xiao-peng-61-47",
        content_type=ZhihuContentType.THOUGHTS,
        content_id="2055972101819986793",
        requested_url=THOUGHT_DETAIL_URL,
    )

    imported = service.import_envelope(envelope, registry, _endpoint_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )

    assert replayed.response_failure is None
    assert replayed.content_record is not None
    body = json.loads(object_store.get_bytes(replayed.content_record.body_object_sha256))
    assert body["content_html"] == "<p>Synthetic complete thought body.</p>"
    assert body["segments"][0]["fold_type"] == "raw"


def test_browser_article_html_replay_registers_verified_full_body(
    state, object_store, tmp_path
) -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, tmp_path)
    envelope = _detail_envelope(
        _fixture("zhihu_article_detail.html"),
        author_source_id="zhihu:huang-wei-yan-30",
        content_type=ZhihuContentType.ARTICLES,
        content_id="367165363",
        requested_url=ARTICLE_DETAIL_URL,
        response_mime="text/html; charset=utf-8",
    )

    imported = service.import_envelope(envelope, registry, _endpoint_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )

    assert imported.response_failure is None
    assert replayed.response_failure is None
    assert replayed.content_record is not None
    assert replayed.content_record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
    assert replayed.content_record.canonical_url == ARTICLE_DETAIL_URL
    assert object_store.verify(imported.record.raw_object_sha256)
    assert replayed.parquet_file is not None and replayed.parquet_file.is_file()


def test_muted_thought_with_complete_bodies_replays_as_verified_detail(
    state, object_store, tmp_path
) -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, tmp_path)
    payload = json.loads(_fixture("zhihu_thought_detail.json"))
    payload["state"] = "muted"
    envelope = _detail_envelope(
        json.dumps(payload).encode(),
        author_source_id="zhihu:xiao-peng-61-47",
        content_type=ZhihuContentType.THOUGHTS,
        content_id="2055972101819986793",
        requested_url=THOUGHT_DETAIL_URL,
    )

    imported = service.import_envelope(envelope, registry, _endpoint_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )

    assert replayed.response_failure is None
    assert replayed.content_record is not None
    assert replayed.content_record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
    assert object_store.verify(imported.record.raw_object_sha256)


@pytest.mark.parametrize(
    ("state_value", "is_deleted", "content_html"),
    [
        ("unknown", False, "<p>Synthetic complete thought body.</p>"),
        ("muted", True, "<p>Synthetic complete thought body.</p>"),
        ("muted", False, None),
    ],
)
def test_muted_thought_still_rejects_unknown_deleted_or_incomplete_payloads(
    state,
    object_store,
    tmp_path,
    state_value,
    is_deleted,
    content_html,
) -> None:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, tmp_path)
    payload = json.loads(_fixture("zhihu_thought_detail.json"))
    payload["state"] = state_value
    payload["is_deleted"] = is_deleted
    payload["content_html"] = content_html
    envelope = _detail_envelope(
        json.dumps(payload).encode(),
        author_source_id="zhihu:xiao-peng-61-47",
        content_type=ZhihuContentType.THOUGHTS,
        content_id="2055972101819986793",
        requested_url=THOUGHT_DETAIL_URL,
    )

    imported = service.import_envelope(envelope, registry, _endpoint_registry())
    replayed = service.replay_detail(
        imported.record.envelope_id,
        registry,
        _parquet_store(tmp_path),
    )

    assert replayed.response_failure is FailureClass.INVALID_RESPONSE
    assert replayed.content_record is None
    assert object_store.verify(imported.record.raw_object_sha256)


def test_out_of_order_imported_listing_remains_pending(state, object_store, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    path = runtime / "page-2.json"
    path.write_text(
        _envelope(
            _fixture("zhihu_answers_page_2.json"),
            listing_page=1,
            request_cursor=PAGE_2_URL,
            requested_url=PAGE_2_URL,
        ).model_dump_json(),
        encoding="utf-8",
    )
    imported = service.import_file(path, registry, _endpoint_registry())

    with pytest.raises(ProviderError) as caught:
        service.replay_listing(
            imported.record.envelope_id,
            registry,
            parquet_store=_parquet_store(tmp_path),
        )

    assert caught.value.failure_class is FailureClass.CONFLICT
    stored = service.repository.get_imported_response(imported.record.envelope_id)
    assert stored is not None
    assert stored.import_status is ZhihuImportStatus.PENDING


def test_imported_root_comment_page_replays_to_comment_checkpoint_and_chain(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "comments.json"
    path.write_text(
        _comment_envelope(_fixture("zhihu_root_comments_page_1.json")).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    imported = service.import_file(path, registry, _endpoint_registry())

    replayed = service.replay_comment(
        imported.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert replayed.response_failure is None
    assert replayed.comment_execution is not None
    assert len(replayed.comment_execution.comment_records) == 3
    assert replayed.record.import_status is ZhihuImportStatus.CONSUMED
    checkpoint = state.get_collection_checkpoint("zhihu:mr-dang-77", "answers", "answer-fixture")
    assert checkpoint is not None
    assert checkpoint.comment_page == 1


def test_imported_restricted_comment_response_is_consumed_as_an_open_gap(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "comments-403.json"
    path.write_text(
        _comment_envelope(b'{"error":{"code":403}}', status_code=403).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    imported = service.import_file(path, registry, _endpoint_registry())

    replayed = service.replay_comment(
        imported.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert replayed.response_failure is FailureClass.ACCESS_RESTRICTED
    assert replayed.comment_execution is None
    assert replayed.record.import_status is ZhihuImportStatus.CONSUMED
    with state.connect() as connection:
        gap = connection.execute("SELECT failure_class,status FROM collection_gap").fetchone()
    assert tuple(gap) == ("ACCESS_RESTRICTED", "OPEN")


@pytest.mark.parametrize(
    ("status_code", "expected_failure"),
    [
        (200, FailureClass.INVALID_RESPONSE),
        (401, FailureClass.AUTH_REQUIRED),
    ],
)
def test_imported_empty_comment_response_is_preserved_consumed_and_classified(
    state,
    object_store,
    tmp_path,
    status_code: int,
    expected_failure: FailureClass,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    envelope = _comment_envelope(b"", status_code=status_code)
    assert envelope.body_base64 == ""
    assert envelope.decoded_body() == b""
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    imported = service.import_envelope(envelope, registry, _endpoint_registry())
    replayed = service.replay_comment(
        imported.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert imported.response_failure is expected_failure
    assert replayed.response_failure is expected_failure
    assert replayed.comment_execution is None
    assert replayed.record.import_status is ZhihuImportStatus.CONSUMED
    assert replayed.record.body_byte_size == 0
    assert object_store.get_bytes(replayed.record.raw_object_sha256) == b""
    with state.connect() as connection:
        gap = connection.execute(
            "SELECT failure_class,status FROM collection_gap"
        ).fetchone()
        comment_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_version"
        ).fetchone()[0]
        page_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0]
    assert tuple(gap) == (expected_failure.value, "OPEN")
    assert comment_count == 0
    assert page_count == 0


def test_imported_comment_cursor_cycle_is_consumed_and_recorded_as_an_open_gap(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    body = json.dumps(
        {
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
            "paging": {"is_end": False, "next": COMMENT_URL, "totals": 1},
        }
    ).encode()
    path = runtime / "comments-cycle.json"
    path.write_text(_comment_envelope(body).model_dump_json(), encoding="utf-8")
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    imported = service.import_file(path, registry, _endpoint_registry())

    replayed = service.replay_comment(
        imported.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert replayed.response_failure is FailureClass.PAGINATION_CYCLE
    assert replayed.comment_execution is None
    assert replayed.record.import_status is ZhihuImportStatus.CONSUMED
    with state.connect() as connection:
        gap = connection.execute("SELECT failure_class,status FROM collection_gap").fetchone()
        assert tuple(gap) == ("PAGINATION_CYCLE", "OPEN")
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 0


def test_verified_child_comment_import_replays_the_exact_root_scope(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "child-comments.json"
    path.write_text(
        _comment_envelope(
            _fixture("zhihu_child_comments_page_1.json"),
            response_kind=ZhihuResponseKind.CHILD_COMMENTS,
            parent_comment_id="comment-root-1",
            requested_url=(
                "https://www.zhihu.com/api/v4/comment_v5/comment/comment-root-1/"
                "child_comment?order_by=ts&limit=20&offset="
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)
    imported = service.import_file(path, registry, _endpoint_registry())
    replayed = service.replay_comment(
        imported.record.envelope_id,
        registry,
        parquet_store=_parquet_store(tmp_path),
    )

    assert replayed.response_failure is None
    assert replayed.comment_execution is not None
    assert len(replayed.comment_execution.comment_records) == 3
    assert {item.root_comment_id for item in replayed.comment_execution.comment_records} == {
        "comment-root-1"
    }
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
        "comment-root-1",
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is not None
    assert service.repository.pending_import_count() == 0


def test_comment_import_rejects_unobserved_child_path_before_persistence(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "wrong-child-comments.json"
    path.write_text(
        _comment_envelope(
            _fixture("zhihu_child_comments_page_1.json"),
            response_kind=ZhihuResponseKind.CHILD_COMMENTS,
            parent_comment_id="comment-root-1",
            requested_url=(
                "https://www.zhihu.com/api/v4/synthetic-fixture/"
                "child-comments/comment-root-1?limit=20&offset="
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(path, registry, _endpoint_registry())

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED
    assert service.repository.pending_import_count() == 0
    assert list(object_store.root.rglob("*")) == []


def test_comment_import_rejects_child_query_outside_verified_contract(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "wrong-child-query.json"
    path.write_text(
        _comment_envelope(
            _fixture("zhihu_child_comments_page_1.json"),
            response_kind=ZhihuResponseKind.CHILD_COMMENTS,
            parent_comment_id="comment-root-1",
            requested_url=(
                "https://www.zhihu.com/api/v4/comment_v5/comment/comment-root-1/"
                "child_comment?order_by=score&limit=100&offset="
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(path, registry, _endpoint_registry())

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED
    assert service.repository.pending_import_count() == 0
    assert list(object_store.root.rglob("*")) == []


def test_comment_import_rejects_path_outside_verified_template(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "wrong-root-path.json"
    path.write_text(
        _comment_envelope(
            b"{}",
            requested_url=(
                "https://www.zhihu.com/api/v4/comment_v5/answers/"
                "answer-fixture/unverified?limit=20&offset="
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(path, registry, _endpoint_registry())

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED
    assert service.repository.pending_import_count() == 0


def test_response_import_rejects_file_outside_runtime_before_persistence(
    state, object_store, tmp_path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(_envelope(b"{}").model_dump_json(), encoding="utf-8")
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(outside, registry)

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED
    assert service.repository.pending_import_count() == 0


@pytest.mark.parametrize(
    "requested_url",
    [
        "http://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=0",
        "https://example.com/api/v4/members/mr-dang-77/answers?limit=2&offset=0",
        "https://www.zhihu.com/api/v4/members/other/answers?limit=2&offset=0",
    ],
)
def test_response_import_rejects_non_https_or_mismatched_listing_scope(
    requested_url: str,
    state,
    object_store,
    tmp_path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "response.json"
    path.write_text(
        _envelope(b"{}", requested_url=requested_url).model_dump_json(),
        encoding="utf-8",
    )
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(path, registry)

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED
    assert service.repository.pending_import_count() == 0


def test_response_import_rejects_extra_cookie_field(state, object_store, tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    payload = json.loads(_envelope(b"{}").model_dump_json())
    payload["cookie"] = "must-not-be-accepted"
    path = runtime / "response.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    service = ZhihuResponseImportService(state, object_store, runtime)

    with pytest.raises(ProviderError) as caught:
        service.import_file(path, registry)

    assert caught.value.failure_class is FailureClass.INVALID_RESPONSE
    assert service.repository.pending_import_count() == 0


def _parquet_store(tmp_path: Path) -> ParquetKnowledgeStore:
    return ParquetKnowledgeStore(tmp_path / "parquet")
