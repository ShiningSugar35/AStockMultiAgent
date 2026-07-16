from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from astock.financial_integrity import (
    balance_identity_difference,
    cash_identity_difference,
    decimal_ratio,
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
