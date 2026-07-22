from __future__ import annotations

import base64
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astock.core.errors import FailureClass, PolicyError
from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuFullCaptureSession,
    ZhihuManualTaskService,
    build_coordinator_capture_extension,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.schemas import (
    CollectionTerminalCondition,
    KnowledgeSourceRegistry,
    ZhihuBrowserResponseEnvelope,
    ZhihuContentCompleteness,
    ZhihuContentType,
    ZhihuResponseKind,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://www.zhihu.com"
ARTICLE_ORIGIN = "https://zhuanlan.zhihu.com"
TOKEN = "full-capture-test-token-with-at-least-32-characters"


def _registry() -> KnowledgeSourceRegistry:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    source = next(item for item in registry.sources if item.source_id == "zhihu:mr-dang-77")
    return KnowledgeSourceRegistry(sources=[source])


def _session(
    state,
    object_store,
    tmp_path: Path,
    *,
    task_response_kinds: list[ZhihuResponseKind] | None = None,
) -> ZhihuFullCaptureSession:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return ZhihuFullCaptureSession(
        state,
        object_store,
        runtime,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        _registry(),
        load_zhihu_endpoint_templates(PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"),
        task_response_kinds=task_response_kinds,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )


def _payload(session: ZhihuFullCaptureSession, body: dict[str, object]) -> bytes:
    request = session.initial_request
    assert request is not None
    values = {
        **request.payload(),
        "status_code": 200,
        "response_mime": "application/json",
        "body_base64": base64.b64encode(json.dumps(body, ensure_ascii=False).encode()).decode(
            "ascii"
        ),
        "transport": ZhihuTransport.CHROME,
        "captured_at": datetime.now(UTC),
    }
    return ZhihuBrowserResponseEnvelope.model_validate(values).model_dump_json().encode()


def _raw_payload(
    session: ZhihuFullCaptureSession,
    body: bytes,
    *,
    response_mime: str,
) -> bytes:
    request = session.initial_request
    assert request is not None
    values = {
        **request.payload(),
        "status_code": 200,
        "response_mime": response_mime,
        "body_base64": base64.b64encode(body).decode("ascii"),
        "transport": ZhihuTransport.CHROME,
        "captured_at": datetime.now(UTC),
    }
    return ZhihuBrowserResponseEnvelope.model_validate(values).model_dump_json().encode()


def _empty_page() -> dict[str, object]:
    return {"data": [], "paging": {"is_end": True, "next": ""}}


def _fixture_json(name: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def test_full_capture_runs_all_listing_types_then_verified_details(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(
        state,
        object_store,
        tmp_path,
        task_response_kinds=[ZhihuResponseKind.CONTENT_DETAIL],
    )
    refresh = Mock(wraps=session.manual_service.refresh)
    session.manual_service.refresh = refresh
    assert session.initial_request is not None
    assert session.initial_request.content_type is ZhihuContentType.ANSWERS
    answer = {
        "data": [
            {
                "id": 103,
                "type": "answer",
                "content": "<p>listing excerpt only</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic fixture question three"},
            }
        ],
        "paging": {"is_end": True, "next": ""},
    }
    answer_payload = _payload(session, answer)

    first = session.process_payload(
        answer_payload,
        origin=ORIGIN,
        session_token=TOKEN,
    )
    repeated = session.process_payload(
        answer_payload,
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert first == repeated
    assert first.next_request is not None
    assert first.next_request["content_type"] == ZhihuContentType.ARTICLES.value
    articles = session.process_payload(
        _payload(session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    assert articles.next_request is not None
    assert articles.next_request["content_type"] == ZhihuContentType.THOUGHTS.value
    thoughts = session.process_payload(
        _payload(session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    assert thoughts.next_request is not None
    assert thoughts.next_request["response_kind"] == "CONTENT_DETAIL"
    assert thoughts.next_request["content_id"] == "103"
    assert refresh.call_count == 1
    detail = {
        "id": 103,
        "type": "answer",
        "content": "<p>Synthetic complete answer detail.</p>",
        "created_time": 1704412800,
        "updated_time": 1704499200,
        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
        "question": {"id": 9003, "title": "Synthetic fixture question three"},
    }
    detail_ack = session.process_payload(
        _payload(session, detail),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    assert detail_ack.next_request is None
    assert detail_ack.done
    assert detail_ack.status == "STOPPED"
    assert detail_ack.terminal_condition == "NEEDS_MANUAL_ACTION"
    assert detail_ack.blocked_task_count == 1
    assert detail_ack.accepted_envelope_count == 4
    assert refresh.call_count == 2
    assert session.safe_status()["status"] == "MANUAL_ACTION_REQUIRED"
    tasks = ZhihuManualTaskService(state).list_open()
    assert [(task.content_type, task.response_kind) for task in tasks] == [
        ("columns", "COLUMN_LISTING")
    ]
    with state.connect() as connection:
        records = connection.execute(
            "SELECT record_json FROM zhihu_content_version WHERE content_id='103'"
        ).fetchall()
    assert any(
        json.loads(record["record_json"])["content_completeness"]
        == ZhihuContentCompleteness.DETAIL_VERIFIED.value
        for record in records
    )


def test_full_capture_switches_to_canonical_article_html_and_finishes(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    session.process_payload(
        _payload(session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    article_listing = {
        "data": [
            {
                "id": 367165363,
                "type": "article",
                "title": "Synthetic fixture article",
                "url": "https://zhuanlan.zhihu.com/p/367165363",
                "created": 1704412800,
                "updated": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
            }
        ],
        "paging": {"is_end": True, "next": ""},
    }
    session.process_payload(
        _payload(session, article_listing),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    listings_done = session.process_payload(
        _payload(session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert listings_done.next_request is not None
    assert listings_done.next_request["response_kind"] == "CONTENT_DETAIL"
    assert listings_done.next_request["requested_url"] == ("https://zhuanlan.zhihu.com/p/367165363")
    article_ack = session.process_payload(
        _raw_payload(
            session,
            (
                PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / "zhihu_article_detail.html"
            ).read_bytes(),
            response_mime="text/html; charset=utf-8",
        ),
        origin=ARTICLE_ORIGIN,
        session_token=TOKEN,
    )

    assert article_ack.response_failure is None
    assert article_ack.content_record_count == 1
    assert article_ack.next_request is None
    assert article_ack.done
    assert article_ack.terminal_condition == "NEEDS_MANUAL_ACTION"
    with state.connect() as connection:
        row = connection.execute(
            "SELECT record_json FROM zhihu_content_version "
            "WHERE source_id='zhihu:mr-dang-77' AND content_type='articles' "
            "AND content_id='367165363' ORDER BY collected_at DESC,version_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    stored = json.loads(row["record_json"])
    assert stored["content_completeness"] == ZhihuContentCompleteness.DETAIL_VERIFIED.value


def _legacy_full_capture_uses_verified_child_pages_and_deduplicates_root_preview(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    listing = {
        "data": [
            {
                "id": "answer-fixture",
                "type": "answer",
                "content": "<p>listing excerpt only</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic fixture question"},
            }
        ],
        "paging": {"is_end": True, "next": ""},
    }
    session.process_payload(_payload(session, listing), origin=ORIGIN, session_token=TOKEN)
    session.process_payload(_payload(session, _empty_page()), origin=ORIGIN, session_token=TOKEN)
    session.process_payload(_payload(session, _empty_page()), origin=ORIGIN, session_token=TOKEN)
    detail = {
        "id": "answer-fixture",
        "type": "answer",
        "content": "<p>Synthetic complete answer detail.</p>",
        "created_time": 1704412800,
        "updated_time": 1704499200,
        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
        "question": {"id": 9003, "title": "Synthetic fixture question"},
    }
    session.process_payload(_payload(session, detail), origin=ORIGIN, session_token=TOKEN)
    first_root = _fixture_json("zhihu_root_comments_page_1.json")
    first_root["paging"]["next"] = (
        "https://www.zhihu.com/api/v4/comment_v5/answers/answer-fixture/"
        "root_comment?order_by=score&limit=10&offset=2"
    )
    first_root_ack = session.process_payload(
        _payload(session, first_root),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    assert first_root_ack.next_request is not None
    assert first_root_ack.next_request["comment_page"] == 1
    second_root_ack = session.process_payload(
        _payload(session, _fixture_json("zhihu_root_comments_page_2.json")),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert second_root_ack.next_request is not None
    assert second_root_ack.next_request["response_kind"] == "CHILD_COMMENTS"
    assert second_root_ack.next_request["parent_comment_id"] == "comment-root-1"
    assert second_root_ack.next_request["requested_url"] == (
        "https://www.zhihu.com/api/v4/comment_v5/comment/comment-root-1/"
        "child_comment?order_by=ts&limit=20&offset="
    )
    finished = session.process_payload(
        _payload(session, _fixture_json("zhihu_child_comments_page_1.json")),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert finished.response_failure is None
    assert finished.comment_record_count == 3
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
        "answer-fixture",
        "comment-root-1",
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 6


def _legacy_full_capture_stops_and_records_a_nonretryable_pagination_cycle(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    listing = {
        "data": [
            {
                "id": "cycle-answer",
                "type": "answer",
                "content": "<p>listing excerpt only</p>",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic cycle question"},
            }
        ],
        "paging": {"is_end": True, "next": ""},
    }
    session.process_payload(_payload(session, listing), origin=ORIGIN, session_token=TOKEN)
    session.process_payload(_payload(session, _empty_page()), origin=ORIGIN, session_token=TOKEN)
    session.process_payload(_payload(session, _empty_page()), origin=ORIGIN, session_token=TOKEN)
    detail = {
        "id": "cycle-answer",
        "type": "answer",
        "content": "<p>Synthetic complete answer detail.</p>",
        "created_time": 1704412800,
        "updated_time": 1704499200,
        "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
        "question": {"id": 9003, "title": "Synthetic cycle question"},
    }
    session.process_payload(_payload(session, detail), origin=ORIGIN, session_token=TOKEN)
    current = session.initial_request
    assert current is not None
    assert current.response_kind.value == "ROOT_COMMENTS"
    cycle_payload = {
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
        "paging": {
            "is_end": False,
            "next": current.requested_url,
            "totals": 1,
        },
    }

    stopped = session.process_payload(
        _payload(session, cycle_payload),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert stopped.done
    assert stopped.status == "STOPPED"
    assert stopped.response_failure == FailureClass.PAGINATION_CYCLE.value
    assert stopped.terminal_condition == FailureClass.PAGINATION_CYCLE.value
    assert stopped.next_request is None
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM zhihu_comment_version").fetchone()[0] == 0
        gap = connection.execute(
            "SELECT failure_class,status FROM collection_gap "
            "WHERE failure_class=?",
            (FailureClass.PAGINATION_CYCLE.value,),
        ).fetchone()
    assert gap is not None
    assert tuple(gap) == (FailureClass.PAGINATION_CYCLE.value, "OPEN")


@pytest.mark.parametrize(
    "response_kind",
    [ZhihuResponseKind.ROOT_COMMENTS, ZhihuResponseKind.CHILD_COMMENTS],
)
def test_full_capture_rejects_retired_interaction_filters_before_capture(
    response_kind: ZhihuResponseKind,
    state,
    object_store,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with pytest.raises(ValueError, match="CONTENT_DETAIL"):
        ZhihuFullCaptureSession(
            state,
            object_store,
            runtime,
            ParquetKnowledgeStore(tmp_path / "parquet"),
            _registry(),
            load_zhihu_endpoint_templates(
                PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"
            ),
            task_response_kinds=[response_kind],
            request_interval_seconds=2,
            session_token=TOKEN,
        )


def test_full_capture_rejects_any_response_outside_the_exact_pending_boundary(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    payload = json.loads(_payload(session, _empty_page()))
    payload["requested_url"] = "https://www.zhihu.com/api/v4/members/other/answers"

    with pytest.raises(PolicyError) as caught:
        session.process_payload(
            json.dumps(payload).encode(),
            origin=ORIGIN,
            session_token=TOKEN,
        )

    assert caught.value.failure_class is FailureClass.POLICY_REJECTED


def test_full_capture_upgrades_same_origin_http_pagination_cursor(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    response = {
        "data": [
            {
                "id": 103,
                "type": "answer",
                "created_time": 1704412800,
                "updated_time": 1704499200,
                "author": {"id": "fixture-author-id", "url_token": "mr-dang-77"},
                "question": {"id": 9003, "title": "Synthetic fixture question"},
            }
        ],
        "paging": {
            "is_end": False,
            "next": (
                "http://www.zhihu.com/api/v4/members/mr-dang-77/"
                "answers?limit=2&offset=2&sort_by=created"
            ),
        },
    }

    ack = session.process_payload(
        _payload(session, response),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert ack.next_request is not None
    assert ack.next_request["requested_url"] == (
        "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=2&sort_by=created"
    )


def test_full_capture_restart_skips_durably_completed_listing_scope(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    first_session = _session(state, object_store, tmp_path)
    first_ack = first_session.process_payload(
        _payload(first_session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert first_ack.next_request is not None
    assert first_ack.next_request["content_type"] == ZhihuContentType.ARTICLES.value

    resumed_session = _session(state, object_store, tmp_path)

    assert resumed_session.initial_request is not None
    assert resumed_session.initial_request.content_type is ZhihuContentType.ARTICLES
    assert resumed_session.initial_request.listing_page == 0
    assert resumed_session.safe_status()["completed_listing_scope_count"] == 1


def test_full_capture_bookmark_reads_the_latest_checkpoint_at_click_time(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)

    installer = html.unescape(session.installer_html("http://127.0.0.1:8765").decode("utf-8"))

    assert "/v1/current" in installer
    assert "/api/v4/members/mr-dang-77/answers" not in installer
    assert "__astockCaptureLoop" in installer
    assert "already running" in installer
    assert "finally{run.running=false;}" in installer
    assert "navigator.locks.request" in installer
    assert "astock-zhihu-capture-v1" in installer
    assert "if(!lock)return;await start();" in installer

    extension_root = tmp_path / "runtime" / "zhihu_capture_extension"
    extension_directories = list(extension_root.iterdir())
    assert len(extension_directories) == 1
    extension_directory = extension_directories[0]
    manifest_bytes = (extension_directory / "manifest.json").read_bytes()
    script_bytes = (extension_directory / "capture.js").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["manifest_version"] == 3
    assert "permissions" not in manifest
    assert "host_permissions" not in manifest
    assert "background" not in manifest
    assert "action" not in manifest
    assert manifest["content_scripts"] == [
        {
            "matches": [
                "https://www.zhihu.com/*",
                "https://zhuanlan.zhihu.com/*",
            ],
            "js": ["capture.js"],
            "run_at": "document_idle",
            "all_frames": False,
            "world": "MAIN",
        }
    ]
    script = script_bytes.decode("utf-8")
    assert TOKEN in script
    assert "http://127.0.0.1:8765/v1/current" not in script
    assert "c.bridge+'/v1/current'" in script
    assert "__astockCaptureLoop" in script
    assert "navigator.locks.request" in script
    assert "astock-zhihu-capture-v1" in script
    assert "if(!lock)return;await start();" in script
    assert "target.origin!==location.origin" in script
    assert "location.replace(target.origin==='https://zhuanlan.zhihu.com'" in script
    assert "?r.requested_url:'https://www.zhihu.com/'" in script
    assert "document.cookie" not in script
    assert "localStorage" not in script
    assert "Authorization" not in script
    repeated_directory = build_coordinator_capture_extension(
        runtime_root=tmp_path / "runtime",
        bridge_origin="http://127.0.0.1:8765",
        session_token=TOKEN,
        interval_ms=2_000,
    )
    assert repeated_directory == extension_directory
    assert (extension_directory / "manifest.json").read_bytes() == manifest_bytes
    assert (extension_directory / "capture.js").read_bytes() == script_bytes


@pytest.mark.parametrize(
    "bridge_origin",
    [
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765/path",
        "http://user@127.0.0.1:8765",
    ],
)
def test_full_capture_extension_rejects_non_exact_loopback_origins(
    tmp_path: Path,
    bridge_origin: str,
) -> None:
    with pytest.raises(ValueError, match="exact 127.0.0.1"):
        build_coordinator_capture_extension(
            runtime_root=tmp_path / "runtime",
            bridge_origin=bridge_origin,
            session_token=TOKEN,
            interval_ms=2_000,
        )


def test_full_capture_retries_an_empty_nonterminal_listing_only_three_times(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    empty_hole = {
        "data": [],
        "paging": {
            "is_end": False,
            "next": (
                "http://www.zhihu.com/api/v4/members/mr-dang-77/"
                "answers?limit=2&offset=2&sort_by=created"
            ),
        },
    }

    first = session.process_payload(
        _payload(session, empty_hole),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    second = session.process_payload(
        _payload(session, empty_hole),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    third = session.process_payload(
        _payload(session, empty_hole),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert first.status == second.status == "RETRYING"
    assert first.retry_count == 1
    assert second.retry_count == 2
    assert first.next_request == second.next_request
    assert third.status == "STOPPED"
    assert third.done
    assert third.retry_count == 3
    assert third.terminal_condition == FailureClass.INVALID_RESPONSE.value


def test_full_capture_continues_when_an_empty_nonterminal_page_recovers(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    retry = session.process_payload(
        _payload(
            session,
            {
                "data": [],
                "paging": {
                    "is_end": False,
                    "next": (
                        "http://www.zhihu.com/api/v4/members/mr-dang-77/"
                        "answers?limit=2&offset=2&sort_by=created"
                    ),
                },
            },
        ),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    recovered = session.process_payload(
        _payload(session, _empty_page()),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert retry.status == "RETRYING"
    assert recovered.status == "COMMITTED"
    assert recovered.retry_count == 0
    assert recovered.next_request is not None
    assert recovered.next_request["content_type"] == ZhihuContentType.ARTICLES.value
