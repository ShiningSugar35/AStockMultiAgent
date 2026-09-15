"""Independent public-sharing acceptance for WP-25; synthetic funds only."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.research.capital_privacy import capital_disclosure_findings
from astock.research.presentation import ResponseGateway, audit_public_answer
from astock.schemas.presentation import ResearchNarrativeBundle, ResponseDetail, ResponseTaskType
from tests.unit.test_full_research_recommendation import _build_receipt, _request
from tests.unit.test_investor_answer_projection import environment as environment
from tests.unit.test_investor_orchestration_guards import good_run


@pytest.mark.parametrize("text", [
    "本轮本金：237,461.35元。",
    "采用默认本金237461.35元规划。",
    "投资本金为二十三万元。",
    "按237461.35元本金计算。",
    "本金为2.3746135e5元。",
    "本金：２３７４６１．３５元。",
    "本\u200b金237461.35元。",
    "账户余额237461.35元。",
    "组合保留现金17461.35元，现金权重20%。",
    "目标盈利约47492.27元，目标年化20%。",
    "按计划配置金额计算，情景预期盈亏约42743.04元。",
    "下行情景损失约4749.23元。",
    "建议买入1700股，目标权重20%。",
    "首仓800股，目标1700股。",
    "当前数量1700股。",
])
def test_public_guard_covers_direct_and_inferable_capital(text: str) -> None:
    assert capital_disclosure_findings(text), "public funding/quantity must be rejected"
    assert not audit_public_answer(text, source_text=text).safe_to_send


@pytest.mark.parametrize("text", [
    "目标年化20%，目标仓位15%，保留现金30%。",
    "参考价12.50元，条件买入区间11.20至12.00元。",
    "下行情景损失占组合资产3%，目标差距5个百分点。",
    "公司营业收入237461.35万元，净利润增长20%。",
    "公司注册资本50亿元，每股收益1.25元。",
    "每手100股属于交易单位；仓位控制在15%。",
    "本金不公开，目标年化20%。",
])
def test_public_guard_keeps_ratios_prices_and_issuer_financials(text: str) -> None:
    assert not capital_disclosure_findings(text)


def _visible_projection(receipt: Any) -> dict[str, Any]:
    return VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (receipt,)}))
    )


@pytest.mark.parametrize("principal", ["237461.35", "783249.70", "61357.25"])
@pytest.mark.parametrize("no_buy", [False, True])
def test_recommendation_projection_is_shareable_for_arbitrary_principal(
    principal: str, no_buy: bool,
) -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service,
        _request(
            f"本金{principal}元，年化目标35%，推荐投资组合",
            request_id=f"shareable-{principal}-{no_buy}",
        ),
        all_ineligible=no_buy,
    )
    before = receipt.model_dump_json()
    fields = _visible_projection(receipt)
    text = "\n".join([fields["conclusion"], *fields["reasons"], *fields["risks"],
                      *fields["actions"], *fields["change_conditions"]])
    assert principal not in text
    assert not re.search(r"(?:现金|预期盈亏|下行情景损失|目标盈利)[^。；\n]{0,20}\d[\d,.]*元", text)
    assert not re.search(r"\d+(?:\.\d+)?股", text)
    assert "35%" in text and "不构成收益承诺" in text
    assert not capital_disclosure_findings(text)
    assert receipt.model_dump_json() == before, "public projection must not change private facts"


def test_legacy_projection_does_not_reintroduce_principal() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(service, _request("推荐投资组合", request_id="legacy-shareable"))
    old_assumptions = receipt.request_contract.portfolio_assumptions.model_copy(update={
        "target_annual_return": None,
        "capital_source": None,
        "target_annual_return_source": None,
    })
    legacy_view = receipt.model_copy(update={
        "request_contract": receipt.request_contract.model_copy(update={
            "portfolio_assumptions": old_assumptions,
        }),
    })
    fields = _visible_projection(legacy_view)
    text = json.dumps(fields, ensure_ascii=False)
    assert not re.search(r"本金[^。；]{0,20}\d", text)
    assert not capital_disclosure_findings(text)


def test_existing_holding_exit_keeps_action_without_exposing_position_size() -> None:
    service = FullResearchRecommendationService()
    receipt = _build_receipt(
        service, _request("现有持仓要不要减仓", request_id="holding-shareable"),
        all_ineligible=True, holding_action="TRIM",
    )
    fields = _visible_projection(receipt)
    text = json.dumps(fields, ensure_ascii=False)
    assert "减仓" in text
    assert not re.search(r"\d+(?:\.\d+)?股", text)
    assert not capital_disclosure_findings(text)


@pytest.mark.parametrize("detail", [ResponseDetail.SHORT, ResponseDetail.STANDARD])
def test_gateway_does_not_echo_leaking_legacy_subject_or_payload(detail: ResponseDetail) -> None:
    gateway = ResponseGateway()
    context = gateway.context(
        "展示投资组合", task_type=ResponseTaskType.FULL_RESEARCH_RECOMMENDATION,
        requested_detail=detail,
    )
    response = gateway.render(context, narrative=ResearchNarrativeBundle(
        subject="本金237461.35元的投资组合",
        headline="先控制仓位，暂不追涨。",
        reasons=["目标年化20%，不构成收益承诺。"],
        risks=["盈利可能不达预期。"],
    ))
    assert "237461.35" not in response.text
    assert "237461.35" not in response.payload.model_dump_json()
    assert response.audit.safe_to_send


def test_alternate_gateway_privacy_fallback_is_not_a_research_gap(environment) -> None:
    from astock.investor_orchestration.gateway import InvestorAnswerGateway

    _, preflight, _, coverage, draft, _ = good_run(environment)
    private_draft = draft.model_copy(update={"conclusion": "本轮本金237461.35元。"})
    result = InvestorAnswerGateway(environment.store).render(
        private_draft, preflight=preflight, coverage=coverage,
    )
    public = result.model_dump_json()
    assert result.degraded
    assert "隐私" in result.conclusion
    assert "237461" not in public
    assert "资料不足" not in public and "资料尚未核实" not in public


def test_verified_projection_privacy_error_survives_gateway_classification(
    environment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astock.investor_orchestration.gateway import InvestorAnswerGateway

    _, _, _, coverage, _, _ = good_run(environment)

    def unsafe_public_projection(_inputs):
        return {
            "conclusion": "模拟账户余额237461.35元。",
            "reasons": (), "risks": ("模拟结果不代表实盘。",),
            "actions": (), "change_conditions": (),
        }

    monkeypatch.setattr(
        VerifiedAnswerProjector, "_paper_status", staticmethod(unsafe_public_projection),
    )
    result = InvestorAnswerGateway(environment.store).publish_verified(
        coverage_receipt_id=coverage.receipt_id,
    )
    assert result.degraded and "隐私" in result.conclusion
    assert "237461" not in result.model_dump_json()
    assert "资料" not in (result.degradation_reason or "")


def test_real_evidence_gap_keeps_its_distinct_fallback(environment) -> None:
    from astock.investor_orchestration.gateway import InvestorAnswerGateway

    _, preflight, _, _, draft, _ = good_run(environment)
    neutral = draft.model_copy(update={
        "conclusion": "暂不形成投资判断。", "reasons": (), "risks": (),
        "actions": (), "change_conditions": (),
    })
    result = InvestorAnswerGateway._safe_answer(neutral, preflight)
    assert "资料" in (result.degradation_reason or "")
    assert "隐私" not in result.conclusion
