"""Frozen factor diagnostics: no future availability or synthetic missing costs."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astock.candidates.factor_diagnostics import FactorObservation, evaluate_factor_panel

T = datetime(2026, 1, 2, tzinfo=UTC)


def observation(name, value, outcome, *, cost=None):
    return FactorObservation(
        name, "industry", T, T-timedelta(days=1), T+timedelta(days=1),
        T+timedelta(days=2), value, outcome, cost, "a"*64, ("source:frozen",),
    )


def evaluate(rows):
    return evaluate_factor_panel(
        tuple(rows), factor_id="cash-conversion", formula_version="frozen-v1",
        evaluated_at=T+timedelta(days=3), quantiles=2,
    )


def test_monotone_rank_ic_known_costs_and_missing_outcomes_are_separate():
    rows = [observation("A", 1, .01), observation("B", 2, .02, cost=.001),
            observation("C", 3, .03, cost=.002), observation("D", None, .1),
            observation("E", 4, None)]
    result = evaluate(rows)
    assert result["mean_date_rank_ic"] == pytest.approx(1)
    assert result["excluded"] == {"MISSING_FEATURE": 1, "MISSING_OUTCOME": 1, "IMMATURE_OUTCOME": 0}
    assert result["dates"][0]["quantiles"][0]["net_return"] is None
    assert not result["blind_tranche_mutation_allowed"]
    assert not result["market_validation_claim"]


def test_ties_stay_together_and_ticker_order_does_not_create_alpha():
    rows = [observation("A", 1, .02), observation("B", 1, .01),
            observation("C", 2, .04), observation("D", 2, .03)]
    first, second = evaluate(rows), evaluate(rows[::-1])
    assert first == second
    assert [q["count"] for q in first["dates"][0]["quantiles"]] == [2, 2]
    constant = evaluate([observation("A", 1, .01), observation("B", 1, .02)])
    assert constant["mean_date_rank_ic"] is None


def test_future_features_duplicate_revisions_and_row_budget_are_rejected():
    row = observation("A", 1, .01)
    with pytest.raises(ValueError, match="not available"):
        replace(row, feature_available_at=T+timedelta(seconds=1))
    with pytest.raises(ValueError, match="duplicate"):
        evaluate([row, row])
    with pytest.raises(ValueError, match="row budget"):
        evaluate_factor_panel(
            (row, row), factor_id="f", formula_version="v", evaluated_at=T,
            max_rows=1,
        )
    immature = evaluate([replace(row, outcome_available_at=T+timedelta(days=10))])
    assert immature["date_count"] == 0 and immature["excluded"]["IMMATURE_OUTCOME"] == 1
    with pytest.raises(ValueError, match="identity"):
        replace(row, source_ids=("",))
