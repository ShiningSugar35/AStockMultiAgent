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
from astock.research.presentation import audit_public_answer
from astock.schemas.committee import DecisionPack, TradeProtocol
from astock.schemas.external_accounts import ExternalAccountOperationReceipt
from astock.schemas.institutional_research import InstitutionalDecisionContext
from astock.schemas.knowledge import HoldingReviewPack
from astock.schemas.paper import (
    PaperOperationReport,
    PaperPreparationReceipt,
    PortfolioNAV,
    ReplayExecutionReport,
)
from astock.schemas.portfolio import PortfolioAnalysisReport
from astock.schemas.research_runtime import ClassifiedTradeProtocol
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
        elif request.normalized_intent in {
            RequestIntent.BUY_DECISION,
            RequestIntent.RECOMMENDATION,
        }:
            fields = self._investment_decision(inputs)
        elif request.normalized_intent is RequestIntent.HOLDING_DECISION:
            fields = self._holding(inputs)
        elif request.normalized_intent is RequestIntent.PORTFOLIO_DECISION:
            fields = self._portfolio(inputs)
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
        if not audit_public_answer("\n".join(part for part in visible if part)).safe_to_send:
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
                f"{_code(receipt.instrument_id)}的{receipt.quantity}股模拟订单请求已准备，"
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
        reasons = ["资金数额来自同一时点已核对的模拟账本。"]
        if orders:
            reasons.append(f"有{len(orders)}笔未完成模拟订单；订单尚未成交的部分不计为持仓。")
        if nav.frozen_cash_fen:
            reasons.append(
                f"另有{_decimal(Decimal(nav.frozen_cash_fen) / 100)}元为模拟订单冻结资金。"
            )
        section = None
        if positions:
            section = (
                "模拟持仓："
                + "；".join(
                    f"{_code(position.instrument_id)}，{_decimal(position.quantity)}股"
                    for position in positions
                )
                + "。"
            )
        return {
            "conclusion": f"该模拟账户可用现金为{_decimal(cash)}元。",
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

    def _investment_decision(self, inputs: VerifiedInputs) -> dict[str, Any]:
        classified = inputs.outputs.get("COMMITTEE", ())
        if not classified or not all(
            isinstance(item, ClassifiedTradeProtocol) for item in classified
        ):
            raise ValueError("investment decision requires canonical classified protocols")
        reasons: list[str] = []
        risks: list[str] = []
        actions: list[str] = []
        conditions: list[str] = []
        conclusions: list[str] = []
        for protocol in classified:
            assert isinstance(protocol, ClassifiedTradeProtocol)
            committee = self.verifier.load(protocol.committee_protocol_artifact_id, TradeProtocol)
            decision = self.verifier.load(protocol.decision_pack_artifact_id, DecisionPack)
            assert isinstance(committee, TradeProtocol) and isinstance(decision, DecisionPack)
            if (
                committee.company_id != protocol.company_id
                or decision.company_id != protocol.company_id
                or committee.decision_id != decision.decision_id
                or committee.decision_sha256 != decision.decision_sha256
            ):
                raise ValueError("investment conclusion has inconsistent frozen decision lineage")
            self.verifier._check_time(
                committee.model_dump(mode="json"), inputs.request.evidence_cutoff
            )
            self.verifier._check_time(
                decision.model_dump(mode="json"), inputs.request.evidence_cutoff
            )
            company = _code(protocol.company_id)
            outcome = protocol.final_outcome.value
            if outcome == "NEEDS_INFO":
                raise ValueError("investment decision is not certified by the domain gate")
            if outcome == "REJECT":
                conclusions.append(f"{company}暂不纳入买入候选。")
            elif outcome == "WATCH":
                conclusions.append(f"{company}目前以观察为主，不确认当前买入。")
            elif outcome == "APPROVE_SIMULATION":
                if (
                    committee.protocol_status.value != "ACTIVE"
                    or not committee.paper_simulation_allowed
                    or committee.verdict.value != "PAPER_ELIGIBLE"
                    or decision.verdict != committee.verdict
                    or protocol.blocking_codes
                    or committee.blocking_codes
                    or decision.hard_blocks
                    or decision.needs_info_task_ids
                ):
                    raise ValueError(
                        "positive decision conflicts with its authoritative admission gates"
                    )
                conclusions.append(f"{company}可作为条件式买入候选，仍须逐项满足入场条件。")
                conditions.append(f"{company}入场条件：{_text(committee.entry_rule)}")
                actions.append(f"{company}：{_text(committee.position_size_rule)}")
            else:
                raise ValueError("unsupported authoritative investment outcome")
            contexts = [
                value
                for value in inputs.outputs.get("COMPANY_RESEARCH", ())
                if isinstance(value, InstitutionalDecisionContext)
                and self.verifier._same_identity(value.company_id, protocol.company_id)
            ]
            if len(contexts) != 1 or not isinstance(contexts[0], InstitutionalDecisionContext):
                raise ValueError(
                    "investment decision has no unique evidence-bound company narrative"
                )
            context = contexts[0]
            reasons.append(f"{company}：{_text(context.draft.investment_thesis.statement)}")
            if not context.draft.competing_hypotheses:
                raise ValueError("investment decision has no evidence-bound opposing case")
            risks.extend(
                f"{company}：{_text(hypothesis.statement)}"
                for hypothesis in context.draft.competing_hypotheses
            )
            conditions.append(f"{company}失效条件：{_text(committee.thesis_invalidation_rule)}")
            conditions.append(f"{company}应在{decision.review_at.date().isoformat()}前复核。")
        risks.append("情景判断和研究条件不是收益保证，也不代表已经下单或成交。")
        return {
            "conclusion": "".join(conclusions),
            "reasons": _unique(reasons),
            "risks": _unique(risks),
            "actions": _unique(actions),
            "change_conditions": _unique(conditions),
        }

    @staticmethod
    def _portfolio(inputs: VerifiedInputs) -> dict[str, Any]:
        reports = inputs.outputs.get("PORTFOLIO", ())
        if len(reports) != 1 or not isinstance(reports[0], PortfolioAnalysisReport):
            raise ValueError("portfolio answer requires one coherent registered risk report")
        report = reports[0]
        if report.status.value == "EMPTY":
            raise ValueError("empty portfolio does not certify personalized allocation")
        if report.metrics is None:
            raise ValueError("portfolio metrics are unavailable")
        metrics = report.metrics
        reasons = [
            "按冻结组合和历史样本计算的年化波动为"
            f"{_decimal(metrics.annualized_volatility * 100)}%。",
            f"同一样本期最大回撤为{_decimal(abs(metrics.max_drawdown) * 100)}%，"
            "不代表未来损失上限。",
        ]
        ordered = sorted(
            report.assets, key=lambda asset: (-asset.risk_contribution_fraction, asset.company_id)
        )
        if ordered:
            reasons.append(
                f"{_code(ordered[0].company_id)}的风险贡献相对较大，需要结合公司研究一起复核。"
            )
        return {
            "conclusion": "当前组合的风险特征如下；风险统计本身不足以确定买卖标的或精确调整仓位。",
            "reasons": tuple(reasons),
            "risks": ("历史相关性和波动会变化，压力期的分散效果可能减弱。",),
            "actions": (),
            "change_conditions": ("持仓、价格数据或关键公司事实变化后，重新计算并评估调整成本。",),
        }

    @staticmethod
    def _holding(inputs: VerifiedInputs) -> dict[str, Any]:
        reviews = inputs.outputs.get("HOLDING_REVIEW", ())
        if not reviews or not all(isinstance(item, HoldingReviewPack) for item in reviews):
            raise ValueError("holding answer requires completed canonical holding reviews")
        labels = {
            "HOLD": "继续持有并跟踪",
            "ADD": "复核加仓条件",
            "TRIM": "复核减仓条件",
            "EXIT": "复核退出条件",
            "REVIEW": "先复核持仓依据",
        }
        thesis_labels = {
            "UNCHANGED": "核心投资假设暂未发生已确认变化",
            "STRENGTHENED": "新增证据强化了核心投资假设",
            "WEAKENED": "新增证据削弱了核心投资假设",
            "UNRESOLVED": "核心投资假设仍需补充证据后复核",
        }
        risk_labels = {
            "UNCHANGED": "已确认风险暂未发生实质变化",
            "HIGHER": "已确认风险较上次复核有所上升",
            "UNKNOWN": "当前证据不足以确认风险方向",
        }
        conclusions: list[str] = []
        reasons: list[str] = []
        conditions: list[str] = []
        for review in reviews:
            assert isinstance(review, HoldingReviewPack)
            action = labels.get(review.recommended_action.value)
            if action is None:
                raise ValueError("holding review does not contain a recognized action decision")
            conclusions.append(action)
            thesis_change = thesis_labels.get(review.thesis_strength_change)
            risk_change = risk_labels.get(review.risk_change)
            if thesis_change is None or risk_change is None:
                raise ValueError("holding review contains an unsupported public state label")
            reasons.append(thesis_change)
            reasons.append(risk_change)
            conditions.extend(_text(value) for value in review.next_review_conditions)
            conditions.extend(_text(value) for value in review.preconditions)
            conditions.extend(_text(value) for value in review.reversal_conditions)
        if not conditions:
            raise ValueError("holding review has no explicit change conditions")
        return {
            "conclusion": "；".join(conclusions) + "。",
            "reasons": _unique(reasons),
            "risks": ("持仓成本不能替代公司估值；实际账户和模拟账户应分别处理。",),
            "actions": _unique(conclusions),
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
