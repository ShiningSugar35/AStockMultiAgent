"""Context-aware privacy checks for personalized account/holding facts.

Planning assumptions, recommendation sizing, security prices and issuer facts are public
research content.  Only verified personal account/holding amounts are hidden by default;
when the current user explicitly asks for such details, the corresponding category may be
shown.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

_NUMBER = (
    r"(?:[+\-−]?\d[\d,_]*(?:\.\d+)?(?:[eE][+\-]?\d+)?\s*[wWkK万亿千]?|"
    r"[零〇一二两三四五六七八九十百千万亿]+)"
)
_GAP = (
    r"[\s\"'`*|:=：]*(?:(?:为|是|约|大约|按|共计|合计|总计)\s*)*"
    r"(?:(?:人民币|RMB|CNY|[¥￥])\s*)?"
)
_END = r"(?:\s*(?:人民币|元|万元|块钱|RMB|CNY))?(?![\d,.%％‰])"
_MONEY = _GAP + _NUMBER + _END
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")

# These labels describe private account/holding facts.  Deliberately absent: generic
# 本金/投资预算/目标盈利/建议买入/目标数量, which are planning/recommendation facts.
_PRIVATE_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "holding_principal": (
        re.compile(
            r"(?:持仓本金|持仓成本|持仓投入|实际持仓成本|实际投入成本|持仓成本金额)"
            + _MONEY,
            re.IGNORECASE,
        ),
        re.compile(
            _NUMBER
            + r"\s*(?:人民币|元|万元|块钱|RMB|CNY)\s*(?:的)?\s*"
            r"(?:持仓本金|持仓成本|持仓投入)",
            re.IGNORECASE,
        ),
    ),
    "holding_pnl": (
        re.compile(
            r"(?:持仓盈亏|持仓盈利|持仓亏损|浮动盈亏|浮动盈利|浮动亏损|"
            r"浮盈|浮亏|已实现盈亏|未实现盈亏|实际盈亏|账户盈亏)"
            + _MONEY,
            re.IGNORECASE,
        ),
    ),
    "account_amount": (
        re.compile(
            r"(?:(?:模拟|实盘|真实)?账户(?:资产|净值|余额|资金|现金|可用现金)|"
            r"账户可用资金|账户总资产|账户总市值|现金余额)"
            + _MONEY,
            re.IGNORECASE,
        ),
    ),
    "holding_quantity": (
        re.compile(
            r"(?:当前持仓数量|持仓数量|当前数量|实际持有|当前持有|账户持有|"
            r"我的持仓|我[^。；，,]{0,18}持有)"
            + _GAP
            + _NUMBER
            + r"\s*股(?!的?整数倍|为?交易单位)",
            re.IGNORECASE,
        ),
    ),
}

_REQUEST_TERMS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "holding_principal": (
        re.compile(r"(?:持仓|我的持仓|账户).{0,10}(?:本金|成本|投入)", re.IGNORECASE),
        re.compile(r"(?:本金|成本).{0,10}(?:多少|是多少|情况|明细)", re.IGNORECASE),
    ),
    "holding_pnl": (
        re.compile(
            r"(?:持仓|账户|我的|我).{0,12}(?:盈亏|浮盈|浮亏|盈利|亏损|赚|亏)",
            re.IGNORECASE,
        ),
        re.compile(r"(?:赚了多少|亏了多少|盈亏多少)", re.IGNORECASE),
    ),
    "account_amount": (
        re.compile(
            r"(?:账户|模拟账户|实盘账户|我的账户).{0,10}(?:资产|净值|余额|现金|可用资金|资金)",
            re.IGNORECASE,
        ),
        re.compile(r"(?:余额|现金|净值|总资产).{0,10}(?:多少|是多少|情况|明细)", re.IGNORECASE),
    ),
    "holding_quantity": (
        re.compile(
            r"(?:持仓|账户|我的|我).{0,12}(?:股数|数量|多少股|持有)", re.IGNORECASE
        ),
        re.compile(r"(?:持有多少股|现在有多少股|当前多少股)", re.IGNORECASE),
    ),
}
_ISSUER_SUBJECT_PREFIX = re.compile(
    r"^(?:上市公司|该公司|公司|发行人|企业|集团|控股股东|大股东|实际控制人|股东)"
    r"[^。；;，,\n]{0,28}$",
    re.IGNORECASE,
)


def _is_issuer_fact(text: str, start: int) -> bool:
    clause = re.split(r"[。；;，,\n]", text[max(0, start - 96):start])[-1].strip()
    return _ISSUER_SUBJECT_PREFIX.fullmatch(clause) is not None


_BROAD_PERSONAL_DETAIL_REQUEST = re.compile(
    r"(?:我的)?(?:持仓|账户|模拟账户|实盘账户).{0,12}"
    r"(?:情况|状态|明细|详情|数据|信息|怎么样|如何|看看|查看|汇总)",
    re.IGNORECASE,
)


def _scan_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).replace("**", "").replace("`", "")
    return _INVISIBLE.sub("", value)


def requested_personal_detail_categories(request_text: str | None) -> frozenset[str]:
    """Return account/holding detail categories explicitly requested in this turn."""
    if not request_text:
        return frozenset()
    normalized = _scan_text(request_text)
    if _BROAD_PERSONAL_DETAIL_REQUEST.search(normalized):
        return frozenset(_PRIVATE_PATTERNS)
    requested = {
        category
        for category, patterns in _REQUEST_TERMS.items()
        if any(pattern.search(normalized) for pattern in patterns)
    }
    # A user who supplied the private fact in the current request has explicitly mentioned it.
    for category, patterns in _PRIVATE_PATTERNS.items():
        if any(pattern.search(normalized) for pattern in patterns):
            requested.add(category)
    return frozenset(requested)


def capital_disclosure_findings(
    text: str,
    *,
    request_text: str | None = None,
) -> tuple[str, ...]:
    """Flag only unrequested personal account/holding amounts or quantities."""
    normalized = _scan_text(text)
    requested = requested_personal_detail_categories(request_text)
    for category, patterns in _PRIVATE_PATTERNS.items():
        if category in requested:
            continue
        for pattern in patterns:
            if any(
                not _is_issuer_fact(normalized, match.start())
                for match in pattern.finditer(normalized)
            ):
                return ("PRIVATE_CAPITAL_AMOUNT_EXPOSED",)
    return ()


def redact_capital_amounts(
    text: str,
    *,
    request_text: str | None = None,
) -> str:
    """Redact only unrequested personal account/holding facts."""
    if not capital_disclosure_findings(text, request_text=request_text):
        return text
    normalized = _scan_text(text)
    requested = requested_personal_detail_categories(request_text)
    for category, patterns in _PRIVATE_PATTERNS.items():
        if category in requested:
            continue
        for pattern in patterns:
            current = normalized
            normalized = pattern.sub(
                lambda match, current=current: (
                    match.group(0)
                    if _is_issuer_fact(current, match.start())
                    else "账户个性化数值已隐藏"
                ),
                current,
            )
    return normalized


class CapitalDisclosureError(ValueError):
    """Public presentation needs privacy repair, not additional research evidence."""

    def __init__(self) -> None:
        super().__init__("Unrequested personal account information must be hidden")
