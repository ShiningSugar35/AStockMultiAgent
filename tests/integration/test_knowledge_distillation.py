from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow.parquet as pq
import pymupdf

from astock.books import PrivateDocxIngestService, PrivatePdfIngestService
from astock.core.object_store import ObjectStore
from astock.knowledge import (
    DistillationRepository,
    KnowledgeDistillationService,
    KnowledgeDraftService,
    KnowledgeRepository,
    load_distillation_rules,
)
from astock.schemas import (
    BookApprovalStatus,
    BookEvaluationStatus,
    DistillationDecision,
    DistillationLocatorType,
    DocumentType,
    FetchStatus,
    HumanReviewStatus,
    KnowledgeSourceDefinition,
    PrivateSkillCandidatePayload,
    PrivateViewpointDraftPayload,
    SourceSnapshot,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_METHOD = "ROE and cash flow require review."
_PRIVATE_SECOND = "Review valuation with PE and PB."
_PRIVATE_NOISE = "Synthetic material without a method keyword."


def _pdf(path: Path) -> None:
    pdf = pymupdf.open()
    first = pdf.new_page()
    first.insert_text((72, 72), _PRIVATE_METHOD)
    second = pdf.new_page()
    second.insert_text((72, 72), _PRIVATE_SECOND)
    path.write_bytes(pdf.tobytes())
    pdf.close()


def _docx(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
   ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{_PRIVATE_METHOD}</w:t></w:r></w:p>
    <w:p><w:r><w:t>{_PRIVATE_NOISE}</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def _source() -> KnowledgeSourceDefinition:
    return KnowledgeSourceDefinition.model_validate(
        {
            "source_id": "zhihu:test-distillation",
            "display_name": "Synthetic author",
            "platform": "zhihu",
            "profile_url": "https://www.zhihu.com/people/test-distillation",
            "url_token": "test-distillation",
            "identity_status": "CONFIRMED",
            "access_status": "LOGGED_IN_ACCESS_VERIFIED",
            "collection_scope": {
                "history_mode": "FULL_ACCESSIBLE_HISTORY",
                "content_types": ["thoughts"],
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


def _register_sources(tmp_path: Path, state, objects: ObjectStore) -> None:
    pdf_path = tmp_path / "private-method.pdf"
    _pdf(pdf_path)
    PrivatePdfIngestService(objects, state).ingest(
        pdf_path,
        source_id="book:test:distillation",
        display_name="Synthetic private PDF",
        author_source_id="zhihu:test-distillation",
        file_version="v1",
        document_type=DocumentType.PRIVATE_BOOK,
        full_parse=True,
        ocr_enabled=False,
    )
    docx_path = tmp_path / "private-method.docx"
    _docx(docx_path)
    PrivateDocxIngestService(objects, state).ingest(
        docx_path,
        source_id="docx:test:distillation",
        display_name="Synthetic private DOCX",
        author_source_id="zhihu:test-distillation",
        file_version="v1",
    )
    body = objects.put_bytes("估值需要安全边际，也需要风险控制。".encode())
    observed_at = datetime(2026, 7, 17, tzinfo=UTC)
    snapshot = SourceSnapshot(
        snapshot_id="snapshot:distillation-thought",
        source_id="zhihu:test-distillation",
        object_sha256=body.sha256,
        fetched_at=observed_at,
        available_to_system_at=observed_at,
        mime="application/json",
        byte_size=body.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
    )
    state.register_snapshot(snapshot)
    KnowledgeRepository(state).register_content(
        ZhihuContentRecord(
            version_id="zhihu-content:distillation-thought-v1",
            author_source_id="zhihu:test-distillation",
            content_id="thought-1",
            content_type=ZhihuContentType.THOUGHTS,
            canonical_url="https://www.zhihu.com/pin/thought-1",
            collected_at=observed_at,
            body_object_sha256=body.sha256,
            metadata_sha256="a" * 64,
            raw_source_snapshot_id=snapshot.snapshot_id,
            created_at=observed_at,
        )
    )


def test_cross_source_distillation_is_idempotent_auditable_and_private(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    _register_sources(tmp_path, state, objects)
    scope_id = state.upsert_collection_scope(
        author_id="zhihu:test-distillation",
        content_type="thoughts",
        status="ACCESS_RESTRICTED",
    )
    for snapshot_id in ("snapshot:failed-first", "snapshot:failed-retry"):
        state.record_collection_gap(
            scope_id=scope_id,
            cursor={
                "listing_page": 1,
                "listing_cursor": "same-cursor",
                "source_snapshot_id": snapshot_id,
            },
            failure_class="ACCESS_RESTRICTED",
            retryable=False,
            status="OPEN",
        )
    rules = load_distillation_rules(
        PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
    )
    service = KnowledgeDistillationService(state, objects, tmp_path / "parquet")

    first = service.run(_source(), rules)
    repeated = service.run(_source(), rules)
    audit = service.audit("zhihu:test-distillation")
    draft_service = KnowledgeDraftService(state, objects)
    draft_execution = draft_service.generate("zhihu:test-distillation")
    repeated_drafts = draft_service.generate("zhihu:test-distillation")
    draft_audit = draft_service.audit("zhihu:test-distillation")

    assert first.run.run_id == repeated.run.run_id
    assert first.report == repeated.report
    assert first.review_queue == repeated.review_queue
    assert first.report.human_review_status is HumanReviewStatus.PENDING
    assert first.report.online_content_count == 1
    assert first.report.open_collection_gap_count == 1
    assert len(first.book_cleaning_report_ids) == 2
    assert len(first.book_method_coverage_report_ids) == 2
    units = DistillationRepository(state).units_for_run(first.run.run_id)
    assert {unit.locator.locator_type for unit in units} == {
        DistillationLocatorType.PAGE_TEXT,
        DistillationLocatorType.BLOCK_TEXT,
        DistillationLocatorType.ZHIHU_CONTENT,
    }
    duplicates = [unit for unit in units if unit.duplicate_of_unit_id]
    assert len(duplicates) == 1
    assert duplicates[0].decision is DistillationDecision.DOWNWEIGHT_CANDIDATE
    assert "EXACT_DUPLICATE" in duplicates[0].reason_codes
    assert first.report.keep_candidate_count >= 3
    assert first.parquet_file.is_file()
    assert audit["status"] == "PASS"
    assert audit["missing_normalized_object_count"] == 0
    assert audit["missing_source_object_count"] == 0
    assert audit["parquet_hash_mismatch_count"] == 0
    assert draft_execution == repeated_drafts
    assert draft_execution.report.human_review_status is HumanReviewStatus.PENDING
    assert draft_execution.report.viewpoint_draft_count == len(
        draft_execution.viewpoint_drafts
    )
    assert draft_execution.report.skill_candidate_count == len(
        draft_execution.skill_candidates
    )
    assert draft_execution.viewpoint_drafts
    assert draft_execution.skill_candidates
    assert all(
        candidate.evaluation_status is BookEvaluationStatus.NOT_RUN
        and candidate.approval_status is BookApprovalStatus.PENDING
        for candidate in draft_execution.skill_candidates
    )
    viewpoint_payloads = [
        PrivateViewpointDraftPayload.model_validate_json(
            objects.get_bytes(draft.payload_object_sha256)
        )
        for draft in draft_execution.viewpoint_drafts
    ]
    assert _PRIVATE_METHOD in {payload.proposition for payload in viewpoint_payloads}
    assert all(
        not payload.applicability_scope
        and not payload.counterevidence
        and not payload.failure_conditions
        and "PROPOSITION_NOT_SYNTHESIZED" in payload.quality_gaps
        for payload in viewpoint_payloads
    )
    skill_payloads = [
        PrivateSkillCandidatePayload.model_validate_json(
            objects.get_bytes(candidate.payload_object_sha256)
        )
        for candidate in draft_execution.skill_candidates
    ]
    assert all(payload.formal_rule is None for payload in skill_payloads)
    assert draft_audit["status"] == "PASS"
    assert draft_audit["missing_payload_object_count"] == 0
    assert draft_audit["candidate_reference_mismatch_count"] == 0
    assert draft_audit["pending_gate_mismatch_count"] == 0
    parquet = pq.ParquetFile(first.parquet_file).read()
    assert parquet.num_rows == len(units)
    assert "normalized_text_sha256" in parquet.column_names
    assert "text" not in parquet.column_names

    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_distillation_run"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_distillation_unit"
        ).fetchone()[0] == len(units)
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_gap WHERE status='OPEN'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM author_distillation_report"
        ).fetchone()[0] == 1
        safe_metadata = "\n".join(
            str(row[0])
            for table, column in (
                ("knowledge_distillation_run", "run_json"),
                ("knowledge_distillation_unit", "unit_json"),
                ("author_distillation_report", "report_json"),
                ("book_cleaning_report", "report_json"),
                ("book_method_coverage_report", "report_json"),
                ("private_viewpoint_draft", "draft_json"),
                ("private_skill_candidate_draft", "candidate_json"),
                ("author_draft_generation_report", "report_json"),
            )
            for row in connection.execute(f"SELECT {column} FROM {table}").fetchall()
        )
    for private_value in (
        _PRIVATE_METHOD,
        _PRIVATE_SECOND,
        _PRIVATE_NOISE,
        str(tmp_path),
        "private-method.pdf",
        "private-method.docx",
    ):
        assert private_value not in safe_metadata
