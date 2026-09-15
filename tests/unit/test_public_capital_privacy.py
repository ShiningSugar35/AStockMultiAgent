from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.research.capital_privacy import (
    capital_disclosure_findings,
    redact_capital_amounts,
    requested_personal_detail_categories,
)
from astock.research.presentation import audit_developer_answer, audit_public_answer
from astock.schemas.presentation import ResponseContext, ResponseMode, ResponseTaskType


@pytest.mark.parametrize(
    "text",
    (
        "持仓本金250000元。",
        "持仓成本为25万元。",
        "账户余额237461.35元。",
        "模拟账户可用现金为35000元。",
        "持仓盈亏-5000元。",
        "浮亏3500元。",
        "当前持仓数量1700股。",
        "我在这家公司持有1700股。",
    ),
)
def test_unrequested_personal_holding_values_are_hidden_by_default(text: str) -> None:
    assert capital_disclosure_findings(text) == ("PRIVATE_CAPITAL_AMOUNT_EXPOSED",)
    assert not audit_public_answer(text, source_text=text).safe_to_send
    assert not audit_developer_answer(text).safe_to_send
    redacted = redact_capital_amounts(text)
    assert not capital_disclosure_findings(redacted)
    assert text != redacted


@pytest.mark.parametrize(
    "text",
    (
        "本轮本金250000元，目标年化30%。",
        "采用默认本金10万元规划。",
        "投资预算为25万元。",
        "组合保留现金17461.35元，现金权重20%。",
        "目标盈利约47492.27元，目标年化20%。",
        "按计划配置金额计算，情景预期盈亏约42743.04元。",
        "下行情景损失约4749.23元。",
        "建议买入1700股，目标权重20%。",
        "首仓800股，目标1700股。",
        "参考价格15元，每股合理价值18元。",
        "公司盈利25亿元。",
        "上市公司预计年度盈利25亿元。",
        "控股股东持有1700股。",
    ),
)
def test_planning_recommendation_and_issuer_facts_are_shareable_by_default(text: str) -> None:
    assert not capital_disclosure_findings(text)
    assert audit_public_answer(text, source_text=text).safe_to_send
    assert redact_capital_amounts(text) == text


@pytest.mark.parametrize(
    ("request_text", "output", "category"),
    (
        ("我的持仓本金是多少", "持仓本金250000元。", "holding_principal"),
        ("这只股票我现在亏了多少", "持仓盈亏-5000元。", "holding_pnl"),
        ("我当前持有多少股", "当前持仓数量1700股。", "holding_quantity"),
        ("我的账户余额是多少", "账户余额237461.35元。", "account_amount"),
        ("看看我的持仓详情", "当前持仓数量1700股，持仓盈亏-5000元。", "holding_pnl"),
    ),
)
def test_user_explicit_request_unlocks_matching_personal_detail(
    request_text: str, output: str, category: str
) -> None:
    assert category in requested_personal_detail_categories(request_text)
    assert not capital_disclosure_findings(output, request_text=request_text)
    context = ResponseContext(
        task_type=ResponseTaskType.FULL_RESEARCH_RECOMMENDATION,
        request_text=request_text,
    )
    assert audit_public_answer(output, context=context, source_text=output).safe_to_send
    assert redact_capital_amounts(output, request_text=request_text) == output


def test_request_for_one_personal_category_does_not_unlock_another() -> None:
    request_text = "告诉我持仓盈亏"
    assert not capital_disclosure_findings("持仓盈亏-5000元。", request_text=request_text)
    assert capital_disclosure_findings("账户余额250000元。", request_text=request_text)


def test_developer_mode_uses_same_user_request_exception() -> None:
    context = ResponseContext(
        mode=ResponseMode.DEVELOPER,
        task_type=ResponseTaskType.DEVELOPER_DIAGNOSTIC,
        diagnostic_intent_detected=True,
        request_text="调试一下，并告诉我当前账户余额",
    )
    assert audit_developer_answer("账户余额250000元。", context=context).safe_to_send


def _visible_projection(receipt: Any, request: Any) -> dict[str, Any]:
    return VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (receipt,)}, request=request))
    )


def test_recommendation_projection_may_show_planning_principal_and_sizing() -> None:
    from tests.unit.test_full_research_recommendation import _build_receipt, _request

    service = FullResearchRecommendationService()
    request = _request("本金25万，年化目标30%，推荐投资组合", request_id="relaxed-plan")
    receipt = _build_receipt(service, request)
    fields = _visible_projection(receipt, request)
    text = json.dumps(fields, ensure_ascii=False)
    assert "250000" in text
    assert "目标年化30%" in text
    assert "股" in text
    assert not capital_disclosure_findings(text, request_text=request.raw_text)


def test_existing_holding_quantity_hidden_unless_current_request_mentions_it() -> None:
    from tests.unit.test_full_research_recommendation import _build_receipt, _request

    service = FullResearchRecommendationService()
    default_request = _request("现有持仓要不要减仓", request_id="holding-default-private")
    receipt = _build_receipt(
        service,
        default_request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    hidden = json.dumps(_visible_projection(receipt, default_request), ensure_ascii=False)
    assert "当前数量500股" not in hidden

    explicit_request = _request(
        "我当前持有多少股，这个持仓要不要减仓",
        request_id="holding-explicit-quantity",
    )
    explicit_receipt = _build_receipt(
        service,
        explicit_request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    visible = json.dumps(
        _visible_projection(explicit_receipt, explicit_request), ensure_ascii=False
    )
    assert "当前数量500股" in visible
    assert not capital_disclosure_findings(visible, request_text=explicit_request.raw_text)


def test_private_facts_stay_inside_receipt_when_not_requested() -> None:
    from tests.unit.test_full_research_recommendation import _build_receipt, _request

    service = FullResearchRecommendationService()
    request = _request("现有持仓要不要减仓", request_id="private-receipt-stays-private")
    receipt = _build_receipt(
        service,
        request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    assert any(item.current_quantity == Decimal("500") for item in receipt.holding_reviews)
    text = json.dumps(_visible_projection(receipt, request), ensure_ascii=False)
    assert "当前数量500股" not in text
