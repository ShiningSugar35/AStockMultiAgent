"""Public capital-disclosure checks; never modifies private research/account facts."""

from __future__ import annotations

import re
import unicodedata

# Match labelled personal capital, not securities prices or issuer financial data.
_NUMBER = (
    r"(?:[+\-−]?\d[\d,_]*(?:\.\d+)?(?:[eE][+\-]?\d+)?\s*[wWkK万亿千]?|"
    r"[零〇一二两三四五六七八九十百千万亿]+)"
)
_LABEL = (
    r"(?:本金(?:金额|数额)?|投资本金|投资预算|可用资金|可投资金额|投入金额|资金规模|"
    r"账户(?:资产|净值|余额|资金|现金)|个人(?:资产|资金)|您的资金|你的资金|我的资金|"
    r"capital[_ ]rmb|principal(?: amount)?|investment capital|portfolio value)"
)
_ACCOUNT_AMOUNT_LABEL = (
    r"(?:组合(?:保留)?现金|(?:可用|剩余|保留|冻结|计划配置)资金|现金(?:余额|金额)|"
    r"(?:全年|年度|阶段|预期|目标|情景)?(?:盈利|盈亏)(?:金额)?|"
    r"(?:估值)?下行情景(?:加权)?损失)"
)
_GAP = r"[\s\"'`*|:=：]*(?:(?:为|是|约|大约|按|共计|合计|总计)\s*)*(?:(?:人民币|RMB|CNY|[¥￥])\s*)?"
_END = r"(?:\s*(?:人民币|元|块钱|RMB|CNY))?(?![\d,.%％‰])"
_MONEY_UNIT = r"\s*(?:人民币|元|块钱|RMB|CNY)(?![\d%％‰])"
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_PATTERNS = (
    re.compile(_LABEL + _GAP + _NUMBER + _END, re.IGNORECASE),
    re.compile(_NUMBER + r"\s*(?:人民币|元|万元|块钱)\s*(?:的|作为|作)?\s*本金", re.IGNORECASE),
    re.compile(_ACCOUNT_AMOUNT_LABEL + _GAP + _NUMBER + _MONEY_UNIT, re.IGNORECASE),
    re.compile(
        r"(?:建议(?:买入|卖出)?|买入|卖出|首仓|目标(?:数量)?|当前数量|持有|持仓数量)"
        + _GAP + _NUMBER + r"\s*股(?!的?整数倍|为?交易单位)",
        re.IGNORECASE,
    ),
)


def _scan_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).replace("**", "").replace("`", "")
    return _INVISIBLE.sub("", value)


def capital_disclosure_findings(text: str) -> tuple[str, ...]:
    """Return an amount-free finding; thresholds and a specific principal are irrelevant."""
    normalized = _scan_text(text)
    if any(pattern.search(normalized) for pattern in _PATTERNS):
        return ("PRIVATE_CAPITAL_AMOUNT_EXPOSED",)
    return ()


def redact_capital_amounts(text: str) -> str:
    """Redact labelled capital in diagnostics without touching security-price figures."""
    if not capital_disclosure_findings(text):
        return text
    normalized = _scan_text(text)
    for pattern in _PATTERNS:
        normalized = pattern.sub("账户金额或数量已隐藏", normalized)
    return normalized


class CapitalDisclosureError(ValueError):
    """Public presentation needs privacy repair, not additional research evidence."""

    def __init__(self) -> None:
        super().__init__("Public account information must be hidden before presentation")
