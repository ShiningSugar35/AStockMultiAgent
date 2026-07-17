"""Pure Decimal M3.2 calculations with no I/O or hidden imputation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext

from astock.financial_integrity.calculations import decimal_ratio


def percentage_change(current: Decimal, comparison: Decimal) -> Decimal:
    """Return a signed change using the absolute comparison value as denominator."""

    if comparison == 0:
        raise ZeroDivisionError("financial comparison denominator is zero")
    return decimal_ratio(current - comparison, abs(comparison))


def midrank_percentile(target: Decimal, peers: Sequence[Decimal]) -> Decimal:
    """Stable empirical percentile; ties receive their midpoint rank."""

    if not peers:
        raise ValueError("at least one peer value is required")
    below = sum(value < target for value in peers)
    equal = sum(value == target for value in peers)
    with localcontext() as context:
        context.prec = 28
        return (Decimal(below) + Decimal(equal) / Decimal(2)) / Decimal(len(peers))


def beneish_m_score(
    current: Mapping[str, Decimal],
    prior: Mapping[str, Decimal],
) -> tuple[Decimal, dict[str, Decimal]]:
    """Eight-variable Beneish M-score using explicitly supplied consecutive periods."""

    dsri = decimal_ratio(
        decimal_ratio(current["accounts_receivable"], current["revenue"]),
        decimal_ratio(prior["accounts_receivable"], prior["revenue"]),
    )
    current_margin = decimal_ratio(
        current["revenue"] - current["operating_cost"], current["revenue"]
    )
    prior_margin = decimal_ratio(
        prior["revenue"] - prior["operating_cost"], prior["revenue"]
    )
    gmi = decimal_ratio(prior_margin, current_margin)
    current_asset_quality = Decimal(1) - decimal_ratio(
        current["current_assets"] + current["ppe"], current["total_assets"]
    )
    prior_asset_quality = Decimal(1) - decimal_ratio(
        prior["current_assets"] + prior["ppe"], prior["total_assets"]
    )
    aqi = decimal_ratio(current_asset_quality, prior_asset_quality)
    sgi = decimal_ratio(current["revenue"], prior["revenue"])
    current_depreciation_rate = decimal_ratio(
        current["depreciation"], current["depreciation"] + current["ppe"]
    )
    prior_depreciation_rate = decimal_ratio(
        prior["depreciation"], prior["depreciation"] + prior["ppe"]
    )
    depi = decimal_ratio(prior_depreciation_rate, current_depreciation_rate)
    sgai = decimal_ratio(
        decimal_ratio(current["sga"], current["revenue"]),
        decimal_ratio(prior["sga"], prior["revenue"]),
    )
    lvgi = decimal_ratio(
        decimal_ratio(current["total_liabilities"], current["total_assets"]),
        decimal_ratio(prior["total_liabilities"], prior["total_assets"]),
    )
    tata = decimal_ratio(
        current["net_profit"] - current["cfo"], current["total_assets"]
    )
    components = {
        "DSRI": dsri,
        "GMI": gmi,
        "AQI": aqi,
        "SGI": sgi,
        "DEPI": depi,
        "SGAI": sgai,
        "LVGI": lvgi,
        "TATA": tata,
    }
    score = (
        Decimal("-4.84")
        + Decimal("0.920") * dsri
        + Decimal("0.528") * gmi
        + Decimal("0.404") * aqi
        + Decimal("0.892") * sgi
        + Decimal("0.115") * depi
        - Decimal("0.172") * sgai
        + Decimal("4.679") * tata
        - Decimal("0.327") * lvgi
    )
    return score, components


def altman_z_score(current: Mapping[str, Decimal]) -> tuple[Decimal, dict[str, Decimal]]:
    """Public-manufacturing-company five-factor Altman Z-score."""

    working_capital_ratio = decimal_ratio(
        current["current_assets"] - current["current_liabilities"],
        current["total_assets"],
    )
    retained_earnings_ratio = decimal_ratio(
        current["retained_earnings"], current["total_assets"]
    )
    ebit_ratio = decimal_ratio(current["ebit"], current["total_assets"])
    market_leverage = decimal_ratio(current["market_cap"], current["total_liabilities"])
    asset_turnover = decimal_ratio(current["revenue"], current["total_assets"])
    components = {
        "WORKING_CAPITAL_TO_ASSETS": working_capital_ratio,
        "RETAINED_EARNINGS_TO_ASSETS": retained_earnings_ratio,
        "EBIT_TO_ASSETS": ebit_ratio,
        "MARKET_CAP_TO_LIABILITIES": market_leverage,
        "SALES_TO_ASSETS": asset_turnover,
    }
    score = (
        Decimal("1.2") * working_capital_ratio
        + Decimal("1.4") * retained_earnings_ratio
        + Decimal("3.3") * ebit_ratio
        + Decimal("0.6") * market_leverage
        + asset_turnover
    )
    return score, components


def piotroski_f_score(
    current: Mapping[str, Decimal],
    prior: Mapping[str, Decimal],
) -> tuple[Decimal, dict[str, Decimal]]:
    """Nine-signal Piotroski F-score with explicit current/prior inputs."""

    average_assets = (current["total_assets"] + prior["total_assets"]) / Decimal(2)
    roa = decimal_ratio(current["net_profit"], average_assets)
    prior_roa = decimal_ratio(prior["net_profit"], prior["total_assets"])
    leverage = decimal_ratio(current["long_term_debt"], average_assets)
    prior_leverage = decimal_ratio(prior["long_term_debt"], prior["total_assets"])
    liquidity = decimal_ratio(current["current_assets"], current["current_liabilities"])
    prior_liquidity = decimal_ratio(
        prior["current_assets"], prior["current_liabilities"]
    )
    gross_margin = decimal_ratio(
        current["revenue"] - current["operating_cost"], current["revenue"]
    )
    prior_gross_margin = decimal_ratio(
        prior["revenue"] - prior["operating_cost"], prior["revenue"]
    )
    asset_turnover = decimal_ratio(current["revenue"], average_assets)
    prior_asset_turnover = decimal_ratio(prior["revenue"], prior["total_assets"])
    signals = {
        "POSITIVE_ROA": Decimal(roa > 0),
        "POSITIVE_CFO": Decimal(current["cfo"] > 0),
        "ROA_IMPROVED": Decimal(roa > prior_roa),
        "CFO_EXCEEDS_NET_INCOME": Decimal(current["cfo"] > current["net_profit"]),
        "LEVERAGE_DECLINED": Decimal(leverage < prior_leverage),
        "LIQUIDITY_IMPROVED": Decimal(liquidity > prior_liquidity),
        "NO_NET_SHARE_ISSUANCE": Decimal(
            current["shares_outstanding"] <= prior["shares_outstanding"]
        ),
        "GROSS_MARGIN_IMPROVED": Decimal(gross_margin > prior_gross_margin),
        "ASSET_TURNOVER_IMPROVED": Decimal(asset_turnover > prior_asset_turnover),
    }
    return sum(signals.values(), Decimal(0)), signals


def sloan_accrual_ratio(
    current: Mapping[str, Decimal],
    prior: Mapping[str, Decimal],
) -> tuple[Decimal, dict[str, Decimal]]:
    average_assets = (current["total_assets"] + prior["total_assets"]) / Decimal(2)
    value = decimal_ratio(current["net_profit"] - current["cfo"], average_assets)
    return value, {"AVERAGE_ASSETS": average_assets}


def dupont_decomposition(
    current: Mapping[str, Decimal],
    prior: Mapping[str, Decimal],
) -> tuple[Decimal, dict[str, Decimal]]:
    average_assets = (current["total_assets"] + prior["total_assets"]) / Decimal(2)
    average_equity = (current["total_equity"] + prior["total_equity"]) / Decimal(2)
    net_margin = decimal_ratio(current["net_profit"], current["revenue"])
    asset_turnover = decimal_ratio(current["revenue"], average_assets)
    equity_multiplier = decimal_ratio(average_assets, average_equity)
    roe = net_margin * asset_turnover * equity_multiplier
    return roe, {
        "NET_PROFIT_MARGIN": net_margin,
        "ASSET_TURNOVER": asset_turnover,
        "EQUITY_MULTIPLIER": equity_multiplier,
        "AVERAGE_ASSETS": average_assets,
        "AVERAGE_EQUITY": average_equity,
    }
