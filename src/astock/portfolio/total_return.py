"""Point-in-time gross total-return research series derived from raw prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from astock.schemas.reference_data import CorporateActionObservation, CorporateActionStatus


@dataclass(frozen=True, slots=True)
class TotalReturnResearchSeries:
    closes_by_date: dict[str, float]
    applied_action_ids: tuple[str, ...]
    warning_codes: tuple[str, ...]


def build_total_return_research_series(
    raw_closes_by_date: dict[str, float],
    actions: list[CorporateActionObservation],
    *,
    as_of: datetime,
) -> TotalReturnResearchSeries:
    """Build a PIT gross-total-return pseudo-price series.

    Raw execution prices remain untouched. On an ex-date, the economic end-of-day
    wealth of one pre-action share is ``close * (1 + stock_ratio) + gross_cash``.
    Only TERMS_VERIFIED actions visible at ``as_of`` may alter the research series.
    """

    if not raw_closes_by_date:
        return TotalReturnResearchSeries({}, (), ())
    ordered_dates = sorted(raw_closes_by_date)
    if any(raw_closes_by_date[item] <= 0 for item in ordered_dates):
        raise ValueError("total-return series requires positive raw closes")

    warnings: set[str] = set()
    eligible_by_date: dict[str, list[CorporateActionObservation]] = {}
    for action in actions:
        if action.available_to_system_at > as_of or action.ex_date is None:
            continue
        key = action.ex_date.isoformat()
        if action.status is not CorporateActionStatus.TERMS_VERIFIED:
            if key in raw_closes_by_date:
                warnings.add("UNVERIFIED_CORPORATE_ACTION_NOT_APPLIED")
            continue
        eligible_by_date.setdefault(key, []).append(action)

    first_date = ordered_dates[0]
    research_closes: dict[str, float] = {first_date: float(raw_closes_by_date[first_date])}
    previous_raw = Decimal(str(raw_closes_by_date[first_date]))
    pseudo = previous_raw
    applied: list[str] = []
    for session_date in ordered_dates[1:]:
        raw_close = Decimal(str(raw_closes_by_date[session_date]))
        stock_ratio = Decimal("0")
        cash_per_share = Decimal("0")
        for action in eligible_by_date.get(session_date, []):
            try:
                stock_ratio += Decimal(action.structured_terms.get("dividStocksPs", "0"))
                stock_ratio += Decimal(action.structured_terms.get("dividReserveToStockPs", "0"))
                gross_cash = action.structured_terms.get("dividCashPsBeforeTax")
                if gross_cash not in {None, ""}:
                    cash_per_share += Decimal(str(gross_cash))
                else:
                    after_tax_yuan = action.structured_terms.get("cashPsAfterTax")
                    after_tax_fen = action.structured_terms.get("cashFenPsAfterTax")
                    if after_tax_yuan not in {None, ""}:
                        cash_per_share += Decimal(str(after_tax_yuan))
                        warnings.add("AFTER_TAX_CASH_USED_AS_GROSS_FALLBACK")
                    elif after_tax_fen not in {None, ""}:
                        cash_per_share += Decimal(str(after_tax_fen)) / Decimal("100")
                        warnings.add("AFTER_TAX_CASH_USED_AS_GROSS_FALLBACK")
                applied.append(action.observation_id)
            except InvalidOperation as exc:
                raise ValueError("corporate-action terms are not numeric") from exc
        if stock_ratio < 0 or cash_per_share < 0:
            raise ValueError("corporate-action total-return terms cannot be negative")
        wealth = raw_close * (Decimal("1") + stock_ratio) + cash_per_share
        if wealth <= 0:
            raise ValueError("corporate-action adjusted wealth must stay positive")
        total_return_factor = wealth / previous_raw
        pseudo *= total_return_factor
        research_closes[session_date] = float(pseudo)
        previous_raw = raw_close

    return TotalReturnResearchSeries(
        closes_by_date=research_closes,
        applied_action_ids=tuple(sorted(set(applied))),
        warning_codes=tuple(sorted(warnings)),
    )


__all__ = ["TotalReturnResearchSeries", "build_total_return_research_series"]
