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
# Only unambiguous issuer-subject clauses qualify. A company word somewhere in a
# personal recommendation must not become a privacy bypass for the rest of a sentence.
_ISSUER_PREFIX = re.compile(
    r"^(?:上市公司|该公司|公司|发行人|企业|集团|控股股东|大股东|实际控制人|股东)"
    r"(?:(?:预计|预期|披露|实现|累计|合计|本期|报告期|本年度|去年|今年|目前|"
    r"直接|间接|已|共|仍|净|上半年|下半年|年度)|[\d年月日\s:：()（）]){0,24}$"
)


def _scan_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).replace("**", "").replace("`", "")
    return _INVISIBLE.sub("", value)


def _is_private_match(index: int, match: re.Match[str], text: str) -> bool:
    if index < 2:
        return True  # Explicit principal/account labels never inherit an issuer exemption.
    if index == 3 and not match.group(0).startswith("持有"):
        return True  # A recommendation is not an issuer-ownership disclosure.
    prefix = re.split(r"[\n。；;，,]", text[max(0, match.start() - 96):match.start()])[-1]
    return _ISSUER_PREFIX.fullmatch(prefix.strip()) is None


def capital_disclosure_findings(text: str) -> tuple[str, ...]:
    """Return an amount-free finding; thresholds and a specific principal are irrelevant."""
    normalized = _scan_text(text)
    for index, pattern in enumerate(_PATTERNS):
        if any(
            _is_private_match(index, match, normalized) for match in pattern.finditer(normalized)
        ):
            return ("PRIVATE_CAPITAL_AMOUNT_EXPOSED",)
    return ()


def redact_capital_amounts(text: str) -> str:
    """Redact personal funds while preserving unambiguous public issuer facts."""
    if not capital_disclosure_findings(text):
        return text
    normalized = _scan_text(text)
    for index, pattern in enumerate(_PATTERNS):
        current = normalized
        normalized = pattern.sub(
            lambda match, index=index, current=current: (
                "账户金额或数量已隐藏"
                if _is_private_match(index, match, current)
                else match.group(0)
            ),
            current,
        )
    return normalized


class CapitalDisclosureError(ValueError):
    """Public presentation needs privacy repair, not additional research evidence."""

    def __init__(self) -> None:
        super().__init__("Public account information must be hidden before presentation")
