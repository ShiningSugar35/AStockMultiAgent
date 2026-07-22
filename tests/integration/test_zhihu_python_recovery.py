from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.errors import FailureClass
from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuFullCaptureSession,
    ZhihuHttpTransport,
    ZhihuPythonRecoveryService,
    ZhihuResponseImportService,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.knowledge.transport import PersistedZhihuResponse
from astock.schemas import (
    KnowledgeSourceRegistry,
    ZhihuBrowserResponseEnvelope,
    ZhihuContentCompleteness,
    ZhihuContentType,
    ZhihuImportStatus,
    ZhihuResponseKind,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://www.zhihu.com"
TOKEN = "python-recovery-test-token-with-at-least-32-characters"


def _registry() -> KnowledgeSourceRegistry:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    source = next(item for item in registry.sources if item.source_id == "zhihu:mr-dang-77")
    return KnowledgeSourceRegistry(sources=[source])


def _payload(session: ZhihuFullCaptureSession, body: dict[str, object]) -> bytes:
    request = session.initial_request
    assert request is not None
    return ZhihuBrowserResponseEnvelope.model_validate(
        {
            **request.payload(),
            "status_code": 200,
            "response_mime": "application/json",
            "body_base64": base64.b64encode(json.dumps(body).encode()).decode("ascii"),
            "transport": ZhihuTransport.CHROME,
            "captured_at": datetime.now(UTC),
        }
    ).model_dump_json().encode()


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


class _RecordedPythonTransport:
    def __init__(
        self,
        persistence: ZhihuHttpTransport,
        body: bytes,
        *,
        status_code: int = 200,
        content_type_header: str = "application/json",
    ) -> None:
        self.persistence = persistence
        self.body = body
        self.status_code = status_code
        self.content_type_header = content_type_header
        self.urls: list[str] = []

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        self.urls.append(url)
        return self.persistence.persist_imported_response(
            author_source_id=author_source_id,
            content_type=content_type,
            requested_url=url,
            status_code=self.status_code,
            content_type_header=self.content_type_header,
            body=self.body,
            transport=ZhihuTransport.PYTHON_HTTP,
        )


class _ClosedCommentTransport:
    def __init__(self, persistence: ZhihuHttpTransport) -> None:
        self.persistence = persistence
        self.urls: list[str] = []

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        self.urls.append(url)
        body = json.dumps(
            {
                "comment_status": {"type": 1, "text": "评论区已关闭"},
                "counts": {"total_counts": 12},
                "data": [],
                "paging": {
                    "is_end": False,
                    "is_start": True,
                    "next": f"{url}closed-page",
                    "totals": 12,
                },
            }
        ).encode()
        return self.persistence.persist_imported_response(
            author_source_id=author_source_id,
            content_type=content_type,
            requested_url=url,
            status_code=200,
            content_type_header="application/json",
            body=body,
            transport=ZhihuTransport.PYTHON_HTTP,
        )


def test_python_recovery_uses_verified_full_detail_query_and_normal_pipeline(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    registry = _registry()
    endpoints = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    browser = ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )
    browser.process_payload(
        _payload(
            browser,
            {
                "data": [
                    {
                        "id": 103,
                        "type": "answer",
                        "created_time": 1704412800,
                        "updated_time": 1704499200,
                        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                        "question": {"id": 9003, "title": "Synthetic question"},
                    }
                ],
                "paging": {"is_end": True, "next": "", "totals": 1},
            },
        ),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    for _ in range(2):
        browser.process_payload(
            _payload(browser, {"data": [], "paging": {"is_end": True, "next": ""}}),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    assert browser.initial_request is not None
    assert browser.initial_request.requested_url.endswith("/api/v4/answers/103?include=content")

    detail = json.dumps(
        {
            "id": 103,
            "type": "answer",
            "content": "<p>Synthetic complete Python detail.</p>",
            "content_need_truncated": False,
            "force_login_when_click_read_more": False,
            "created_time": 1704412800,
            "updated_time": 1704499200,
            "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
            "question": {"id": 9003, "title": "Synthetic question"},
        }
    ).encode()
    transport = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        detail,
    )
    execution = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.CONTENT_DETAIL],
        max_requests=1,
    )

    assert execution.status == "MANUAL_ACTION_REQUIRED"
    assert execution.blocked_task_count == 1
    assert execution.response_kinds == [ZhihuResponseKind.CONTENT_DETAIL.value]
    assert execution.request_count == 1
    assert transport.urls == [
        "https://www.zhihu.com/api/v4/answers/103?include=content"
    ]
    with state.connect() as connection:
        rows = connection.execute(
            "SELECT record_json FROM zhihu_content_version WHERE content_id='103'"
        ).fetchall()
    records = [json.loads(row["record_json"]) for row in rows]
    assert any(
        record["content_completeness"] == ZhihuContentCompleteness.DETAIL_VERIFIED.value
        for record in records
    )


@pytest.mark.parametrize(
    "response_kind",
    [ZhihuResponseKind.ROOT_COMMENTS, ZhihuResponseKind.CHILD_COMMENTS],
)
def test_python_recovery_rejects_retired_interaction_filters_before_network(
    response_kind: ZhihuResponseKind,
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    transport = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        b"must not be fetched",
    )
    service = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        _registry(),
        load_zhihu_endpoint_templates(
            PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
        ),
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    )

    with pytest.raises(ValueError, match="CONTENT_DETAIL"):
        service.run(response_kinds=[response_kind], max_requests=1)

    assert transport.urls == []


def _legacy_python_recovery_can_run_only_discovered_child_comment_tasks(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    registry = _registry()
    endpoints = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    browser = ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )
    listing = {
        "data": [
            {
                "id": "answer-fixture",
                "type": "answer",
                "content": "<p>listing excerpt</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic question"},
            }
        ],
        "paging": {"is_end": True, "next": "", "totals": 1},
    }
    browser.process_payload(_payload(browser, listing), origin=ORIGIN, session_token=TOKEN)
    for _ in range(2):
        browser.process_payload(
            _payload(browser, {"data": [], "paging": {"is_end": True, "next": ""}}),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    detail = {
        "id": "answer-fixture",
        "type": "answer",
        "content": "<p>Synthetic complete answer.</p>",
        "created_time": 1704412800,
        "updated_time": 1704499200,
        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
        "question": {"id": 9003, "title": "Synthetic question"},
    }
    browser.process_payload(_payload(browser, detail), origin=ORIGIN, session_token=TOKEN)
    first_root = json.loads(_fixture("zhihu_root_comments_page_1.json"))
    first_root["paging"]["next"] = (
        "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
        "root_comment?order_by=score&limit=10&offset=2"
    )
    browser.process_payload(_payload(browser, first_root), origin=ORIGIN, session_token=TOKEN)
    browser.process_payload(
        _payload(browser, json.loads(_fixture("zhihu_root_comments_page_2.json"))),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    transport = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        _fixture("zhihu_child_comments_page_1.json"),
    )

    execution = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.CHILD_COMMENTS],
        max_requests=1,
    )

    assert execution.response_failure is None
    assert execution.response_kinds == [ZhihuResponseKind.CHILD_COMMENTS.value]
    assert transport.urls == [
        "https://www.zhihu.com/api/v4/comment_v5/comment/comment-root-1/"
        "child_comment?order_by=ts&limit=20&offset="
    ]
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
        "comment-root-1",
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is not None


def _legacy_python_recovery_consumes_an_empty_root_response_as_an_explicit_gap(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    registry = _registry()
    endpoints = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    browser = ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )
    listing = {
        "data": [
            {
                "id": "empty-answer",
                "type": "answer",
                "content": "<p>listing excerpt</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic empty response"},
            }
        ],
        "paging": {"is_end": True, "next": "", "totals": 1},
    }
    browser.process_payload(_payload(browser, listing), origin=ORIGIN, session_token=TOKEN)
    for _ in range(2):
        browser.process_payload(
            _payload(browser, {"data": [], "paging": {"is_end": True, "next": ""}}),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    detail = {
        "id": "empty-answer",
        "type": "answer",
        "content": "<p>Synthetic complete answer.</p>",
        "created_time": 1704412800,
        "updated_time": 1704499200,
        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
        "question": {"id": 9003, "title": "Synthetic empty response"},
    }
    browser.process_payload(_payload(browser, detail), origin=ORIGIN, session_token=TOKEN)
    assert browser.initial_request is not None
    assert browser.initial_request.response_kind is ZhihuResponseKind.ROOT_COMMENTS
    transport = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        b"",
    )

    execution = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.ROOT_COMMENTS],
        max_requests=1,
    )

    assert execution.request_count == 1
    assert execution.response_failure == FailureClass.INVALID_RESPONSE.value
    assert execution.status == "MANUAL_ACTION_REQUIRED"
    assert execution.terminal_condition == FailureClass.INVALID_RESPONSE.value
    with state.connect() as connection:
        imported = connection.execute(
            "SELECT import_status,body_byte_size FROM zhihu_imported_response "
            "WHERE response_kind='ROOT_COMMENTS' AND body_byte_size=0"
        ).fetchone()
        gap = connection.execute(
            "SELECT failure_class,status FROM collection_gap"
        ).fetchone()
        comment_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_version"
        ).fetchone()[0]
        page_count = connection.execute(
            "SELECT COUNT(*) FROM zhihu_comment_page_manifest"
        ).fetchone()[0]
    assert tuple(imported) == (ZhihuImportStatus.CONSUMED.value, 0)
    assert tuple(gap) == (FailureClass.INVALID_RESPONSE.value, "OPEN")
    assert comment_count == 0
    assert page_count == 0

    with state.connect() as connection:
        envelope_id = connection.execute(
            "SELECT envelope_id FROM zhihu_imported_response "
            "WHERE response_kind='ROOT_COMMENTS' AND body_byte_size=0"
        ).fetchone()[0]
    recovered = ZhihuResponseImportService(
        state,
        object_store,
        runtime,
    ).replay_comment(
        envelope_id,
        registry,
        parquet,
        recover_consumed=True,
    )
    assert recovered.response_failure is FailureClass.INVALID_RESPONSE

    repeated = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.ROOT_COMMENTS],
        max_requests=3,
    )

    assert repeated.request_count == 0
    assert repeated.accepted_envelope_count == 0
    assert repeated.response_failure is None
    assert repeated.status == "MANUAL_ACTION_REQUIRED"
    assert repeated.terminal_condition == "NEEDS_MANUAL_ACTION"
    assert repeated.blocked_task_count == 1
    assert len(transport.urls) == 1


def _legacy_python_recovery_skips_explicitly_closed_comment_areas_in_one_session(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    registry = _registry()
    endpoints = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    browser = ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )
    listing = {
        "data": [
            {
                "id": content_id,
                "type": "answer",
                "content": "<p>listing excerpt</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": question_id, "title": "Closed comments fixture"},
            }
            for content_id, question_id in (("closed-a", 9011), ("closed-b", 9012))
        ],
        "paging": {"is_end": True, "next": "", "totals": 2},
    }
    browser.process_payload(_payload(browser, listing), origin=ORIGIN, session_token=TOKEN)
    for _ in range(2):
        browser.process_payload(
            _payload(browser, {"data": [], "paging": {"is_end": True, "next": ""}}),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    for question_id in (9011, 9012):
        request = browser.initial_request
        assert request is not None and request.content_id is not None
        browser.process_payload(
            _payload(
                browser,
                {
                    "id": request.content_id,
                    "type": "answer",
                    "content": "<p>complete answer detail</p>",
                    "created_time": 1704412800,
                    "updated_time": 1704499200,
                    "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                    "question": {
                        "id": question_id,
                        "title": "Closed comments fixture",
                    },
                },
            ),
            origin=ORIGIN,
            session_token=TOKEN,
        )

    transport = _ClosedCommentTransport(ZhihuHttpTransport(object_store, state))
    execution = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.ROOT_COMMENTS],
        max_requests=2,
    )

    assert execution.request_count == 2
    assert execution.accepted_envelope_count == 2
    assert execution.response_failure is None
    assert execution.status == "MANUAL_ACTION_REQUIRED"
    assert execution.terminal_condition == "NEEDS_MANUAL_ACTION"
    assert execution.blocked_task_count == 2
    assert len(transport.urls) == 2
    with state.connect() as connection:
        gaps = connection.execute(
            "SELECT failure_class,status,COUNT(*) FROM collection_gap "
            "GROUP BY failure_class,status"
        ).fetchall()
    assert [tuple(row) for row in gaps] == [
        (FailureClass.ACCESS_RESTRICTED.value, "OPEN", 2)
    ]


def _legacy_python_recovery_retries_transient_network_gap_and_resolves_it(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    registry = _registry()
    endpoints = load_zhihu_endpoint_templates(
        PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
    )
    browser = ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )
    listing = {
        "data": [
            {
                "id": "network-retry-answer",
                "type": "answer",
                "content": "<p>listing excerpt</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9021, "title": "Network retry fixture"},
            }
        ],
        "paging": {"is_end": True, "next": "", "totals": 1},
    }
    browser.process_payload(_payload(browser, listing), origin=ORIGIN, session_token=TOKEN)
    for _ in range(2):
        browser.process_payload(
            _payload(browser, {"data": [], "paging": {"is_end": True, "next": ""}}),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    browser.process_payload(
        _payload(
            browser,
            {
                "id": "network-retry-answer",
                "type": "answer",
                "content": "<p>complete answer detail</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9021, "title": "Network retry fixture"},
            },
        ),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    unavailable = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        b"temporary upstream failure",
        status_code=503,
        content_type_header="text/plain",
    )
    failed = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=unavailable,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.ROOT_COMMENTS],
        max_requests=1,
    )
    assert failed.request_count == 1
    assert failed.response_failure == FailureClass.NETWORK.value
    assert failed.terminal_condition == FailureClass.NETWORK.value

    recovered_transport = _RecordedPythonTransport(
        ZhihuHttpTransport(object_store, state),
        json.dumps(
            {"data": [], "paging": {"is_end": True, "next": "", "totals": 0}}
        ).encode(),
    )
    recovered = ZhihuPythonRecoveryService(
        state,
        object_store,
        runtime,
        parquet,
        registry,
        endpoints,
        transport=recovered_transport,
        request_interval_seconds=2,
        sleeper=lambda _: None,
    ).run(
        response_kinds=[ZhihuResponseKind.ROOT_COMMENTS],
        max_requests=1,
    )
    assert recovered.request_count == 1
    assert recovered.accepted_envelope_count == 1
    assert recovered.response_failure is None
    assert recovered.status == "COMPLETE"
    assert recovered.terminal_condition == "COMPLETE"
    with state.connect() as connection:
        gaps = connection.execute(
            "SELECT failure_class,retryable,status FROM collection_gap"
        ).fetchall()
    assert [tuple(row) for row in gaps] == [
        (FailureClass.NETWORK.value, 1, "RESOLVED")
    ]
