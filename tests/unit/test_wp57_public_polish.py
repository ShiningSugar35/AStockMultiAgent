"""WP-57 public editor must be shared by agent and QQ without inventing or hiding business facts."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from astock.investor_orchestration.cli import app
from astock.research.presentation import audit_public_answer
from astock.research.public_polish import polish_public_text


@pytest.mark.parametrize(
    "original",
    [
        "结论：看跌，合理价值18.50元，参考价27.30元。[S1]",
        "公司仍未盈利，经营现金流为负，未来可能继续亏损。",
        "公司2026年9月30日的正式财报尚未披露；这会影响估值。",
        "建议持有，不建议加仓，主要风险是需求继续下降。",
        "营业收入下降12.3%，毛利率从28%降至24%。",
    ],
)
def test_business_fact_and_negative_risk_are_preserved(original: str) -> None:
    result = polish_public_text(original, explain_terms=False)
    assert result.text == original


def test_qq_metadata_including_chinese_adjacent_identifier_is_removed() -> None:
    original = (
        "结论：继续观察。QQ投递：report_id=internal-123；"
        "DeliveryLedger未更新。"
    )
    result = polish_public_text(original, explain_terms=False)
    assert "继续观察" in result.text
    for forbidden in ("QQ投递", "report_id", "internal-123", "DeliveryLedger"):
        assert forbidden not in result.text


def test_process_prose_is_removed_but_explicit_business_uncertainty_survives() -> None:
    original = (
        "部分官方公告索引尚未逐项完整枚举。\n"
        "估值区间为20元至25元，正式季报尚未披露。\n"
        "QQ投递：报告已提交，群送达待确认。"
    )
    result = polish_public_text(original, explain_terms=False)
    assert "公告尚待核实" in result.text
    assert "20元至25元" in result.text
    assert "季报尚未披露" in result.text
    assert "群送达" not in result.text


def test_sensitive_data_is_redacted_without_losing_citations_or_valuation() -> None:
    original = (
        "参考价20元。[S1]\n"
        "Authorization: Bearer abcd1234。\n"
        r"私人路径 C:\Users\Alice\private\position.csv" + "。\n"
        "现金流为负，继续观察。[S2]"
    )
    result = polish_public_text(original, explain_terms=False)
    assert "20元" in result.text
    assert "[S1]" in result.text and "[S2]" in result.text
    assert "现金流为负" in result.text
    for forbidden in ("abcd1234", "Alice", "position.csv", "Authorization", "Bearer"):
        assert forbidden not in result.text


def test_internal_heading_does_not_erase_business_risk() -> None:
    original = (
        "## 研究边界\n"
        "经营现金流连续两季为负，建议继续观察。[S3]\n"
        "后台投递回执待核对。\n"
        "## 公司判断\n"
        "估值高于合理区间，暂缓买入。"
    )
    result = polish_public_text(original, explain_terms=False)
    assert "经营现金流连续两季为负" in result.text
    assert "建议继续观察" in result.text
    assert "[S3]" in result.text
    assert "估值高于合理区间" in result.text
    assert "后台投递回执" not in result.text


@pytest.mark.parametrize(
    "source,expected",
    [
        ("研究边界：经营现金流连续两季为负，建议继续观察。[S3]",
         "经营现金流连续两季为负，建议继续观察。[S3]"),
        ("内部状态：公司尚未盈利，合理估值区间15元至19元。[S4]",
         "公司尚未盈利，合理估值区间15元至19元。[S4]"),
    ],
)
def test_process_label_must_not_erase_inline_investment_facts(
    source: str, expected: str,
) -> None:
    result = polish_public_text(source, explain_terms=False)
    assert result.text == expected
    assert polish_public_text(result.text, explain_terms=False).text == expected


def test_polisher_is_idempotent_and_does_not_invent_claims() -> None:
    text = "结论：看跌，参考价27.30元。[S1]\nQQ投递：report_id=x1。"
    first = polish_public_text(text, explain_terms=False)
    second = polish_public_text(first.text, explain_terms=False)
    assert first.text == second.text
    assert first.text == "结论：看跌，参考价27.30元。[S1]"


def test_nested_windows_user_profile_is_redacted_without_losing_investment_facts() -> None:
    original = (
        "参考价20元。[S1] 私人目录 "
        r"D:\360MoveData\Users\Alice\Documents\holdings.csv"
        "。经营现金流为负，继续观察。"
    )
    assert audit_public_answer(original).private_path_exposed
    result = polish_public_text(original, explain_terms=False)
    assert "20元" in result.text and "[S1]" in result.text
    assert "经营现金流为负，继续观察" in result.text
    assert "Alice" not in result.text and "holdings.csv" not in result.text
    assert not audit_public_answer(result.text).private_path_exposed


def test_agent_public_polish_cli_uses_same_editor_as_qq(tmp_path) -> None:
    draft = "建议继续观察，参考价20元。[S1]\nQQ投递：report_id=internal-x。"
    text_file = tmp_path / "agent-draft.txt"
    text_file.write_text(draft, encoding="utf-8")
    result = CliRunner().invoke(app, ["public-polish", str(text_file)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == polish_public_text(draft).text
    assert "20元" in result.output and "[S1]" in result.output
    assert "report_id" not in result.output


def test_technical_process_only_input_does_not_become_buy_advice() -> None:
    result = polish_public_text("QQ投递：report_id=x。", explain_terms=False)
    assert "买入" not in result.text
    assert "看涨" not in result.text
    assert "QQ投递" not in result.text
