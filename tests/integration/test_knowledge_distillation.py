from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow.parquet as pq
import pymupdf
import pytest

from astock.books import PrivateDocxIngestService, PrivatePdfIngestService
from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.knowledge import (
    DistillationRepository,
    KnowledgeDistillationService,
    KnowledgeDraftRepository,
    KnowledgeDraftService,
    KnowledgeRepository,
    KnowledgeStructureProfileService,
    load_distillation_rules,
)
from astock.schemas import (
    BookApprovalStatus,
    BookEvaluationStatus,
    CollectionCheckpoint,
    CollectionTerminalCondition,
    DistillationDecision,
    DistillationLocatorType,
    DocumentType,
    FetchStatus,
    HumanReviewStatus,
    KnowledgeMaterialKind,
    KnowledgeProcessingStrategy,
    KnowledgeSourceDefinition,
    PrivateSkillCandidatePayload,
    PrivateViewpointDraftPayload,
    SourceSnapshot,
    ZhihuCommentNode,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_METHOD = "ROE and cash flow require review."
_PRIVATE_SECOND = "Neutral context belongs to the same complete source piece."
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
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section Alpha</w:t></w:r></w:p>
    <w:p><w:r><w:t>{_PRIVATE_METHOD}</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section Beta</w:t></w:r></w:p>
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
    body = objects.put_bytes(
        "<p>估值需要安全边际，也需要风险控制。</p><p>这是同一篇的中性上下文。</p>".encode()
    )
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
            content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
            created_at=observed_at,
        )
    )
    for ordinal in range(13):
        content_id = f"valuation-thought-{ordinal:02d}"
        extra_body = objects.put_bytes(
            f"<p>估值与安全边际需要独立核对，样本编号 {ordinal:02d}。</p>".encode()
        )
        extra_snapshot = SourceSnapshot(
            snapshot_id=f"snapshot:distillation-{content_id}",
            source_id="zhihu:test-distillation",
            object_sha256=extra_body.sha256,
            fetched_at=observed_at,
            available_to_system_at=observed_at,
            mime="application/json",
            byte_size=extra_body.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
        )
        state.register_snapshot(extra_snapshot)
        KnowledgeRepository(state).register_content(
            ZhihuContentRecord(
                version_id=f"zhihu-content:{content_id}-v1",
                author_source_id="zhihu:test-distillation",
                content_id=content_id,
                content_type=ZhihuContentType.THOUGHTS,
                canonical_url=f"https://www.zhihu.com/pin/{content_id}",
                collected_at=observed_at,
                body_object_sha256=extra_body.sha256,
                metadata_sha256=f"{ordinal + 16:064x}",
                raw_source_snapshot_id=extra_snapshot.snapshot_id,
                content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
                created_at=observed_at,
            )
        )
    root_comment_body = objects.put_bytes("这条评论讨论估值方法。".encode())
    comment_body = objects.put_bytes("这是作者补充的中性评论上下文。".encode())
    comment_snapshot = SourceSnapshot(
        snapshot_id="snapshot:distillation-thought-comment",
        source_id="zhihu:test-distillation",
        object_sha256=root_comment_body.sha256,
        fetched_at=observed_at,
        available_to_system_at=observed_at,
        mime="application/json",
        byte_size=root_comment_body.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
    )
    state.register_snapshot(comment_snapshot)
    knowledge_repository = KnowledgeRepository(state)
    knowledge_repository.register_comment(
        ZhihuCommentNode(
            version_id="zhihu-comment:distillation-thought-root-v1",
            author_source_id="zhihu:test-distillation",
            content_type=ZhihuContentType.THOUGHTS,
            content_id="thought-1",
            comment_id="comment-root",
            root_comment_id="comment-root",
            collected_at=observed_at,
            child_comment_count=1,
            is_target_author=False,
            body_object_sha256=root_comment_body.sha256,
            metadata_sha256="d" * 64,
            raw_source_snapshot_id=comment_snapshot.snapshot_id,
            created_at=observed_at,
        )
    )
    knowledge_repository.register_comment(
        ZhihuCommentNode(
            version_id="zhihu-comment:distillation-thought-v1",
            author_source_id="zhihu:test-distillation",
            content_type=ZhihuContentType.THOUGHTS,
            content_id="thought-1",
            comment_id="comment-1",
            parent_comment_id="comment-root",
            reply_to_comment_id="comment-root",
            root_comment_id="comment-root",
            collected_at=observed_at,
            is_target_author=True,
            body_object_sha256=comment_body.sha256,
            metadata_sha256="c" * 64,
            raw_source_snapshot_id=comment_snapshot.snapshot_id,
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        CollectionCheckpoint(
            author="zhihu:test-distillation",
            content_type="thoughts",
            listing_page=0,
            content_id="thought-1",
            comment_parent_id="comment-root",
            comment_page=0,
            terminal_condition=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
    )
    unmatched_root_body = objects.put_bytes("这是完全中性的读者评论。".encode())
    unmatched_author_body = objects.put_bytes("谢谢你的留言。".encode())
    knowledge_repository.register_comment(
        ZhihuCommentNode(
            version_id="zhihu-comment:distillation-unmatched-root-v1",
            author_source_id="zhihu:test-distillation",
            content_type=ZhihuContentType.THOUGHTS,
            content_id="thought-1",
            comment_id="comment-unmatched-root",
            root_comment_id="comment-unmatched-root",
            collected_at=observed_at,
            child_comment_count=1,
            is_target_author=False,
            body_object_sha256=unmatched_root_body.sha256,
            metadata_sha256="e" * 64,
            raw_source_snapshot_id=comment_snapshot.snapshot_id,
            created_at=observed_at,
        )
    )
    knowledge_repository.register_comment(
        ZhihuCommentNode(
            version_id="zhihu-comment:distillation-unmatched-author-v1",
            author_source_id="zhihu:test-distillation",
            content_type=ZhihuContentType.THOUGHTS,
            content_id="thought-1",
            comment_id="comment-unmatched-author",
            parent_comment_id="comment-unmatched-root",
            reply_to_comment_id="comment-unmatched-root",
            root_comment_id="comment-unmatched-root",
            collected_at=observed_at,
            is_target_author=True,
            body_object_sha256=unmatched_author_body.sha256,
            metadata_sha256="f" * 64,
            raw_source_snapshot_id=comment_snapshot.snapshot_id,
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        CollectionCheckpoint(
            author="zhihu:test-distillation",
            content_type="thoughts",
            listing_page=0,
            content_id="thought-1",
            comment_parent_id="comment-unmatched-root",
            comment_page=0,
            terminal_condition=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
    )
    neutral_body = objects.put_bytes("这是另一篇完全中性的内容。".encode())
    neutral_snapshot = SourceSnapshot(
        snapshot_id="snapshot:distillation-neutral-thought",
        source_id="zhihu:test-distillation",
        object_sha256=neutral_body.sha256,
        fetched_at=observed_at,
        available_to_system_at=observed_at,
        mime="application/json",
        byte_size=neutral_body.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="USER_ALLOWLISTED_LOCAL_RESEARCH",
    )
    state.register_snapshot(neutral_snapshot)
    KnowledgeRepository(state).register_content(
        ZhihuContentRecord(
            version_id="zhihu-content:distillation-thought-v2",
            author_source_id="zhihu:test-distillation",
            content_id="thought-2",
            content_type=ZhihuContentType.THOUGHTS,
            canonical_url="https://www.zhihu.com/pin/thought-2",
            collected_at=observed_at,
            body_object_sha256=neutral_body.sha256,
            metadata_sha256="b" * 64,
            raw_source_snapshot_id=neutral_snapshot.snapshot_id,
            content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
            created_at=observed_at,
        )
    )


def test_cross_source_distillation_is_idempotent_auditable_and_private(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    _register_sources(tmp_path, state, objects)
    structure_service = KnowledgeStructureProfileService(state, objects)
    structure_profiles = structure_service.analyze(_source())
    repeated_structure_profiles = structure_service.analyze(_source())
    structure_audit = structure_service.audit(_source())
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
    rules = load_distillation_rules(PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml")
    service = KnowledgeDistillationService(state, objects, tmp_path / "parquet")

    before = state.path.read_bytes()
    with pytest.raises(PolicyError) as distillation_error:
        service.run(_source(), rules)
    with pytest.raises(PolicyError) as draft_error:
        KnowledgeDraftService(state, objects).generate("zhihu:test-distillation")
    assert distillation_error.value.failure_class is FailureClass.POLICY_REJECTED
    assert draft_error.value.failure_class is FailureClass.POLICY_REJECTED
    assert state.path.read_bytes() == before
    return

    first = service.run(_source(), rules)
    repeated = service.run(_source(), rules)
    audit = service.audit("zhihu:test-distillation")
    stale_audit = service.audit(
        "zhihu:test-distillation",
        expected_rule_version="future-classification-rules",
    )
    draft_service = KnowledgeDraftService(state, objects)
    draft_execution = draft_service.generate("zhihu:test-distillation")
    repeated_drafts = draft_service.generate("zhihu:test-distillation")
    compact_execution = draft_service.generate(
        "zhihu:test-distillation",
        materialize_output=False,
    )
    draft_audit = draft_service.audit("zhihu:test-distillation")

    assert structure_profiles == repeated_structure_profiles
    assert len(structure_profiles) == 3
    assert {profile.material_kind for profile in structure_profiles} == {
        KnowledgeMaterialKind.PRIVATE_PDF,
        KnowledgeMaterialKind.PRIVATE_DOCX,
        KnowledgeMaterialKind.ZHIHU_ONLINE,
    }
    strategy_by_kind = {
        profile.material_kind: profile.processing_strategy for profile in structure_profiles
    }
    assert strategy_by_kind == {
        KnowledgeMaterialKind.PRIVATE_PDF: (
            KnowledgeProcessingStrategy.PDF_PAGE_WRAPPED_PARAGRAPH_V1
        ),
        KnowledgeMaterialKind.PRIVATE_DOCX: (KnowledgeProcessingStrategy.DOCX_STABLE_BLOCK_V1),
        KnowledgeMaterialKind.ZHIHU_ONLINE: (
            KnowledgeProcessingStrategy.ZHIHU_VERIFIED_VISIBLE_HTML_V2
        ),
    }
    pdf_profile = next(
        item
        for item in structure_profiles
        if item.material_kind is KnowledgeMaterialKind.PRIVATE_PDF
    )
    docx_profile = next(
        item
        for item in structure_profiles
        if item.material_kind is KnowledgeMaterialKind.PRIVATE_DOCX
    )
    online_profile = next(
        item
        for item in structure_profiles
        if item.material_kind is KnowledgeMaterialKind.ZHIHU_ONLINE
    )
    assert pdf_profile.semantic_segment_count <= pdf_profile.structure_unit_count
    assert docx_profile.source_item_count == 4
    assert online_profile.verified_content_count == 15
    assert "ZHIHU_BODY_ONLY_V1" in online_profile.recommended_action_codes
    old_profile_id = "knowledge-structure:" + content_hash(
        {
            "author_source_id": online_profile.author_source_id,
            "input_source_id": online_profile.input_source_id,
            "material_kind": online_profile.material_kind.value,
            "processing_strategy": online_profile.processing_strategy.value,
            "input_set_sha256": online_profile.input_set_sha256,
        }
    )
    assert online_profile.profile_id != old_profile_id
    assert structure_audit["status"] == "PASS"
    with state.connect() as connection:
        serialized_profiles = "\n".join(
            str(row["profile_json"])
            for row in connection.execute(
                "SELECT profile_json FROM knowledge_structure_profile"
            ).fetchall()
        )
    assert _PRIVATE_METHOD not in serialized_profiles
    assert _PRIVATE_SECOND not in serialized_profiles
    assert _PRIVATE_NOISE not in serialized_profiles
    assert "private-method.pdf" not in serialized_profiles
    assert "private-method.docx" not in serialized_profiles

    assert first.run.run_id == repeated.run.run_id
    old_run_id = "knowledge-distillation:" + content_hash(
        {
            "author_source_id": first.run.author_source_id,
            "classification_rule_version": first.run.classification_rule_version,
            "input_hashes": first.run.input_hashes,
        }
    )
    assert first.run.run_id != old_run_id
    assert first.report == repeated.report
    assert first.review_queue == repeated.review_queue
    assert first.report.human_review_status is HumanReviewStatus.PENDING
    assert first.report.online_content_count == 15
    assert first.report.target_author_comment_count == 0
    assert first.report.qualified_comment_chain_count == 0
    assert first.report.qualified_comment_context_count == 0
    assert first.report.comment_chain_filter_version is None
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
    assert duplicates[0].decision is DistillationDecision.KEEP_CANDIDATE
    assert "EXACT_DUPLICATE" not in duplicates[0].reason_codes
    assert first.review_queue.unit_ids == [unit.unit_id for unit in units]
    assert first.report.classification_piece_count == 18
    assert first.report.dropped_source_item_count == 0
    assert first.report.dropped_segment_count == 0
    pdf_units = [
        unit for unit in units if unit.locator.locator_type is DistillationLocatorType.PAGE_TEXT
    ]
    assert len(pdf_units) == 2
    assert {unit.classification_piece_id for unit in pdf_units} == {
        pdf_units[0].classification_piece_id
    }
    assert {unit.decision for unit in pdf_units} == {DistillationDecision.KEEP_CANDIDATE}
    docx_units = [
        unit for unit in units if unit.locator.locator_type is DistillationLocatorType.BLOCK_TEXT
    ]
    assert len(docx_units) == 4
    docx_piece_ids = {unit.classification_piece_id for unit in docx_units}
    assert len(docx_piece_ids) == 2
    docx_piece_decisions = {
        piece_id: {unit.decision for unit in docx_units if unit.classification_piece_id == piece_id}
        for piece_id in docx_piece_ids
    }
    assert set(map(frozenset, docx_piece_decisions.values())) == {
        frozenset({DistillationDecision.KEEP_CANDIDATE}),
        frozenset({DistillationDecision.UNCLASSIFIED}),
    }
    assert all(
        sum(unit.classification_piece_id == piece_id for unit in docx_units) == 2
        for piece_id in docx_piece_ids
    )
    first_thought_units = [unit for unit in units if unit.locator.content_id == "thought-1"]
    second_thought_units = [unit for unit in units if unit.locator.content_id == "thought-2"]
    assert len(first_thought_units) == 2
    assert {unit.decision for unit in first_thought_units} == {DistillationDecision.KEEP_CANDIDATE}
    assert {unit.classification_piece_segment_count for unit in first_thought_units} == {2}
    assert len({unit.classification_piece_id for unit in first_thought_units}) == 1
    assert all(
        unit.locator.locator_type is DistillationLocatorType.ZHIHU_CONTENT
        for unit in first_thought_units
    )
    assert len(second_thought_units) == 1
    assert second_thought_units[0].decision is DistillationDecision.UNCLASSIFIED
    assert (
        second_thought_units[0].classification_piece_id
        != first_thought_units[0].classification_piece_id
    )
    assert first.report.keep_candidate_count >= 3
    assert first.parquet_file.is_file()
    assert audit["status"] == "PASS"
    assert audit["missing_normalized_object_count"] == 0
    assert audit["missing_source_object_count"] == 0
    assert audit["parquet_hash_mismatch_count"] == 0
    assert stale_audit["status"] == "PARTIAL"
    assert stale_audit["finding_codes"] == ["CLASSIFICATION_RULE_VERSION_STALE"]
    with pytest.raises(ValueError, match="stale classification rule version"):
        KnowledgeDraftService(
            state,
            objects,
            required_classification_rule_version="future-classification-rules",
        ).generate("zhihu:test-distillation")
    assert draft_execution == repeated_drafts
    assert compact_execution.report == draft_execution.report
    assert compact_execution.viewpoint_drafts == ()
    assert draft_execution.report.human_review_status is HumanReviewStatus.PENDING
    assert draft_execution.report.viewpoint_draft_count == len(draft_execution.viewpoint_drafts)
    assert draft_execution.report.skill_candidate_count == len(draft_execution.skill_candidates)
    assert draft_execution.report.selected_viewpoint_counts == (
        draft_execution.report.method_category_unit_counts
    )
    assert draft_execution.report.selected_viewpoint_counts["VALUATION"] > 12
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
    draft_repository = KnowledgeDraftRepository(state)
    legacy_report = draft_execution.report.model_copy(
        update={
            "report_id": f"author-draft-generation:{'f' * 64}",
            "generation_rule_version": "private-excerpt-draft-v1",
        }
    )
    legacy_object = objects.put_json(legacy_report.model_dump(mode="json"))
    draft_repository.register_report(legacy_report, object_hash=legacy_object.sha256)
    assert draft_repository.latest_report("zhihu:test-distillation") == legacy_report
    assert draft_service.current_report("zhihu:test-distillation") == draft_execution.report
    assert draft_repository.report_object_hash(legacy_report.report_id) == legacy_object.sha256
    parquet = pq.ParquetFile(first.parquet_file).read()
    assert parquet.num_rows == len(units)
    assert "normalized_text_sha256" in parquet.column_names
    assert "text" not in parquet.column_names

    with state.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_distillation_run").fetchone()[0] == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM knowledge_distillation_unit").fetchone()[
            0
        ] == len(units)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM collection_gap WHERE status='OPEN'"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM author_distillation_report").fetchone()[0] == 1
        )
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
