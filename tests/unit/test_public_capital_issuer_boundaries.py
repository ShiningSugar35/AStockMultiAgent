"""Issuer facts remain public; issuer wording cannot launder personal positions."""
from __future__ import annotations

import pytest

from astock.research.capital_privacy import capital_disclosure_findings, redact_capital_amounts
from astock.research.presentation import audit_public_answer


@pytest.mark.parametrize("text", (
    "公司盈利25亿元。",
    "上市公司预计年度盈利25亿元。",
    "发行人预计盈利25亿元。",
    "公司2026年上半年盈利25亿元。",
    "控股股东持有1700股。",
    "大股东直接持有1700股。",
    "股东目前持有1700股。",
    "公司：盈利25亿元。",
))
def test_clear_issuer_subject_preserves_public_financial_and_ownership_fact(text: str) -> None:
    assert not capital_disclosure_findings(text)
    assert redact_capital_amounts(text) == text
    assert "PRIVATE_CAPITAL_AMOUNT_EXPOSED" not in audit_public_answer(text).finding_codes


@pytest.mark.parametrize("text", (
    "我在这家公司持有1700股。",
    "组合预计盈利25万元。",
    "公司建议买入1700股。",
    "公司投资组合目标盈利25万元。",
    "公司目标仓位20%，建议买入1700股。",
    "控股股东持有1700股；我的本金25万元。",
    "公司盈利25亿元，目标盈利25万元。",
    "公司盈利25亿元。当前数量1700股。",
    "上市公司本金25万元。",
))
def test_issuer_word_does_not_bypass_personal_funds_or_quantity_guard(text: str) -> None:
    assert capital_disclosure_findings(text)
    assert not audit_public_answer(text, source_text=text).safe_to_send
    assert not capital_disclosure_findings(redact_capital_amounts(text))


def test_mixed_diagnostic_redaction_preserves_issuer_fact_and_hides_personal_quantity() -> None:
    value = redact_capital_amounts("公司盈利25亿元；当前数量1700股。")
    assert "公司盈利25亿元" in value
    assert "1700" not in value
    assert not capital_disclosure_findings(value)
