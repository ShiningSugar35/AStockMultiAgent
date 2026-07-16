"""Small pure Decimal calculations used by the financial audit engine."""

from __future__ import annotations

from decimal import Decimal, localcontext


def balance_identity_difference(
    total_assets: Decimal,
    total_liabilities: Decimal,
    total_equity: Decimal,
) -> Decimal:
    return total_assets - total_liabilities - total_equity


def cash_identity_difference(
    cash_ending: Decimal,
    cash_beginning: Decimal,
    net_cash_operating: Decimal,
    net_cash_investing: Decimal,
    net_cash_financing: Decimal,
    exchange_effect: Decimal,
) -> Decimal:
    return cash_ending - (
        cash_beginning
        + net_cash_operating
        + net_cash_investing
        + net_cash_financing
        + exchange_effect
    )


def decimal_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise ZeroDivisionError("financial ratio denominator is zero")
    with localcontext() as context:
        context.prec = 28
        return numerator / denominator


def reporting_rounding_tolerance(
    reporting_quanta_cny: list[Decimal],
    tolerance_reporting_units: Decimal,
) -> Decimal:
    if not reporting_quanta_cny:
        raise ValueError("at least one reporting quantum is required")
    if tolerance_reporting_units < 0:
        raise ValueError("tolerance_reporting_units must be nonnegative")
    return max(reporting_quanta_cny) * tolerance_reporting_units
