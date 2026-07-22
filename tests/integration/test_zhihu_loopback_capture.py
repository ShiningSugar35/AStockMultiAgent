from __future__ import annotations

import base64
import html
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from astock.core.errors import FailureClass, PolicyError, ProviderError
from astock.knowledge import (
    ParquetKnowledgeStore,
    ZhihuLoopbackCaptureSession,
    create_loopback_capture_server,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
    loopback_installer_url,
    loopback_status_url,
)
from astock.knowledge.repository import KnowledgeRepository
from astock.schemas import (
    CollectionTerminalCondition,
    ZhihuBrowserResponseEnvelope,
    ZhihuContentType,
    ZhihuResponseKind,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://www.zhihu.com"
ARTICLE_ORIGIN = "https://zhuanlan.zhihu.com"
TOKEN = "test-session-token-that-is-at-least-32-characters"
LISTING_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=0&sort_by=created"
)
PAGE_2_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=2&sort_by=created"
)
LEDGER_TABLES = (
    "paper_account",
    "ledger_account",
    "journal",
    "ledger_entry",
    "order_record",
    "fill",
    "position",
    "replay_checkpoint",
    "position_settlement",
)


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


def _session(state, object_store, tmp_path: Path) -> ZhihuLoopbackCaptureSession:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return ZhihuLoopbackCaptureSession(
        state,
        object_store,
        runtime,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml"),
        load_zhihu_endpoint_templates(PROJECT_ROOT / "configs" / "zhihu_endpoint_templates.yaml"),
        source_id="zhihu:mr-dang-77",
        content_type=ZhihuContentType.ANSWERS,
        page_size=2,
        request_interval_seconds=2,
        session_token=TOKEN,
    )


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


def _payload(body: bytes, **updates: object) -> bytes:
    return _envelope(body, **updates).model_dump_json().encode("utf-8")


def _ledger_counts(state) -> dict[str, int]:
    with state.connect() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in LEDGER_TABLES
        }


def test_loopback_capture_commits_in_order_idempotently_and_leaves_ledger_untouched(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    ledger_before = _ledger_counts(state)

    first = session.process_payload(
        _payload(_fixture("zhihu_answers_page_1.json")),
        origin=ORIGIN,
        session_token=TOKEN,
    )
    repeated = session.process_payload(
        _payload(_fixture("zhihu_answers_page_1.json")),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert first == repeated
    assert first.status == "COMMITTED"
    assert not first.done
    assert first.next_page == 1
    assert first.next_url == PAGE_2_URL
    assert first.content_record_count == 2
    assert session.safe_status()["accepted_envelope_count"] == 1
    assert object_store.get_bytes(first.source_snapshot_id.split(":")[-1]) != b""

    second = session.process_payload(
        _payload(
            _fixture("zhihu_answers_page_2.json"),
            listing_page=1,
            request_cursor=PAGE_2_URL,
            requested_url=PAGE_2_URL,
        ),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert second.done
    assert second.status == "COMMITTED"
    assert second.terminal_condition == CollectionTerminalCondition.PAGINATION_COMPLETE.value
    assert second.content_record_count == 1
    assert session.safe_status()["status"] == "COMPLETE"
    assert (
        KnowledgeRepository(state).content_version_count(
            "zhihu:mr-dang-77", ZhihuContentType.ANSWERS
        )
        == 3
    )
    assert _ledger_counts(state) == ledger_before


def test_loopback_capture_rejects_wrong_origin_token_scope_and_sensitive_fields(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    valid = _payload(_fixture("zhihu_answers_page_1.json"))

    with pytest.raises(PolicyError) as wrong_origin:
        session.process_payload(valid, origin="https://example.com", session_token=TOKEN)
    assert wrong_origin.value.failure_class is FailureClass.POLICY_REJECTED

    with pytest.raises(PolicyError) as wrong_token:
        session.process_payload(valid, origin=ORIGIN, session_token="x" * 40)
    assert wrong_token.value.failure_class is FailureClass.POLICY_REJECTED

    extra = json.loads(valid)
    extra["cookie"] = "must-never-be-accepted"
    with pytest.raises(ProviderError) as sensitive:
        session.process_payload(
            json.dumps(extra).encode(),
            origin=ORIGIN,
            session_token=TOKEN,
        )
    assert sensitive.value.failure_class is FailureClass.INVALID_RESPONSE

    wrong_scope = _payload(
        _fixture("zhihu_answers_page_1.json"),
        author_source_id="zhihu:xiao-peng-61-47",
        requested_url=LISTING_URL.replace("mr-dang-77", "xiao-peng-61-47"),
    )
    with pytest.raises(PolicyError) as escaped:
        session.process_payload(wrong_scope, origin=ORIGIN, session_token=TOKEN)
    assert escaped.value.failure_class is FailureClass.POLICY_REJECTED

    assert KnowledgeRepository(state).pending_import_count() == 0
    assert list(object_store.root.rglob("*")) == []


def test_loopback_capture_persists_restricted_response_then_stops_with_gap(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    restricted_body = b'{"error":{"code":403}}'

    ack = session.process_payload(
        _payload(restricted_body, status_code=403),
        origin=ORIGIN,
        session_token=TOKEN,
    )

    assert ack.done
    assert ack.status == "STOPPED"
    assert ack.response_failure == FailureClass.ACCESS_RESTRICTED.value
    assert ack.terminal_condition == CollectionTerminalCondition.ACCESS_RESTRICTED.value
    assert object_store.get_bytes(ack.source_snapshot_id.split(":")[-1]) == restricted_body
    with state.connect() as connection:
        gap = connection.execute("SELECT failure_class,status FROM collection_gap").fetchone()
    assert tuple(gap) == (FailureClass.ACCESS_RESTRICTED.value, "OPEN")


def test_loopback_http_server_is_token_gated_local_and_cors_limited(
    state,
    object_store,
    tmp_path: Path,
) -> None:
    session = _session(state, object_store, tmp_path)
    server = create_loopback_capture_server(session, port=0)
    assert server.server_address[0] == "127.0.0.1"
    installer_url = loopback_installer_url(server)
    status_url = loopback_status_url(server)
    origin = installer_url.split("/install/", maxsplit=1)[0]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(timeout=5) as client:
            assert client.get(origin + "/").status_code == 404
            installer = client.get(installer_url)
            assert installer.status_code == 200
            installer_source = html.unescape(installer.text)
            assert "document.cookie" not in installer_source
            assert "localStorage" not in installer_source
            assert "Authorization" not in installer_source
            assert "credentials:'include'" in installer_source
            status = client.get(status_url)
            assert status.json()["status"] == "READY"

            current_denied = client.get(
                origin + "/v1/current",
                headers={"Origin": ORIGIN},
            )
            assert current_denied.status_code == 403
            current = client.get(
                origin + "/v1/current",
                headers={
                    "Origin": ORIGIN,
                    "X-AStock-Capture-Token": TOKEN,
                },
            )
            assert current.status_code == 200
            assert current.headers["access-control-allow-origin"] == ORIGIN
            assert current.json()["status"] == "READY"

            article_current = client.get(
                origin + "/v1/current",
                headers={
                    "Origin": ARTICLE_ORIGIN,
                    "X-AStock-Capture-Token": TOKEN,
                },
            )
            assert article_current.status_code == 200
            assert article_current.headers["access-control-allow-origin"] == ARTICLE_ORIGIN

            denied = client.post(origin + "/v1/envelopes", content=b"{}")
            assert denied.status_code == 403
            preflight = client.options(
                origin + "/v1/envelopes",
                headers={"Origin": ORIGIN},
            )
            assert preflight.status_code == 204
            assert preflight.headers["access-control-allow-origin"] == ORIGIN
            assert preflight.headers["access-control-allow-private-network"] == "true"
            current_preflight = client.options(
                origin + "/v1/current",
                headers={"Origin": ORIGIN},
            )
            assert current_preflight.status_code == 204
            assert current_preflight.headers["access-control-allow-methods"] == "GET, OPTIONS"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive()
