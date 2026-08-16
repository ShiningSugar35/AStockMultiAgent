"""Investor-facing research presentation with a strict developer-noise boundary."""

from __future__ import annotations

import re

from astock.research.internal_vocabulary import internal_vocabulary_terms
from astock.schemas.research_acquisition import (
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    InvestorAnswerAudit,
    InvestorGapCategory,
    InvestorResearchState,
    InvestorResearchView,
)
from astock.schemas.research_runtime import ResearchRunReport, ResearchRunStatus

_MAX_INVESTOR_ANSWER_CHARS = 2600
_MAX_INVESTOR_BULLETS = 18

_GAP_MESSAGES: dict[InvestorGapCategory, str] = {
    InvestorGapCategory.EVIDENCE: (
        "有些会影响投资判断的关键事实还需要从公司公告或正式报告进一步核实。"
    ),
    InvestorGapCategory.FINANCIAL: "最新一期财务数据还需要和公司正式披露逐项核对。",
    InvestorGapCategory.FUNDAMENTAL_MODEL: (
        "目前还缺少足够可靠的依据来把未来盈利和合理估值区间算得足够稳健。"
    ),
    InvestorGapCategory.BASE_CASE: "核心投资逻辑、反方情形和关键假设还需要进一步收敛。",
    InvestorGapCategory.SPECIALIST: "个别会影响结论的专项问题仍需补充证据。",
    InvestorGapCategory.KNOWLEDGE: "现有材料还不足以覆盖一个关键判断维度。",
    InvestorGapCategory.COMMITTEE: "现有证据还不足以支持明确的买入时点结论。",
    InvestorGapCategory.EXECUTION_READINESS: (
        "如果要给出具体买入价、止损或卖出条件，还需要核实最新交易条件。"
    ),
    InvestorGapCategory.GENERAL: "还有一项会影响投资判断的材料尚未完成核实。",
}

_ANSWER_POLICY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "INTERNAL_PROTOCOL_TERM_EXPOSED",
        (
            r"\bmarketpriceanchor\b",
            r"\bclassifiedtradeprotocol\b",
            r"\binstrumentreferencerelease\b",
            r"\bfrozenevidencepack\b",
            r"\bevidencepack\b",
            r"\bbasecase(?:pack)?\b",
            r"\btradingclassification\b",
            r"\binvestment committee\b",
            r"投资委员会",
            r"投委会",
            r"(?<![A-Za-z])PIT(?![A-Za-z])",
        ),
    ),
    (
        "CLI_OR_PIPELINE_EXPOSED",
        (
            r"research-plan",
            r"research-acquire-current",
            r"research-run-company",
            r"uv run astock",
            r"current_stage",
            r"\bworkflow\b",
            r"\bskill\b",
        ),
    ),
    (
        "RAW_MACHINE_STATE_EXPOSED",
        (
            r"needs_info",
            r"claim_ids_required",
            r"evidence_pack_required",
            r"trade_protocol_outcome",
            r"approve_simulation",
            r"paper_ledger",
            r"broker_execution",
        ),
    ),
    (
        "STORAGE_OR_ARTIFACT_TERM_EXPOSED",
        (
            r"artifact_id",
            r"object_hash",
            r"snapshot_id",
            r"\bsqlite\b",
            r"\bmigration\b",
            r"source_snapshot",
            r"manifest_object",
        ),
    ),
    (
        "PROVIDER_DIAGNOSTIC_EXPOSED",
        (
            r"\bbaostock\b",
            r"eastmoney-reference",
            r"eastmoney-financial",
            r"sina-financial",
            r"instrument identity",
        ),
    ),
    (
        "DEVELOPER_META_EXPOSED",
        (
            r"这套系统",
            r"当前系统",
            r"系统的风格",
            r"系统设计",
            r"调试工程师",
            r"内部实现",
            r"后台日志",
            r"正式链",
            r"\bphase\s*\d+\b",
        ),
    ),
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
            headline="现有材料已经比较完整，可以直接讨论公司质量、估值、赔率与主要风险。",
            plain_language_gaps=[],
            next_step="直接给出投资逻辑、合理估值区间、关键风险和需要继续观察的条件。",
            created_at=report.created_at,
        )

    messages: list[str] = []
    categories = report.investor_gap_categories or [InvestorGapCategory.GENERAL]
    for category in categories:
        if (
            category is InvestorGapCategory.EXECUTION_READINESS
            and not include_execution_readiness
        ):
            continue
        message = _GAP_MESSAGES[category]
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("还有一项会影响买入时点判断的关键信息需要进一步核实。")
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.DECISION_NOT_CERTIFIED,
        headline=(
            "目前还缺少少量会影响买入时点判断的关键信息，"
            "我会先继续补齐可自动获取的公开资料。"
        ),
        plain_language_gaps=messages,
        next_step=(
            "先继续核对自动数据源和权威网页资料；只有自动渠道都无法完成时，"
            "再一次性列出需要你协助提供的材料。"
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
            gaps.append("核心资料已经够用，但还有少量辅助信息值得继续核实。")
        return InvestorResearchView(
            company_id=report.company_id,
            state=InvestorResearchState.EVIDENCE_READY,
            headline="当前分析所需的核心公开资料已经拿到，可以继续判断盈利趋势和估值是否有吸引力。",
            plain_language_gaps=gaps,
            next_step="继续分析盈利驱动、估值区间、关键催化、主要风险和可能推翻判断的条件。",
            created_at=report.decision_as_of,
        )
    gaps = [item.research_question for item in report.external_research_needs]
    if report.manual_actions:
        gaps.extend(item.instruction for item in report.manual_actions)
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.EVIDENCE_STILL_COLLECTING,
        headline="还有几项影响判断的公开资料没有核实完，我会继续从权威来源补齐。",
        plain_language_gaps=gaps or ["仍有公开研究材料需要补齐。"],
        next_step=(
            "优先从交易所、法定披露平台、公司官网和监管机构继续核实；"
            "只有这些自动渠道也无法解决时，再向你一次性请求协助。"
        ),
        created_at=report.decision_as_of,
    )


def audit_investor_answer(text: str) -> InvestorAnswerAudit:
    """Fail closed when a normal investor answer leaks developer/runtime vocabulary."""

    finding_codes: set[str] = set()
    stripped = text.strip()
    if not stripped:
        finding_codes.add("EMPTY_INVESTOR_ANSWER")
    if len(stripped) > _MAX_INVESTOR_ANSWER_CHARS:
        finding_codes.add("INVESTOR_ANSWER_TOO_LONG")
    bullet_count = sum(
        line.lstrip().startswith(("- ", "* ", "• "))
        for line in stripped.splitlines()
    )
    if bullet_count > _MAX_INVESTOR_BULLETS:
        finding_codes.add("INVESTOR_ANSWER_TOO_MANY_BULLETS")
    sentences = [
        re.sub(r"\s+", "", item)
        for item in re.split(r"[。！？!?；;\n]+", stripped)
        if len(re.sub(r"\s+", "", item)) >= 16
    ]
    if len(sentences) != len(set(sentences)):
        finding_codes.add("INVESTOR_ANSWER_REPETITIVE")
    for code, patterns in _ANSWER_POLICY_PATTERNS:
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            finding_codes.add(code)
    lowered = text.lower()
    if any(term in lowered for term in internal_vocabulary_terms()):
        finding_codes.add("DYNAMIC_INTERNAL_VOCABULARY_EXPOSED")
    ordered = sorted(finding_codes)
    developer_meta = "DEVELOPER_META_EXPOSED" in finding_codes
    implementation = bool(finding_codes - {"DEVELOPER_META_EXPOSED", "EMPTY_INVESTOR_ANSWER"})
    return InvestorAnswerAudit(
        status="FAIL" if ordered else "PASS",
        finding_codes=ordered,
        internal_implementation_exposed=implementation,
        developer_meta_exposed=developer_meta,
    )


__all__ = [
    "audit_investor_answer",
    "investor_view_from_acquisition",
    "investor_view_from_run",
]
