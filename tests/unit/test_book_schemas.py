from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from astock.schemas import (
    BOOK_DOWNWEIGHT_CLASSES,
    BOOK_KEEP_CLASSES,
    BookApprovalStatus,
    BookCleaningReport,
    BookEvaluationStatus,
    BookMethodCategory,
    BookMethodCoverageMetric,
    BookMethodCoverageReport,
    BookProcessingStatus,
    BookSkillCandidate,
    BookSkillTarget,
    BookViewpointCard,
    CoverageStatus,
    HumanReviewDecision,
    HumanReviewStatus,
    HumanReviewVerdict,
    PrivateSkillCandidateDraft,
    PrivateViewpointDraft,
    ViewpointDraftDerivation,
)


def _cleaning_report(**updates) -> BookCleaningReport:
    values = {
        "report_id": "cleaning:fixture",
        "manifest_id": "manifest:fixture",
        "input_parse_report_ids": [],
        "cleaning_pipeline_version": "not-run-v1",
        "processing_status": BookProcessingStatus.NOT_RUN,
        "human_review_status": HumanReviewStatus.NOT_STARTED,
        "downweight_classes": list(BOOK_DOWNWEIGHT_CLASSES),
        "keep_classes": list(BOOK_KEEP_CLASSES),
    }
    values.update(updates)
    return BookCleaningReport.model_validate(values)


def test_cleaning_report_can_be_honestly_not_run_but_cannot_fake_completion() -> None:
    report = _cleaning_report()
    assert report.original_char_count is None
    assert report.raw_content_preserved
    assert report.cleaning_reconstructable
    with pytest.raises(ValidationError, match="every mandatory metric"):
        _cleaning_report(processing_status=BookProcessingStatus.COMPLETE)
    with pytest.raises(ValidationError, match="never delete"):
        _cleaning_report(raw_content_preserved=False)


def _coverage(status: CoverageStatus) -> BookMethodCoverageMetric:
    return BookMethodCoverageMetric(paragraph_count=None, evidence_count=0, status=status)


def test_author_silent_requires_full_source_and_human_approval() -> None:
    insufficient = _coverage(CoverageStatus.INSUFFICIENT_SOURCE)
    report = BookMethodCoverageReport(
        report_id="coverage:fixture",
        manifest_id="manifest:fixture",
        processing_status=BookProcessingStatus.SAMPLE_ONLY,
        human_review_status=HumanReviewStatus.NOT_STARTED,
        selection=insufficient,
        entry=insufficient,
        holding=insufficient,
        add=insufficient,
        trim=insufficient,
        exit=insufficient,
        risk=insufficient,
        review=insufficient,
    )
    assert report.entry.status is CoverageStatus.INSUFFICIENT_SOURCE
    with pytest.raises(ValidationError, match="AUTHOR_SILENT"):
        BookMethodCoverageReport.model_validate(
            {
                **report.model_dump(mode="python"),
                "entry": _coverage(CoverageStatus.AUTHOR_SILENT),
            }
        )


def test_human_approval_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="approval requires evidence"):
        HumanReviewDecision(
            decision_id="review:fixture",
            artifact_type="BookSkillCandidate",
            artifact_id="candidate:fixture",
            verdict=HumanReviewVerdict.APPROVE,
            reviewer_id="human:test",
            reviewed_at=datetime(2026, 7, 13, tzinfo=UTC),
            rationale="fixture",
            evidence_ids=[],
        )


def test_viewpoint_and_skill_rules_require_page_and_excerpt_lineage() -> None:
    with pytest.raises(ValidationError):
        BookViewpointCard(
            card_id="card:fixture",
            manifest_id="manifest:fixture",
            proposition="A testable method statement",
            method_category=BookMethodCategory.STOCK_SELECTION,
            evidence_ids=[],
            source_page_numbers=[],
            source_excerpt_hashes=[],
            counterevidence=[],
            failure_conditions=[],
            human_review_status=HumanReviewStatus.PENDING,
        )
    with pytest.raises(ValidationError, match="excerpt hashes"):
        BookSkillCandidate(
            candidate_id="candidate:fixture",
            manifest_id="manifest:fixture",
            target_skill=BookSkillTarget.CANDIDATE_SELECTION,
            method_category=BookMethodCategory.STOCK_SELECTION,
            rule_json={"condition": "fixture"},
            evidence_ids=["evidence:fixture"],
            source_page_numbers=[1],
            source_excerpt_hashes=["not-a-hash"],
            evaluation_status=BookEvaluationStatus.NOT_RUN,
            evaluation_results={},
            approval_status=BookApprovalStatus.PENDING,
        )


def test_automatic_private_drafts_cannot_self_approve_or_claim_evaluation() -> None:
    with pytest.raises(ValidationError, match="explicit review decision"):
        PrivateViewpointDraft(
            draft_id="private-viewpoint:fixture",
            run_id="distillation:fixture",
            author_source_id="zhihu:fixture",
            method_category=BookMethodCategory.STOCK_SELECTION,
            source_unit_ids=["unit:fixture"],
            source_excerpt_hashes=["a" * 64],
            payload_object_sha256="b" * 64,
            proposition_derivation=(
                ViewpointDraftDerivation.SOURCE_EXCERPT_NOT_SYNTHESIZED
            ),
            generation_rule_version="private-excerpt-draft-v1",
            human_review_status=HumanReviewStatus.APPROVED,
            quality_gaps=["PROPOSITION_NOT_SYNTHESIZED"],
        )
    with pytest.raises(ValidationError, match="cannot claim an evaluation result"):
        PrivateSkillCandidateDraft(
            candidate_id="private-skill:fixture",
            run_id="distillation:fixture",
            author_source_id="zhihu:fixture",
            target_skill=BookSkillTarget.CANDIDATE_SELECTION,
            method_category=BookMethodCategory.STOCK_SELECTION,
            source_viewpoint_draft_ids=["private-viewpoint:fixture"],
            source_unit_ids=["unit:fixture"],
            payload_object_sha256="c" * 64,
            generation_rule_version="private-excerpt-draft-v1",
            evaluation_status=BookEvaluationStatus.PASSED,
            approval_status=BookApprovalStatus.APPROVED,
            quality_gaps=["HUMAN_REVIEW_REQUIRED"],
        )
