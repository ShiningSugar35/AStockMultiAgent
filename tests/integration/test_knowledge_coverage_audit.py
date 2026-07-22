from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from astock.books import PrivateDocxIngestService
from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore
from astock.knowledge import (
    KnowledgeCoverageAuditService,
    KnowledgeRepository,
    ParquetKnowledgeStore,
)
from astock.schemas import (
    AuthorCollectionCoverageReport,
    CollectionCheckpoint,
    CollectionTerminalCondition,
    CoverageStatus,
    KnowledgeAuditStatus,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    ZhihuAuthorIdentity,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContainerType,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuImportedResponse,
    ZhihuResponseKind,
    ZhihuTransport,
)


def _docx(path: Path) -> bytes:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
   ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Synthetic audit block one.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Synthetic audit block two.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return path.read_bytes()


def _local_source(file_sha256: str) -> KnowledgeSourceDefinition:
    return KnowledgeSourceDefinition.model_validate(
        {
            "source_id": "zhihu:local-audit-author",
            "display_name": "Local audit author",
            "platform": "zhihu",
            "identity_status": "LOCAL_EXPORT_USER_CONFIRMED_COMPLETE",
            "access_status": "LOCAL_EXPORT_PARSED_COMPLETE",
            "local_seed_sources": [
                {
                    "source_id": "zhihu-export:local-audit-author:articles",
                    "source_type": "PRIVATE_DOCX_EXPORT",
                    "author_source_id": "zhihu:local-audit-author",
                    "file_version": "v1",
                    "expected_sha256": file_sha256,
                    "expected_block_count": 2,
                    "rights_status": "LOCAL_PRIVATE_RESEARCH",
                    "ingestion_scope": "FULL_DOCUMENT_BLOCK_PARSE",
                    "online_history_coverage": "USER_CONFIRMED_COMPLETE_EXPORT",
                }
            ],
            "collection_scope": {
                "history_mode": "USER_CONFIRMED_COMPLETE_LOCAL_EXPORT",
                "content_types": ["exported_content"],
                "include_question_context": True,
                "include_required_comment_pages": False,
                "include_nested_replies": False,
                "derive_author_participation_chains": True,
                "incremental_updates": True,
            },
            "online_collection_required": False,
            "rights_status": "USER_ALLOWLISTED_LOCAL_RESEARCH",
            "enabled": True,
        }
    )


def _online_source() -> KnowledgeSourceDefinition:
    return KnowledgeSourceDefinition.model_validate(
        {
            "source_id": "zhihu:online-audit-author",
            "display_name": "Online audit author",
            "platform": "zhihu",
            "profile_url": "https://www.zhihu.com/people/online-audit-author",
            "url_token": "online-audit-author",
            "identity_status": "CONFIRMED",
            "access_status": "LOGGED_IN_ACCESS_VERIFIED",
            "collection_scope": {
                "history_mode": "FULL_ACCESSIBLE_HISTORY",
                "content_types": ["answers", "thoughts", "articles"],
                "include_question_context": True,
                "include_required_comment_pages": True,
                "include_nested_replies": True,
                "derive_author_participation_chains": True,
                "incremental_updates": True,
            },
            "rights_status": "USER_ALLOWLISTED_LOCAL_RESEARCH",
            "enabled": True,
        }
    )


def _empty_coverage_report(
    source_id: str,
    content_type: str,
    *,
    report_id: str,
    created_at: datetime,
    coverage_status: CoverageStatus = CoverageStatus.COMPLETE,
) -> AuthorCollectionCoverageReport:
    return AuthorCollectionCoverageReport(
        report_id=report_id,
        author_id=source_id,
        content_type=content_type,
        discovered_count=0,
        scheduled_count=0,
        success_count=0,
        failed_count=0,
        restricted_count=0,
        skipped_duplicate_count=0,
        updated_count=0,
        missing_count=0,
        terminal_condition=CollectionTerminalCondition.CONFIRMED_EMPTY,
        coverage_status=coverage_status,
        created_at=created_at,
    )


def _pending_import(
    source_id: str,
    envelope_id: str,
    imported_at: datetime,
    *,
    response_kind: ZhihuResponseKind = ZhihuResponseKind.CONTENT_DETAIL,
) -> ZhihuImportedResponse:
    return ZhihuImportedResponse(
        envelope_id=envelope_id,
        author_source_id=source_id,
        response_kind=response_kind,
        content_type=ZhihuContentType.ANSWERS,
        content_id=f"answer-{envelope_id}",
        requested_url=f"https://www.zhihu.com/answer/{envelope_id}",
        status_code=200,
        response_mime="application/json",
        transport=ZhihuTransport.CHROME,
        source_snapshot_id=f"snapshot:{envelope_id}",
        raw_object_sha256=sha256_bytes(envelope_id.encode()),
        body_byte_size=2,
        captured_at=imported_at,
        imported_at=imported_at,
    )


def _ingest(tmp_path: Path, state):
    path = tmp_path / "coverage-fixture.docx"
    raw = _docx(path)
    objects = ObjectStore(tmp_path / "objects")
    result = PrivateDocxIngestService(objects, state).ingest(
        path,
        source_id="zhihu-export:local-audit-author:articles",
        display_name="Synthetic local export",
        author_source_id="zhihu:local-audit-author",
        file_version="v1",
    )
    return raw, objects, result


def test_local_export_coverage_verifies_hash_report_blocks_and_objects(
    tmp_path: Path,
    state,
) -> None:
    raw, objects, result = _ingest(tmp_path, state)
    source = _local_source(sha256_bytes(raw))

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_local_source(source)

    assert report.status is KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT
    assert report.coverage_basis == "USER_CONFIRMED_COMPLETE_EXPORT"
    assert report.manifest_id == result.manifest.manifest_id
    assert report.parse_report_id == result.parse_report.docx_parse_report_id
    assert report.expected_block_count == 2
    assert report.registered_block_count == 2
    assert report.verified_text_object_count == 2
    assert report.verified_metadata_object_count == 2
    assert report.missing_object_count == 0
    assert report.findings == []
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_local_coverage_report"
        ).fetchone()[0] == 1


def test_local_export_missing_object_is_partial_and_aggregate_remains_truthful(
    tmp_path: Path,
    state,
) -> None:
    raw, objects, result = _ingest(tmp_path, state)
    source = _local_source(sha256_bytes(raw))
    objects.path_for(result.parse_report.block_set_sha256).unlink()
    service = KnowledgeCoverageAuditService(state, objects, tmp_path / "parquet")

    local_report = service.audit_local_source(source)
    aggregate = service.audit_registry(
        KnowledgeSourceRegistry(sources=[source, _online_source()])
    )

    assert local_report.status is KnowledgeAuditStatus.PARTIAL
    assert "DOCX_BLOCK_SET_OBJECT_MISSING_OR_INVALID" in local_report.findings
    assert local_report.missing_object_count == 1
    assert aggregate.status is KnowledgeAuditStatus.PARTIAL
    assert aggregate.missing_object_count == 1
    local_audit = next(
        item for item in aggregate.source_reports if item.source_id == source.source_id
    )
    online_audit = next(
        item
        for item in aggregate.source_reports
        if item.source_id == "zhihu:online-audit-author"
    )
    assert local_audit.status is KnowledgeAuditStatus.PARTIAL
    assert online_audit.status is KnowledgeAuditStatus.PARTIAL
    assert {scope.status for scope in online_audit.scope_reports} == {
        KnowledgeAuditStatus.NOT_COLLECTED
    }
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_coverage_audit_report"
        ).fetchone()[0] == 1


def test_online_audit_counts_missing_boundaries_not_repeated_attempts(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    scope_id = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="answers",
        status="ACCESS_RESTRICTED",
    )
    for snapshot_id in ("snapshot:first", "snapshot:retry"):
        state.record_collection_gap(
            scope_id=scope_id,
            cursor={
                "listing_page": 0,
                "listing_cursor": None,
                "source_snapshot_id": snapshot_id,
            },
            failure_class="AUTH_REQUIRED",
            retryable=False,
            status="OPEN",
        )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(KnowledgeSourceRegistry(sources=[source]))

    source_report = report.source_reports[0]
    answers = next(
        item for item in source_report.scope_reports if item.content_type == "answers"
    )
    assert report.total_open_gap_count == 1
    assert source_report.open_gap_count == 1
    assert answers.open_gap_count == 1
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" in source_report.findings
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" in report.findings
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap WHERE status='OPEN'"
        ).fetchone()[0] == 2


def test_online_audit_excludes_retired_pending_and_gap_from_a_passing_scope(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    now = datetime.now(UTC)
    repository = KnowledgeRepository(state)
    repository.register_identity(
        ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id="online-audit-author-id",
            url_token="online-audit-author",
            display_name="Online audit author",
            profile_url="https://www.zhihu.com/people/online-audit-author",
            profile_snapshot_id="snapshot:online-audit-author",
            profile_object_sha256="1" * 64,
            verified_at=now,
        )
    )
    for index, content_type in enumerate(source.collection_scope.content_types, start=2):
        repository.register_coverage_report(
            AuthorCollectionCoverageReport(
                report_id=f"coverage:{content_type}",
                author_id=source.source_id,
                content_type=content_type,
                discovered_count=0,
                scheduled_count=0,
                success_count=0,
                failed_count=0,
                restricted_count=0,
                skipped_duplicate_count=0,
                updated_count=0,
                missing_count=0,
                terminal_condition=CollectionTerminalCondition.CONFIRMED_EMPTY,
                coverage_status=CoverageStatus.COMPLETE,
            ),
            object_hash=f"{index:x}" * 64,
        )
    repository.register_imported_response(
        ZhihuImportedResponse(
            envelope_id="historical-root-pending",
            author_source_id=source.source_id,
            response_kind=ZhihuResponseKind.ROOT_COMMENTS,
            content_type=ZhihuContentType.ANSWERS,
            content_id="answer-1",
            comment_page=0,
            requested_url=(
                "https://www.zhihu.com/api/v4/comment_v5/answers/answer-1/root_comment"
            ),
            status_code=200,
            response_mime="application/json",
            transport=ZhihuTransport.CHROME,
            source_snapshot_id="snapshot:historical-root-pending",
            raw_object_sha256="6" * 64,
            body_byte_size=2,
            captured_at=now,
            imported_at=now,
        )
    )
    retired_scope = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="comments:answers:answer-1:__root__",
        status="ACCESS_RESTRICTED",
    )
    state.record_collection_gap(
        scope_id=retired_scope,
        cursor={"comment_page": 0, "source_snapshot_id": "snapshot:retired-gap"},
        failure_class="ACCESS_RESTRICTED",
        retryable=False,
        status="OPEN",
    )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )

    source_report = report.source_reports[0]
    assert report.status is KnowledgeAuditStatus.PASS
    assert report.total_pending_import_count == 0
    assert report.total_open_gap_count == 0
    assert source_report.status is KnowledgeAuditStatus.PASS
    assert source_report.pending_import_count == 0
    assert source_report.open_gap_count == 0
    assert {scope.status for scope in source_report.scope_reports} == {
        KnowledgeAuditStatus.PASS
    }
    assert repository.pending_import_count(source.source_id) == 1
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap WHERE status='OPEN'"
        ).fetchone()[0] == 1


def test_online_audit_keeps_verified_body_scopes_partial_until_column_observation(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    source = source.model_copy(
        update={
            "collection_scope": source.collection_scope.model_copy(
                update={"container_types": [ZhihuContainerType.COLUMNS]}
            )
        }
    )
    now = datetime.now(UTC)
    repository = KnowledgeRepository(state)
    repository.register_identity(
        ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id="online-audit-author-id",
            url_token="online-audit-author",
            display_name="Online audit author",
            profile_url="https://www.zhihu.com/people/online-audit-author",
            profile_snapshot_id="snapshot:online-audit-author",
            profile_object_sha256="7" * 64,
            verified_at=now,
        )
    )
    for index, content_type in enumerate(source.collection_scope.content_types, start=8):
        repository.register_coverage_report(
            AuthorCollectionCoverageReport(
                report_id=f"column-gate-coverage:{content_type}",
                author_id=source.source_id,
                content_type=content_type,
                discovered_count=0,
                scheduled_count=0,
                success_count=0,
                failed_count=0,
                restricted_count=0,
                skipped_duplicate_count=0,
                updated_count=0,
                missing_count=0,
                terminal_condition=CollectionTerminalCondition.CONFIRMED_EMPTY,
                coverage_status=CoverageStatus.COMPLETE,
            ),
            object_hash=f"{index:x}" * 64,
        )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )

    source_report = report.source_reports[0]
    assert {scope.status for scope in source_report.scope_reports} == {
        KnowledgeAuditStatus.PASS
    }
    assert source_report.status is KnowledgeAuditStatus.PARTIAL
    assert source_report.findings == ["COLUMN_ENUMERATION_NOT_VERIFIED"]
    assert report.status is KnowledgeAuditStatus.PARTIAL
    assert report.findings == ["SOURCE_COVERAGE_INCOMPLETE"]


def test_online_audit_ignores_historical_interaction_total_mismatch(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    objects = ObjectStore(tmp_path / "objects")
    content_body = objects.put_bytes(b"full answer")
    comment_body = objects.put_bytes(b"one root")
    now = datetime(2026, 7, 18, tzinfo=UTC)
    repository = KnowledgeRepository(state)
    repository.register_content(
        ZhihuContentRecord(
            version_id="content:answer-1",
            author_source_id=source.source_id,
            content_id="answer-1",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url="https://www.zhihu.com/question/1/answer/answer-1",
            question_id="1",
            question_title="Synthetic question",
            collected_at=now,
            body_object_sha256=content_body.sha256,
            metadata_sha256="a" * 64,
            raw_source_snapshot_id="snapshot:answer-1",
            created_at=now,
        )
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:root-1",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="answer-1",
            comment_id="root-1",
            root_comment_id="root-1",
            collected_at=now,
            child_comment_count=0,
            is_target_author=False,
            body_object_sha256=comment_body.sha256,
            metadata_sha256="b" * 64,
            raw_source_snapshot_id="snapshot:root-1",
            created_at=now,
        )
    )
    repository.register_comment_page(
        ZhihuCommentPage(
            page_id="comment-page:answer-1",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="answer-1",
            comment_page=0,
            request_url=(
                "https://www.zhihu.com/api/v4/comment_v5/answers/answer-1/root_comment"
            ),
            is_end=True,
            reported_total=2,
            comment_ids=["root-1"],
            source_snapshot_id="snapshot:root-page",
            raw_object_sha256="c" * 64,
            transport=ZhihuTransport.CHROME,
            http_status=200,
            response_structure_version="fixture",
            fetched_at=now,
            created_at=now,
        )
    )
    state.set_collection_checkpoint(
        CollectionCheckpoint(
            author=source.source_id,
            content_type="answers",
            listing_page=0,
            content_id="answer-1",
            comment_page=1,
            terminal_condition=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="c" * 64,
    )

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_registry(KnowledgeSourceRegistry(sources=[source]))

    answers = next(
        item for item in report.source_reports[0].scope_reports if item.content_type == "answers"
    )
    assert answers.root_comment_total_mismatch_count == 0
    assert answers.platform_comment_total_mismatch_count == 0
    assert all("COMMENT" not in finding and "REPLY" not in finding for finding in answers.findings)


def test_online_audit_ignores_historical_child_count_differences(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    objects = ObjectStore(tmp_path / "objects")
    content_body = objects.put_bytes(b"full answer")
    root_body = objects.put_bytes(b"root")
    child_body = objects.put_bytes(b"one preview child")
    now = datetime.now(UTC)
    repository = KnowledgeRepository(state)
    repository.register_content(
        ZhihuContentRecord(
            version_id="content:child-audit-answer",
            author_source_id=source.source_id,
            content_id="child-audit-answer",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url=(
                "https://www.zhihu.com/question/1/answer/child-audit-answer"
            ),
            question_id="1",
            question_title="Synthetic question",
            collected_at=now,
            body_object_sha256=content_body.sha256,
            metadata_sha256="1" * 64,
            raw_source_snapshot_id="snapshot:child-audit-answer",
            created_at=now,
        )
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:child-audit-root",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="child-audit-answer",
            comment_id="child-audit-root",
            root_comment_id="child-audit-root",
            collected_at=now,
            child_comment_count=2,
            is_target_author=False,
            body_object_sha256=root_body.sha256,
            metadata_sha256="2" * 64,
            raw_source_snapshot_id="snapshot:child-audit-root",
            created_at=now,
        )
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:child-audit-preview",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="child-audit-answer",
            comment_id="child-audit-preview",
            parent_comment_id="child-audit-root",
            root_comment_id="child-audit-root",
            collected_at=now,
            child_comment_count=0,
            is_target_author=False,
            body_object_sha256=child_body.sha256,
            metadata_sha256="3" * 64,
            raw_source_snapshot_id="snapshot:child-audit-preview",
            created_at=now,
        )
    )
    service = KnowledgeCoverageAuditService(state, objects, tmp_path / "parquet")

    before = service.audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    before_answers = next(
        item
        for item in before.source_reports[0].scope_reports
        if item.content_type == "answers"
    )

    assert before_answers.child_reply_required_count == 0
    assert before_answers.child_reply_terminal_count == 0
    assert before_answers.child_reply_count_mismatch_count == 0
    assert all(
        "COMMENT" not in finding and "REPLY" not in finding
        for finding in before_answers.findings
    )

    state.set_collection_checkpoint(
        CollectionCheckpoint(
            author=source.source_id,
            content_type="answers",
            listing_page=0,
            content_id="child-audit-answer",
            comment_parent_id="child-audit-root",
            comment_page=0,
            terminal_condition=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="4" * 64,
    )
    after = service.audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    after_answers = next(
        item
        for item in after.source_reports[0].scope_reports
        if item.content_type == "answers"
    )

    assert after_answers.child_reply_terminal_count == 0
    assert after_answers.child_reply_count_mismatch_count == 0
    assert all(
        "COMMENT" not in finding and "REPLY" not in finding
        for finding in after_answers.findings
    )


def test_online_audit_ignores_historical_interaction_pagination_cycles(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    objects = ObjectStore(tmp_path / "objects")
    body = objects.put_bytes(b"full answer")
    now = datetime.now(UTC)
    repository = KnowledgeRepository(state)
    repository.register_content(
        ZhihuContentRecord(
            version_id="content:cycle-audit-answer",
            author_source_id=source.source_id,
            content_id="cycle-audit-answer",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url=(
                "https://www.zhihu.com/question/1/answer/cycle-audit-answer"
            ),
            question_id="1",
            question_title="Synthetic question",
            collected_at=now,
            body_object_sha256=body.sha256,
            metadata_sha256="5" * 64,
            raw_source_snapshot_id="snapshot:cycle-audit-answer",
            created_at=now,
        )
    )
    cycle_url = (
        "https://www.zhihu.com/api/v4/comment_v5/answers/cycle-audit-answer/"
        "root_comment?order_by=score&limit=10&offset=cycle"
    )
    for page_number in (5, 6):
        repository.register_comment_page(
            ZhihuCommentPage(
                page_id=f"comment-page:cycle-{page_number}",
                author_source_id=source.source_id,
                content_type=ZhihuContentType.ANSWERS,
                content_id="cycle-audit-answer",
                comment_page=page_number,
                request_url=cycle_url,
                request_cursor=cycle_url,
                next_cursor=cycle_url,
                is_end=False,
                reported_total=231,
                comment_ids=["cycle-root"],
                source_snapshot_id=f"snapshot:cycle-page-{page_number}",
                raw_object_sha256=str(page_number) * 64,
                transport=ZhihuTransport.CHROME,
                http_status=200,
                response_structure_version="fixture",
                fetched_at=now,
                created_at=now,
            )
        )

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    answers = next(
        item
        for item in report.source_reports[0].scope_reports
        if item.content_type == "answers"
    )

    assert answers.comment_pagination_cycle_count == 0
    assert all("COMMENT" not in finding for finding in answers.findings)


def test_audit_uses_one_default_or_explicit_cutoff_and_rejects_negative_lag(
    tmp_path: Path,
    state,
) -> None:
    service = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    )
    registry = KnowledgeSourceRegistry(sources=[_online_source()])

    default = service.audit_registry(registry)
    stopped = service.audit_registry(registry, quiescence_lag=timedelta(0))

    assert default.data_cutoff_at is not None
    assert stopped.data_cutoff_at is not None
    assert default.audited_at - default.data_cutoff_at == timedelta(seconds=30)
    assert stopped.audited_at == stopped.data_cutoff_at
    assert all(
        scope.data_cutoff_at == default.data_cutoff_at
        for source in default.source_reports
        for scope in source.scope_reports
    )
    import pytest

    with pytest.raises(ValueError, match="cannot be negative"):
        service.audit_registry(registry, quiescence_lag=timedelta(microseconds=-1))


def test_online_audit_selects_latest_coverage_report_at_or_before_cutoff(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    repository = KnowledgeRepository(state)
    before = datetime.now(UTC) - timedelta(minutes=1)
    after = datetime.now(UTC) + timedelta(days=1)
    repository.register_identity(
        ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id="online-audit-author-id",
            url_token="online-audit-author",
            display_name="Online audit author",
            profile_url="https://www.zhihu.com/people/online-audit-author",
            profile_snapshot_id="snapshot:cutoff-identity",
            profile_object_sha256="1" * 64,
            verified_at=before,
        )
    )
    for index, content_type in enumerate(source.collection_scope.content_types, start=1):
        repository.register_coverage_report(
            _empty_coverage_report(
                source.source_id,
                content_type,
                report_id=f"coverage:before:{content_type}",
                created_at=before,
            ),
            object_hash=f"{index:x}" * 64,
        )
    repository.register_coverage_report(
        _empty_coverage_report(
            source.source_id,
            "answers",
            report_id="coverage:after:answers",
            created_at=after,
            coverage_status=CoverageStatus.ACCESS_RESTRICTED,
        ),
        object_hash="f" * 64,
    )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    answers = next(
        scope for scope in report.source_reports[0].scope_reports if scope.content_type == "answers"
    )

    assert answers.listing_report_id == "coverage:before:answers"
    assert answers.listing_coverage_status is CoverageStatus.COMPLETE
    latest = repository.latest_coverage_report(
        source.source_id, ZhihuContentType.ANSWERS
    )
    assert latest is not None
    assert latest.report_id == "coverage:after:answers"


def test_identity_remains_registered_at_cutoff_after_a_later_reverification(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    repository = KnowledgeRepository(state)
    first_verified = datetime.now(UTC) - timedelta(hours=2)
    later_reverification = datetime.now(UTC) + timedelta(hours=1)
    stored_reverification = repository.register_identity(
        ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id="online-audit-author-id",
            url_token="online-audit-author",
            display_name="Online audit author",
            profile_url="https://www.zhihu.com/people/online-audit-author",
            profile_snapshot_id="snapshot:first-verification",
            profile_object_sha256="1" * 64,
            verified_at=first_verified,
        )
    )
    repository.register_identity(
        ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id="online-audit-author-id",
            url_token="online-audit-author",
            display_name="Online audit author",
            profile_url="https://www.zhihu.com/people/online-audit-author",
            profile_snapshot_id="snapshot:later-reverification",
            profile_object_sha256="2" * 64,
            verified_at=later_reverification,
        )
    )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )

    assert report.source_reports[0].identity_registered
    assert "CONFIRMED_IDENTITY_NOT_REGISTERED" not in report.source_reports[0].findings
    with state.connect() as connection:
        stored_row = connection.execute(
            "SELECT verified_at,identity_json FROM knowledge_source_identity WHERE source_id=?",
            (source.source_id,),
        ).fetchone()
    stored_json = ZhihuAuthorIdentity.model_validate_json(stored_row["identity_json"])
    assert datetime.fromisoformat(stored_row["verified_at"]) == first_verified
    assert stored_reverification.verified_at == first_verified
    assert stored_json.verified_at == first_verified
    assert stored_json.profile_snapshot_id == "snapshot:later-reverification"
    assert repository.get_identity(source.source_id) == stored_json


def test_cutoff_queries_order_existing_offset_timestamps_by_absolute_time(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    repository = KnowledgeRepository(state)
    cutoff = datetime(2026, 7, 22, 12, 45, tzinfo=UTC)
    older = datetime(2026, 7, 22, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    newer = datetime(2026, 7, 22, 11, 30, tzinfo=timezone(-timedelta(hours=1)))
    for report_id, created_at, object_hash in (
        ("coverage:offset:older", older, "3" * 64),
        ("coverage:offset:newer", newer, "4" * 64),
    ):
        repository.register_coverage_report(
            _empty_coverage_report(
                source.source_id,
                "answers",
                report_id=report_id,
                created_at=created_at,
            ),
            object_hash=object_hash,
        )
    repository.register_imported_response(
        _pending_import(source.source_id, "offset-pending", older)
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_coverage_report SET created_at=? WHERE report_id=?",
            (older.isoformat(), "coverage:offset:older"),
        )
        connection.execute(
            "UPDATE knowledge_coverage_report SET created_at=? WHERE report_id=?",
            (newer.isoformat(), "coverage:offset:newer"),
        )
        connection.execute(
            "UPDATE zhihu_imported_response SET imported_at=? WHERE envelope_id=?",
            (older.isoformat(), "offset-pending"),
        )

    latest = repository.latest_coverage_report(
        source.source_id,
        ZhihuContentType.ANSWERS,
        data_cutoff_at=cutoff,
    )
    assert latest is not None
    assert latest.report_id == "coverage:offset:newer"
    assert repository.pending_import_count(
        source.source_id,
        response_kinds=(ZhihuResponseKind.CONTENT_DETAIL,),
        data_cutoff_at=datetime(2026, 7, 22, 12, 10, tzinfo=UTC),
    ) == 1


def test_pending_import_count_reconstructs_queue_at_cutoff_and_excludes_interactions(
    state,
) -> None:
    source_id = _online_source().source_id
    repository = KnowledgeRepository(state)
    cutoff = datetime(2026, 7, 22, 12, tzinfo=UTC)
    before = cutoff - timedelta(minutes=1)
    after = cutoff + timedelta(minutes=1)
    for envelope_id, imported_at, response_kind in (
        ("pending-before", before, ZhihuResponseKind.CONTENT_DETAIL),
        ("consumed-after", before, ZhihuResponseKind.LISTING),
        ("consumed-before", before, ZhihuResponseKind.CONTENT_DETAIL),
        ("pending-after", after, ZhihuResponseKind.CONTENT_DETAIL),
        ("retired-comment", before, ZhihuResponseKind.ROOT_COMMENTS),
    ):
        repository.register_imported_response(
            _pending_import(
                source_id,
                envelope_id,
                imported_at,
                response_kind=response_kind,
            )
        )
    repository.mark_import_consumed("consumed-after", after)
    repository.mark_import_consumed("consumed-before", before + timedelta(seconds=1))

    assert repository.pending_import_count(
        source_id,
        response_kinds=(ZhihuResponseKind.LISTING, ZhihuResponseKind.CONTENT_DETAIL),
        data_cutoff_at=cutoff,
    ) == 2
    assert repository.pending_import_count(source_id) == 3


def test_local_source_legacy_online_state_uses_the_same_cutoff(
    tmp_path: Path,
    state,
) -> None:
    raw, objects, _ = _ingest(tmp_path, state)
    source = _local_source(sha256_bytes(raw))
    now = datetime.now(UTC)
    repository = KnowledgeRepository(state)
    repository.register_imported_response(
        _pending_import(source.source_id, "local-after-cutoff", now + timedelta(hours=1))
    )
    scope_id = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="answers",
        status="PARTIAL",
    )
    gap_id = state.record_collection_gap(
        scope_id=scope_id,
        cursor={"offset": 0},
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ((now - timedelta(hours=2)).isoformat(),),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE gap_id=?",
            ((now - timedelta(hours=1)).isoformat(), gap_id),
        )
        connection.execute(
            "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?", (gap_id,)
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? "
            "WHERE gap_id=? AND status='RESOLVED'",
            ((now + timedelta(hours=1)).isoformat(), gap_id),
        )

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    source_report = report.source_reports[0]

    assert source_report.pending_import_count == 0
    assert source_report.open_gap_count == 1
    assert source_report.status is KnowledgeAuditStatus.PARTIAL
    assert "OPEN_COLLECTION_GAPS" in source_report.findings


def test_online_audit_reconstructs_gap_and_stale_job_state_at_cutoff(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    now = datetime.now(UTC)
    answer_scope = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="answers",
        status="PARTIAL",
    )
    gap_id = state.record_collection_gap(
        scope_id=answer_scope,
        cursor={"offset": 0},
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )
    late_scope = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="thoughts",
        status="PARTIAL",
    )
    state.record_collection_gap(
        scope_id=late_scope,
        cursor={"offset": 20},
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ((now - timedelta(hours=3)).isoformat(),),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE gap_id=?",
            ((now - timedelta(hours=2)).isoformat(), gap_id),
        )
        connection.execute(
            "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?", (gap_id,)
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? "
            "WHERE gap_id=? AND status='RESOLVED'",
            ((now + timedelta(hours=1)).isoformat(), gap_id),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE scope_id=?",
            ((now + timedelta(hours=1)).isoformat(), late_scope),
        )
        for suffix, started_at, ended_at in (
            ("stale-at-cutoff", now - timedelta(hours=2), now + timedelta(hours=1)),
            ("starts-after", now + timedelta(hours=1), None),
        ):
            connection.execute(
                "INSERT INTO job(job_id,type,status,input_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (suffix, "zhihu-capture", "SUCCEEDED", suffix, now.isoformat(), now.isoformat()),
            )
            connection.execute(
                "INSERT INTO job_attempt(attempt_id,job_id,started_at,ended_at,retryable) "
                "VALUES(?,?,?,?,0)",
                (
                    f"attempt:{suffix}",
                    suffix,
                    started_at.isoformat(),
                    ended_at.isoformat() if ended_at else None,
                ),
            )

    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )

    assert report.total_open_gap_count == 1
    assert report.source_reports[0].open_gap_count == 1
    assert report.stale_running_job_count == 1
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" not in report.findings


def test_online_audit_same_millisecond_open_gap_cannot_false_pass(
    tmp_path: Path,
    state,
    monkeypatch,
) -> None:
    source = _online_source()
    cutoff = datetime(2026, 7, 22, 12, 0, 0, 123500, tzinfo=UTC)
    KnowledgeRepository(state).register_coverage_report(
        _empty_coverage_report(
            source.source_id,
            "answers",
            report_id="coverage:same-ms:answers",
            created_at=cutoff - timedelta(seconds=1),
        ),
        object_hash="5" * 64,
    )
    scope_id = state.upsert_collection_scope(
        author_id=source.source_id,
        content_type="answers",
        status="PARTIAL",
    )
    gap_id = state.record_collection_gap(
        scope_id=scope_id,
        cursor={"offset": 0},
        failure_class="AUTH_REQUIRED",
        retryable=False,
        status="OPEN",
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE collection_gap_temporal_meta SET reliable_from=? WHERE singleton=1",
            ("2026-07-22T11:59:59.999+00:00",),
        )
        connection.execute(
            "UPDATE collection_gap_state_event SET occurred_at=? WHERE gap_id=?",
            ("2026-07-22T12:00:00.123+00:00", gap_id),
        )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff if tz is not None else cutoff.replace(tzinfo=None)

    monkeypatch.setattr("astock.knowledge.audit.datetime", FixedDatetime)
    report = KnowledgeCoverageAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        tmp_path / "parquet",
    ).audit_registry(
        KnowledgeSourceRegistry(sources=[source]),
        quiescence_lag=timedelta(0),
    )
    source_report = report.source_reports[0]
    answers = next(
        scope for scope in source_report.scope_reports if scope.content_type == "answers"
    )

    assert report.total_open_gap_count == 1
    assert source_report.open_gap_count == 1
    assert answers.open_gap_count == 1
    assert answers.status is KnowledgeAuditStatus.PARTIAL
    assert source_report.status is KnowledgeAuditStatus.PARTIAL
    assert report.status is KnowledgeAuditStatus.PARTIAL
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" in answers.findings
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" in source_report.findings
    assert "GAP_CUTOFF_HISTORY_UNAVAILABLE" in report.findings


def test_online_audit_excludes_inflight_active_content_rows_after_frozen_cutoff(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    objects = ObjectStore(tmp_path / "objects")
    body = objects.put_bytes(b"inflight answer")
    now = datetime.now(UTC)
    parquet = ParquetKnowledgeStore(tmp_path / "parquet")
    parquet.write(
        ZhihuContentRecord(
            version_id="content:inflight-parquet-only",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="answer-inflight-parquet-only",
            canonical_url="https://www.zhihu.com/question/1/answer/inflight-parquet-only",
            question_id="1",
            question_title="Inflight",
            collected_at=now,
            body_object_sha256=body.sha256,
            metadata_sha256="9" * 64,
            raw_source_snapshot_id="snapshot:inflight",
            created_at=now,
        )
    )
    paired = ZhihuContentRecord(
        version_id="content:inflight-paired",
        author_source_id=source.source_id,
        content_type=ZhihuContentType.ANSWERS,
        content_id="answer-inflight-paired",
        canonical_url="https://www.zhihu.com/question/1/answer/inflight-paired",
        question_id="1",
        question_title="Inflight paired",
        collected_at=now,
        body_object_sha256=body.sha256,
        metadata_sha256="8" * 64,
        raw_source_snapshot_id="snapshot:inflight-paired",
        created_at=now,
    )
    KnowledgeRepository(state).register_content(paired)
    parquet.write(paired)

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_registry(KnowledgeSourceRegistry(sources=[source]))

    answers = next(
        item for item in report.source_reports[0].scope_reports if item.content_type == "answers"
    )
    assert report.data_cutoff_at is not None
    assert report.data_cutoff_at < report.audited_at
    assert answers.data_cutoff_at == report.data_cutoff_at
    assert answers.sqlite_content_version_count == 0
    assert answers.parquet_content_version_count == 0
    assert answers.orphan_content_parquet_count == 0
    assert report.parquet_mismatch_count == 0


def test_online_audit_reports_active_parquet_only_row_before_cutoff(
    tmp_path: Path,
    state,
) -> None:
    source = _online_source()
    objects = ObjectStore(tmp_path / "objects")
    body = objects.put_bytes(b"settled orphan answer")
    settled = datetime.now(UTC) - timedelta(minutes=1)
    ParquetKnowledgeStore(tmp_path / "parquet").write(
        ZhihuContentRecord(
            version_id="content:settled-parquet-only",
            author_source_id=source.source_id,
            content_type=ZhihuContentType.ANSWERS,
            content_id="answer-settled-parquet-only",
            canonical_url="https://www.zhihu.com/question/1/answer/settled-parquet-only",
            question_id="1",
            question_title="Settled orphan",
            collected_at=settled,
            body_object_sha256=body.sha256,
            metadata_sha256="7" * 64,
            raw_source_snapshot_id="snapshot:settled-orphan",
            created_at=settled,
        )
    )

    report = KnowledgeCoverageAuditService(
        state,
        objects,
        tmp_path / "parquet",
    ).audit_registry(KnowledgeSourceRegistry(sources=[source]))
    answers = next(
        scope for scope in report.source_reports[0].scope_reports if scope.content_type == "answers"
    )

    assert answers.orphan_content_parquet_count == 1
    assert report.parquet_mismatch_count == 1
    assert "PARQUET_SQLITE_MISMATCH" in report.findings
