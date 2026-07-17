from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from astock.financial_integrity import (
    altman_z_score,
    balance_identity_difference,
    cash_identity_difference,
    decimal_ratio,
    midrank_percentile,
)

decimals = st.decimals(
    min_value=Decimal("-1000000000"),
    max_value=Decimal("1000000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
nonzero_decimals = decimals.filter(lambda value: value != 0)


@given(liabilities=decimals, equity=decimals)
def test_balance_identity_is_exact_for_constructed_equation(
    liabilities: Decimal,
    equity: Decimal,
) -> None:
    assets = liabilities + equity
    assert balance_identity_difference(assets, liabilities, equity) == 0


@given(
    beginning=decimals,
    operating=decimals,
    investing=decimals,
    financing=decimals,
    exchange=decimals,
)
def test_cash_identity_is_exact_for_constructed_equation(
    beginning: Decimal,
    operating: Decimal,
    investing: Decimal,
    financing: Decimal,
    exchange: Decimal,
) -> None:
    ending = beginning + operating + investing + financing + exchange
    assert (
        cash_identity_difference(
            ending,
            beginning,
            operating,
            investing,
            financing,
            exchange,
        )
        == 0
    )


@given(numerator=decimals, denominator=nonzero_decimals, scale=st.integers(1, 10000))
def test_ratio_is_invariant_to_common_unit_scale(
    numerator: Decimal,
    denominator: Decimal,
    scale: int,
) -> None:
    factor = Decimal(scale)
    assert decimal_ratio(numerator, denominator) == decimal_ratio(
        numerator * factor, denominator * factor
    )


@given(
    target=decimals,
    peers=st.lists(decimals, min_size=3, max_size=30),
)
def test_midrank_percentile_is_input_order_invariant(
    target: Decimal, peers: list[Decimal]
) -> None:
    assert midrank_percentile(target, peers) == midrank_percentile(target, list(reversed(peers)))


@given(scale=st.integers(1, 10000))
def test_altman_score_is_currency_scale_invariant(scale: int) -> None:
    values = {
        "total_assets": Decimal("1000"),
        "total_liabilities": Decimal("600"),
        "current_assets": Decimal("500"),
        "current_liabilities": Decimal("300"),
        "retained_earnings": Decimal("160"),
        "ebit": Decimal("120"),
        "market_cap": Decimal("900"),
        "revenue": Decimal("1000"),
    }
    scaled = {key: value * Decimal(scale) for key, value in values.items()}
    assert altman_z_score(values)[0] == altman_z_score(scaled)[0]
