"""Independent examples and negative contracts for paper-defined diagnostics."""
from math import sqrt

import pytest

from astock.shadow.statistics import (
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_probability,
    expected_maximum_sharpe,
    sample_sharpe,
)


def test_expected_maximum_scales_with_actual_trial_dispersion():
    # 100 zero-mean trial Sharpes with sample variance exactly one.
    values = [-sqrt(0.99)] * 50 + [sqrt(0.99)] * 50
    threshold = expected_maximum_sharpe(values, effective_trial_count=100)
    assert threshold is not None
    assert threshold == pytest.approx(2.53, abs=0.003)
    assert expected_maximum_sharpe(
        [2 * value for value in values], effective_trial_count=100
    ) == pytest.approx(2 * threshold)
    assert expected_maximum_sharpe(values, effective_trial_count=1) == 0


def test_dsr_needs_all_trials_and_disclosed_independence():
    returns = [-0.01, 0.02, 0.01, 0.03] * 20
    assert deflated_sharpe_probability(returns, selection_candidate_count=3) is None
    assert sample_sharpe(returns) is not None
    low_dispersion = deflated_sharpe_probability(
        returns, selection_candidate_count=3,
        trial_sharpes=[0.1, 0.2, 0.3], effective_trial_count=3,
    )
    high_dispersion = deflated_sharpe_probability(
        returns, selection_candidate_count=3,
        trial_sharpes=[-1.0, 0.0, 1.0], effective_trial_count=3,
    )
    assert low_dispersion is not None and high_dispersion is not None
    assert high_dispersion < low_dispersion
    with pytest.raises(ValueError, match="every attempted"):
        deflated_sharpe_probability(
            returns, selection_candidate_count=4,
            trial_sharpes=[0.1, 0.2, 0.3], effective_trial_count=3,
        )


def test_cscv_uses_all_six_complementary_half_splits_and_neutral_ties():
    # AA/BB and BB/AA both reverse the winner. Four mixed splits tie.
    # Mean criterion is intentionally frozen here: (2*1 + 4*0.5)/6 = 2/3.
    panel = [[3.0] * 4 + [0.0] * 4, [0.0] * 4 + [3.0] * 4]
    assert cscv_probability_of_backtest_overfitting(
        panel, block_count=4, metric="MEAN",
    ) == pytest.approx(2 / 3)
    assert cscv_probability_of_backtest_overfitting(
        panel[::-1], block_count=4, metric="MEAN",
    ) == pytest.approx(2 / 3)


def test_cscv_and_sharpe_refuse_missing_or_false_information():
    assert cscv_probability_of_backtest_overfitting(
        [[0.1] * 16, [0.1] * 16], block_count=4,
    ) is None
    assert cscv_probability_of_backtest_overfitting(
        [[0.1] * 17, [0.2] * 17], block_count=4,
    ) is None  # Do not silently truncate unmatched observations.
    with pytest.raises(ValueError, match="even block"):
        cscv_probability_of_backtest_overfitting([[1.0]*20]*2, block_count=5)
    with pytest.raises(ValueError, match="budget"):
        cscv_probability_of_backtest_overfitting(
            [[1.0]*32]*2, block_count=16, max_combinations=100,
        )
    with pytest.raises(ValueError, match="finite"):
        sample_sharpe([0.1, float("nan")])
    with pytest.raises(ValueError, match="observed trial"):
        expected_maximum_sharpe([0.1, 0.2], effective_trial_count=100)
