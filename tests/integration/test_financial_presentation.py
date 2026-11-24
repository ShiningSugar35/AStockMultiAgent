"""Recorded official PDF as evidence for shadow semantic proposal; no network or trading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from astock.core.hashing import sha256_bytes
from astock.documents import DocumentPageRepository
from astock.financial_sources.presentation import FinancialPresentationService
from astock.schemas import FinancialPeriodType, FinancialStatementScope, Market
from astock.schemas.financial_presentation import (
    FinancialPageAnchor,
    FinancialPresentationProposal,
    PresentationReviewStatus,
    PresentationSourceMode,
)
from tests.integration.test_financial_sources import (
    PERIOD_END,
    _capture_existing_annual,
    _service,
)


def test_recorded_issuer_semantics_preserve_authority_and_negative_anchors(
    tmp_path, monkeypatch,
) -> None:
    sources = _service(tmp_path)
    _capture_existing_annual(sources)
    for provider in sources.providers.values():
        monkeypatch.setattr(
            provider, "fetch",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline only")),
        )
    sync = sources.sync(
        "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL,
        live=True, allow_network=False,
    )
    assert sync.status.value == "CERTIFIED"
    engine = FinancialPresentationService(sources)
    as_of = datetime.now(UTC) + timedelta(seconds=5)
    text_context = engine.context(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=as_of,
    )
    assert text_context["release_id"] == sync.release_id
    assert text_context["formal_financial_authority"] is False
    assert text_context["available_page_numbers"]
    assert text_context["page_excerpts"]

    bound = engine._bound("000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of)
    page, text, position = None, None, None
    for candidate in bound.report.pages:
        raw = sources.objects.get_bytes(candidate.text_object_sha256).decode("utf-8")
        found = raw.find("合并资产负债表")
        if found >= 0:
            page, text, position = candidate, raw, found
            break
    assert page is not None and text is not None and position is not None
    anchor = FinancialPageAnchor(
        page_id=page.page_id, page_number=page.page_number,
        text_sha256=page.text_sha256, start=position,
        end=position + len("合并资产负债表"), quote="合并资产负债表",
    )
    base = {
        "company_id": "000001", "instrument_id": "XSHE:000001",
        "release_id": text_context["release_id"],
        "official_document_id": text_context["official_document_id"],
        "official_snapshot_id": text_context["official_snapshot_id"],
        "official_source_hash": text_context["official_source_hash"],
        "period_end": PERIOD_END, "period_type": FinancialPeriodType.ANNUAL,
        "as_of": as_of, "model_id": "chat-host-llm-test",
        "prompt_version": "wp78-structured-v1",
        "statement_scope": "CONSOLIDATED",
        "scope_evidence": [anchor.model_dump(mode="json")],
    }
    good = FinancialPresentationProposal.model_validate(base)
    report = engine.review(good, persist=True)
    assert report.status is PresentationReviewStatus.EVIDENCE_BOUND
    assert report.formal_financial_authority is False
    assert not report.active_parser_changed
    assert engine.review(good, persist=True).proposal_hash == report.proposal_hash
    preview = engine.rule_preview(good)
    assert preview.approval_status == "SHADOW_ONLY_NOT_ADMITTED"
    assert preview.formal_financial_authority is False
    assert {"balance_sheet_identity", "cash_flow_identity"}.issubset(
        preview.core_rule_ids_preserved
    )
    assert "ISSUER_ACTUAL_ADOPTION_NOT_CONFIRMED" in preview.unresolved_reasons
    with sources.state.connect() as connection:
        rows = connection.execute(
            "SELECT type FROM artifact_registry WHERE type IN "
            "('FinancialPresentationProposal','FinancialPresentationReview')"
        ).fetchall()
    assert len(rows) == 2, "identical shadow review must be idempotent"
    assert {row["type"] for row in rows} == {
        "FinancialPresentationProposal", "FinancialPresentationReview",
    }

    # Field aliases proposed by the LLM must be re-parsed in the existing
    # FinancialPdfCertifier and checked against already certified facts.
    assets = next(
        item for item in sources.mappings if item.field_code.value == "TOTAL_ASSETS"
    )
    official_row = f"{assets.official_label} 1000"
    row_start = text.find(official_row)
    assert row_start >= 0
    row_anchor = FinancialPageAnchor(
        page_id=page.page_id, page_number=page.page_number,
        text_sha256=page.text_sha256, start=row_start,
        end=row_start + len(official_row), quote=official_row,
    )
    proposed_line = {
        "field_code": "TOTAL_ASSETS", "statement_type": "BALANCE_SHEET",
        "statement_scope": "CONSOLIDATED", "original_label": assets.official_label,
        "numeric_text": "1000", "unit": "TEN_THOUSAND_CNY",
        "evidence": row_anchor.model_dump(mode="json"),
    }
    line_ok = engine.review(FinancialPresentationProposal.model_validate({
        **base, "lines": [proposed_line],
    }))
    assert line_ok.status is PresentationReviewStatus.EVIDENCE_BOUND
    assert line_ok.line_reviews[0].exact_official_match

    # A plausible numerical substring in the same real row is not the actual cell.
    line_false = engine.review(FinancialPresentationProposal.model_validate({
        **base, "lines": [{**proposed_line, "numeric_text": "0"}],
    }))
    assert line_false.status is PresentationReviewStatus.REJECTED
    assert "OFFICIAL_VALUE_OR_UNIT_MISMATCH" in line_false.line_reviews[0].reason_codes
    wrong_unit = engine.review(FinancialPresentationProposal.model_validate({
        **base, "lines": [{**proposed_line, "unit": "CNY"}],
    }))
    assert wrong_unit.status is PresentationReviewStatus.REJECTED

    # A quote on an unrelated page cannot be laundered by a syntactically valid JSON.
    wrong_quote = anchor.model_copy(update={"quote": "伪并资产负债表"})
    wrong = FinancialPresentationProposal.model_validate({
        **base, "scope_evidence": [wrong_quote.model_dump(mode="json")]
    })
    denied = engine.review(wrong)
    assert denied.status is PresentationReviewStatus.REJECTED
    assert "ANCHOR_QUOTE_OR_OFFSET_MISMATCH" in denied.reason_codes

    # Matching page text proves its existence, NOT whether the company adopted a new standard.
    claimed = FinancialPresentationProposal.model_validate({
        **base, "framework": "PRC_ASBE",
        "standard_version_candidate": "CAS30-2026",
        "adoption": "CONFIRMED",
        "adoption_evidence": [anchor.model_dump(mode="json")],
    })
    unresolved = engine.review(claimed)
    assert unresolved.status is PresentationReviewStatus.INCONCLUSIVE
    assert "ADOPTION_SEMANTICS_REQUIRE_INDEPENDENT_REVIEW" in unresolved.reason_codes

    mismatched_release = FinancialPresentationProposal.model_validate({
        **base, "release_id": "f" * 64,
    })
    assert engine.review(mismatched_release).status is PresentationReviewStatus.REJECTED

    # PIT must not peek at a later certified source release.
    assert bound.verified is not None
    old_asof = bound.verified.manifest.available_to_system_at - timedelta(microseconds=1)
    from pytest import raises

    with raises(ValueError, match="no verifiable official"):
        engine.context("000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=old_asof)

    # Two different parser texts for the *same official PDF page* may not be
    # silently ordered by parser version or accepted as proof of any financial fact.
    altered_text = text + "\\nADVERSARIAL_REPARSE_ROW\\n"
    altered_bytes = altered_text.encode("utf-8")
    altered = sources.objects.put_bytes(altered_bytes)
    duplicate = page.model_copy(update={
        "page_id": f"synthetic-conflict:{page.page_id}",
        "parser_version": "wp78-conflicting-parser-v99",
        "text_object_sha256": altered.sha256,
        "text_sha256": sha256_bytes(altered_bytes),
        "text_char_count": len(altered_text),
    })
    DocumentPageRepository(sources.state).register_page(duplicate)
    with raises(ValueError, match="conflicting parser versions"):
        engine.context("000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=as_of)


def test_semantic_context_explicit_source_pages_stay_bounded(tmp_path, monkeypatch) -> None:
    sources = _service(tmp_path)
    _capture_existing_annual(sources)
    for provider in sources.providers.values():
        monkeypatch.setattr(
            provider, "fetch",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline")),
        )
    sources.sync(
        "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL,
        live=True, allow_network=False,
    )
    service = FinancialPresentationService(sources)
    as_of = datetime.now(UTC) + timedelta(seconds=5)
    context = service.context(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=as_of, page_numbers=[1], max_pages=2,
    )
    assert [page["page_number"] for page in context["page_excerpts"]] == [1]
    from pytest import raises

    with raises(ValueError, match="outside official source"):
        service.context(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=as_of, page_numbers=[999999], max_pages=2,
        )



def test_admitted_official_pdf_before_certification_is_safe_semantic_recovery(
    tmp_path, monkeypatch,
) -> None:
    """A new table layout may break certification; the LLM must still see official text."""
    import pytest
    from pydantic import ValidationError

    sources = _service(tmp_path)
    _capture_existing_annual(sources)
    for provider in sources.providers.values():
        monkeypatch.setattr(
            provider, "fetch",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline only")),
        )
    engine = FinancialPresentationService(sources)
    as_of = datetime.now(UTC) + timedelta(seconds=5)
    assert sources.read_certified_release(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=as_of,
    ) is None
    with pytest.raises(ValueError, match="no verifiable official certified"):
        engine.context("000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=as_of)

    context = engine.context(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=as_of, market=Market.XSHE, allow_uncertified_capture=True,
    )
    assert context["source_mode"] == "OFFICIAL_CAPTURE_ONLY"
    assert context["release_id"] is None
    assert not context["financial_facts_certified"]
    assert context["available_page_numbers"]
    assert context["formal_financial_authority"] is False

    report = engine._captured_bound("000001", Market.XSHE, PERIOD_END,
                                     FinancialPeriodType.ANNUAL, as_of)
    exact = None
    for page in report.report.pages:
        content = sources.objects.get_bytes(page.text_object_sha256).decode("utf-8")
        pos = content.find("合并资产负债表")
        if pos >= 0:
            exact = FinancialPageAnchor(
                page_id=page.page_id, page_number=page.page_number,
                text_sha256=page.text_sha256, start=pos,
                end=pos + len("合并资产负债表"), quote="合并资产负债表",
            )
            break
    assert exact is not None
    proposal = FinancialPresentationProposal(
        company_id="000001", instrument_id="XSHE:000001",
        source_mode=PresentationSourceMode.OFFICIAL_CAPTURE_ONLY, release_id=None,
        official_document_id=context["official_document_id"],
        official_snapshot_id=context["official_snapshot_id"],
        official_source_hash=context["official_source_hash"],
        period_end=PERIOD_END, period_type=FinancialPeriodType.ANNUAL,
        as_of=as_of, model_id="host-llm", prompt_version="capture-shadow-v1",
        statement_scope=FinancialStatementScope.CONSOLIDATED, scope_evidence=[exact],
    )
    review = engine.review(proposal, persist=True)
    assert review.release_id is None
    assert review.status is PresentationReviewStatus.INCONCLUSIVE
    assert "OFFICIAL_CAPTURE_HAS_NO_CERTIFIED_FINANCIAL_FACTS" in review.reason_codes
    preview = engine.rule_preview(proposal)
    assert preview.release_id is None and not preview.formal_financial_authority
    assert "OFFICIAL_CAPTURE_HAS_NO_CERTIFIED_FINANCIAL_FACTS" in preview.unresolved_reasons
    assert {"balance_sheet_identity", "cash_flow_identity"}.issubset(
        preview.core_rule_ids_preserved
    )
    with pytest.raises(ValidationError, match="uncertified capture cannot claim"):
        FinancialPresentationProposal.model_validate({
            **proposal.model_dump(mode="json"), "release_id": "a" * 64,
        })
    with pytest.raises(ValueError):
        engine.context(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=as_of, market=Market.XSHG, allow_uncertified_capture=True,
        )
    with pytest.raises(ValueError, match="official capture identity/PIT"):
        engine.context(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=report.report.snapshot.available_to_system_at - timedelta(microseconds=1),
            market=Market.XSHE, allow_uncertified_capture=True,
        )
