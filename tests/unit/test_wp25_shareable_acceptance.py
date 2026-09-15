"""Independent acceptance for WP-25's relaxed, request-aware privacy boundary."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.research.capital_privacy import capital_disclosure_findings
from astock.research.presentation import audit_public_answer
from tests.unit.test_full_research_recommendation import _build_receipt, _request
from tests.unit.test_investor_answer_projection import environment as environment
from tests.unit.test_investor_orchestration_guards import good_run


@pytest.mark.parametrize(
    "text",
    (
        "本轮本金237461.35元，目标年化20%。",
        "采用默认本金237461.35元规划。",
        "投资本金为二十三万元。",
        "组合保留现金17461.35元，现金权重20%。",
        "目标盈利约47492.27元，目标年化20%。",
        "按计划配置金额计算，情景预期盈亏约42743.04元。",
        "下行情景损失约4749.23元。",
        "建议买入1700股，目标权重20%。",
        "首仓800股，目标1700股。",
    ),
)
def test_planning_and_recommendation_numbers_are_not_treated_as_private_holdings(
    text: str,
) -> None:
    assert not capital_disclosure_findings(text)
    assert audit_public_answer(text, source_text=text).safe_to_send


@pytest.mark.parametrize(
    "text",
    (
        "持仓本金237461.35元。",
        "持仓盈亏-17461.35元。",
        "账户余额237461.35元。",
        "当前持仓数量1700股。",
    ),
)
def test_unrequested_personal_holding_numbers_remain_private(text: str) -> None:
    assert capital_disclosure_findings(text)
    assert not audit_public_answer(text, source_text=text).safe_to_send


def _visible_projection(receipt: Any, request: Any) -> dict[str, Any]:
    return VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (receipt,)}, request=request))
    )


@pytest.mark.parametrize("principal", ["237461.35", "783249.70", "61357.25"])
@pytest.mark.parametrize("no_buy", [False, True])
def test_recommendation_projection_keeps_planning_capital_and_target_path(
    principal: str, no_buy: bool
) -> None:
    service = FullResearchRecommendationService()
    request = _request(
        f"本金{principal}元，年化目标35%，推荐投资组合",
        request_id=f"relaxed-shareable-{principal}-{no_buy}",
    )
    receipt = _build_receipt(service, request, all_ineligible=no_buy)
    before = receipt.model_dump_json()
    fields = _visible_projection(receipt, request)
    text = json.dumps(fields, ensure_ascii=False)
    rendered_principal = str(Decimal(principal).normalize())
    assert rendered_principal in text
    assert "35%" in text and "不构成收益承诺" in text
    assert not capital_disclosure_findings(text, request_text=request.raw_text)
    assert receipt.model_dump_json() == before


def test_existing_holding_quantity_defaults_private_but_is_user_releasable() -> None:
    service = FullResearchRecommendationService()
    default_request = _request("现有持仓要不要减仓", request_id="holding-private-default")
    default_receipt = _build_receipt(
        service,
        default_request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    hidden = json.dumps(_visible_projection(default_receipt, default_request), ensure_ascii=False)
    assert "当前数量500股" not in hidden

    explicit_request = _request(
        "我当前持有多少股，这个持仓要不要减仓",
        request_id="holding-private-explicit",
    )
    explicit_receipt = _build_receipt(
        service,
        explicit_request,
        all_ineligible=True,
        holding_action="TRIM",
    )
    shown = json.dumps(_visible_projection(explicit_receipt, explicit_request), ensure_ascii=False)
    assert "当前数量500股" in shown
    assert not capital_disclosure_findings(shown, request_text=explicit_request.raw_text)


def test_verified_paper_status_may_show_cash_when_user_asks_for_account_funds(environment) -> None:
    from astock.investor_orchestration.gateway import InvestorAnswerGateway

    request, _, _, coverage, _, _ = good_run(environment)
    assert "账户" in request.raw_text and "资金" in request.raw_text
    result = InvestorAnswerGateway(environment.store).publish_verified(
        coverage_receipt_id=coverage.receipt_id,
    )
    assert not result.degraded
    assert "1000元" in result.conclusion
    assert not capital_disclosure_findings(
        result.model_dump_json(), request_text=request.raw_text
    )
