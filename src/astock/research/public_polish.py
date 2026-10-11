"""Shared editorial pass for ordinary investor-facing Agent, report and QQ prose.

This module is not a research/valuation engine and never grants publication authority.
It removes administrative narration, redacts private data, and keeps investment claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from astock.research.capital_privacy import redact_capital_amounts
from astock.research.presentation_policy import PresentationPolicy, load_presentation_policy
from astock.research.report_style import explain_terms_at_first_use, public_status_text

_SECRET_PATTERNS = (
    r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s，。；;（）()\[\]]+",
    r"(?i)bearer\s+[A-Za-z0-9._~+\-/]+=*",
    r"(?i)cookie\s*[:=]\s*[^\s，。；;（）()\[\]]+",
    r"(?i)(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s，。；;（）()\[\]]+",
)
PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:\\(?:[^\\\s，。；;（）()\[\]]+\\)*Users\\[^\s，。；;（）()\[\]]+"
    r"|/home/[^\s，。；;（）()\[\]]+|/Users/[^\s，。；;（）()\[\]]+)"
)
_SECTION = re.compile(
    r"^\s*#{1,6}\s*(?:研究边界|后台运行记录|内部运行状态|"
    r"任务编排记录|QQ投递明细|内部技术说明)\s*$",
    re.I,
)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_META_START = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:QQ投递|QQ群送达|投递前先|"
    r"本轮账户只读核验|截至本轮读取机器状态|"
    r"正式推荐收据|技术运行状态|系统运行记录|"
    r"研究边界|内部状态|全部\d+只身份均来自当前registry)"
    r"\s*[:：，]?",
    re.I,
)
_META_TEXT = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:report_id|DeliveryLedger|SourceSnapshot)(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])(?:FinancialSourceRelease|Publication Gate)(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])(?:artifact_id|snapshot_id|research-(?:plan|run-company|acquire-current))(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z])(?:NEEDS_INFO|CERTIFIED|QUARTERLY|UNKNOWN|PARTIAL)(?![A-Za-z])|"
    r"(?:不下真实|不下模拟|不生成持仓收益|不生成实际持仓|不下单)|"
    r"(?:未增加账户持仓|报告投递|原始证据已认证))",
    re.I,
)
# These two are process descriptions, even if they mention a large count.
_PROCESSED_META = re.compile(
    r"^(?:本轮|本次)(?:账户只读核验|机器状态核验|系统状态检查|"
    r"读取机器状态|已对账的运行回执)|^截至本轮读取机器状态",
)
_PHRASE_REWRITES: tuple[tuple[str, str], ...] = (
    ("部分官方公告索引尚未逐项完整枚举", "部分官方公告尚待核实"),
    ("部分官方公告索引尚未完整枚举", "部分官方公告尚待核实"),
    ("部分官方公告索引尚未逐项核对", "部分官方公告尚待核实"),
    ("尚未作为项目正式官方SourceSnapshot/FinancialSourceRelease完成入库", "尚待进一步核实"),
    ("二级公司原文转载、新闻核对线索，不等于项目官方证据已认证", "相关消息仍需以公司正式披露为准"),
)
_STANDALONE_STYLE = re.compile(
    r"^(?:未增加账户持仓|不生成实际持仓|不生成持仓收益|不下单|"
    r"不下真实/模拟订单|不对.*作伪保证|不是.*正式Portfolio)$",
    re.I,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；])")
# Preserve quoted primary facts and cited material; do not drop an investor risk
# simply because a string is unfamiliar.
_MATERIAL = re.compile(
    r"(?:看涨|看跌|买入|卖出|加仓|减仓|持有|观望|继续观察|"
    r"现金流|利润|盈利|盈亏|亏损|估值|目标价|合理价值|增长|收入|"
    r"毛利|回撤|风险|负债|股息|分红|市盈率|市净率|"
    r"三季报|半年报|年报|季度报告|正式财报|股价|"
    r"\b\d{6}\b|\d+(?:\.\d+)?(?:元|%|％|亿元|万元|倍|股))"
)


@dataclass(frozen=True, slots=True)
class PublicPolishResult:
    text: str
    changed: bool
    removed_process_segments: int
    sensitive_values_hidden: int


def _redact(text: str, *, request_text: str) -> tuple[str, int]:
    safe = redact_capital_amounts(text, request_text=request_text)
    hidden = int(safe != text)
    for pattern in _SECRET_PATTERNS:
        safe, count = re.subn(pattern, "", safe)
        hidden += count
    safe, count = PRIVATE_PATH_PATTERN.subn("", safe)
    return safe, hidden + count


def _edit_segment(segment: str) -> tuple[str, int]:
    text = segment.strip()
    if not text:
        return "", 0
    for old, new in _PHRASE_REWRITES:
        text = text.replace(old, new)
    # Process labels may prefix genuine investment risks; trim the label only.
    removed_label = 0
    marker = _META_START.match(text)
    if marker:
        remaining = text[marker.end():].strip().strip("：，、；;").strip()
        if not remaining or not _MATERIAL.search(remaining):
            return "", 1
        text = remaining
        removed_label = 1
    if _PROCESSED_META.match(text):
        return "", 1
    if _STANDALONE_STYLE.fullmatch(text.strip().strip("。；，、")):
        return "", 1
    if not _META_TEXT.search(text):
        return text, removed_label

    # Split off an administrative suffix while keeping the preceding thesis.
    match = re.search(
        r"(?:QQ投递|本轮账户只读核验|截至本轮读取机器状态|"
        r"(?:报告|消息)投递|(?:本轮|本次)系统状态)[:：，]?",
        text,
        re.I,
    )
    if match and match.start():
        prefix = text[: match.start()].rstrip("，、；; ")
        if prefix and _MATERIAL.search(prefix):
            return prefix, 1

    # An unrecognized technical clause can also contain an economic claim.
    # Remove only known internal substrings, never discard that claim.
    if _MATERIAL.search(text):
        text = re.sub(
            r"(?i)(?<![A-Za-z0-9_])(?:SourceSnapshot|FinancialSourceRelease|DeliveryLedger|"
            r"Publication Gate|report_id|artifact_id|snapshot_id|"
            r"research-plan|research-run-company|research-acquire-current)(?![A-Za-z0-9_])"
            r"(?:[:=][\w:./-]+)?",
            "",
            text,
        )
        text = re.sub(r"(?i)\b(?:CERTIFIED|QUARTERLY|NEEDS_INFO)\b", "", text)
        text = re.sub(
            r"(?:不下真实/模拟订单|不生成实际持仓或持仓收益|"
            r"不生成持仓收益|不下单)(?:[，、；;]\s*)?",
            "",
            text,
        )
        return text.strip("，、；; "), 1
    return "", 1


def polish_public_text(
    text: str,
    *,
    policy: PresentationPolicy | None = None,
    request_text: str = "",
    explain_terms: bool = True,
) -> PublicPolishResult:
    """Lightly revise public prose without blocking delivery for style alone.

    The upstream research/completeness gate remains authoritative. This editor
    never changes a buy/sell direction or invents a price, financial fact or source.
    """
    selected = policy or load_presentation_policy()
    source = str(text).replace("\r\n", "\n").replace("\r", "\n")
    redacted, hidden = _redact(source, request_text=request_text)
    lines: list[str] = []
    removed = 0
    in_internal_section = False
    for raw in redacted.split("\n"):
        if _SECTION.match(raw):
            in_internal_section = True
            removed += 1
            continue
        if in_internal_section:
            if _HEADING.match(raw):
                in_internal_section = False
            elif not _MATERIAL.search(raw) or _META_START.match(raw):
                if raw.strip():
                    removed += 1
                continue
            # A technical heading does not authorize removing a business
            # risk, citation or financial result embedded in that section.
        if not raw.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        segments = _SENTENCE_BOUNDARY.split(public_status_text(raw))
        kept: list[str] = []
        for segment in segments:
            revised, count = _edit_segment(segment)
            removed += count
            if revised:
                kept.append(revised)
        revised_line = "".join(kept).strip()
        if revised_line:
            lines.append(revised_line)
    while lines and not lines[-1]:
        lines.pop()
    output = "\n".join(lines).strip()
    if not output:
        output = "当前暂无可展示的投资结论。"
    if explain_terms:
        output = "\n".join(
            explain_terms_at_first_use(output.split("\n"), selected.term_explanations)
        )
    output = re.sub(r"\n{3,}", "\n\n", output)
    return PublicPolishResult(
        text=output,
        changed=(output != text),
        removed_process_segments=removed,
        sensitive_values_hidden=hidden,
    )


__all__ = ["PRIVATE_PATH_PATTERN", "PublicPolishResult", "polish_public_text"]
