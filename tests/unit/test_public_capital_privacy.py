from __future__ import annotations

import json
import re
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.research.capital_privacy import capital_disclosure_findings, redact_capital_amounts
from astock.research.presentation import (
    ResponseGateway,
    audit_developer_answer,
    audit_public_answer,
)
from astock.schemas.presentation import (
    DeveloperDiagnosticsInput,
    FactFingerprint,
    ResearchNarrativeBundle,
    ResponseMode,
    ResponseTaskType,
)


@pytest.mark.parametrize(
    "text",
    (
        "本金250000元，目标年化30%。",
        "本金：￥250,000。",
        "投资预算为25万元。",
        "可用资金 25w RMB。",
        "本金五十万元。",
        "本金：２５００００元。",
        "**本金** | 25万元",
        '"capital_rmb": 250000',
        "investment capital: CNY 250000",
        "250000元的本金",
        "账户资产25万元。",
        "本金1e6元。",
    ),
)
def test_capital_disclosure_rejected_without_a_fixed_amount(text: str) -> None:
    assert capital_disclosure_findings(text) == ("PRIVATE_CAPITAL_AMOUNT_EXPOSED",)
    for audit in (audit_public_answer(text), audit_developer_answer(text)):
        assert not audit.safe_to_send
        assert "PRIVATE_CAPITAL_AMOUNT_EXPOSED" in audit.finding_codes
    redacted = redact_capital_amounts(text)
    assert not capital_disclosure_findings(redacted)


@pytest.mark.parametrize(
    "text",
    (
        "目标年化100%；目标仓位25%；现金权重20%。",
        "参考价格15元，每股合理价值18元。",
        "公司收入增长20%，营业收入25亿元。",
        "公司注册资本25亿元。",
        "本金比例25%，不展示具体金额。",
        "本金不对外披露。",
    ),
)
def test_public_security_facts_and_ratios_are_not_capital_disclosures(text: str) -> None:
    assert not capital_disclosure_findings(text)
    assert redact_capital_amounts(text) == text


@pytest.mark.parametrize("capital", ("35000", "987654.32", "2500000"))
def test_recommendation_projection_hides_account_scale_but_keeps_private_sizing(
    capital: str,
) -> None:
    from tests.unit.test_full_research_recommendation import _build_receipt, _request

    service = FullResearchRecommendationService()
    request = _request("推荐现在可以买的股票", request_id=f"public-capital-{capital}")
    request = request.model_copy(
        update={
            "metadata": {
                **request.metadata,
                "portfolio_assumptions": {
                    "capital_rmb": capital,
                    "target_annual_return": "0.3",
                },
            }
        }
    )
    receipt = _build_receipt(service, request)
    assert receipt.request_contract.portfolio_assumptions.capital_rmb == Decimal(capital)
    assert receipt.portfolio.capital == Decimal(capital)
    before = receipt.model_dump_json()
    result = VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (receipt,)}))
    )
    text = json.dumps(result, ensure_ascii=False)
    assert capital not in text
    assert not capital_disclosure_findings(text)
    assert not re.search(r"(?:首仓|目标数量|当前数量|目标)\d+(?:-\d+)?股", text)
    assert "目标年化30%" in text
    assert "不构成收益承诺" in text
    assert "参考价格" in text
    assert "现金权重" in text
    assert before == receipt.model_dump_json()


def test_legacy_receipt_projection_does_not_restore_principal_disclosure() -> None:
    from tests.unit.test_full_research_recommendation import _build_receipt, _request

    service = FullResearchRecommendationService()
    receipt = _build_receipt(service, _request("推荐股票", request_id="legacy-public-capital"))
    assumptions = receipt.request_contract.portfolio_assumptions.model_copy(
        update={
            "target_annual_return": None,
            "capital_source": None,
            "target_annual_return_source": None,
        }
    )
    legacy = receipt.model_copy(
        update={
            "request_contract": receipt.request_contract.model_copy(
                update={"portfolio_assumptions": assumptions}
            )
        }
    )
    result = VerifiedAnswerProjector._full_research_decision(
        cast(Any, SimpleNamespace(outputs={"FULL_RESEARCH_GATE": (legacy,)}))
    )
    assert not capital_disclosure_findings(json.dumps(result, ensure_ascii=False))
    assert "本金" not in json.dumps(result, ensure_ascii=False)


def test_private_capital_is_not_released_even_when_locked_as_a_source_fact() -> None:
    text = "本金321000元，目标年化30%。"
    audit = audit_public_answer(
        text,
        source_text=text,
        required_fingerprint=FactFingerprint(numbers=["321000", "30"]),
    )
    assert not audit.safe_to_send
    assert "PRIVATE_CAPITAL_AMOUNT_EXPOSED" in audit.finding_codes


def test_public_fallback_never_echoes_amount_or_calls_privacy_a_research_gap() -> None:
    gateway = ResponseGateway()
    result = gateway.render(
        gateway.context("推荐组合", task_type=ResponseTaskType.FULL_RESEARCH_RECOMMENDATION),
        narrative=ResearchNarrativeBundle(subject="组合分析", headline="本金321000元。"),
    )
    assert result.safe_fallback_used
    assert result.audit.safe_to_send
    assert "321000" not in result.model_dump_json()
    assert "账户资金信息" in result.text
    assert "资料不足" not in result.text


def test_developer_mode_also_redacts_principal_but_keeps_security_prices() -> None:
    gateway = ResponseGateway()
    result = gateway.render(
        gateway.context("调试", explicit_mode=ResponseMode.DEVELOPER),
        diagnostics=DeveloperDiagnosticsInput(
            user_impact="本金321000元；证券报价15元。",
            failure_class="DIAGNOSTIC",
            correlation_id="public-capital-check",
        ),
    )
    assert "321000" not in result.model_dump_json()
    assert "15元" in result.text
    assert result.audit.safe_to_send
