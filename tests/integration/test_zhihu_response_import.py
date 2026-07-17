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
)
from astock.schemas import (
    ZhihuBrowserResponseEnvelope,
    ZhihuContentType,
    ZhihuImportStatus,
    ZhihuResponseKind,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LISTING_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/"
    "answers?limit=2&offset=0&sort_by=created"
)
PAGE_2_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/"
    "answers?limit=2&offset=2&sort_by=created"
)


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


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
    assert service.repository.content_version_count(
        "zhihu:mr-dang-77", ZhihuContentType.ANSWERS
    ) == 2
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
    assert service.repository.content_version_count(
        "zhihu:mr-dang-77", ZhihuContentType.ANSWERS
    ) == 3


def test_out_of_order_imported_listing_remains_pending(
    state, object_store, tmp_path
) -> None:
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
    imported = service.import_file(path, registry)

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


def test_response_import_rejects_extra_cookie_field(
    state, object_store, tmp_path
) -> None:
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
