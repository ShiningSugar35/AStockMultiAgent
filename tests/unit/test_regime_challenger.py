"""Numerical and PIT regression tests for the real HMM training implementation.

The numerical samples are explicitly synthetic and cannot qualify live/shadow
admission. The recurrence is checked against exhaustive hidden-path sums.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from astock.investor_orchestration.models import MarketRegimeFeatureSnapshot, RegimeState
from astock.investor_orchestration.regime_challenger import (
    GaussianMarkovChallenger,
    GaussianRegimeModel,
    RegimeChallengerPolicy,
)

BASE = datetime(2025, 1, 1, tzinfo=UTC)
FEATURE_POLICY = "recorded-numerical-feature-policy"


def policy(**updates: Any) -> RegimeChallengerPolicy:
    values: dict[str, Any] = {
        "version": "recorded-two-state-test",
        "history_tier": "CORE_LONG_HISTORY",
        "feature_fields": ("trend_score", "breadth_score"),
        "state_prototypes": {
            RegimeState.HEALTHY_BULL: (0.5, 0.5),
            RegimeState.TREND_BEAR: (-0.5, -0.5),
        },
        "minimum_train_samples": 20,
        "maximum_iterations": 30,
    }
    values.update(updates)
    return RegimeChallengerPolicy.model_validate(values)


def history(count: int = 120) -> list[MarketRegimeFeatureSnapshot]:
    rng = np.random.default_rng(20260907)
    rows = []
    for index in range(count):
        sign = 1 if index // 15 % 2 == 0 else -1
        values = np.clip(rng.normal(sign * np.array([0.65, 0.35]), 0.045), -1, 1)
        rows.append(
            MarketRegimeFeatureSnapshot(
                feature_snapshot_id=f"synthetic-hmm-test-{index}",
                as_of=BASE + timedelta(days=index),
                trend_score=float(values[0]),
                breadth_score=float(values[1]),
                source_revisions={"feature_policy": FEATURE_POLICY},
            )
        )
    return rows


@pytest.fixture(scope="module")
def fitted():
    challenger = GaussianMarkovChallenger(policy())
    rows = history()
    model = challenger.fit(
        rows[:80], training_cutoff=rows[79].as_of, feature_policy_hash=FEATURE_POLICY
    )
    return challenger, model, rows


def test_forward_filter_matches_exhaustive_hidden_path_probability() -> None:
    initial = np.array([0.3, 0.7])
    transition = np.array([[0.8, 0.2], [0.1, 0.9]])
    likelihoods = np.array([[0.7, 0.2], [0.2, 0.6], [0.8, 0.3], [0.1, 0.9]])
    probabilities, densities = GaussianMarkovChallenger._forward(
        np.log(likelihoods),
        initial,
        transition,
        [0],
    )
    previous_mass = 1.0
    for stop in range(1, len(likelihoods) + 1):
        masses = np.zeros(2)
        for path in itertools.product(range(2), repeat=stop):
            mass = initial[path[0]] * likelihoods[0, path[0]]
            for index in range(1, stop):
                mass *= transition[path[index - 1], path[index]] * likelihoods[index, path[index]]
            masses[path[-1]] += mass
        np.testing.assert_allclose(probabilities[stop - 1], masses / masses.sum(), rtol=1e-12)
        assert densities[stop - 1] == pytest.approx(np.log(masses.sum() / previous_mass))
        previous_mass = masses.sum()


def test_em_fits_parameters_instead_of_returning_configured_constants(fitted: Any) -> None:
    _, model, _ = fitted
    assert len(model.log_likelihood_trace) > 1
    assert model.log_likelihood_trace[-1] > model.log_likelihood_trace[0]
    assert model.means[0] != (0.5, 0.5)
    assert model.transition[0][0] != 0.85
    assert model.means[0][0] > 0.4 and model.means[1][0] < -0.4
    assert sum(model.effective_state_samples) == pytest.approx(80)
    model.assert_integrity()
    assert model.training_origin == "UNREGISTERED_DIAGNOSTIC"
    assert model.admission_status == "SHADOW_ONLY"


def test_filtered_prefix_is_invariant_to_future_observations(fitted: Any) -> None:
    challenger, model, rows = fitted
    short = challenger.filter(model, rows[80:88])
    long = challenger.filter(model, rows[80:])
    assert short == long[:8]
    poisoned_tail = [
        item.model_copy(update={"trend_score": -1.0, "breadth_score": 1.0}) for item in rows[88:]
    ]
    adversarial = challenger.filter(model, [*rows[80:88], *poisoned_tail])
    assert short == adversarial[:8]
    assert all(sum(item.probabilities.values()) == pytest.approx(1) for item in long)


def test_numerical_parameters_are_deterministic_and_do_not_consume_holdout(fitted: Any) -> None:
    challenger, original, rows = fitted
    repeated = challenger.fit(
        rows[:80], training_cutoff=rows[79].as_of, feature_policy_hash=FEATURE_POLICY
    )
    assert repeated.means == original.means
    assert repeated.variances == original.variances
    assert repeated.transition == original.transition
    assert repeated.training_input_hash == original.training_input_hash
    assert repeated.parameter_hash == original.parameter_hash


def test_training_cutoff_and_out_of_sample_boundaries_are_enforced(fitted: Any) -> None:
    challenger, model, rows = fitted
    with pytest.raises(ValueError, match="cutoff"):
        challenger.fit(
            rows[:80], training_cutoff=rows[70].as_of, feature_policy_hash=FEATURE_POLICY
        )
    with pytest.raises(ValueError, match="training interval"):
        challenger.filter(model, rows[79:90])
    with pytest.raises(ValueError, match="future"):
        challenger.fit(
            rows[:80],
            training_cutoff=datetime.now(UTC) + timedelta(days=1),
            feature_policy_hash=FEATURE_POLICY,
        )


@pytest.mark.parametrize("case", ["reverse", "duplicate", "missing", "wrong-policy", "nonfinite"])
def test_invalid_feature_history_does_not_produce_a_model(case: str) -> None:
    rows = history(30)
    if case == "reverse":
        rows.reverse()
    elif case == "duplicate":
        rows[1] = rows[0]
    elif case == "missing":
        rows[3] = rows[3].model_copy(update={"trend_score": None})
    elif case == "wrong-policy":
        rows[3] = rows[3].model_copy(update={"source_revisions": {"feature_policy": "different"}})
    else:
        rows[3] = rows[3].model_copy(update={"trend_score": float("nan")})
    with pytest.raises(ValueError):
        GaussianMarkovChallenger(policy()).fit(
            rows, training_cutoff=BASE + timedelta(days=40), feature_policy_hash=FEATURE_POLICY
        )


def test_model_serialization_round_trip_and_parameter_tampering(fitted: Any) -> None:
    challenger, model, rows = fitted
    recovered = GaussianRegimeModel.model_validate_json(model.model_dump_json())
    assert challenger.filter(recovered, rows[80:85]) == challenger.filter(model, rows[80:85])
    altered = model.model_copy(update={"means": ((0.0, 0.0), model.means[1])})
    with pytest.raises(ValueError, match="hash"):
        challenger.filter(altered, rows[80:85])


def test_walk_forward_uses_disjoint_chronological_windows_and_does_not_claim_calibration() -> None:
    folds = GaussianMarkovChallenger(policy()).walk_forward(
        history(90),
        feature_policy_hash=FEATURE_POLICY,
        train_size=40,
        test_size=10,
        gap=2,
    )
    assert len(folds) == 4
    assert all(fold.train_end < fold.test_start <= fold.test_end for fold in folds)
    assert all(fold.train_count == 40 and fold.test_count == 10 for fold in folds)
    assert all(
        fold.calibration_status == "NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME" for fold in folds
    )
    assert all(0 <= fold.switch_rate <= 1 for fold in folds)
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert earlier.test_end < later.test_start


def test_time_gaps_reset_filter_instead_of_joining_unobserved_history(fitted: Any) -> None:
    challenger, model, rows = fitted
    distant = [rows[80].model_copy(update={"as_of": BASE + timedelta(days=200)})]
    result = challenger.filter(model, distant)[0]
    vector = np.array([[distant[0].trend_score, distant[0].breadth_score]])
    emissions = challenger._emissions(vector, np.asarray(model.means), np.asarray(model.variances))
    expected, _ = challenger._forward(
        emissions, np.asarray(model.initial), np.asarray(model.transition), [0]
    )
    np.testing.assert_allclose(list(result.probabilities.values()), expected[0])


def test_training_expectation_matches_exact_smoothed_marginals_and_transition_counts() -> None:
    initial = np.array([0.3, 0.7])
    transition = np.array([[0.8, 0.2], [0.1, 0.9]])
    likelihoods = np.array([[0.7, 0.2], [0.2, 0.6], [0.8, 0.3]])
    gamma, xi, start_counts, log_density = GaussianMarkovChallenger._expectation(
        np.log(likelihoods),
        initial,
        transition,
        [0],
    )
    exact_gamma, exact_xi, total = np.zeros_like(likelihoods), np.zeros_like(transition), 0.0
    for path in itertools.product(range(2), repeat=3):
        mass = initial[path[0]] * likelihoods[0, path[0]]
        for index in range(1, 3):
            mass *= transition[path[index - 1], path[index]] * likelihoods[index, path[index]]
        total += mass
        for index, state in enumerate(path):
            exact_gamma[index, state] += mass
        for previous, following in zip(path, path[1:], strict=False):
            exact_xi[previous, following] += mass
    np.testing.assert_allclose(gamma, exact_gamma / total, rtol=1e-12)
    np.testing.assert_allclose(xi, exact_xi / total, rtol=1e-12)
    np.testing.assert_allclose(start_counts, gamma[0], rtol=1e-12)
    assert log_density == pytest.approx(np.log(total))


def test_nested_challenger_policy_mutation_cannot_silently_change_training() -> None:
    challenger = GaussianMarkovChallenger(policy())
    challenger.policy.state_prototypes[RegimeState.HEALTHY_BULL] = (-0.9, -0.9)
    rows = history(30)
    with pytest.raises(ValueError, match="policy changed"):
        challenger.fit(rows, training_cutoff=rows[-1].as_of, feature_policy_hash=FEATURE_POLICY)


def test_unobserved_statuses_are_not_learned_market_states() -> None:
    with pytest.raises(ValueError, match="latent"):
        policy(
            state_prototypes={RegimeState.UNCLASSIFIED: (0.0, 0.0), RegimeState.PANIC: (-1.0, -1.0)}
        )
