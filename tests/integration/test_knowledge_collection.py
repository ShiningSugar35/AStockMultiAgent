from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from astock.knowledge import (
    KnowledgeCoverageAuditService,
    ParquetKnowledgeStore,
    ZhihuCollectionService,
    ZhihuHttpTransport,
    load_knowledge_sources,
)
from astock.knowledge.transport import PersistedZhihuResponse
from astock.schemas import (
    CollectionTerminalCondition,
    CoverageStatus,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    ZhihuContentType,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGE_1_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=0&sort_by=created"
)
PAGE_2_URL = (
    "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=2&offset=2&sort_by=created"
)
PROFILE_URL = "https://www.zhihu.com/api/v4/members/mr-dang-77"


class RecordedZhihuTransport:
    def __init__(
        self,
        persistence: ZhihuHttpTransport,
        responses: dict[str, tuple[int, bytes]],
    ) -> None:
        self.persistence = persistence
        self.responses = responses
        self.requested_urls: list[str] = []

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        self.requested_urls.append(url)
        status_code, body = self.responses[url]
        return self.persistence.persist_imported_response(
            author_source_id=author_source_id,
            content_type=content_type,
            requested_url=url,
            status_code=status_code,
            content_type_header="application/json",
            body=body,
            transport=ZhihuTransport.PYTHON_HTTP,
        )


def _source() -> KnowledgeSourceDefinition:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    return next(source for source in registry.sources if source.source_id == "zhihu:mr-dang-77")


def _fixture(name: str) -> bytes:
    return (PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / name).read_bytes()


def _service(state, object_store, tmp_path, responses):
    persistence = ZhihuHttpTransport(object_store, state)
    transport = RecordedZhihuTransport(persistence, responses)
    service = ZhihuCollectionService(
        state,
        object_store,
        ParquetKnowledgeStore(tmp_path / "parquet"),
        transport=transport,
        minimum_request_interval_seconds=0,
    )
    return service, transport


def test_recorded_zhihu_pages_are_frozen_versioned_and_checkpointed(
    state, object_store, tmp_path
) -> None:
    service, transport = _service(
        state,
        object_store,
        tmp_path,
        {
            PAGE_1_URL: (200, _fixture("zhihu_answers_page_1.json")),
            PAGE_2_URL: (200, _fixture("zhihu_answers_page_2.json")),
        },
    )

    execution = service.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )

    assert transport.requested_urls == [PAGE_1_URL, PAGE_2_URL]
    assert execution.report.coverage_status is CoverageStatus.COMPLETE
    assert execution.report.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    assert execution.report.discovered_count == 3
    assert execution.report.success_count == 3
    assert len(execution.listing_pages) == 2
    assert {page.reported_total for page in execution.listing_pages} == {3}
    assert len(execution.content_records) == 3
    assert len(execution.parquet_files) == 3
    assert all(path.is_file() for path in execution.parquet_files)
    assert all(
        object_store.verify(record.body_object_sha256) for record in execution.content_records
    )
    checkpoint = state.get_collection_checkpoint(
        "zhihu:mr-dang-77",
        "answers",
    )
    assert checkpoint is not None
    assert checkpoint.terminal_condition is CollectionTerminalCondition.PAGINATION_COMPLETE
    with state.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM zhihu_listing_page_manifest").fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM zhihu_content_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_coverage_report").fetchone()[0] == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM source_access_decision").fetchone()[0] == 2


def test_coverage_audit_rejects_platform_total_and_unique_id_mismatch(
    state,
    object_store,
    tmp_path,
) -> None:
    terminal = json.loads(_fixture("zhihu_answers_page_2.json"))
    terminal["paging"]["totals"] = 4
    service, _ = _service(
        state,
        object_store,
        tmp_path,
        {
            PAGE_1_URL: (200, _fixture("zhihu_answers_page_1.json")),
            PAGE_2_URL: (200, json.dumps(terminal).encode()),
        },
    )
    source = _source()
    service.sync_listing(source, ZhihuContentType.ANSWERS, page_size=2)

    report = KnowledgeCoverageAuditService(
        state,
        object_store,
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    answers = next(
        scope
        for scope in report.source_reports[0].scope_reports
        if scope.content_type == ZhihuContentType.ANSWERS.value
    )

    assert answers.listing_reported_total == 4
    assert answers.listing_unique_content_count == 3
    assert answers.listing_total_mismatch_count == 1
    assert answers.listing_total_change_count == 1
    assert "LISTING_TOTAL_MISMATCH:1" in answers.findings
    assert "LISTING_REPORTED_TOTAL_CHANGED:1" in answers.findings


def test_metadata_only_listing_discovers_id_without_claiming_full_body(
    state, object_store, tmp_path
) -> None:
    service, _ = _service(
        state,
        object_store,
        tmp_path,
        {
            PAGE_1_URL: (200, _fixture("zhihu_answers_metadata_only.json")),
        },
    )

    execution = service.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )

    assert execution.report.coverage_status is CoverageStatus.COMPLETE
    assert len(execution.content_records) == 1
    record = execution.content_records[0]
    assert record.content_id == "101"
    assert object_store.get_bytes(record.body_object_sha256) == b""
    assert record.content_completeness.value == "LISTING_UNVERIFIED"


def test_checkpoint_is_not_advanced_when_boundary_commit_crashes(
    state, object_store, tmp_path, monkeypatch
) -> None:
    service, _ = _service(
        state,
        object_store,
        tmp_path,
        {PAGE_1_URL: (200, _fixture("zhihu_answers_page_1.json"))},
    )
    original = state.set_collection_checkpoint

    def crash_before_checkpoint(*args, **kwargs):
        raise RuntimeError("synthetic checkpoint crash")

    monkeypatch.setattr(state, "set_collection_checkpoint", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        service.sync_listing(
            _source(),
            ZhihuContentType.ANSWERS,
            page_size=2,
        )
    assert state.get_collection_checkpoint("zhihu:mr-dang-77", "answers") is None
    assert (
        service.repository.content_version_count("zhihu:mr-dang-77", ZhihuContentType.ANSWERS) == 2
    )

    monkeypatch.setattr(state, "set_collection_checkpoint", original)
    resumed, _ = _service(
        state,
        object_store,
        tmp_path,
        {
            PAGE_1_URL: (200, _fixture("zhihu_answers_page_1.json")),
            PAGE_2_URL: (200, _fixture("zhihu_answers_page_2.json")),
        },
    )
    execution = resumed.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )
    assert execution.report.skipped_duplicate_count == 2
    assert (
        resumed.repository.content_version_count("zhihu:mr-dang-77", ZhihuContentType.ANSWERS) == 3
    )


def test_access_restriction_is_a_gap_not_a_confirmed_empty_collection(
    state, object_store, tmp_path
) -> None:
    service, _ = _service(
        state,
        object_store,
        tmp_path,
        {PAGE_1_URL: (401, b'{"error":{"code":401}}')},
    )

    execution = service.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )

    assert execution.report.coverage_status is CoverageStatus.ACCESS_RESTRICTED
    assert execution.report.terminal_condition is CollectionTerminalCondition.ACCESS_RESTRICTED
    assert execution.report.restricted_count == 1
    assert execution.report.discovered_count == 0
    assert execution.report.gaps[0]["failure_class"] == "AUTH_REQUIRED"
    assert state.get_collection_checkpoint("zhihu:mr-dang-77", "answers") is None
    assert len(execution.report.source_snapshot_ids) == 1
    snapshot_hash = execution.report.source_snapshot_ids[0].rsplit(":", 1)[-1]
    assert object_store.verify(snapshot_hash)


def test_successful_retry_resolves_the_matching_historical_gap(
    state, object_store, tmp_path
) -> None:
    restricted, _ = _service(
        state,
        object_store,
        tmp_path,
        {PAGE_1_URL: (401, b'{"error":{"code":401}}')},
    )
    restricted.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )

    resumed, _ = _service(
        state,
        object_store,
        tmp_path,
        {
            PAGE_1_URL: (200, _fixture("zhihu_answers_page_1.json")),
            PAGE_2_URL: (200, _fixture("zhihu_answers_page_2.json")),
        },
    )
    execution = resumed.sync_listing(
        _source(),
        ZhihuContentType.ANSWERS,
        page_size=2,
    )

    assert execution.report.coverage_status is CoverageStatus.COMPLETE
    with state.connect() as connection:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM collection_gap ORDER BY gap_id"
            ).fetchall()
        ]
    assert statuses == ["RESOLVED"]


def test_profile_probe_records_confirmed_platform_identity(state, object_store, tmp_path) -> None:
    profile = json.dumps(
        {
            "id": "fixture-author-id",
            "url_token": "mr-dang-77",
            "name": "MR Dang",
        }
    ).encode()
    service, transport = _service(
        state,
        object_store,
        tmp_path,
        {PROFILE_URL: (200, profile)},
    )

    identity = service.probe_identity(_source())

    assert transport.requested_urls == [PROFILE_URL]
    assert identity.platform_user_id == "fixture-author-id"
    assert identity.url_token == "mr-dang-77"
    assert service.repository.get_identity("zhihu:mr-dang-77") == identity
