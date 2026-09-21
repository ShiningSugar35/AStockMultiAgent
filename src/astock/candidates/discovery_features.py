"""Derived discovery features over current certified financial facts.

No I/O, no certification authority, no ranking model and no investment verdict.
Callers retain responsibility for official source admission, immutable byte checks,
current corrections/conflicts and the snapshot-to-statement-scope binding. This
module consumes that binding; it never reconstructs scope from a company name.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Context, Decimal, DecimalException, Inexact, Underflow, localcontext

from astock.financial_integrity.advanced_calculations import midrank_percentile
from astock.financial_integrity.calculations import decimal_ratio
from astock.schemas.financial import (
    FinancialDurationSemantics as Duration,
)
from astock.schemas.financial import (
    FinancialFact,
)
from astock.schemas.financial import (
    FinancialFieldCode as Field,
)
from astock.schemas.financial import (
    FinancialFindingStatus as Status,
)
from astock.schemas.financial import (
    FinancialIndustryProfile as Industry,
)
from astock.schemas.financial import (
    FinancialPeriodType as Period,
)
from astock.schemas.financial import (
    FinancialStatementType as Statement,
)
from astock.schemas.financial import (
    FinancialUnit as Unit,
)
from astock.schemas.financial_sources import FinancialStatementScope as Scope

FORMULA_VERSION = "discovery-fundamentals-v1"
_QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}
_MONEY_SCALE = {
    Unit.CNY: Decimal(1),
    Unit.THOUSAND_CNY: Decimal(1000),
    Unit.TEN_THOUSAND_CNY: Decimal(10000),
    Unit.MILLION_CNY: Decimal(1000000),
    Unit.HUNDRED_MILLION_CNY: Decimal(100000000),
}
_FLOW_FIELDS = {
    Field.REVENUE: Statement.INCOME_STATEMENT,
    Field.NET_PROFIT_INCOME: Statement.INCOME_STATEMENT,
    Field.NET_CASH_OPERATING: Statement.CASH_FLOW_STATEMENT,
}
_STOCK_FIELDS = frozenset(
    {Field.TOTAL_ASSETS, Field.TOTAL_LIABILITIES, Field.ACCOUNTS_RECEIVABLE, Field.INVENTORY}
)
_INDUSTRIAL_METRICS = frozenset(
    {Industry.GENERAL_INDUSTRIAL, Industry.RESOURCE_MINING, Industry.REAL_ESTATE}
)


@dataclass(frozen=True)
class DiscoveryFeatureValue:
    metric_id: str
    status: Status
    value: Decimal | None
    unit: Unit
    period_start: date | None
    period_end: date
    formula: str
    source_fact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    dependency_fingerprint: str = ""

    def __post_init__(self) -> None:
        if (self.status is Status.CALCULATED) != (self.value is not None):
            raise ValueError("only calculated discovery features may have a value")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("discovery feature must be finite")


@dataclass(frozen=True)
class DiscoveryFeaturePacket:
    company_id: str
    industry_profile: Industry
    business_group_id: str
    statement_scope: Scope
    period_end: date
    fact_state_version: str
    formula_version: str
    semantic_fingerprint: str
    features: tuple[DiscoveryFeatureValue, ...]

    def feature(self, metric_id: str) -> DiscoveryFeatureValue:
        for item in self.features:
            if item.metric_id == metric_id:
                return item
        raise KeyError(metric_id)

    @property
    def coverage(self) -> dict[str, int]:
        """Counts only; deliberately not an imputed quality or investment score."""
        return {
            status.value: sum(item.status is status for item in self.features)
            for status in (
                Status.CALCULATED,
                Status.INSUFFICIENT_DATA,
                Status.CONFLICTED,
                Status.NOT_APPLICABLE,
            )
        }


@dataclass(frozen=True)
class PeerPercentileResult:
    metric_id: str
    status: Status
    value: Decimal | None
    sample_size: int
    company_ids: tuple[str, ...]
    reason_codes: tuple[str, ...] = ()
    eligible_company_ids: tuple[str, ...] = ()
    unavailable_company_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Value:
    status: Status
    value: Decimal | None = None
    facts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    signatures: tuple[str, ...] = ()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _decimal_key(value: Decimal) -> str:
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    # Arithmetic inputs are finite; an integer exponent is guaranteed here.
    assert isinstance(exponent, int)
    trimmed = list(digits)
    while trimmed[-1] == 0:
        trimmed.pop()
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in trimmed)}e{exponent}"


def _collect(
    values: Sequence[_Value],
    *,
    status: Status | None = None,
    value: Decimal | None = None,
    reason: str | None = None,
) -> _Value:
    reasons = {code for item in values for code in item.reasons}
    if reason:
        reasons.add(reason)
    inferred = Status.CALCULATED
    if any(item.status is Status.CONFLICTED for item in values):
        inferred = Status.CONFLICTED
    elif any(item.status is Status.INSUFFICIENT_DATA for item in values):
        inferred = Status.INSUFFICIENT_DATA
    elif any(item.status is Status.NOT_APPLICABLE for item in values):
        inferred = Status.NOT_APPLICABLE
    final = status or inferred
    return _Value(
        final,
        value if final is Status.CALCULATED else None,
        tuple(sorted({key for item in values for key in item.facts})),
        tuple(sorted({key for item in values for key in item.evidence})),
        tuple(sorted(reasons)),
        tuple(sorted({key for item in values for key in item.signatures})),
    )


def _linear(values: Sequence[_Value], weights: Sequence[int]) -> _Value:
    base = _collect(values)
    if base.status is not Status.CALCULATED:
        return base
    try:
        with localcontext(Context(prec=50)) as ctx:
            ctx.traps[Inexact] = True
            ctx.traps[Underflow] = True
            total = sum(
                (
                    item.value * weight
                    for item, weight in zip(values, weights, strict=True)
                    if item.value is not None
                ),
                Decimal(0),
            )
    except DecimalException:
        return _collect(
            (base,), status=Status.INSUFFICIENT_DATA, reason="NUMERIC_OPERATION_UNSUPPORTED"
        )
    return replace(base, value=total)


def _ratio(numerator: _Value, denominator: _Value) -> _Value:
    base = _collect((numerator, denominator))
    if base.status is not Status.CALCULATED:
        return base
    assert numerator.value is not None and denominator.value is not None
    if denominator.value <= 0:
        return replace(
            base, status=Status.NOT_APPLICABLE, value=None, reasons=("NON_POSITIVE_DENOMINATOR",)
        )
    try:
        # Ratio rounding is intentional; overflow/underflow and caller-context
        # changes must never silently produce a fake finite investment metric.
        with localcontext(Context(prec=28)) as ctx:
            ctx.traps[Underflow] = True
            result = decimal_ratio(numerator.value, denominator.value)
    except DecimalException:
        return _collect(
            (base,), status=Status.INSUFFICIENT_DATA, reason="NUMERIC_OPERATION_UNSUPPORTED"
        )
    return replace(base, value=result)


def _quarter_start(end: date) -> date:
    return date(end.year, end.month - 2, 1)


def _period_issue(fact: FinancialFact) -> str | None:
    end = fact.period_end
    if (end.month, end.day) not in _QUARTER_ENDS:
        return "NON_STANDARD_REPORTING_PERIOD"
    expected_type = (
        Period.ANNUAL
        if end.month == 12
        else Period.SEMIANNUAL
        if end.month == 6
        else Period.QUARTERLY
    )
    standalone_quarter = (
        fact.duration_semantics is Duration.STANDALONE_PERIOD
        and fact.period_start == _quarter_start(end)
    )
    if fact.period_type is not expected_type and not (
        standalone_quarter and fact.period_type is Period.QUARTERLY
    ):
        return "PERIOD_TYPE_MISMATCH"
    if fact.field_code in _STOCK_FIELDS:
        if (
            fact.statement_type is not Statement.BALANCE_SHEET
            or fact.duration_semantics is not Duration.INSTANT
            or fact.period_start is not None
        ):
            return "AMBIGUOUS_PERIOD_SEMANTICS"
        return None
    if fact.statement_type is not _FLOW_FIELDS[fact.field_code]:
        return "STATEMENT_TYPE_MISMATCH"
    annual = fact.period_start == date(end.year, 1, 1) and end.month == 12
    if fact.duration_semantics is Duration.REPORTED_PERIOD:
        if not annual or fact.period_type is not Period.ANNUAL:
            return "AMBIGUOUS_PERIOD_SEMANTICS"
    elif fact.duration_semantics is Duration.YEAR_TO_DATE:
        if fact.period_start != date(end.year, 1, 1):
            return "AMBIGUOUS_PERIOD_SEMANTICS"
    elif fact.duration_semantics is Duration.STANDALONE_PERIOD:
        if not annual and not standalone_quarter:
            return "AMBIGUOUS_PERIOD_SEMANTICS"
    else:
        return "AMBIGUOUS_PERIOD_SEMANTICS"
    return None


class _FactIndex:
    """Index one company's supplied current facts, without timestamps selecting truth."""

    def __init__(
        self,
        company_id: str,
        facts: Sequence[FinancialFact],
        scopes: Mapping[str, Scope],
        scope: Scope,
    ):
        groups: dict[tuple[Field, date | None, date], list[_Value]] = defaultdict(list)
        self.invalid: dict[tuple[Field, date], list[_Value]] = defaultdict(list)
        all_signatures: set[str] = set()
        for fact in facts:
            if fact.company_id != company_id:
                raise ValueError("discovery facts belong to another company")
            bound_scope = scopes.get(fact.source_snapshot_id or "")
            if bound_scope is not None and bound_scope is not scope:
                raise ValueError("discovery fact scope differs from requested scope")
            if fact.field_code not in _FLOW_FIELDS and fact.field_code not in _STOCK_FIELDS:
                continue
            reasons: list[str] = []
            if (
                not (fact.source_snapshot_id or "").strip()
                or not fact.evidence_ids
                or any(not evidence_id.strip() for evidence_id in fact.evidence_ids)
            ):
                reasons.append("MISSING_SOURCE_LINEAGE")
            if bound_scope is None:
                reasons.append("MISSING_STATEMENT_SCOPE")
            issue = _period_issue(fact)
            if issue:
                reasons.append(issue)
            multiplier = _MONEY_SCALE.get(fact.unit)
            number: Decimal | None = None
            if multiplier is None:
                reasons.append("UNIT_MISMATCH")
            elif not fact.reported_value.is_finite():
                reasons.append("NON_FINITE_VALUE")
            else:
                try:
                    with localcontext(Context(prec=50)) as ctx:
                        ctx.traps[Inexact] = True
                        ctx.traps[Underflow] = True
                        number = fact.reported_value * multiplier
                except DecimalException:
                    reasons.append("NUMERIC_RANGE_UNSUPPORTED")
                if number is not None and fact.field_code in _STOCK_FIELDS and number < 0:
                    reasons.append("NEGATIVE_BALANCE_SHEET_VALUE")
            signature = _digest(
                {
                    "company": company_id,
                    "field": fact.field_code.value,
                    "start": fact.period_start.isoformat() if fact.period_start else None,
                    "end": fact.period_end.isoformat(),
                    "scope": bound_scope.value if bound_scope else None,
                    "value_cny": _decimal_key(number) if number is not None else None,
                    "source": fact.source_snapshot_id,
                    "evidence": sorted(fact.evidence_ids),
                    "issues": sorted(reasons),
                    "raw_on_error": {
                        "value": str(fact.reported_value),
                        "unit": fact.unit.value,
                        "period_type": fact.period_type.value,
                        "duration_semantics": fact.duration_semantics.value,
                    }
                    if reasons
                    else None,
                }
            )
            all_signatures.add(signature)
            row = _Value(
                Status.INSUFFICIENT_DATA if reasons else Status.CALCULATED,
                None if reasons else number,
                (fact.fact_id,),
                tuple(sorted(fact.evidence_ids)),
                tuple(sorted(reasons)),
                (signature,),
            )
            groups[(fact.field_code, fact.period_start, fact.period_end)].append(row)
            if reasons:
                self.invalid[(fact.field_code, fact.period_end)].append(row)
        self.signatures = tuple(sorted(all_signatures))
        self.cells: dict[tuple[Field, date | None, date], _Value] = {}
        for key, rows in groups.items():
            base = _collect(rows)
            unique = {row.value for row in rows if row.value is not None}
            if len(unique) > 1:
                base = _collect(rows, status=Status.CONFLICTED, reason="CONFLICTING_VALUES")
            elif base.status is Status.CALCULATED:
                base = replace(base, value=next(iter(unique)))
            self.cells[key] = base

        self._validate_durations()

    def _validate_durations(self) -> None:
        # Contradictory quarter evidence invalidates all dependent uses of that
        # cumulative endpoint, not merely the optional displayed quarter metric.
        for (field, start, end), direct in self.cells.items():
            if (
                field not in _FLOW_FIELDS
                or (end.month, end.day) not in _QUARTER_ENDS
                or end.month == 3
                or start != _quarter_start(end)
            ):
                continue
            assert start is not None
            current = self.cells.get((field, date(end.year, 1, 1), end))
            prior = self.cells.get((field, date(end.year, 1, 1), start - timedelta(days=1)))
            if current is None or prior is None:
                continue
            values = (direct, current, prior)
            if any(item.status is not Status.CALCULATED for item in values):
                continue
            derived = _linear((current, prior), (1, -1))
            if direct.value != derived.value:
                self.invalid[(field, end)].append(
                    _collect(values, status=Status.CONFLICTED, reason="QUARTER_CUMULATIVE_CONFLICT")
                )

    def exact(self, field: Field, start: date | None, end: date) -> _Value:
        invalid = self.invalid.get((field, end))
        if invalid:
            return _collect(invalid)
        return self.cells.get(
            (field, start, end), _Value(Status.INSUFFICIENT_DATA, reasons=("MISSING_FACT",))
        )

    def ytd(self, field: Field, end: date) -> _Value:
        return self.exact(field, date(end.year, 1, 1), end)

    def ttm(self, field: Field, end: date) -> _Value:
        current = self.ytd(field, end)
        if end.month == 12:
            return current
        return _linear(
            (
                self.ytd(field, date(end.year - 1, 12, 31)),
                current,
                self.ytd(field, end.replace(year=end.year - 1)),
            ),
            (1, 1, -1),
        )

    def quarter(self, field: Field, end: date) -> _Value:
        start = _quarter_start(end)
        direct = self.exact(field, start, end)
        if end.month == 3:
            return direct
        cumulative = _linear(
            (self.ytd(field, end), self.ytd(field, start - timedelta(days=1))), (1, -1)
        )
        direct_present = (field, start, end) in self.cells
        if direct_present and direct.status is not Status.CALCULATED:
            return direct
        if cumulative.status is Status.CONFLICTED:
            return cumulative
        if direct.status is Status.CALCULATED:
            if cumulative.status is Status.CALCULATED:
                if direct.value != cumulative.value:
                    return _collect(
                        (direct, cumulative),
                        status=Status.CONFLICTED,
                        reason="QUARTER_CUMULATIVE_CONFLICT",
                    )
                return _collect((direct, cumulative), value=direct.value)
            return direct
        return cumulative


def build_discovery_features(
    *,
    company_id: str,
    industry_profile: Industry,
    business_group_id: str,
    period_end: date,
    fact_state_version: str,
    facts: Sequence[FinancialFact],
    scope_by_snapshot_id: Mapping[str, Scope],
    statement_scope: Scope = Scope.CONSOLIDATED,
) -> DiscoveryFeaturePacket:
    """Build a disposable feature view; incomplete fields never become zero or PASS.

    ``fact_state_version`` belongs to the canonical corrections/conflicts state.
    ``business_group_id`` must come from admitted business comparability metadata,
    not from a guessed company-name/author/keyword match. No scopes are defaulted
    for individual source snapshots. Fiscal calendars other than calendar-year
    quarterly reporting require a separately validated adapter.
    """
    if not company_id.strip() or not business_group_id.strip() or not fact_state_version.strip():
        raise ValueError("company, business comparability and fact state are required")
    if not isinstance(industry_profile, Industry) or not isinstance(statement_scope, Scope):
        raise ValueError("industry and statement scope must be typed")
    if (period_end.month, period_end.day) not in _QUARTER_ENDS:
        raise ValueError("feature period_end must be a calendar quarter end")
    index = _FactIndex(company_id, facts, scope_by_snapshot_id, statement_scope)
    features: list[DiscoveryFeatureValue] = []

    def add(
        name: str,
        value: _Value,
        formula: str,
        *,
        unit: Unit = Unit.RATIO,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        result_end = end or period_end
        # This is an economic dependency key, never a source-admission key.
        # The whole packet still binds the current canonical fact-state version.
        fingerprint = _digest(
            {
                "version": FORMULA_VERSION,
                "company": company_id,
                "scope": statement_scope.value,
                "metric": name,
                "industry": industry_profile.value,
                "start": start.isoformat() if start else None,
                "end": result_end.isoformat(),
                "formula": formula,
                "status": value.status.value,
                "unit": unit.value,
                "value": _decimal_key(value.value) if value.value is not None else None,
                "dependencies": value.signatures,
                "issues": value.reasons,
            }
        )
        features.append(
            DiscoveryFeatureValue(
                name,
                value.status,
                value.value,
                unit,
                start,
                result_end,
                formula,
                value.facts,
                value.evidence,
                value.reasons,
                fingerprint,
            )
        )

    prior_end = period_end.replace(year=period_end.year - 1)
    prior_year_end = date(period_end.year - 1, 12, 31)
    ttm_start = (
        date(period_end.year, 1, 1) if period_end.month == 12 else prior_end + timedelta(days=1)
    )
    revenue = index.ttm(Field.REVENUE, period_end)
    profit = index.ttm(Field.NET_PROFIT_INCOME, period_end)
    cash = index.ttm(Field.NET_CASH_OPERATING, period_end)
    for name, field, value in (
        ("REVENUE", Field.REVENUE, revenue),
        ("NET_PROFIT", Field.NET_PROFIT_INCOME, profit),
        ("OPERATING_CASH", Field.NET_CASH_OPERATING, cash),
    ):
        add(
            f"{name}_TTM",
            value,
            "prior_annual + current_ytd - prior_comparable_ytd; annual=reported",
            unit=Unit.CNY,
            start=ttm_start,
        )
        add(
            f"{name}_QUARTER",
            index.quarter(field, period_end),
            "current_ytd - prior_quarter_ytd; direct=cross_check",
            unit=Unit.CNY,
            start=_quarter_start(period_end),
        )
    for name, field in (("REVENUE", Field.REVENUE), ("NET_PROFIT", Field.NET_PROFIT_INCOME)):
        current, prior = index.ytd(field, period_end), index.ytd(field, prior_end)
        delta = _linear((current, prior), (1, -1))
        add(
            f"{name}_YOY",
            _ratio(delta, prior),
            "(current_ytd - prior_comparable_ytd) / positive_prior",
            start=date(period_end.year, 1, 1),
        )
        if name == "NET_PROFIT":
            add(
                "NET_PROFIT_CHANGE",
                delta,
                "current_ytd - prior_comparable_ytd",
                unit=Unit.CNY,
                start=date(period_end.year, 1, 1),
            )
    add(
        "NET_MARGIN",
        _ratio(profit, revenue),
        "net_profit_ttm / positive_revenue_ttm",
        start=ttm_start,
    )
    not_industrial = _Value(Status.NOT_APPLICABLE, reasons=("INDUSTRY_METHOD_NOT_APPLICABLE",))
    applicable = industry_profile in _INDUSTRIAL_METRICS
    add(
        "CASH_CONVERSION",
        _ratio(cash, profit) if applicable else not_industrial,
        "operating_cash_ttm / positive_net_profit_ttm",
        start=ttm_start,
    )
    add(
        "LIABILITIES_TO_ASSETS",
        _ratio(
            index.exact(Field.TOTAL_LIABILITIES, None, period_end),
            index.exact(Field.TOTAL_ASSETS, None, period_end),
        )
        if applicable
        else not_industrial,
        "total_liabilities / positive_total_assets",
    )
    for name, field in (
        ("RECEIVABLES_YOY", Field.ACCOUNTS_RECEIVABLE),
        ("INVENTORY_YOY", Field.INVENTORY),
    ):
        current, prior = index.exact(field, None, period_end), index.exact(field, None, prior_end)
        add(
            name,
            _ratio(_linear((current, prior), (1, -1)), prior) if applicable else not_industrial,
            "(current_balance - prior_comparable_balance) / positive_prior_balance",
        )
    annual_end = period_end if period_end.month == 12 else prior_year_end
    years = [
        index.ytd(Field.NET_PROFIT_INCOME, annual_end.replace(year=annual_end.year - offset))
        for offset in range(3)
    ]
    history = _collect(years)
    if history.status is Status.CALCULATED:
        history = replace(
            history, value=Decimal(sum(item.value is not None and item.value > 0 for item in years))
        )
    add(
        "PROFITABLE_ANNUAL_PERIODS",
        history,
        "positive_profit_count_in_three_complete_consecutive_years",
        unit=Unit.SCORE,
        start=date(annual_end.year - 2, 1, 1),
        end=annual_end,
    )
    add(
        "DIVIDEND_CASH_COVERAGE",
        _Value(Status.INSUFFICIENT_DATA, reasons=("DIVIDEND_FIELD_UNAVAILABLE",)),
        "requires_canonical_cash_dividend_fact_not_net_financing_cash",
    )
    specialist = (
        _Value(Status.NOT_APPLICABLE, reasons=("GENERAL_PROFILE",))
        if industry_profile is Industry.GENERAL_INDUSTRIAL
        else _Value(Status.INSUFFICIENT_DATA, reasons=("SPECIALIST_FIELDS_UNAVAILABLE",))
    )
    add(
        "SPECIALIST_FINANCIAL_METRICS",
        specialist,
        "requires_industry_native_fields_not_industrial_proxies",
    )
    fingerprint = _digest(
        {
            "company": company_id,
            "industry": industry_profile.value,
            "business_group": business_group_id,
            "scope": statement_scope.value,
            "period": period_end.isoformat(),
            "state": fact_state_version,
            "version": FORMULA_VERSION,
            "inputs": index.signatures,
        }
    )
    return DiscoveryFeaturePacket(
        company_id,
        industry_profile,
        business_group_id,
        statement_scope,
        period_end,
        fact_state_version,
        FORMULA_VERSION,
        fingerprint,
        tuple(features),
    )


def peer_percentile(
    target: DiscoveryFeaturePacket,
    peers: Sequence[DiscoveryFeaturePacket],
    metric_id: str,
    *,
    minimum_sample_size: int = 10,
) -> PeerPercentileResult:
    """Midrank of observed values in an explicit cohort, with missing coverage.

    Duplicate company wrappers do not increase N. Unknown/conflicting entries do
    not become zero. The target is never inserted to pad a nine-company sample.
    Financial/state admission must already have occurred for these current packets.
    """
    if type(minimum_sample_size) is not int or minimum_sample_size < 10:
        raise ValueError("discovery peer minimum_sample_size must be an integer of at least ten")
    chosen = target.feature(metric_id)
    companies: dict[str, Decimal] = {}
    signatures: dict[str, tuple[Status, Decimal | None]] = {}
    eligible: set[str] = set()
    conflict = False
    target_group = (
        target.industry_profile,
        target.business_group_id,
        target.statement_scope,
        target.period_end,
        target.formula_version,
    )
    for peer in peers:
        if (
            peer.industry_profile,
            peer.business_group_id,
            peer.statement_scope,
            peer.period_end,
            peer.formula_version,
        ) != target_group:
            continue
        eligible.add(peer.company_id)
        try:
            item = peer.feature(metric_id)
        except KeyError:
            signature = (Status.INSUFFICIENT_DATA, None)
            if peer.company_id in signatures and signatures[peer.company_id] != signature:
                conflict = True
            signatures[peer.company_id] = signature
            continue
        comparable = (item.period_start, item.period_end, item.unit, item.formula) == (
            chosen.period_start,
            chosen.period_end,
            chosen.unit,
            chosen.formula,
        )
        signature = (item.status, item.value) if comparable else (Status.INSUFFICIENT_DATA, None)
        if peer.company_id in signatures and signatures[peer.company_id] != signature:
            conflict = True
        signatures[peer.company_id] = signature
        if comparable and item.status is Status.CONFLICTED:
            conflict = True
        elif comparable and item.status is Status.CALCULATED and item.value is not None:
            companies[peer.company_id] = item.value
    ids = tuple(sorted(companies))

    def result(
        status: Status, value: Decimal | None = None, reasons: tuple[str, ...] = ()
    ) -> PeerPercentileResult:
        return PeerPercentileResult(
            metric_id,
            status,
            value,
            len(ids),
            ids,
            reasons,
            tuple(sorted(eligible)),
            tuple(sorted(eligible - set(companies))),
        )

    if conflict:
        return result(Status.CONFLICTED, reasons=("PEER_FACT_CONFLICT",))
    if chosen.status is not Status.CALCULATED or chosen.value is None:
        return result(chosen.status, reasons=chosen.reason_codes)
    if target.company_id not in companies or len(ids) < minimum_sample_size:
        return result(Status.INSUFFICIENT_DATA, reasons=("INSUFFICIENT_COMPARABLE_COMPANIES",))
    if companies[target.company_id] != chosen.value:
        return result(Status.CONFLICTED, reasons=("TARGET_PEER_VALUE_CONFLICT",))
    return result(Status.CALCULATED, midrank_percentile(chosen.value, list(companies.values())))
