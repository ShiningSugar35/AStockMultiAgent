from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.schemas import (
    BookMethodCategory,
    BookVisualCoverageReport,
    BookVisualCoverageStatus,
    BookVisualQualityStatus,
    ChartUnitType,
    KeywordScreenResult,
    ParagraphLocator,
    ParagraphMergeAction,
    ParagraphUnit,
    ParagraphUnitKind,
    RhetoricalRole,
    SemanticPacketParagraph,
)

NOW = datetime(2026, 7, 26, tzinfo=UTC)
HASH = "a" * 64


def _visual_paragraph_payload() -> dict[str, object]:
    return {
        "paragraph_id": "paragraph:visual",
        "run_id": "semantic:run",
        "author_source_id": "author:test",
        "content_type": "PRIVATE_BOOK_PAGE",
        "content_id": "book:page:1",
        "content_version_id": HASH,
        "ordinal": 2,
        "locator": {
            "locator_type": "BOOK_PDF_IMAGE_PLACEMENT",
            "source_snapshot_id": "snapshot:test",
            "source_object_sha256": HASH,
            "content_id": "book:page:1",
            "page_number": 1,
            "bbox": [20.0, 100.0, 300.0, 220.0],
            "char_start": 0,
            "char_end": 12,
            "created_at": NOW,
        },
        "text_object_sha256": HASH,
        "normalized_char_count": 12,
        "primary_role": "EVIDENCE",
        "rhetorical_roles": ["EVIDENCE"],
        "role_scores": {"EVIDENCE": 1.0},
        "standalone_distillable": False,
        "context_value": 1.0,
        "depends_on_previous": True,
        "depends_on_next": True,
        "merge_action": "MERGE_WITH_BOTH",
        "topic_relevance": 0.8,
        "methodological_completeness": 0.3,
        "matched_keyword_terms": ["profit"],
        "reason_codes": ["VISUAL_EVIDENCE_REQUIRES_BOTH_SIDES"],
        "role_rule_version": "roles-v1",
        "paragraph_kind": "VISUAL_EVIDENCE",
        "visual_evidence_ids": ["image:e1"],
        "visual_chart_unit_ids": ["chart:c1"],
        "visual_quality_status": "SUCCESS",
        "visual_reason_codes": [],
        "created_at": NOW,
    }


def test_visual_paragraph_is_always_context_bound_and_never_standalone() -> None:
    paragraph = ParagraphUnit.model_validate(_visual_paragraph_payload())
    assert paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
    assert paragraph.merge_action is ParagraphMergeAction.MERGE_WITH_BOTH
    assert paragraph.rhetorical_roles == [RhetoricalRole.EVIDENCE]

    invalid = _visual_paragraph_payload()
    invalid["standalone_distillable"] = True
    invalid["merge_action"] = "KEEP_AS_ARGUMENT"
    with pytest.raises(ValueError, match="visual paragraphs"):
        ParagraphUnit.model_validate(invalid)


def test_old_locator_and_text_paragraph_remain_readable_without_visual_fields() -> None:
    locator = ParagraphLocator.model_validate(
        {
            "locator_type": "ZHIHU_VISIBLE_BLOCK",
            "source_snapshot_id": "snapshot:legacy",
            "source_object_sha256": HASH,
            "content_id": "answer:legacy",
            "dom_path": "html/body/p[1]",
            "char_start": 0,
            "char_end": 10,
            "created_at": NOW,
        }
    )
    assert locator.bbox is None

    payload = _visual_paragraph_payload()
    payload.update(
        {
            "paragraph_id": "paragraph:legacy",
            "locator": locator.model_dump(mode="json"),
            "primary_role": "BACKGROUND",
            "rhetorical_roles": ["BACKGROUND"],
            "role_scores": {"BACKGROUND": 1.0},
            "depends_on_previous": False,
            "depends_on_next": True,
            "merge_action": "MERGE_WITH_FOLLOWING",
        }
    )
    for field in (
        "paragraph_kind",
        "visual_evidence_ids",
        "visual_chart_unit_ids",
        "visual_quality_status",
        "visual_reason_codes",
    ):
        payload.pop(field)
    paragraph = ParagraphUnit.model_validate(payload)
    assert paragraph.paragraph_kind is ParagraphUnitKind.TEXT
    assert paragraph.visual_evidence_ids == []


def test_packet_paragraph_preserves_visual_locator_quality_and_lineage() -> None:
    paragraph = ParagraphUnit.model_validate(_visual_paragraph_payload())
    packet = SemanticPacketParagraph(
        paragraph_id=paragraph.paragraph_id,
        ordinal=paragraph.ordinal,
        text="profit chart",
        text_object_sha256=paragraph.text_object_sha256,
        primary_role=paragraph.primary_role,
        rhetorical_roles=paragraph.rhetorical_roles,
        standalone_distillable=paragraph.standalone_distillable,
        depends_on_previous=paragraph.depends_on_previous,
        depends_on_next=paragraph.depends_on_next,
        merge_action=paragraph.merge_action,
        locator=paragraph.locator,
        paragraph_kind=paragraph.paragraph_kind,
        visual_evidence_ids=paragraph.visual_evidence_ids,
        visual_chart_unit_ids=paragraph.visual_chart_unit_ids,
        visual_quality_status=paragraph.visual_quality_status,
        visual_reason_codes=paragraph.visual_reason_codes,
        created_at=NOW,
    )
    assert packet.locator is not None and packet.locator.bbox is not None
    assert packet.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
    assert packet.visual_evidence_ids == ["image:e1"]
    assert packet.visual_quality_status == "SUCCESS"


def test_no_keyword_candidate_exception_is_scoped_to_book_visual_lineage() -> None:
    payload = {
        "screen_id": "screen:test",
        "run_id": "run:test",
        "item_id": "item:test",
        "decision": "CANDIDATE",
        "matched_terms_by_category": {
            category.value: [] for category in BookMethodCategory
        },
        "matched_paragraph_ids": [],
        "keyword_rule_version": "keywords-v1",
        "result_object_sha256": HASH,
        "created_at": NOW,
    }
    with pytest.raises(ValueError, match="matched term"):
        KeywordScreenResult.model_validate(payload)
    screen = KeywordScreenResult.model_validate(
        {
            **payload,
            "candidate_reason_codes": ["BOOK_VISUAL_ARGUMENT_LINEAGE"],
        }
    )
    assert screen.candidate_reason_codes == ["BOOK_VISUAL_ARGUMENT_LINEAGE"]


def test_coverage_and_quality_statuses_are_independent_and_counts_partition() -> None:
    report = BookVisualCoverageReport(
        report_id="report:test",
        run_id="run:test",
        coverage_status=BookVisualCoverageStatus.COMPLETE,
        quality_status=BookVisualQualityStatus.REVIEW_REQUIRED,
        source_pages=2,
        image_pages=1,
        image_placements=2,
        processed_placements=2,
        ocr_failed=0,
        low_confidence=1,
        no_text=0,
        duplicate=0,
        classification_counts={
            unit_type: (2 if unit_type is ChartUnitType.CHART else 0)
            for unit_type in ChartUnitType
        },
        affected_argument_unit_count=1,
        image_only_ready_candidate_count=0,
        created_at=NOW,
    )
    assert report.coverage_status is BookVisualCoverageStatus.COMPLETE
    assert report.quality_status is BookVisualQualityStatus.REVIEW_REQUIRED

    invalid = report.model_dump(mode="json")
    invalid["classification_counts"] = {
        unit_type.value: 0 for unit_type in ChartUnitType
    }
    with pytest.raises(ValueError, match="partition"):
        BookVisualCoverageReport.model_validate(invalid)
