"""Issuer/planning facts stay public while unrequested personal holdings stay private."""
from __future__ import annotations

import pytest

from astock.research.capital_privacy import capital_disclosure_findings, redact_capital_amounts
from astock.research.presentation import audit_public_answer


@pytest.mark.parametrize(
    "text",
    (
        "公司盈利25亿元。",
        "上市公司预计年度盈利25亿元。",
        "发行人预计盈利25亿元。",
        "公司2026年上半年盈利25亿元。",
        "控股股东持有1700股。",
        "大股东直接持有1700股。",
        "股东目前持有1700股。",
        "公司：盈利25亿元。",
        "公司建议买入1700股。",
        "公司投资组合目标盈利25万元。",
        "公司目标仓位20%，建议买入1700股。",
        "公司账户余额25亿元。",
        "发行人账户现金1000万元。",
        "公司持仓数量1700股。",
        "本轮本金25万元，目标年化20%。",
    ),
)
def test_issuer_and_planning_facts_are_not_privacy_findings(text: str) -> None:
    assert not capital_disclosure_findings(text)
    assert redact_capital_amounts(text) == text
    assert "PRIVATE_CAPITAL_AMOUNT_EXPOSED" not in audit_public_answer(text).finding_codes


@pytest.mark.parametrize(
    "text",
    (
        "我在这家公司持有1700股。",
        "当前持仓数量1700股。",
        "我的持仓本金25万元。",
        "持仓盈亏-2500元。",
        "账户余额25万元。",
        "控股股东持有1700股；我的持仓本金25万元。",
        "公司盈利25亿元。当前持仓数量1700股。",
    ),
)
def test_issuer_word_does_not_bypass_unrequested_personal_holding_guard(text: str) -> None:
    assert capital_disclosure_findings(text)
    assert not audit_public_answer(text, source_text=text).safe_to_send
    assert not capital_disclosure_findings(redact_capital_amounts(text))


def test_mixed_redaction_preserves_issuer_fact_and_hides_personal_quantity() -> None:
    value = redact_capital_amounts("公司盈利25亿元；当前持仓数量1700股。")
    assert "公司盈利25亿元" in value
    assert "1700" not in value
    assert not capital_disclosure_findings(value)


def test_explicit_user_request_allows_personal_holding_detail() -> None:
    request_text = "我现在持有多少股"
    output = "当前持仓数量1700股。"
    assert not capital_disclosure_findings(output, request_text=request_text)
    assert audit_public_answer(output, request_text=request_text, source_text=output).safe_to_send
