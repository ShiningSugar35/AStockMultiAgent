"""Small, local Chinese prose checks; never rewrite investment claims or call a model."""

from __future__ import annotations

import re
from collections.abc import Mapping

# Adapted design patterns, not copied upstream skill text. See the WP-24 scouting note.
_QUOTES = re.compile(r"“[^”\n]*”|「[^」\n]*」|https?://[^\s）)]+")
_STATUS_LABEL = re.compile(
    r"^\s*(?:状态\s*[:：]\s*)?`?(NEEDS?_INFO|PUBLIC_DATA_UNAVAILABLE|BLOCKED|UNAVAILABLE|PENDING|READY)`?\s*[。.]?\s*$",
    re.I,
)

# Investor-facing terminology translation. Internal artifact/state names remain
# available in developer diagnostics but never appear as public prose. Scenario
# tokens use bounded regexes so ordinary English words are not accidentally edited.
_PUBLIC_TERM_PATTERNS = (
    (re.compile(r"(?<![A-Za-z])(?:BearCase|BEAR_CASE|Bear Case)(?![A-Za-z])", re.I), "空头看跌"),
    (re.compile(r"(?<![A-Za-z])(?:BaseCase|BASE_CASE|Base Case)(?![A-Za-z])", re.I), "基准情景"),
    (re.compile(r"(?<![A-Za-z])(?:BullCase|BULL_CASE|Bull Case)(?![A-Za-z])", re.I), "多头看涨"),
    (re.compile(r"(?<![A-Za-z])BEAR(?=\s*[:：]?\s*[-+]?\d)"), "空头看跌"),
    (re.compile(r"(?<![A-Za-z])BASE(?=\s*[:：]?\s*[-+]?\d)"), "基准情景"),
    (re.compile(r"(?<![A-Za-z])BULL(?=\s*[:：]?\s*[-+]?\d)"), "多头看涨"),
    (re.compile(r"冻结估值"), "估值"),
    (re.compile(r"冻结价格"), "价格"),
    (re.compile(r"冻结数据"), "数据"),
    (re.compile(r"冻结快照"), "当前资料"),
    (re.compile(r"冻结证据"), "现有证据"),
)


def public_status_text(text: str) -> str:
    """Translate standalone machine states only; arbitrary diagnostic sentences stay rejected.

    NEEDS_INFO alone does not prove that public recovery was exhausted or that
    private input is needed. The caller must state a genuine manual gap explicitly.
    """
    for pattern, target in _PUBLIC_TERM_PATTERNS:
        text = pattern.sub(target, text)
    match = _STATUS_LABEL.fullmatch(text)
    if match is None:
        return text
    status = match[1].upper()
    if status in {"NEED_INFO", "NEEDS_INFO"}:
        return "信息尚未核实完整，暂不形成投资结论。"
    if status in {"BLOCKED", "UNAVAILABLE", "PUBLIC_DATA_UNAVAILABLE"}:
        return "本次尚未取得足以支持判断的可靠资料，暂不形成投资结论。"
    if status == "READY":
        return "研究材料已整理，投资判断仍需结合估值与风险。"
    return "相关事项尚待核实。"


def manual_gap_text(*, recovery_exhausted: bool, private_input_required: bool) -> str:
    if recovery_exhausted and private_input_required:
        return "信息缺失，且无法通过公开资料自动补齐，需要您协助提供。"
    return "现有信息仍有缺口，暂不形成投资结论。"


def style_findings(text: str, patterns: tuple[str, ...]) -> tuple[str, ...]:
    # Source quotations and URLs are not prose to paraphrase. Real uncertainty,
    # negation, units, citations and economic terminology must remain intact.
    prose = _QUOTES.sub("", text)
    prose = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">"))
    found = {"CHINESE_STYLE_TEMPLATE_CONTRAST" for pattern in patterns if re.search(pattern, prose)}
    if len(re.findall(r"——|—", prose)) >= 3:
        found.add("CHINESE_STYLE_EXCESSIVE_DASHES")
    if prose.count("**") >= 12:
        found.add("CHINESE_STYLE_EXCESSIVE_BOLD")
    if re.search(r"[\U0001f300-\U0001faff]", prose):
        found.add("CHINESE_STYLE_DECORATIVE_EMOJI")
    return tuple(sorted(found))


def terminology_note(text: str, explanations: Mapping[str, str]) -> str | None:
    """Add an explanation once, without injecting data, confidence or an investment view."""
    missing: list[str] = []
    for term, explanation in explanations.items():
        pattern = (
            rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])" if term.isascii() else re.escape(term)
        )
        if not re.search(pattern, text, re.I):
            continue
        if explanation in text or re.search(pattern + r"[（(][^）)\n]{2,120}[）)]", text, re.I):
            continue
        missing.append(f"{term}指{explanation}")
    return "术语说明：" + "；".join(missing) + "。" if missing else None
