from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from astock.books import PrivateDocxIngestService
from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore
from astock.knowledge import KnowledgeCoverageAuditService
from astock.schemas import (
    KnowledgeAuditStatus,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
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
