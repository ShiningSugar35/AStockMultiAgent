from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from astock.schemas import MarketRegime, ShadowExecutionObservation
from astock.shadow import ShadowEvaluationService


def test_walk_forward_reassigns_same_company_overlap_to_later_fold() -> None:
    start = datetime(2026, 7, 17, tzinfo=UTC)
    pairs: list[tuple[ShadowExecutionObservation, ShadowExecutionObservation]] = []
    for index in range(41):
        signal_time = start + timedelta(days=index * 10)
        company_id = (
            "company:overlap" if index in {19, 20} else f"company:{index:03d}"
        )
        observation = cast(
            ShadowExecutionObservation,
            SimpleNamespace(
                company_id=company_id,
                signal_time=signal_time,
                valuation_time=signal_time + timedelta(days=90),
            ),
        )
        pairs.append((observation, observation))

    folds = ShadowEvaluationService._walk_forward_folds(  # noqa: SLF001
        pairs,
        fold_size=20,
    )
    assert [(number, len(items), moved) for number, items, moved in folds] == [
        (1, 19, 0),
        (2, 21, 1),
        (3, 1, 0),
    ]


def test_profit_concentration_groups_distinct_snapshots_by_market_regime() -> None:
    class _Repository:
        @staticmethod
        def get_regime(regime_id: str) -> SimpleNamespace:
            regime = (
                MarketRegime.PANIC
                if regime_id in {"snapshot:panic:1", "snapshot:panic:2"}
                else MarketRegime.RANGE
            )
            return SimpleNamespace(regime=regime)

    service = cast(
        ShadowEvaluationService,
        SimpleNamespace(repository=_Repository()),
    )
    pairs = []
    for regime_id, delta in (
        ("snapshot:panic:1", Decimal("0.1")),
        ("snapshot:panic:2", Decimal("0.2")),
        ("snapshot:range:1", Decimal("0.1")),
    ):
        baseline = cast(
            ShadowExecutionObservation,
            SimpleNamespace(net_return=Decimal("0")),
        )
        experimental = cast(
            ShadowExecutionObservation,
            SimpleNamespace(net_return=delta, regime_id=regime_id),
        )
        pairs.append((baseline, experimental))

    single, regime = ShadowEvaluationService._profit_concentrations(  # noqa: SLF001
        service,
        pairs,
    )
    assert single == Decimal("0.5")
    assert regime == Decimal("0.75")
