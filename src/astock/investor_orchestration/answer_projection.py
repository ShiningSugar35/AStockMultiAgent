"""Deterministic public-answer projection over authenticated domain results.

Registration authenticates bytes, not the meaning of arbitrary prose. This
module has no free-text draft input. Publication can reproduce the answer from
its request, preflight and coverage, and reject any changed public field.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityRunStatus,
    InvestorAnswerDraft,
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    RequestIntent,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier, output_model
from astock.investor_orchestration.utils import content_hash
from astock.research.capital_privacy import CapitalDisclosureError
from astock.research.presentation import audit_public_answer
from astock.schemas.entry_quality import EntryQualityState
from astock.schemas.external_accounts import ExternalAccountOperationReceipt
from astock.schemas.full_research import RecommendationResearchReceipt
from astock.schemas.institutional_research import InstitutionalDecisionContext
from astock.schemas.paper import (
    PaperOperationReport,
    PaperPreparationReceipt,
    PortfolioNAV,
    ReplayExecutionReport,
)
from astock.schemas.research_team import ResearchRoleOutput

PROJECTION_POLICY = "verified-investor-answer-projection-v1"
_LOCAL = frozenset(
    {
        "REQUEST_TIME",
        "ENTITY_IDENTITY",
        "SESSION_PREFLIGHT",
        "MARKET_REGIME",
        "SUBJECT_REGISTRY",
        "RESPONSE_GATEWAY",
    }
)
_ENTRY_QUALITY_PUBLIC_TEXT = {
    EntryQualityState.ATTRACTIVE_DISLOCATION: "中长期价格位置较低，且已有企稳迹象",
    EntryQualityState.BASE_BUILDING: "处于中低位筑底区间",
    EntryQualityState.TREND_CONFIRMED: "趋势已经确认，但并非绝对低位",
    EntryQualityState.FALLING_KNIFE_RISK: "价格虽低，但下跌趋势尚未充分企稳",
    EntryQualityState.EXTENDED: "趋势偏强，但相对中期位置已经明显延伸",
    EntryQualityState.NEUTRAL: "价格位置与趋势处于中性区间",
    EntryQualityState.INSUFFICIENT_HISTORY: "可验证的价格位置历史不足",
}


@dataclass(frozen=True)
class VerifiedInputs:
    request: InvestorRequestEnvelope
    preflight: InvestorSessionPreflightReceipt
    coverage: CapabilityCoverageReceipt
    outputs: dict[str, tuple[BaseModel, ...]]
    artifact_ids: tuple[str, ...]
    input_hashes: tuple[str, ...]


def _decimal(value: Decimal | int | float) -> str:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("public numbers must be finite")
    rendered = format(result.quantize(Decimal("0.01")), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _code(value: str) -> str:
    market_names = {"XSHG": "沪市", "XSHE": "深市", "BJSE": "北交所"}
    if ":" in value:
        market, symbol = value.split(":", 1)
    elif "." in value:
        symbol, market = value.rsplit(".", 1)
    else:
        market, symbol = "", value
    if market and market not in market_names:
        raise ValueError("public security identity has an unsupported explicit market")
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("public security identity is not a resolved A-share code")
    return market_names.get(market, "") + symbol


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a required domain narrative is unavailable")
    return value.strip()


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True)
class _FullResearchPublicIndex:
    valuations: dict[str, Any]
    executions: dict[str, Any]
    rankings: dict[str, Any]
    narratives: dict[str, Any]
    industries: dict[str, Any]
    financials: dict[str, Any]
    governance: dict[str, Any]
    challengers: dict[str, Any]


def _full_research_receipt(inputs: VerifiedInputs) -> RecommendationResearchReceipt:
    receipts = inputs.outputs.get("FULL_RESEARCH_GATE", ())
    if len(receipts) != 1 or not isinstance(receipts[0], RecommendationResearchReceipt):
        raise ValueError("full research decision requires one sealed recommendation receipt")
    receipt = receipts[0]
    if not receipt.publication.formal_recommendation_allowed:
        raise ValueError("full research receipt did not pass publication")
    return receipt


def _full_research_index(receipt: RecommendationResearchReceipt) -> _FullResearchPublicIndex:
    return _FullResearchPublicIndex(
        valuations={item.instrument_id: item for item in receipt.valuations},
        executions={item.instrument_id: item for item in receipt.execution_plans},
        rankings={item.instrument_id: item for item in receipt.candidate_rankings},
        narratives={item.instrument_id: item for item in receipt.candidate_narratives},
        industries={item.industry_id: item for item in receipt.industries},
        financials={item.instrument_id: item for item in receipt.financial_quality},
        governance={item.instrument_id: item for item in receipt.governance},
        challengers={item.instrument_id: item for item in receipt.challengers},
    )


def _full_research_base_reasons(receipt: RecommendationResearchReceipt) -> list[str]:
    assumptions = receipt.request_contract.portfolio_assumptions
    portfolio = receipt.portfolio
    base: list[str] = []
    # Goals are public; the bankroll and its monetary derivatives remain private.
    if assumptions.target_annual_return is not None:
        base.append(
            f"本次以目标年化{_decimal(assumptions.target_annual_return * 100)}%规划，"
            "用于评估配置和收益路径，不构成收益承诺。"
        )
    base.append(
        f"宏观环境：{_text(receipt.macro.macro_regime)}；流动性：{_text(receipt.macro.liquidity_regime)}；"
        f"风险偏好：{_text(receipt.macro.risk_appetite)}。"
    )
    if portfolio.target_horizon_months is not None and portfolio.target_horizon_return is not None:
        base.append(
            f"按可核实的共同研究期限{_decimal(portfolio.target_horizon_months)}个月折算，"
            f"目标回报约{_decimal(portfolio.target_horizon_return * 100)}%。"
        )
    else:
        base.append(
            f"计划持有期限为{assumptions.horizon_min_months}-{assumptions.horizon_max_months}个月；"
            "估值回报期限尚未统一，因此只展示情景收益率，不直接判断目标年化能否达到。"
        )
    base.extend(assumptions.expectation_notes)
    return base


def _holding_review_fields(
    receipt: RecommendationResearchReceipt,
) -> tuple[list[str], list[str], list[str], list[str]]:
    reasons: list[str] = []
    risks: list[str] = []
    actions: list[str] = []
    conditions: list[str] = []
    labels = {
        "HOLD": "继续持有",
        "ADD": "按条件加仓",
        "TRIM": "按条件减仓",
        "EXIT": "按条件退出",
        "REVIEW": "先复核，暂不机械调整",
    }
    for review in receipt.holding_reviews:
        code = _code(review.instrument_id)
        weight = (
            f"，当前仓位{_decimal(review.current_weight * 100)}%"
            if review.current_weight is not None
            else ""
        )
        confidence = _decimal(review.action_confidence * 100)
        reasons.append(
            f"{code}持仓复核：投资逻辑{_text(review.thesis_strength_change)}，"
            f"风险变化{_text(review.risk_change)}，置信度{confidence}%{weight}。"
        )
        action = labels[review.recommended_action]
        detail = ""
        if review.target_weight_lower is not None and review.target_weight_upper is not None:
            detail += (
                f"，目标权重{_decimal(review.target_weight_lower * 100)}%-"
                f"{_decimal(review.target_weight_upper * 100)}%"
            )
        actions.append(f"{code}：{action}{detail}。")
        if review.risk_change not in {"UNCHANGED", "LOWER"}:
            risks.append(f"{code}持仓风险状态：{_text(review.risk_change)}。")
        conditions.extend(f"{code}执行前提：{_text(value)}" for value in review.preconditions)
        conditions.extend(f"{code}反转条件：{_text(value)}" for value in review.reversal_conditions)
        conditions.extend(
            f"{code}下次复核：{_text(value)}" for value in review.next_review_conditions
        )
    return reasons, risks, actions, conditions


def _full_research_no_buy(
    receipt: RecommendationResearchReceipt,
    reasons: list[str],
) -> dict[str, Any]:
    rejected = "、".join(_code(instrument) for instrument in sorted(receipt.rejected_candidates))
    if rejected:
        reasons.append(f"被淘汰候选：{rejected}；至少一项估值、质量、治理、证据或组合约束未达标。")
    holding_reasons, holding_risks, holding_actions, holding_conditions = _holding_review_fields(
        receipt
    )
    reasons.extend(holding_reasons)
    risks = list(holding_risks)
    risks.append("保留现金同样是组合决策；后续估值、基本面或市场状态变化后需要重新评估。")
    conditions = list(holding_conditions)
    conditions.append("出现新的合格候选，或现有候选安全边际与证据置信度显著改善时重新构建组合。")
    conclusion = (
        "完整研究后当前没有新增买入标的；现有持仓处置已按持仓复核结果纳入本次决策。"
        if receipt.holding_reviews
        else "完整研究与组合约束审计后，当前没有足够有吸引力的股票需要强行买入。"
    )
    return {
        "conclusion": conclusion,
        "reasons": _unique(reasons),
        "risks": _unique(risks),
        "actions": _unique(holding_actions),
        "change_conditions": _unique(conditions),
    }


def _position_reasons(index: _FullResearchPublicIndex, position: Any) -> list[str]:
    instrument = position.instrument_id
    valuation = index.valuations[instrument]
    ranking = index.rankings[instrument]
    narrative = index.narratives[instrument]
    industry = index.industries[ranking.industry_id]
    scenarios = {item.scenario: item for item in valuation.scenarios}
    values = [
        f"{_code(instrument)}投资逻辑：{_text(narrative.investment_thesis)}",
        f"{_code(instrument)}为什么是现在：{_text(narrative.why_now)}；行业处于{_text(industry.cycle_phase)}阶段。",
        f"{_code(instrument)}：参考价格{_decimal(valuation.current_price)}元；"
        f"悲观/基础/乐观合理价值分别为{_decimal(scenarios['BEAR'].per_share_value)}/"
        f"{_decimal(scenarios['BASE'].per_share_value)}/{_decimal(scenarios['BULL'].per_share_value)}元；"
        f"期望收益{_decimal(valuation.expected_return_mean * 100)}%，"
        f"安全边际{_decimal(valuation.margin_of_safety * 100)}%，"
        f"证据置信度{_decimal(ranking.evidence_confidence * 100)}%。",
    ]
    if valuation.entry_quality_state is not None:
        entry_text = _ENTRY_QUALITY_PUBLIC_TEXT[valuation.entry_quality_state]
        score = (
            f"，入场位置评分{_decimal(valuation.entry_quality_score * 100)}/100"
            if valuation.entry_quality_score is not None
            else ""
        )
        values.append(
            f"{_code(instrument)}入场位置：{entry_text}{score}；该判断只用于建仓节奏。"
        )
    values.extend(f"{_code(instrument)}催化剂：{_text(value)}" for value in narrative.catalysts)
    return values


def _position_risks(index: _FullResearchPublicIndex, position: Any) -> list[str]:
    instrument = position.instrument_id
    narrative = index.narratives[instrument]
    challenger = index.challengers.get(instrument)
    if challenger is None:
        raise ValueError("BUY candidate lacks its independent challenger")
    values = [f"{_code(instrument)}主要风险：{_text(value)}" for value in narrative.primary_risks]
    values.append(
        f"{_code(instrument)}反方：{_text(challenger.market_may_be_right_because)}；"
        f"最大合理下行{_decimal(challenger.maximum_reasonable_downside * 100)}%。"
    )
    if index.financials[instrument].red_flags:
        values.append(f"{_code(instrument)}存在非否决性的财务质量风险点，需要持续复核。")
    if index.governance[instrument].red_flags:
        values.append(f"{_code(instrument)}存在非否决性的治理风险点，需要持续复核。")
    return values


def _position_actions(
    receipt: RecommendationResearchReceipt,
    index: _FullResearchPublicIndex,
    position: Any,
    *,
    existing_holding: bool = False,
) -> tuple[list[str], list[str]]:
    instrument = position.instrument_id
    narrative = index.narratives[instrument]
    execution = index.executions.get(instrument)
    if execution is None:
        raise ValueError("portfolio candidate lacks execution planning")
    conditions = [
        f"{_code(instrument)}逻辑失效：{_text(value)}"
        for value in narrative.thesis_invalidation_conditions
    ]
    actions: list[str] = []
    if execution.buy_range_low is not None and execution.buy_range_high is not None:
        actions.append(
            f"{_code(instrument)}条件买入区间{_decimal(execution.buy_range_low)}-"
            f"{_decimal(execution.buy_range_high)}元，最大可接受价格"
            f"{_decimal(execution.maximum_acceptable_price or execution.buy_range_high)}"
            f"元；目标权重{_decimal(position.target_weight * 100)}%。"
        )
    actions.extend(
        f"{_code(instrument)}加仓条件：{_text(value)}" for value in execution.add_conditions
    )
    actions.extend(
        f"{_code(instrument)}减仓/退出条件：{_text(value)}"
        for value in execution.reduce_exit_conditions
    )
    if execution.goal_entry_price_ceiling is not None:
        actions.append(f"{_code(instrument)}按基础估值情景与目标年化倒推的条件买入价不高于{_decimal(execution.goal_entry_price_ceiling)}元；这不是价格预测，仍需满足盈利、估值与风险条件。")
    if (
        execution.scenario_profit_rmb is not None
        and execution.downside_loss_rmb is not None
        and position.target_amount > 0
    ):
        scenario_return = execution.scenario_profit_rmb / position.target_amount * 100
        downside_return = execution.downside_loss_rmb / position.target_amount * 100
        actions.append(
            f"{_code(instrument)}相对该标的配置额，情景预期收益率约{_decimal(scenario_return)}%，"
            f"下行情景损失比例约{_decimal(downside_return)}%；盈利假设失效时复核退出。"
        )
    actions.append(f"{_code(instrument)}时间退出：{_text(execution.time_stop_condition)}")
    actions.append(f"{_code(instrument)}估值退出：{_text(execution.valuation_exit_condition)}")
    actions.extend(
        f"{_code(instrument)}重大事件退出：{_text(value)}"
        for value in execution.event_exit_conditions
    )
    if receipt.publication.instant_trade_parameters_allowed:
        if execution.initial_shares is None or execution.target_shares is None:
            raise ValueError("instant publication lacks executable share quantities")
        target_weight = _decimal(position.target_weight * 100)
        if existing_holding:
            actions.append(f"{_code(instrument)}结合现有持仓按条件调整至目标仓位{target_weight}%。")
        elif execution.target_shares:
            first_weight = (
                position.target_weight * Decimal(execution.initial_shares)
                / Decimal(execution.target_shares) * 100
            )
            actions.append(
                f"{_code(instrument)}首仓比例约{_decimal(first_weight)}%，目标仓位{target_weight}%。"
            )
    return actions, conditions


def _portfolio_summary(
    receipt: RecommendationResearchReceipt,
    reasons: list[str],
    risks: list[str],
    actions: list[str],
) -> None:
    labels = {
        "VALUE": "价值",
        "QUALITY": "质量",
        "GROWTH": "成长",
        "MOMENTUM": "动量",
        "LOW_VOLATILITY": "低波动",
        "LIQUIDITY": "流动性",
        "SIZE": "规模",
        "EARNINGS_REVISION": "盈利预测修正",
        "PROFITABILITY": "盈利能力",
        "CROWDING": "拥挤度",
    }
    top_factors = sorted(
        receipt.portfolio.factor_exposures.items(), key=lambda item: (-abs(item[1]), item[0])
    )[:3]
    reasons.append(
        f"组合现金权重{_decimal(receipt.portfolio.cash_weight * 100)}%。"
    )
    if top_factors:
        reasons.append(
            "组合主要因子暴露："
            + "、".join(f"{labels.get(name, name)}{_decimal(value)}" for name, value in top_factors)
            + "。"
        )
    if (
        receipt.portfolio.expected_research_return is not None
        and receipt.portfolio.expected_research_profit is not None
        and receipt.portfolio.modeled_downside_loss is not None
    ):
        downside_pct = receipt.portfolio.modeled_downside_loss / receipt.portfolio.capital * 100
        reasons.append(
            f"组合当前估值情景加权预期回报约"
            f"{_decimal(receipt.portfolio.expected_research_return * 100)}%，"
            f"估值下行情景加权损失占组合资产约"
            f"{_decimal(downside_pct)}%。"
        )
    reasons.append(
        f"组合风险：Beta {_decimal(receipt.risk_audit.portfolio_beta)}，"
        f"预期波动{_decimal(receipt.risk_audit.expected_volatility * 100)}%，"
        f"预期短缺{_decimal(receipt.risk_audit.expected_shortfall * 100)}%，"
        f"最大回撤代理{_decimal(receipt.risk_audit.max_drawdown_proxy * 100)}%。"
    )
    if receipt.portfolio.objective_status == "BELOW_HORIZON_TARGET":
        assert receipt.portfolio.return_objective_gap is not None
        gap_percentage_points = max(Decimal("0"), receipt.portfolio.return_objective_gap) * 100
        risks.append(
            f"按当前已通过研究的组合，估值期预期回报距离目标路径仍差约"
            f"{_decimal(gap_percentage_points)}个百分点。继续遵守集中度、流动性和风险上限，"
            "不会为了追求目标年化自动放大杠杆或突破风险约束。"
        )
    elif receipt.portfolio.objective_status == "HORIZON_NOT_COMPARABLE":
        risks.append("各标的估值期限尚未统一或未明确，当前情景盈亏不能直接与目标年化比较；不据此宣称目标可达。")
    elif receipt.portfolio.objective_status == "NO_ELIGIBLE_POSITIONS":
        risks.append("当前没有满足完整研究与风险约束的可配置标的，目标收益暂不具备可执行组合。")
    risks.append("上述情景盈亏已扣除估算建仓费用和滑点，尚未计入卖出税费；下行情景不是最大亏损保证，跳空和流动性不足可能使实际损失更大。")
    if not receipt.publication.instant_trade_parameters_allowed:
        risks.append("当前报价时效不足，执行前需重新核实有效报价。")
    if receipt.rejected_candidates:
        reasons.append(
            "其余候选因估值、质量、治理、财务、证据或组合约束未达标而被保留在淘汰记录中，未为凑数放行。"
        )
    if receipt.portfolio.cash_weight > Decimal("0"):
        actions.append("未配置的仓位保留现金，不用低质量或低流动性标的填仓。")


class VerifiedAnswerProjector:
    def __init__(self, verifier: RegisteredOutputVerifier) -> None:
        self.verifier = verifier

    def inputs(
        self,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
    ) -> VerifiedInputs:
        preflight = InvestorSessionPreflightReceipt.model_validate(preflight.model_dump())
        coverage = CapabilityCoverageReceipt.model_validate(coverage.model_dump())
        if preflight.request_id != coverage.request_id or preflight.freshness != "FRESH":
            raise ValueError("answer projection requires one fresh, authenticated request")
        if (
            content_hash(coverage.model_dump(exclude={"receipt_id", "receipt_hash"}))
            != coverage.receipt_hash
        ):
            raise ValueError("answer projection coverage payload hash differs")
        if not self.verifier.authenticated_preflight(
            preflight
        ) or not self.verifier.authenticated_coverage(
            coverage.request_id, coverage.receipt_id, coverage.receipt_hash
        ):
            raise ValueError("answer projection requires verified persisted coverage")
        request_record = self.verifier.state.artifact_record(
            f"InvestorRequestEnvelope:{content_hash(coverage.request_id)}"
        )
        if request_record is None or request_record["type"] != "InvestorRequestEnvelope":
            raise ValueError("answer projection has no authenticated original request")
        request = InvestorRequestEnvelope.model_validate_json(
            self.verifier.objects.get_bytes(str(request_record["object_hash"]))
        )
        if (
            request.request_id != coverage.request_id
            or coverage.request_fingerprint != content_hash(request)
            or request.normalized_intent != preflight.normalized_intent
            or request.evidence_cutoff != preflight.as_of
        ):
            raise ValueError("answer projection request/intent/cutoff binding differs")
        with self.verifier.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM capability_execution_plans WHERE plan_id=?",
                (coverage.plan_id,),
            ).fetchone()
        if row is None:
            raise ValueError("answer projection execution plan is unavailable")
        plan = CapabilityExecutionPlan.model_validate_json(str(row[0]))
        nodes = {node.capability_id: node for node in plan.nodes}
        outputs: dict[str, tuple[BaseModel, ...]] = {}
        identities: list[str] = []
        hashes = {preflight.receipt_hash, coverage.receipt_hash, str(request_record["object_hash"])}
        for record in coverage.records:
            if record.status not in {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}:
                continue
            node = nodes[record.capability_id]
            self.verifier.verify(node, record.artifact_ids, request, preflight)
            if record.capability_id in _LOCAL:
                continue
            models = []
            for artifact_id in record.artifact_ids:
                models.append(self.verifier.load(artifact_id, output_model(node.output_schema)))
                registered = self.verifier.state.artifact_record(artifact_id)
                if registered is None:
                    raise ValueError("answer source disappeared during verification")
                hashes.add(str(registered["object_hash"]))
                identities.append(artifact_id)
            outputs[record.capability_id] = tuple(models)
        return VerifiedInputs(
            request, preflight, coverage, outputs, tuple(sorted(identities)), tuple(sorted(hashes))
        )

    def derive(
        self,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
    ) -> InvestorAnswerDraft:
        return self._derive(self.inputs(preflight, coverage))

    def freeze(
        self,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
    ) -> tuple[str, InvestorAnswerDraft]:
        inputs = self.inputs(preflight, coverage)
        draft = self._derive(inputs)
        reference = self.verifier.objects.put_json(draft.model_dump(mode="json"))
        artifact_id = f"InvestorAnswerDraft:{reference.sha256}"
        self.verifier.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="InvestorAnswerDraft",
            schema_version=PROJECTION_POLICY,
            object_hash=reference.sha256,
            input_hashes=list(inputs.input_hashes),
        )
        return artifact_id, draft

    def _derive(self, inputs: VerifiedInputs) -> InvestorAnswerDraft:
        request = inputs.request
        if request.normalized_intent is RequestIntent.PAPER_STATUS:
            fields = self._paper_status(inputs)
        elif request.normalized_intent is RequestIntent.PAPER_PREPARE:
            fields = self._paper_prepare(inputs)
        elif request.normalized_intent is RequestIntent.PAPER_CONFIRM:
            fields = self._paper_confirmation(inputs)
        elif request.normalized_intent is RequestIntent.ACCOUNT_FACT_WRITE:
            fields = self._account_operation(inputs)
        elif request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION:
            fields = self._full_research_decision(inputs)
        elif request.normalized_intent is RequestIntent.RESEARCH:
            fields = self._research(inputs)
        elif request.normalized_intent is RequestIntent.MONITOR:
            fields = self._monitor(inputs)
        else:
            # Account writes and paper confirmation have their own signed,
            # post-write receipts. Never turn this read-only projection into an
            # economic executor or imply that a prepared order was filled.
            raise ValueError("this intent requires its canonical economic-operation presenter")
        draft = InvestorAnswerDraft(
            request_id=request.request_id,
            evidence_as_of=inputs.preflight.as_of,
            internal_metadata={
                "projection_policy": PROJECTION_POLICY,
                "source_artifact_ids": list(inputs.artifact_ids),
                "coverage_receipt_id": inputs.coverage.receipt_id,
            },
            **fields,
        )
        visible = [
            draft.conclusion,
            *draft.reasons,
            *draft.risks,
            *draft.actions,
            *draft.change_conditions,
            draft.actual_holding_section or "",
            draft.paper_holding_section or "",
        ]
        audit = audit_public_answer("\n".join(part for part in visible if part))
        if "PRIVATE_CAPITAL_AMOUNT_EXPOSED" in audit.finding_codes:
            raise CapitalDisclosureError()
        if not audit.safe_to_send:
            raise ValueError("verified domain output failed the public presentation audit")
        return draft

    @staticmethod
    def _account_operation(inputs: VerifiedInputs) -> dict[str, Any]:
        receipts = inputs.outputs.get("EXTERNAL_ACCOUNT", ())
        if len(receipts) != 1 or not isinstance(receipts[0], ExternalAccountOperationReceipt):
            raise ValueError("account operation requires one canonical account-operation receipt")
        receipt = receipts[0]
        if receipt.request_id != inputs.request.request_id:
            raise ValueError("account operation receipt belongs to another request")
        if receipt.status == "PROVISIONAL":
            precision_names = {
                "DATE_ONLY": "只精确到日期",
                "MONTH_ONLY": "只精确到月份",
                "UNKNOWN": "时间精度未知",
                "EXACT": "精确时间",
            }
            rendered = "、".join(
                precision_names.get(value, value) for value in receipt.date_precisions
            )
            conclusion = "这笔持仓声明已暂存为待核实事实，没有伪造成精确成交记录。"
            reasons = (
                f"当前声明的时间精度为{rendered or '未确定'}。",
                "待补足精确成交事实后，才会进入正式账户成交事件。",
            )
        elif receipt.status == "NO_CHANGE":
            conclusion = "这笔账户事实已存在，本次没有重复增加持仓或现金。"
            reasons = ("系统按原有幂等身份复用了已有记录。",)
        else:
            conclusion = "这笔账户事实已按追加记录方式保存。"
            reasons = (
                f"本次记录了{len(receipt.event_artifact_ids)}条账户事实，并保留原历史记录。",
            )
        return {
            "conclusion": conclusion,
            "reasons": reasons,
            "risks": (
                "账户记录依据本次用户声明或已核实账户事实，不替代券商对账单。",
                "本次处理不会写入模拟账户，也不会向真实券商发单。",
            ),
            "actions": (),
            "change_conditions": (
                "如账户、数量、价格或时间信息有误，应追加更正记录而不是覆盖历史。",
            ),
        }

    @staticmethod
    def _paper_prepare(inputs: VerifiedInputs) -> dict[str, Any]:
        receipts = inputs.outputs.get("PAPER", ())
        if len(receipts) != 1 or not isinstance(receipts[0], PaperPreparationReceipt):
            raise ValueError("paper preparation requires one canonical preparation receipt")
        receipt = receipts[0]
        if receipt.request_id != inputs.request.request_id:
            raise ValueError("paper preparation receipt belongs to another request")
        if receipt.status == "NEEDS_INFO":
            names = {
                "LIMIT_PRICE": "委托价格",
                "ORDER_TYPE": "订单类型",
                "ACCOUNT": "模拟账户",
                "INSTRUMENT": "证券",
            }
            missing = "、".join(names.get(value, value) for value in receipt.missing_fields)
            return {
                "conclusion": "模拟买入请求尚未形成订单。",
                "reasons": (f"还需要确认{missing}，因此没有替你补造交易参数。",),
                "risks": ("未确认的模拟请求不是订单，也不是持仓或成交。",),
                "actions": (),
                "change_conditions": ("补齐交易参数并通过独立确认后，才可进入模拟订单流程。",),
            }
        detail = "模拟订单请求已准备，但尚未确认、尚未成交。"
        if receipt.instrument_id and receipt.quantity:
            detail = (
                f"{_code(receipt.instrument_id)}的模拟订单请求已准备，"
                "但尚未确认、尚未成交。"
            )
        return {
            "conclusion": detail,
            "reasons": ("准备阶段不会创建持仓；只有后续确认并发生模拟成交后才更新持仓。",),
            "risks": ("模拟交易不代表真实账户表现，也不会向真实券商发单。",),
            "actions": (),
            "change_conditions": ("独立确认过期、参数变化或账户约束变化时必须重新准备。",),
        }

    @staticmethod
    def _paper_confirmation(inputs: VerifiedInputs) -> dict[str, Any]:
        reports = inputs.outputs.get("PAPER", ())
        if len(reports) != 1 or not isinstance(reports[0], PaperOperationReport):
            raise ValueError("paper confirmation requires one canonical operation report")
        report = reports[0]
        status = report.status.value
        return {
            "conclusion": f"模拟操作确认结果为{status}。",
            "reasons": ("结果来自已确认的模拟操作记录；是否形成持仓仍以模拟成交为准。",),
            "risks": ("模拟操作不会向真实券商发单。",),
            "actions": (),
            "change_conditions": ("订单状态、成交或结算发生变化后重新查看模拟账户。",),
        }

    @staticmethod
    def _paper_replay_status(
        inputs: VerifiedInputs, report: ReplayExecutionReport
    ) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        lane = inputs.preflight.context.paper
        account = inputs.request.account_id
        if account is None:
            if len(lane.account_ids) != 1:
                raise ValueError("paper replay account selection is ambiguous")
            account = lane.account_ids[0]
        if report.account_id != account or account not in lane.account_ids:
            raise ValueError("paper replay account differs from the restored portfolio")
        checkpoint = report.checkpoint
        if checkpoint is None or checkpoint.coverage_end is None:
            raise ValueError("paper replay has no verified coverage endpoint")
        label = _code(f"{report.market.value}:{report.symbol}")
        covered = checkpoint.coverage_end.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y年%m月%d日 %H:%M"
        )
        outcome = (
            f"本次回放产生{len(report.fill_ids)}笔模拟成交。"
            if report.fill_ids
            else "本次检查未新增模拟成交。"
        )
        risks = (
            "小时行情回放属于近似模拟，不能证明盘口排队或小时内成交先后；"
            "路径存在实质歧义时需用五分钟行情复核。"
            if checkpoint.actual_resolution == "60m"
            else "五分钟行情模拟仍不能证明真实盘口成交，模拟结果不等于实盘成交。"
        )
        return {
            "conclusion": f"已核对{label}的已确认模拟订单，{outcome}",
            "reasons": (
                f"已处理{report.processed_bars}根新增行情柱，回放覆盖至{covered}（北京时间）。",
                "本次只核对既有模拟订单，没有新建订单或修改委托价格；重复检查不会重复成交。",
            ),
            "risks": (risks,),
            "change_conditions": ("行情缺失、确认或费用规则不匹配时应停止回放并复核。",),
            "actions": (),
        }

    @staticmethod
    def _paper_status(inputs: VerifiedInputs) -> dict[str, Any]:
        outputs = inputs.outputs.get("PAPER", ())
        if len(outputs) != 1:
            raise ValueError("paper status needs exactly one canonical paper result")
        if isinstance(outputs[0], ReplayExecutionReport):
            return VerifiedAnswerProjector._paper_replay_status(inputs, outputs[0])
        if not isinstance(outputs[0], PortfolioNAV):
            raise ValueError("paper status requires a registered account NAV or replay report")
        nav = outputs[0]
        lane = inputs.preflight.context.paper
        selected_account = inputs.request.account_id
        if selected_account is None:
            if len(lane.account_ids) != 1:
                raise ValueError(
                    "paper account selection is ambiguous; an explicit account is required"
                )
            selected_account = lane.account_ids[0]
        if selected_account not in lane.account_ids or nav.account_id != selected_account:
            raise ValueError("paper NAV does not belong to the selected account")
        cash = lane.cash_by_account.get(selected_account)
        if cash is None or cash != Decimal(nav.cash_fen) / 100:
            raise ValueError(
                "paper NAV cash is inconsistent with the authenticated account snapshot"
            )
        if nav.nav_fen != (
            nav.cash_fen
            + nav.frozen_cash_fen
            + nav.market_value_fen
            + nav.receivable_fen
            - nav.payable_fen
        ):
            raise ValueError("paper NAV accounting identity does not reconcile")
        if (
            len(lane.account_ids) == 1
            and lane.frozen_cash is not None
            and (Decimal(nav.frozen_cash_fen) / 100 != lane.frozen_cash)
        ):
            raise ValueError("paper frozen cash differs from the account snapshot")
        orders = [item for item in lane.open_orders if item.account_id == nav.account_id]
        positions = [item for item in lane.positions if item.account_id == nav.account_id]
        reasons = ["账户状态来自同一时点已核对的模拟记录；公开展示使用比例。"]
        if orders:
            reasons.append(f"有{len(orders)}笔未完成模拟订单；订单尚未成交的部分不计为持仓。")
        if nav.frozen_cash_fen:
            reasons.append(
                f"模拟订单冻结资金占账户资产"
                f"{_decimal(Decimal(nav.frozen_cash_fen) / Decimal(nav.nav_fen) * 100)}%。"
                if nav.nav_fen > 0
                else "存在模拟订单冻结资金；净资产非正时不计算资金比例。"
            )
        section = None
        if positions:
            section = (
                "模拟持仓："
                + "；".join(
                    _code(position.instrument_id) for position in positions
                )
                + "。"
            )
        return {
            "conclusion": (
                f"该模拟账户可用现金占账户资产"
                f"{_decimal(Decimal(nav.cash_fen) / Decimal(nav.nav_fen) * 100)}%。"
                if nav.nav_fen > 0
                else "该模拟账户净资产非正，暂不计算资金比例，应先核对账户风险。"
            ),
            "reasons": _unique(reasons),
            "risks": ("模拟结果不代表真实账户表现。",),
            "actions": (),
            "change_conditions": ("账户发生已确认的资金或成交变化后重新核对。",),
            "paper_holding_section": section,
        }

    @staticmethod
    def _research(inputs: VerifiedInputs) -> dict[str, Any]:
        models = inputs.outputs.get("COMPANY_RESEARCH", ())
        if not models or not all(isinstance(item, InstitutionalDecisionContext) for item in models):
            raise ValueError("company research has no completed institutional decision context")
        reasons: list[str] = []
        risks: list[str] = []
        conditions: list[str] = []
        for model in models:
            assert isinstance(model, InstitutionalDecisionContext)
            company = _code(model.company_id)
            reasons.append(f"{company}：{_text(model.draft.investment_thesis.statement)}")
            reasons.append(_text(model.draft.variant_perception.statement))
            risks.extend(
                _text(hypothesis.statement) for hypothesis in model.draft.competing_hypotheses
            )
            conditions.append(
                f"研究期限至{model.draft.decision_horizon_end.isoformat()}；关键假设变化时复核。"
            )
        if not risks:
            raise ValueError("company research has no explicit competing hypothesis")
        return {
            "conclusion": "现有资料支持以下公司研究判断，但这不是对当前买入时点或成交价格的确认。",
            "reasons": _unique(reasons),
            "risks": _unique(risks),
            "actions": (),
            "change_conditions": _unique(conditions),
        }

    @staticmethod
    def _full_research_decision(inputs: VerifiedInputs) -> dict[str, Any]:
        receipt = _full_research_receipt(inputs)
        reasons = _full_research_base_reasons(receipt)
        if not receipt.portfolio.positions:
            return _full_research_no_buy(receipt, reasons)
        index = _full_research_index(receipt)
        holding_reasons, holding_risks, holding_actions, holding_conditions = (
            _holding_review_fields(receipt)
        )
        reasons.extend(holding_reasons)
        risks: list[str] = list(holding_risks)
        actions: list[str] = list(holding_actions)
        conditions: list[str] = list(holding_conditions)
        holding_ids = {item.instrument_id for item in receipt.holding_reviews}
        names: list[str] = []
        for position in receipt.portfolio.positions:
            names.append(_code(position.instrument_id))
            reasons.extend(_position_reasons(index, position))
            risks.extend(_position_risks(index, position))
            position_actions, position_conditions = _position_actions(
                receipt,
                index,
                position,
                existing_holding=position.instrument_id in holding_ids,
            )
            actions.extend(position_actions)
            conditions.extend(position_conditions)
        _portfolio_summary(receipt, reasons, risks, actions)
        return {
            "conclusion": (
                f"完整研究闭环通过后，模型组合当前保留{len(receipt.portfolio.positions)}只候选："
                f"{'、'.join(names)}。"
                + ("现有持仓处置已纳入同一份完整研究结论。" if receipt.holding_reviews else "")
            ),
            "reasons": _unique(reasons),
            "risks": _unique(risks),
            "actions": _unique(actions),
            "change_conditions": _unique(conditions),
        }

    @staticmethod
    def _monitor(inputs: VerifiedInputs) -> dict[str, Any]:
        outputs = inputs.outputs.get("EVENT_RESEARCH", ())
        if not outputs or not all(isinstance(item, ResearchRoleOutput) for item in outputs):
            raise ValueError("monitor answer requires completed event research")
        return {
            "conclusion": "已完成这次事件资料的研究复核，主要变化与影响如下。",
            "reasons": _unique(
                [_text(item.summary) for item in outputs if isinstance(item, ResearchRoleOutput)]
            ),
            "risks": (
                "事件影响仍取决于后续正式披露、执行进度和经营变化；未经确认的信息不作为交易事实。",
            ),
            "actions": ("结合已确认的新事实复核原有研究判断。",),
            "change_conditions": ("正式公告修订或关键经营假设发生变化时重新评估。",),
        }
