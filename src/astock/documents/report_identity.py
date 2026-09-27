"""Report-title semantics shared by official capture and financial selection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from astock.schemas import FinancialPeriodType


class ReportCompleteness(StrEnum):
    FULL = "FULL"
    SUMMARY = "SUMMARY"
    UNKNOWN = "UNKNOWN"


class ReportRevisionStatus(StrEnum):
    ORIGINAL = "ORIGINAL"
    REVISION = "REVISION"
    CORRECTION = "CORRECTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReportTitleIdentity:
    period_match: bool
    completeness: ReportCompleteness
    revision_status: ReportRevisionStatus


def compact_report_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower().strip()
    return re.sub(r"[\s\-_·•]+", "", normalized)


_ALLOWED_REPORT_TRAILING = re.compile(
    r"^(?:"
    r"(?:[（(]?(?:修订(?:版|稿)?|更正(?:版|稿)?|更新版|全文|正文)[）)]?)|"
    r"(?:[([]?(?:revised|revision|amended|restated|corrected|correction)[)\]]?)|"
    r"(?:andaccounts)"
    r")*(?:\.(?:pdf|docx?|txt))?$"
)


# Protocol-level wrappers identify documents *about* a periodic report rather than the
# periodic report itself. This is centralized here so adapters share one semantic rule.
_NON_PRIMARY_REPORT_PREFIX = re.compile(
    r"(?:"
    r"关于|延期(?:披露)?|取消|撤回|审议|问询|回复|意见|决议|公告|说明|提示|核查|"
    r"监管(?:函)?|独立董事|董事会|监事会|审计委员会|"
    r"regarding|notice|resolution|opinion|response|inquiry|query|delay(?:ed)?|"
    r"postpon(?:e|ed|ement)|cancel(?:led|lation)?|withdraw(?:al|n)?|announcement"
    r")",
    re.IGNORECASE,
)


def _primary_report_prefix_allowed(prefix: str) -> bool:
    return not prefix or _NON_PRIMARY_REPORT_PREFIX.search(prefix) is None


def _is_primary_report_title(
    compact: str,
    chinese_patterns: tuple[str, ...],
    english_tokens: tuple[str, ...],
) -> bool:
    """Require the period report phrase to be the title's terminal semantic object."""

    for pattern in chinese_patterns:
        for match in re.finditer(pattern, compact):
            if (
                _primary_report_prefix_allowed(compact[: match.start()])
                and _ALLOWED_REPORT_TRAILING.fullmatch(compact[match.end() :])
            ):
                return True
    for token in english_tokens:
        start = 0
        while True:
            index = compact.find(token, start)
            if index < 0:
                break
            if (
                _primary_report_prefix_allowed(compact[:index])
                and _ALLOWED_REPORT_TRAILING.fullmatch(compact[index + len(token) :])
            ):
                return True
            start = index + 1
    return False


def report_completeness(title: str) -> ReportCompleteness:
    compact = compact_report_title(title)
    if "摘要" in compact or "summary" in compact:
        return ReportCompleteness.SUMMARY
    if "报告" in compact or "report" in compact:
        return ReportCompleteness.FULL
    return ReportCompleteness.UNKNOWN


def report_revision_status(title: str) -> ReportRevisionStatus:
    compact = compact_report_title(title)
    if any(token in compact for token in ("更正", "corrected", "correction")):
        return ReportRevisionStatus.CORRECTION
    if any(
        token in compact
        for token in ("修订", "revised", "revision", "amended", "restated")
    ):
        return ReportRevisionStatus.REVISION
    if report_completeness(title) is not ReportCompleteness.UNKNOWN:
        return ReportRevisionStatus.ORIGINAL
    return ReportRevisionStatus.UNKNOWN


def analyze_report_title(
    title: str,
    period_end: date,
    period_type: FinancialPeriodType,
) -> ReportTitleIdentity:
    compact = compact_report_title(title)
    year = str(period_end.year)
    chinese_patterns: tuple[str, ...]
    english_tokens: tuple[str, ...]
    if period_type is FinancialPeriodType.ANNUAL:
        chinese_patterns = (rf"{year}年年度(?:财务)?报告",)
        english_tokens = (
            f"{year}annualreport",
            f"annualreport{year}",
        )
    elif period_type is FinancialPeriodType.SEMIANNUAL:
        chinese_patterns = (
            rf"{year}年(?:半年度|中期)报告",
        )
        english_tokens = (
            f"{year}interimreport",
            f"{year}semiannualreport",
            f"{year}halfyearreport",
            f"interimreport{year}",
            f"semiannualreport{year}",
            f"halfyearreport{year}",
        )
    else:
        if period_end.month == 3:
            chinese_patterns = (rf"{year}年(?:第一|第1|一|1)季度报告",)
            english_tokens = (
                f"{year}firstquarterreport",
                f"{year}firstquarterlyreport",
                f"{year}1stquarterreport",
                f"{year}q1report",
                f"firstquarterreport{year}",
                f"firstquarterlyreport{year}",
                f"1stquarterreport{year}",
                f"q1report{year}",
            )
        elif period_end.month == 9:
            chinese_patterns = (rf"{year}年(?:第三|第3|三|3)季度报告",)
            english_tokens = (
                f"{year}thirdquarterreport",
                f"{year}thirdquarterlyreport",
                f"{year}3rdquarterreport",
                f"{year}q3report",
                f"thirdquarterreport{year}",
                f"thirdquarterlyreport{year}",
                f"3rdquarterreport{year}",
                f"q3report{year}",
            )
        else:
            chinese_patterns = ()
            english_tokens = ()

    period_match = _is_primary_report_title(compact, chinese_patterns, english_tokens)
    return ReportTitleIdentity(
        period_match=period_match,
        completeness=report_completeness(title),
        revision_status=report_revision_status(title),
    )


def is_full_report_title(
    title: str,
    period_end: date,
    period_type: FinancialPeriodType,
) -> bool:
    identity = analyze_report_title(title, period_end, period_type)
    return identity.period_match and identity.completeness is ReportCompleteness.FULL


__all__ = [
    "ReportCompleteness",
    "ReportRevisionStatus",
    "ReportTitleIdentity",
    "analyze_report_title",
    "compact_report_title",
    "is_full_report_title",
    "report_completeness",
    "report_revision_status",
]
