from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from astock.shadow.statistics import (
    deterministic_block_bootstrap,
    holm_adjust,
    maximum_drawdown,
    maximum_drawdown_from_pnl,
    percentile,
    wilson_interval,
)

AS_OF = datetime(2026, 7, 17, tzinfo=UTC)


def test_hash_seeded_block_bootstrap_is_stable_and_bounded() -> None:
    values = [
        Decimal("0.01"),
        Decimal("0.03"),
        Decimal("-0.01"),
        Decimal("0.02"),
        Decimal("0.04"),
    ]
    first = deterministic_block_bootstrap(
        values,
        seed="study:arm-a:arm-b",
        replicates=200,
        block_length=2,
        confidence_level=Decimal("0.95"),
        metric="DELTA",
        created_at=AS_OF,
    )
    second = deterministic_block_bootstrap(
        values,
        seed="study:arm-a:arm-b",
        replicates=200,
        block_length=2,
        confidence_level=Decimal("0.95"),
        metric="DELTA",
        created_at=AS_OF,
    )
    assert first == second
    interval, p_value = first
    assert interval.lower is not None
    assert interval.estimate is not None
    assert interval.upper is not None
    assert interval.lower <= interval.estimate <= interval.upper
    assert p_value is not None and Decimal("0") <= p_value <= Decimal("1")


def test_statistics_handle_empty_edges_holm_and_drawdown() -> None:
    empty, p_value = deterministic_block_bootstrap(
        [],
        seed="empty",
        replicates=100,
        block_length=5,
        confidence_level=Decimal("0.95"),
        metric="EMPTY",
        created_at=AS_OF,
    )
    assert empty.sample_count == 0
    assert empty.estimate is None
    assert p_value is None
    assert percentile([Decimal("1"), Decimal("3")], Decimal("0.5")) == Decimal("2")
    assert holm_adjust(
        {"a": Decimal("0.01"), "b": Decimal("0.03"), "c": Decimal("0.04")}
    ) == {"a": Decimal("0.03"), "b": Decimal("0.06"), "c": Decimal("0.06")}
    assert maximum_drawdown([Decimal("0.10"), Decimal("-0.20")]) == Decimal("0.20")
    assert maximum_drawdown_from_pnl(100, [10, -20]) == Decimal("20") / Decimal(
        "110"
    )

    all_wins = wilson_interval(
        10,
        10,
        confidence_level=Decimal("0.95"),
        created_at=AS_OF,
    )
    assert all_wins.estimate == Decimal("1")
    assert all_wins.lower is not None and all_wins.lower < Decimal("1")
    assert all_wins.upper == Decimal("1")

    all_losses = wilson_interval(
        0,
        10,
        confidence_level=Decimal("0.95"),
        created_at=AS_OF,
    )
    assert all_losses.estimate == Decimal("0")
    assert all_losses.lower == Decimal("0")
    assert all_losses.upper is not None and all_losses.upper > Decimal("0")

    constant, constant_p = deterministic_block_bootstrap(
        [Decimal("-0.02")] * 7,
        seed="constant-negative",
        replicates=100,
        block_length=5,
        confidence_level=Decimal("0.95"),
        metric="CONSTANT_NEGATIVE",
        created_at=AS_OF,
    )
    assert constant.lower == Decimal("-0.02")
    assert constant.estimate == Decimal("-0.02")
    assert constant.upper == Decimal("-0.02")
    assert constant_p == Decimal("1")
