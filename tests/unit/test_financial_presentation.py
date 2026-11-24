"""WP-78: negative contracts for untrusted LLM presentation proposals."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from astock.cli import app
from astock.schemas.financial_presentation import (
    FinancialPageAnchor,
    FinancialPresentationProposal,
    FinancialPresentationReview,
    FinancialPresentationRulePreview,
    PresentationReviewStatus,
)


def _base() -> dict:
    return {
        "company_id": "000001",
        "instrument_id": "XSHE:000001",
        "release_id": "a" * 64,
        "official_document_id": "cninfo:test",
        "official_snapshot_id": "official:snapshot",
        "official_source_hash": "b" * 64,
        "period_end": date(2026, 9, 30),
        "period_type": "QUARTERLY",
        "as_of": datetime(2026, 11, 23, tzinfo=UTC),
        "model_id": "llm-test",
        "prompt_version": "wp78-shadow-v1",
    }


def test_unverified_adoption_cannot_claim_known_version_without_framework() -> None:
    values = _base()
    values.update(standard_version_candidate="CAS30-2026")
    with pytest.raises(ValidationError, match="unknown framework"):
        FinancialPresentationProposal.model_validate(values)


def test_confirmed_standard_requires_issuer_source_evidence() -> None:
    values = _base()
    values.update(
        framework="PRC_ASBE", standard_version_candidate="CAS30-2026",
        adoption="CONFIRMED",
    )
    with pytest.raises(ValidationError, match="confirmed standard adoption"):
        FinancialPresentationProposal.model_validate(values)


def test_page_anchor_must_provide_exact_offset_quote() -> None:
    with pytest.raises(ValidationError, match="anchor quote length/offset mismatch"):
        FinancialPageAnchor(
            page_id="a", page_number=1, text_sha256="f"*64,
            start=1, end=6, quote="资产总计",
        )


def test_assumed_industry_scope_and_period_must_each_cite_source() -> None:
    for name, value in (
        ("industry_profile_candidate", "BANK"),
        ("statement_scope", "CONSOLIDATED"),
        ("duration_semantics", "YEAR_TO_DATE"),
        ("restatement_claim", "重述了比较期"),
    ):
        with pytest.raises(ValidationError, match="needs source anchors"):
            FinancialPresentationProposal.model_validate({**_base(), name: value})


def test_issuer_identity_and_unknown_rule_duplicates_cannot_forge_proposal() -> None:
    with pytest.raises(ValidationError, match="identity mismatch"):
        FinancialPresentationProposal.model_validate({
            **_base(), "instrument_id": "XSHG:600000",
        })
    with pytest.raises(ValidationError, match="unique"):
        FinancialPresentationProposal.model_validate({
            **_base(), "proposed_rule_ids": ["cash_flow_identity"] * 2,
        })


def test_shadow_review_must_never_declare_formal_authority() -> None:
    basic = {
        "proposal_hash": "f" * 64, "release_id": "e" * 64,
        "status": PresentationReviewStatus.EVIDENCE_BOUND,
        "anchored_claim_count": 0, "checked_line_count": 0,
    }
    for property_name in (
        "formal_financial_authority", "active_parser_changed", "published_recommendation",
    ):
        with pytest.raises(ValidationError, match="cannot claim production authority"):
            FinancialPresentationReview.model_validate({**basic, property_name: True})

def test_proposed_currency_unit_and_comparative_period_need_exact_evidence() -> None:
    for name, value in (
        ("declared_currency", "USD"),
        ("declared_unit", "CNY"),
        ("comparative_period_end", date(2025, 9, 30)),
        ("adoption_effective_date", date(2026, 8, 1)),
    ):
        with pytest.raises(ValidationError, match="source anchors|disclosure evidence"):
            FinancialPresentationProposal.model_validate({**_base(), name: value})
    with pytest.raises(ValidationError, match="comparative period"):
        FinancialPresentationProposal.model_validate({
            **_base(),
            "comparative_period_end": date(2027, 9, 30),
            "comparative_period_evidence": [{
                "page_id": "test", "page_number": 1,
                "text_sha256": "f" * 64,
                "start": 0, "end": 2, "quote": "同比",
            }],
        })


def test_preview_schema_has_no_active_rule_authority() -> None:
    payload = {
        "release_id": "d" * 64,
        "proposal_hash": "e" * 64,
        "registry_version": "financial-rules-m3.2-v3",
        "framework_candidate": "PRC_ASBE",
    }
    with pytest.raises(ValidationError, match="cannot grant rule authority"):
        FinancialPresentationRulePreview.model_validate({
            **payload, "formal_financial_authority": True,
        })
    with pytest.raises(ValidationError, match="cannot grant rule authority"):
        FinancialPresentationRulePreview.model_validate({
            **payload, "approval_status": "AUTO_ADMITTED",
        })


def test_wp78_cli_commands_expose_host_llm_schema_without_data_writes() -> None:
    result = CliRunner().invoke(app, ["financial-presentation-schema"])
    assert result.exit_code == 0, result.output
    response = json.loads(result.output)
    assert response["production_writes"] is False
    assert "proposal" in response and "rule_preview" in response
    for command in ("financial-presentation-context",
                    "financial-presentation-review",
                    "financial-presentation-rule-preview"):
        help_result = CliRunner().invoke(app, [command, "--help"])
        assert help_result.exit_code == 0, help_result.output
