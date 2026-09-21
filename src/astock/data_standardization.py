"""Deterministic normalization for current market and financial research inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from astock.schemas.financial import (
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialUnit,
)
from astock.schemas.research_sla import CanonicalMarketSnapshot, StandardizedFinancialMetrics

_NULL_TEXT = {"", "-", "--", "N/A", "NA", "NONE", "NULL", "NAN"}

_FACT_KEYS: dict[FinancialFieldCode, str] = {
    FinancialFieldCode.TOTAL_ASSETS: "TOTAL_ASSETS",
    FinancialFieldCode.TOTAL_LIABILITIES: "TOTAL_LIABILITIES",
    FinancialFieldCode.REVENUE: "REVENUE",
    FinancialFieldCode.NET_PROFIT_INCOME: "NET_PROFIT",
    FinancialFieldCode.NET_CASH_OPERATING: "NET_CASH_OPERATING",
}

_FINANCIAL_UNIT_MULTIPLIERS: dict[FinancialUnit, Decimal] = {
    FinancialUnit.CNY: Decimal("1"),
    FinancialUnit.THOUSAND_CNY: Decimal("1000"),
    FinancialUnit.TEN_THOUSAND_CNY: Decimal("10000"),
    FinancialUnit.MILLION_CNY: Decimal("1000000"),
    FinancialUnit.HUNDRED_MILLION_CNY: Decimal("100000000"),
    FinancialUnit.SHARES: Decimal("1"),
}

_DEFAULT_QUOTE_ALIASES: dict[str, tuple[str, ...]] = {
    "price": ("price", "last", "close", "current", "last_price"),
    "pe_ttm": ("pe_ttm", "pe", "per", "pettm"),
    "pb_mrq": ("pb_mrq", "pb", "pbr"),
    "market_cap_cny": ("market_cap_cny", "market_cap", "total_mv", "total_market_cap"),
    "turnover_cny": ("turnover_cny", "amount", "turnover", "成交额"),
}

_CRITICAL_FINANCIAL_METRICS: dict[FinancialIndustryProfile, frozenset[str]] = {
    FinancialIndustryProfile.GENERAL_INDUSTRIAL: frozenset(
        {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "REVENUE", "NET_PROFIT", "NET_CASH_OPERATING"}
    ),
    FinancialIndustryProfile.BANK: frozenset(
        {
            "TOTAL_ASSETS",
            "TOTAL_LIABILITIES",
            "NET_PROFIT",
            "NET_INTEREST_MARGIN",
            "NON_PERFORMING_LOAN_RATIO",
            "PROVISION_COVERAGE_RATIO",
        }
    ),
    FinancialIndustryProfile.RESOURCE_MINING: frozenset(
        {
            "REVENUE",
            "NET_PROFIT",
            "NET_CASH_OPERATING",
            "PRODUCTION_VOLUME",
            "UNIT_COST",
        }
    ),
    FinancialIndustryProfile.INSURANCE: frozenset(
        {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "NET_PROFIT"}
    ),
    FinancialIndustryProfile.SECURITIES: frozenset(
        {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "NET_PROFIT"}
    ),
    FinancialIndustryProfile.REAL_ESTATE: frozenset(
        {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "REVENUE", "NET_PROFIT"}
    ),
    FinancialIndustryProfile.EARLY_BIOTECH: frozenset({"TOTAL_ASSETS", "NET_CASH_OPERATING"}),
    FinancialIndustryProfile.OTHER: frozenset(),
}


def parse_decimal(value: object, *, percent_as_ratio: bool = False) -> Decimal | None:
    """Parse common public-provider numeric forms without guessing absent values."""

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (Decimal, int, float)):
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    text = str(value).strip()
    if text.upper() in _NULL_TEXT:
        return None
    text = text.replace(",", "").replace("，", "")
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    # Parenthesized accounting values are negative.
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    if not result.is_finite():
        return None
    if negative:
        result = -result
    if is_percent and percent_as_ratio:
        result /= Decimal("100")
    return result


def normalize_provider_snapshot(
    *,
    provider_id: str,
    instrument_id: str,
    payload: Mapping[str, object],
    aliases: Mapping[str, Sequence[str]] | None = None,
    scales: Mapping[str, Decimal] | None = None,
) -> CanonicalMarketSnapshot:
    """Normalize provider quote fields into one canonical seed snapshot.

    Alias selection is deterministic: the first present, parseable alias wins. Missing
    values stay missing rather than becoming zero. Provider adapters may pass their
    native alias map and unit scales; generic defaults only cover common field names.
    """

    alias_map = {key: tuple(value) for key, value in (aliases or _DEFAULT_QUOTE_ALIASES).items()}
    scale_map = dict(scales or {})
    values: dict[str, Decimal | None] = {}
    source_fields: dict[str, str] = {}
    missing: list[str] = []
    for canonical in _DEFAULT_QUOTE_ALIASES:
        resolved: Decimal | None = None
        resolved_field: str | None = None
        for field in alias_map.get(canonical, _DEFAULT_QUOTE_ALIASES[canonical]):
            if field not in payload:
                continue
            parsed = parse_decimal(payload[field])
            if parsed is None:
                continue
            parsed *= scale_map.get(canonical, Decimal("1"))
            if canonical == "price" and parsed <= 0:
                continue
            if canonical in {"market_cap_cny", "turnover_cny"} and parsed < 0:
                continue
            resolved = parsed
            resolved_field = field
            break
        values[canonical] = resolved
        if resolved_field is None:
            missing.append(canonical)
        else:
            source_fields[canonical] = resolved_field
    return CanonicalMarketSnapshot(
        provider_id=provider_id,
        instrument_id=instrument_id,
        price=values["price"],
        pe_ttm=values["pe_ttm"],
        pb_mrq=values["pb_mrq"],
        market_cap_cny=values["market_cap_cny"],
        turnover_cny=values["turnover_cny"],
        source_fields=source_fields,
        missing_fields=sorted(missing),
    )


def build_financial_fact_pack(
    *,
    company_id: str,
    industry_profile: FinancialIndustryProfile,
    raw_metrics: Mapping[str, object],
    metric_aliases: Mapping[str, Sequence[str]] | None = None,
    metric_scales: Mapping[str, Decimal] | None = None,
    percent_metrics: Sequence[str] = (),
) -> StandardizedFinancialMetrics:
    """Normalize already-located financial facts and expose industry-specific gaps.

    This function does not locate values in PDFs and does not invent missing facts. It
    is intended to sit after a provider/parser has identified a fact and before model
    construction, replacing ad-hoc LLM cleaning of number formats and aliases.
    """

    aliases = {key: tuple(value) for key, value in (metric_aliases or {}).items()}
    scales = dict(metric_scales or {})
    percent_set = set(percent_metrics)
    alias_source_fields = {field for fields in aliases.values() for field in fields}
    canonical_keys = (
        (set(raw_metrics) - alias_source_fields)
        | set(aliases)
        | set(_CRITICAL_FINANCIAL_METRICS[industry_profile])
    )
    metrics: dict[str, Decimal] = {}
    ignored: list[str] = []
    for canonical in sorted(canonical_keys):
        candidates = aliases.get(canonical, (canonical,))
        parsed: Decimal | None = None
        saw_value = False
        for field in candidates:
            if field not in raw_metrics:
                continue
            saw_value = True
            parsed = parse_decimal(raw_metrics[field], percent_as_ratio=canonical in percent_set)
            if parsed is not None:
                parsed *= scales.get(canonical, Decimal("1"))
                break
        if parsed is not None:
            metrics[canonical] = parsed
        elif saw_value:
            ignored.append(canonical)
    missing = sorted(_CRITICAL_FINANCIAL_METRICS[industry_profile] - set(metrics))
    return StandardizedFinancialMetrics(
        company_id=company_id,
        industry_profile=industry_profile.value,
        metrics=metrics,
        missing_critical_metrics=missing,
        ignored_non_numeric_fields=sorted(set(ignored)),
    )


def standardize_financial_facts(
    *,
    company_id: str,
    industry_profile: FinancialIndustryProfile,
    facts: Sequence[FinancialFact],
) -> StandardizedFinancialMetrics:
    """Project certified facts into the industry preflight metric namespace.

    The latest period for each supported canonical field wins. Industry-specific metrics
    that are not part of the generic FinancialFact contract remain explicitly missing so
    callers can route them to a specialized extractor/LLM takeover instead of silently
    applying an industrial template.
    """

    if any(fact.company_id != company_id for fact in facts):
        raise ValueError("financial facts belong to another company")
    latest: dict[str, tuple[object, Decimal]] = {}
    for fact in sorted(facts, key=lambda item: (item.period_end, item.fact_id)):
        canonical = _FACT_KEYS.get(fact.field_code)
        if canonical is None:
            continue
        multiplier = _FINANCIAL_UNIT_MULTIPLIERS.get(fact.unit)
        if multiplier is None:
            continue
        latest[canonical] = (fact.period_end, fact.reported_value * multiplier)
    return build_financial_fact_pack(
        company_id=company_id,
        industry_profile=industry_profile,
        raw_metrics={key: value for key, (_period, value) in latest.items()},
    )


def critical_metrics_for(profile: FinancialIndustryProfile) -> tuple[str, ...]:
    return tuple(sorted(_CRITICAL_FINANCIAL_METRICS[profile]))


__all__ = [
    "build_financial_fact_pack",
    "critical_metrics_for",
    "normalize_provider_snapshot",
    "parse_decimal",
    "standardize_financial_facts",
]
