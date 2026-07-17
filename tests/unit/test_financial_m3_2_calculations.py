from __future__ import annotations

from decimal import Decimal

from astock.financial_integrity import (
    altman_z_score,
    beneish_m_score,
    dupont_decomposition,
    midrank_percentile,
    percentage_change,
    piotroski_f_score,
    sloan_accrual_ratio,
)


def _period() -> dict[str, Decimal]:
    return {
        "total_assets": Decimal("1000"),
        "total_liabilities": Decimal("600"),
        "total_equity": Decimal("400"),
        "current_assets": Decimal("500"),
        "current_liabilities": Decimal("300"),
        "retained_earnings": Decimal("160"),
        "ebit": Decimal("120"),
        "revenue": Decimal("1000"),
        "operating_cost": Decimal("600"),
        "accounts_receivable": Decimal("100"),
        "ppe": Decimal("300"),
        "depreciation": Decimal("30"),
        "sga": Decimal("100"),
        "net_profit": Decimal("100"),
        "cfo": Decimal("120"),
        "long_term_debt": Decimal("200"),
        "market_cap": Decimal("900"),
        "shares_outstanding": Decimal("100"),
    }


def test_beneish_identical_period_golden() -> None:
    score, components = beneish_m_score(_period(), _period())
    assert score == Decimal("-2.57358")
    assert set(components) == {"DSRI", "GMI", "AQI", "SGI", "DEPI", "SGAI", "LVGI", "TATA"}


def test_altman_sloan_and_dupont_golden() -> None:
    current = _period()
    z_score, z_components = altman_z_score(current)
    assert z_score == Decimal("2.760")
    assert z_components["WORKING_CAPITAL_TO_ASSETS"] == Decimal("0.2")
    accrual, _ = sloan_accrual_ratio(current, current)
    assert accrual == Decimal("-0.02")
    roe, components = dupont_decomposition(current, current)
    assert roe == Decimal("0.25")
    assert components["EQUITY_MULTIPLIER"] == Decimal("2.5")


def test_piotroski_has_nine_auditable_signals() -> None:
    prior = _period()
    current = dict(prior)
    current.update(
        {
            "net_profit": Decimal("120"),
            "cfo": Decimal("140"),
            "long_term_debt": Decimal("150"),
            "current_assets": Decimal("550"),
            "operating_cost": Decimal("550"),
            "revenue": Decimal("1100"),
        }
    )
    score, signals = piotroski_f_score(current, prior)
    assert len(signals) == 9
    assert score == sum(signals.values(), Decimal(0))
    assert score >= Decimal(8)


def test_changes_and_midrank_ties_are_stable() -> None:
    assert percentage_change(Decimal("120"), Decimal("100")) == Decimal("0.2")
    assert percentage_change(Decimal("80"), Decimal("-100")) == Decimal("1.8")
    peers = [Decimal("1"), Decimal("2"), Decimal("2"), Decimal("3")]
    assert midrank_percentile(Decimal("2"), peers) == Decimal("0.5")
