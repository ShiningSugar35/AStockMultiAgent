"""Investor-facing research state presentation without backend implementation noise."""

from __future__ import annotations

from astock.schemas.research_acquisition import (
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    InvestorResearchState,
    InvestorResearchView,
)
from astock.schemas.research_runtime import ResearchRunReport, ResearchRunStatus

_GAP_MESSAGES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("EVIDENCE", "CLAIM"), "关键投资事实的正式证据还没有全部核实。"),
    (("FINANCIAL",), "最新财务数据仍需完成权威来源核对。"),
    (("FUNDAMENTAL_MODEL", "INSTITUTIONAL"), "盈利驱动、预测和估值模型还没有完成正式冻结。"),
    (("BASE_CASE",), "公司的基础研究底稿还没有形成可复用的正式版本。"),
    (("SPECIALIST",), "专项研究仍有一部分没有完成。"),
    (("KNOWLEDGE",), "研究方法库当前没有完整就绪。"),
    (("COMMITTEE",), "投资委员会还没有形成正式决策。"),
    (("TRADING_CLASSIFICATION", "CLASSIFICATION"), "交易执行条件尚未完成核验。"),
)


def investor_view_from_run(
    report: ResearchRunReport,
    *,
    include_execution_readiness: bool = False,
) -> InvestorResearchView:
    if report.status is ResearchRunStatus.COMPLETE:
        return InvestorResearchView(
            company_id=report.company_id,
            state=InvestorResearchState.DECISION_READY,
            headline="正式研究链已经完成，可以基于研究结论讨论是否值得配置。",
            plain_language_gaps=[],
            next_step="直接展示投资逻辑、估值区间、主要风险和正式决策，不展示后台工件编号。",
            created_at=report.created_at,
        )

    messages: list[str] = []
    for code in report.needs_info_codes:
        if not include_execution_readiness and any(
            token in code for token in ("TRADING_CLASSIFICATION", "CLASSIFICATION")
        ):
            continue
        message = _message_for_code(code)
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("正式投资结论所需的研究材料仍在补全。")
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.DECISION_NOT_CERTIFIED,
        headline="目前还不能把研究结果认证为正式买入结论，但系统会继续自动补齐缺失材料。",
        plain_language_gaps=messages,
        next_step=(
            "先完成自动数据源和权威网页来源的补充检索；只有自动渠道都无法完成时，"
            "再一次性列出需要你协助的材料。"
        ),
        created_at=report.created_at,
    )


def investor_view_from_acquisition(
    report: CurrentResearchAcquisitionReport,
) -> InvestorResearchView:
    if report.status in {
        CurrentResearchAcquisitionStatus.READY,
        CurrentResearchAcquisitionStatus.DEGRADED,
    }:
        gaps = []
        if report.status is CurrentResearchAcquisitionStatus.DEGRADED:
            gaps.append("核心研究数据已经取得，但有非核心执行/辅助数据仍可继续补强。")
        return InvestorResearchView(
            company_id=report.company_id,
            state=InvestorResearchState.EVIDENCE_READY,
            headline="当前研究所需的核心公开数据已经自动取得，可以继续做基本面和估值分析。",
            plain_language_gaps=gaps,
            next_step="以采集结束后的统一决策快照时间继续预测、估值和委员会流程。",
            created_at=report.decision_as_of,
        )
    gaps = [item.research_question for item in report.external_research_needs]
    if report.manual_actions:
        gaps.extend(item.instruction for item in report.manual_actions)
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.EVIDENCE_STILL_COLLECTING,
        headline="本地数据源尚未覆盖全部核心材料，下一步应自动转向权威网页来源继续检索。",
        plain_language_gaps=gaps or ["仍有公开研究材料需要补齐。"],
        next_step=(
            "优先检索交易所、法定披露平台、发行人官网和监管机构；"
            "只有这些自动渠道也失败时，再向你一次性请求人工协助。"
        ),
        created_at=report.decision_as_of,
    )


def _message_for_code(code: str) -> str:
    for tokens, message in _GAP_MESSAGES:
        if any(token in code for token in tokens):
            return message
    return "还有一项正式研究材料尚未完成核验。"


__all__ = ["investor_view_from_acquisition", "investor_view_from_run"]
