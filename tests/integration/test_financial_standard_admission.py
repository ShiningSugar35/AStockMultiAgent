"""Controlled production admission and actual deterministic accounting replay.

All issuers and revised-standard text in this test are synthetic recorded fixtures,
not an assertion that any real listed company adopted CAS30-2026.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from astock.financial_sources.presentation import FinancialPresentationService
from astock.schemas import (
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialStatementScope,
    Market,
)
from astock.schemas.financial_presentation import (
    AdoptionClaim,
    FinancialPageAnchor,
    FinancialPresentationProposal,
    PresentationFramework,
)
from tests.integration.test_financial_sources import (
    PERIOD_END,
    _capture_existing_annual,
    _service,
)

SYNTHETIC_ADOPTION = (
    "本公司自2014年7月1日起执行2014年修订企业会计准则第30号财务报表列报"
)


def _source_fixture(tmp_path, monkeypatch):
    sources = _service(tmp_path)
    _capture_existing_annual(sources, extra_cash_rows=SYNTHETIC_ADOPTION)
    for provider in sources.providers.values():
        monkeypatch.setattr(
            provider, "fetch",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no network")),
        )
    synced = sources.sync(
        "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL,
        live=True, allow_network=False,
    )
    assert synced.status.value == "CERTIFIED"
    engine = FinancialPresentationService(sources)
    asof = datetime.now(UTC) + timedelta(seconds=30)
    bound = engine._bound("000001", PERIOD_END, FinancialPeriodType.ANNUAL, asof)
    assert bound.verified is not None

    def anchor(term: str) -> FinancialPageAnchor:
        for page in bound.report.pages:
            text = sources.objects.get_bytes(page.text_object_sha256).decode("utf-8")
            start = text.find(term)
            if start >= 0:
                return FinancialPageAnchor(
                    page_id=page.page_id, page_number=page.page_number,
                    text_sha256=page.text_sha256, start=start,
                    end=start + len(term), quote=term,
                )
        raise AssertionError(f"synthetic official document missing phrase: {term}")

    return sources, engine, asof, bound, anchor


def test_explicit_standard_approval_replays_through_live_accounting_engine(
    tmp_path, monkeypatch,
) -> None:
    sources, engine, asof, bound, anchor = _source_fixture(tmp_path, monkeypatch)
    assert bound.verified is not None
    proposal = FinancialPresentationProposal(
        company_id="000001",
        instrument_id="XSHE:000001",
        release_id=bound.verified.manifest.release_id,
        official_document_id=bound.report.document.document_id,
        official_snapshot_id=bound.report.snapshot.snapshot_id,
        official_source_hash=bound.report.snapshot.object_sha256,
        period_end=PERIOD_END,
        period_type=FinancialPeriodType.ANNUAL,
        as_of=asof,
        model_id="model-proposal-1",
        prompt_version="wp78-synthetic-v1",
        framework=PresentationFramework.PRC_ASBE,
        standard_version_candidate="CAS30-2014",
        adoption=AdoptionClaim.CONFIRMED,
        adoption_effective_date=date(2014, 7, 1),
        adoption_evidence=[anchor(SYNTHETIC_ADOPTION)],
        industry_profile_candidate=FinancialIndustryProfile.BANK,
        industry_evidence=[anchor("合并资产负债表")],
        statement_scope=FinancialStatementScope.CONSOLIDATED,
        scope_evidence=[anchor("合并资产负债表")],
        declared_currency="CNY",
        currency_evidence=[anchor("人民币")],
    )
    with pytest.raises(ValueError, match="explicit production"):
        engine.admit_standard(
            proposal, independently_reviewed_by="auditor-human",
            independent_review_note="I read the issuer's actual adoption disclosure.",
            approve=False,
        )
    with pytest.raises(ValueError, match="independent accounting"):
        engine.admit_standard(
            proposal, independently_reviewed_by=proposal.model_id,
            independent_review_note="I read the issuer's actual adoption disclosure.",
            approve=True,
        )
    identifier, admission = engine.admit_standard(
        proposal, independently_reviewed_by="auditor-human",
        independent_review_note="I reviewed the real source locations and adoption semantics.",
        approve=True,
    )
    assert identifier.startswith("financial-standard-admission:")
    assert admission.policy_id == "PRC_ASBE_CAS30_2014"
    assert admission.financial_release_id == bound.verified.manifest.release_id
    assert engine.admit_standard(
        proposal, independently_reviewed_by="auditor-human",
        independent_review_note="I reviewed the real source locations and adoption semantics.",
        approve=True,
    )[0] == identifier

    pack = sources.run_audit(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=asof,
        industry_profile=FinancialIndustryProfile.BANK,
        standard_admission_id=identifier,
    )
    assert pack.model_versions["presentation_standard"] == "PRC_ASBE_CAS30_2014"
    assert "balance_sheet_identity" in pack.rule_versions
    assert "cash_flow_identity" in pack.rule_versions
    assert pack.advisory_only

    baseline = sources.run_audit(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=asof, industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
    )
    assert "presentation_standard" not in baseline.model_versions
    assert baseline.audit_run_id != pack.audit_run_id

    with pytest.raises(ValueError, match="scope/PIT mismatch"):
        sources.run_audit(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=asof, industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            standard_admission_id=identifier,
        )
    with pytest.raises(ValueError, match="scope/PIT mismatch"):
        sources.run_audit(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=admission.admitted_at - timedelta(microseconds=1),
            industry_profile=FinancialIndustryProfile.BANK,
            standard_admission_id=identifier,
        )
    with pytest.raises(ValueError, match="unrecognized"):
        sources.run_audit(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=asof, industry_profile=FinancialIndustryProfile.BANK,
            standard_admission_id="financial-standard-admission:" + "0" * 64,
        )

    # Production must revalidate its actual stored model proposal + review,
    # even when the deterministic audit result is already cached.
    with sources.state.transaction() as connection:
        connection.execute(
            "DELETE FROM artifact_registry WHERE artifact_id=?",
            (admission.review_artifact_id,),
        )
    with pytest.raises(ValueError, match="proposal/review artifact is missing"):
        sources.run_audit(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
            as_of=asof, industry_profile=FinancialIndustryProfile.BANK,
            standard_admission_id=identifier,
        )


def test_new_standard_not_backdated_or_promoted_from_uncertified_pdfs(
    tmp_path, monkeypatch,
) -> None:
    _sources, engine, asof, bound, anchor = _source_fixture(tmp_path, monkeypatch)
    assert bound.verified is not None
    base = {
        "company_id": "000001",
        "instrument_id": "XSHE:000001",
        "release_id": bound.verified.manifest.release_id,
        "official_document_id": bound.report.document.document_id,
        "official_snapshot_id": bound.report.snapshot.snapshot_id,
        "official_source_hash": bound.report.snapshot.object_sha256,
        "period_end": PERIOD_END,
        "period_type": FinancialPeriodType.ANNUAL,
        "as_of": asof,
        "model_id": "first-model",
        "prompt_version": "synthetic-v1",
        "framework": "PRC_ASBE",
        "standard_version_candidate": "CAS30-2026",
        "adoption": "CONFIRMED",
        "adoption_effective_date": date(2025, 10, 1),
        "adoption_evidence": [anchor(SYNTHETIC_ADOPTION)],
        "industry_profile_candidate": FinancialIndustryProfile.BANK,
        "industry_evidence": [anchor("合并资产负债表")],
        "statement_scope": "CONSOLIDATED",
        "scope_evidence": [anchor("合并资产负债表")],
        "declared_currency": "CNY",
        "currency_evidence": [anchor("人民币")],
    }
    proposal = FinancialPresentationProposal.model_validate(base)
    with pytest.raises(ValueError, match="precedes official"):
        engine.admit_standard(
            proposal, independently_reviewed_by="different-reviewer",
            independent_review_note="Review of the issuer's reported adoption date.",
            approve=True,
        )
    with pytest.raises(ValidationError, match="uncertified capture cannot claim"):
        FinancialPresentationProposal.model_validate({
            **base, "source_mode": "OFFICIAL_CAPTURE_ONLY",
        })
