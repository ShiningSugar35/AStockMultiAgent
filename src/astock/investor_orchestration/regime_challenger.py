"""Train-only Gaussian HMM challenger with causal, filtered inference.

EM may smooth the training window internally; published test probabilities use
only forward filtering. Semantic prototypes and hyperparameters are frozen by
policy, never renamed using holdout returns. Hidden-state probabilities are NOT
observable-outcome calibration, and every fitted model remains SHADOW_ONLY.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import AwareDatetime, Field, model_validator
from scipy.optimize import linear_sum_assignment
from scipy.special import logsumexp

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import (
    MarketRegimeFeatureSnapshot,
    RegimeState,
    StrictModel,
)
from astock.investor_orchestration.regime_features import FAMILY_FIELDS, RegimeFeatureBuilder
from astock.investor_orchestration.utils import content_hash, utc_now

Array = NDArray[np.float64]


class RegimeChallengerPolicy(StrictModel):
    schema_version: Literal["regime-challenger-policy-v1"] = "regime-challenger-policy-v1"
    version: str = Field(min_length=1)
    feature_fields: tuple[str, ...] = Field(min_length=2, max_length=7)
    state_prototypes: dict[RegimeState, tuple[float, ...]]
    minimum_train_samples: int = Field(default=120, ge=20)
    maximum_train_samples: int = Field(default=10000, ge=20)
    maximum_iterations: int = Field(default=50, ge=1, le=500)
    convergence_tolerance: float = Field(default=1e-5, gt=0)
    variance_floor: float = Field(default=0.0025, gt=0, le=1)
    transition_pseudocount: float = Field(default=0.01, gt=0)
    initial_persistence: float = Field(default=0.85, gt=0, lt=1)
    maximum_gap_days: float = Field(default=10, gt=0)
    history_tier: Literal["CORE_LONG_HISTORY", "ENRICHED_HISTORY"]

    @model_validator(mode="after")
    def shape_and_semantics(self) -> RegimeChallengerPolicy:
        if len(set(self.feature_fields)) != len(self.feature_fields):
            raise ValueError("challenger feature fields must be unique")
        if not set(self.feature_fields) <= set(FAMILY_FIELDS.values()):
            raise ValueError("challenger policy names an unsupported feature")
        if not 2 <= len(self.state_prototypes) <= 6:
            raise ValueError("challenger needs two to six frozen semantic states")
        if {RegimeState.UNCLASSIFIED, RegimeState.TRANSITION} & self.state_prototypes.keys():
            raise ValueError("data-availability gates are not latent market states")
        for values in self.state_prototypes.values():
            if len(values) != len(self.feature_fields) or any(
                not -1 <= value <= 1 for value in values
            ):
                raise ValueError("semantic prototype must match normalized feature dimensions")
        if self.maximum_train_samples < self.minimum_train_samples:
            raise ValueError("challenger training sample limits conflict")
        return self


class GaussianRegimeModel(StrictModel):
    schema_version: Literal["gaussian-regime-model-v1"] = "gaussian-regime-model-v1"
    policy: RegimeChallengerPolicy
    feature_policy_hash: str
    training_input_hash: str
    training_artifact_ids: tuple[str, ...] = ()
    training_origin: Literal["REGISTERED_PIT_FEATURES", "UNREGISTERED_DIAGNOSTIC"]
    training_start: AwareDatetime
    training_end: AwareDatetime
    fitted_at: AwareDatetime
    training_samples: int
    state_order: tuple[RegimeState, ...]
    initial: tuple[float, ...]
    transition: tuple[tuple[float, ...], ...]
    means: tuple[tuple[float, ...], ...]
    variances: tuple[tuple[float, ...], ...]
    last_filtered_probability: tuple[float, ...]
    effective_state_samples: tuple[float, ...]
    log_likelihood_trace: tuple[float, ...]
    converged: bool
    parameter_hash: str = ""
    admission_status: Literal["SHADOW_ONLY"] = "SHADOW_ONLY"
    calibration_status: Literal["NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"] = (
        "NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"
    )

    @model_validator(mode="after")
    def model_shape(self) -> GaussianRegimeModel:
        k, d = len(self.state_order), len(self.policy.feature_fields)
        if (
            set(self.state_order) != set(self.policy.state_prototypes)
            or len(set(self.state_order)) != k
        ):
            raise ValueError("fitted model state mapping differs from frozen policy")
        for vector in (self.initial, self.last_filtered_probability):
            if (
                len(vector) != k
                or any(value < 0 for value in vector)
                or not math.isclose(sum(vector), 1, abs_tol=1e-8)
            ):
                raise ValueError("invalid fitted probability vector")
        if len(self.transition) != k or any(len(row) != k for row in self.transition):
            raise ValueError("transition matrix shape mismatch")
        if any(
            any(value <= 0 for value in row) or not math.isclose(sum(row), 1, abs_tol=1e-8)
            for row in self.transition
        ):
            raise ValueError("fitted transition rows must be positive and normalized")
        if any(
            len(matrix) != k or any(len(row) != d for row in matrix)
            for matrix in (self.means, self.variances)
        ):
            raise ValueError("Gaussian emission shape mismatch")
        if any(value <= 0 for row in self.variances for value in row):
            raise ValueError("Gaussian variances must be strictly positive")
        if len(self.effective_state_samples) != k or any(
            value < 0 for value in self.effective_state_samples
        ):
            raise ValueError("invalid effective state sample counts")
        if not self.training_start <= self.training_end <= self.fitted_at:
            raise ValueError("fitted model training interval is invalid")
        return self

    def assert_integrity(self) -> None:
        expected = content_hash(
            self.model_dump(mode="json", exclude={"parameter_hash", "fitted_at"})
        )
        if expected != self.parameter_hash:
            raise ValueError("challenger parameter hash mismatch")


class RegimeFilteredPrediction(StrictModel):
    as_of: AwareDatetime
    feature_snapshot_id: str
    model_parameter_hash: str
    probabilities: dict[RegimeState, float]
    selected_state: RegimeState
    entropy: float
    predictive_log_density: float
    ood_score: float
    admission_status: Literal["SHADOW_ONLY"] = "SHADOW_ONLY"


class RegimeWalkForwardFold(StrictModel):
    train_start: AwareDatetime
    train_end: AwareDatetime
    test_start: AwareDatetime
    test_end: AwareDatetime
    train_count: int
    test_count: int
    model_parameter_hash: str
    mean_log_density: float
    mean_entropy: float
    switch_rate: float
    mean_ood_score: float
    calibration_status: Literal["NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"] = (
        "NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"
    )
    predictions: tuple[RegimeFilteredPrediction, ...]


class GaussianMarkovChallenger:
    def __init__(self, policy: RegimeChallengerPolicy) -> None:
        self.policy = RegimeChallengerPolicy.model_validate(policy.model_dump())
        self.policy_hash = content_hash(self.policy)

    def _matrix(
        self, rows: Sequence[MarketRegimeFeatureSnapshot], feature_policy_hash: str
    ) -> tuple[list[MarketRegimeFeatureSnapshot], Array]:
        if content_hash(self.policy) != self.policy_hash:
            raise ValueError("challenger policy changed after it was frozen")
        checked = [MarketRegimeFeatureSnapshot.model_validate(row.model_dump()) for row in rows]
        if not checked:
            raise ValueError("challenger needs nonempty feature history")
        times = [row.as_of for row in checked]
        if any(left >= right for left, right in zip(times, times[1:], strict=False)):
            raise ValueError("challenger history must be strictly time-ordered")
        if times[-1] > utc_now():
            raise ValueError("future features cannot enter challenger history")
        if any(
            row.source_revisions.get("feature_policy") != feature_policy_hash for row in checked
        ):
            raise ValueError("challenger cannot mix feature policy versions")
        raw = [[getattr(row, field) for field in self.policy.feature_fields] for row in checked]
        if any(value is None for row in raw for value in row):
            raise ValueError(
                "missing features require a separate frozen history-tier model, not zero imputation"
            )
        values = np.asarray(raw, dtype=np.float64)
        if not np.isfinite(values).all():
            raise ValueError("challenger matrix contains nonfinite values")
        return checked, values

    @staticmethod
    def _emissions(values: Array, means: Array, variances: Array) -> Array:
        return -0.5 * (
            np.log(2 * math.pi * variances)[None, :, :]
            + (values[:, None, :] - means[None, :, :]) ** 2 / variances[None, :, :]
        ).sum(axis=2)

    @staticmethod
    def _expectation(
        emissions: Array, initial: Array, transition: Array, starts: Sequence[int]
    ) -> tuple[Array, Array, Array, float]:
        t, k = emissions.shape
        alpha, beta = np.empty_like(emissions), np.zeros_like(emissions)
        gamma, xi = np.empty_like(emissions), np.zeros((k, k), dtype=np.float64)
        initial_counts = np.zeros(k, dtype=np.float64)
        log_a = np.log(transition)
        total_ll = 0.0
        boundaries = [*starts, t]
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            alpha[start] = np.log(np.maximum(initial, 1e-300)) + emissions[start]
            for index in range(start + 1, end):
                alpha[index] = emissions[index] + logsumexp(
                    alpha[index - 1][:, None] + log_a, axis=0
                )
            ll = float(logsumexp(alpha[end - 1]))
            total_ll += ll
            for index in range(end - 2, start - 1, -1):
                beta[index] = logsumexp(
                    log_a + emissions[index + 1][None, :] + beta[index + 1][None, :], axis=1
                )
            gamma[start:end] = np.exp(alpha[start:end] + beta[start:end] - ll)
            gamma[start:end] /= gamma[start:end].sum(axis=1, keepdims=True)
            initial_counts += gamma[start]
            for index in range(start, end - 1):
                log_xi = (
                    alpha[index][:, None]
                    + log_a
                    + emissions[index + 1][None, :]
                    + beta[index + 1][None, :]
                )
                xi += np.exp(log_xi - float(logsumexp(log_xi)))
        return gamma, xi, initial_counts, total_ll

    def fit(
        self,
        rows: Sequence[MarketRegimeFeatureSnapshot],
        *,
        training_cutoff: datetime,
        feature_policy_hash: str,
    ) -> GaussianRegimeModel:
        if (
            training_cutoff.tzinfo is None
            or training_cutoff.utcoffset() is None
            or training_cutoff > utc_now()
        ):
            raise ValueError("training cutoff must be aware and not in the future")
        checked, values = self._matrix(rows, feature_policy_hash)
        n, d = values.shape
        if not self.policy.minimum_train_samples <= n <= self.policy.maximum_train_samples:
            raise ValueError("challenger history is outside frozen training sample limits")
        if checked[-1].as_of > training_cutoff:
            raise ValueError("training window includes data after its frozen cutoff")
        states = tuple(self.policy.state_prototypes)
        prototypes = np.asarray(
            [self.policy.state_prototypes[state] for state in states], dtype=np.float64
        )
        k = len(states)
        means = prototypes.copy()
        variances = np.tile(np.maximum(np.var(values, axis=0), self.policy.variance_floor), (k, 1))
        initial = np.full(k, 1 / k, dtype=np.float64)
        transition = np.full(
            (k, k), (1 - self.policy.initial_persistence) / (k - 1), dtype=np.float64
        )
        np.fill_diagonal(transition, self.policy.initial_persistence)
        starts = [0] + [
            index
            for index in range(1, n)
            if (checked[index].as_of - checked[index - 1].as_of).total_seconds()
            > self.policy.maximum_gap_days * 86400
        ]
        trace: list[float] = []
        converged = False
        gamma = np.full((n, k), 1 / k, dtype=np.float64)
        for _ in range(self.policy.maximum_iterations):
            emissions = self._emissions(values, means, variances)
            gamma, xi, start_counts, ll = self._expectation(emissions, initial, transition, starts)
            if not math.isfinite(ll):
                raise ValueError("nonfinite HMM likelihood; no fitted artifact produced")
            trace.append(ll)
            if len(trace) > 1 and abs(
                trace[-1] - trace[-2]
            ) <= self.policy.convergence_tolerance * (1 + abs(trace[-2])):
                converged = True
                break
            weights = gamma.sum(axis=0)
            denominator = np.maximum(weights[:, None], 1e-12)
            updated_means = gamma.T @ values / denominator
            updated_variances = (gamma.T @ (values**2)) / denominator - updated_means**2
            active = weights > 1e-8
            means[active] = updated_means[active]
            variances[active] = np.maximum(updated_variances[active], self.policy.variance_floor)
            transition = xi + self.policy.transition_pseudocount
            transition /= transition.sum(axis=1, keepdims=True)
            initial = start_counts + self.policy.transition_pseudocount
            initial /= initial.sum()
        # Match learned emissions to the PREDECLARED feature prototypes using train data only.
        _, columns = linear_sum_assignment(
            ((prototypes[:, None, :] - means[None, :, :]) ** 2).sum(axis=2)
        )
        means, variances = means[columns], variances[columns]
        transition = transition[columns][:, columns]
        initial = initial[columns]
        emissions = self._emissions(values, means, variances)
        gamma, _, _, final_ll = self._expectation(emissions, initial, transition, starts)
        if not trace or final_ll != trace[-1]:
            trace.append(final_ll)
        filtered, _ = self._forward(emissions, initial, transition, starts)
        model = GaussianRegimeModel(
            policy=self.policy,
            feature_policy_hash=feature_policy_hash,
            training_input_hash=content_hash([row.model_dump(mode="json") for row in checked]),
            training_origin="UNREGISTERED_DIAGNOSTIC",
            training_start=checked[0].as_of,
            training_end=training_cutoff,
            fitted_at=utc_now(),
            training_samples=n,
            state_order=states,
            initial=tuple(initial.tolist()),
            transition=tuple(tuple(row) for row in transition.tolist()),
            means=tuple(tuple(row) for row in means.tolist()),
            variances=tuple(tuple(row) for row in variances.tolist()),
            last_filtered_probability=tuple(filtered[-1].tolist()),
            effective_state_samples=tuple(gamma.sum(axis=0).tolist()),
            log_likelihood_trace=tuple(trace),
            converged=converged,
        )
        return model.model_copy(
            update={
                "parameter_hash": content_hash(
                    model.model_dump(mode="json", exclude={"parameter_hash", "fitted_at"})
                )
            }
        )

    @staticmethod
    def _forward(
        emissions: Array, initial: Array, transition: Array, starts: Sequence[int]
    ) -> tuple[Array, Array]:
        probabilities = np.empty_like(emissions)
        densities = np.empty(len(emissions), dtype=np.float64)
        starts_set = set(starts)
        for index, emission in enumerate(emissions):
            prior = initial if index in starts_set else probabilities[index - 1] @ transition
            logits = np.log(np.maximum(prior, 1e-300)) + emission
            densities[index] = float(logsumexp(logits))
            probabilities[index] = np.exp(logits - densities[index])
        return probabilities, densities

    def filter(
        self,
        model: GaussianRegimeModel,
        rows: Sequence[MarketRegimeFeatureSnapshot],
    ) -> tuple[RegimeFilteredPrediction, ...]:
        model = GaussianRegimeModel.model_validate(model.model_dump())
        model.assert_integrity()
        if content_hash(model.policy) != self.policy_hash:
            raise ValueError("challenger policy differs from the fitted model")
        checked, values = self._matrix(rows, model.feature_policy_hash)
        if checked[0].as_of <= model.training_end:
            raise ValueError("out-of-sample filtering cannot include the training interval")
        transition = np.asarray(model.transition, dtype=np.float64)
        initial = np.asarray(model.last_filtered_probability, dtype=np.float64) @ transition
        base_initial = np.asarray(model.initial, dtype=np.float64)
        means, variances = (
            np.asarray(model.means, dtype=np.float64),
            np.asarray(model.variances, dtype=np.float64),
        )
        emissions = self._emissions(values, means, variances)
        probabilities, densities = (
            np.empty_like(emissions),
            np.empty(len(emissions), dtype=np.float64),
        )
        previous_time = model.training_end
        for index, row in enumerate(checked):
            gap = (row.as_of - previous_time).total_seconds() > self.policy.maximum_gap_days * 86400
            prior = (
                base_initial
                if gap
                else initial
                if index == 0
                else probabilities[index - 1] @ transition
            )
            logits = np.log(np.maximum(prior, 1e-300)) + emissions[index]
            densities[index] = float(logsumexp(logits))
            probabilities[index] = np.exp(logits - densities[index])
            previous_time = row.as_of
        results = []
        for row, vector, density, values_row in zip(
            checked, probabilities, densities, values, strict=True
        ):
            probabilities_by_state = {
                state: float(value) for state, value in zip(model.state_order, vector, strict=True)
            }
            distance = float(np.min(np.mean((values_row - means) ** 2 / variances, axis=1)))
            results.append(
                RegimeFilteredPrediction(
                    as_of=row.as_of,
                    feature_snapshot_id=row.feature_snapshot_id,
                    model_parameter_hash=model.parameter_hash,
                    probabilities=probabilities_by_state,
                    selected_state=max(
                        probabilities_by_state, key=probabilities_by_state.__getitem__
                    ),
                    entropy=float(-np.sum(vector * np.log(np.maximum(vector, 1e-300)))),
                    predictive_log_density=float(density),
                    ood_score=min(1.0, distance / 16.0),
                )
            )
        return tuple(results)

    def walk_forward(
        self,
        rows: Sequence[MarketRegimeFeatureSnapshot],
        *,
        feature_policy_hash: str,
        train_size: int,
        test_size: int,
        gap: int = 1,
    ) -> tuple[RegimeWalkForwardFold, ...]:
        checked, _ = self._matrix(rows, feature_policy_hash)
        if train_size < self.policy.minimum_train_samples or test_size < 1 or gap < 0:
            raise ValueError("invalid frozen walk-forward window sizes")
        folds = []
        for train_end in range(train_size, len(checked) - gap - test_size + 1, test_size):
            train = checked[train_end - train_size : train_end]
            test = checked[train_end + gap : train_end + gap + test_size]
            model = self.fit(
                train, training_cutoff=train[-1].as_of, feature_policy_hash=feature_policy_hash
            )
            predictions = self.filter(model, test)
            changes = sum(
                left.selected_state != right.selected_state
                for left, right in zip(predictions, predictions[1:], strict=False)
            )
            folds.append(
                RegimeWalkForwardFold(
                    train_start=train[0].as_of,
                    train_end=train[-1].as_of,
                    test_start=test[0].as_of,
                    test_end=test[-1].as_of,
                    train_count=len(train),
                    test_count=len(test),
                    model_parameter_hash=model.parameter_hash,
                    mean_log_density=sum(item.predictive_log_density for item in predictions)
                    / len(test),
                    mean_entropy=sum(item.entropy for item in predictions) / len(test),
                    switch_rate=changes / max(1, len(test) - 1),
                    mean_ood_score=sum(item.ood_score for item in predictions) / len(test),
                    predictions=predictions,
                )
            )
        if not folds:
            raise ValueError("history is too short for one complete walk-forward fold")
        return tuple(folds)

    def fit_registered(
        self,
        state: StateStore,
        objects: ObjectStore,
        artifact_ids: Sequence[str],
        *,
        training_cutoff: datetime,
        feature_policy_hash: str,
    ) -> GaussianRegimeModel:
        snapshots = []
        for artifact_id in artifact_ids:
            report = RegimeFeatureBuilder.load_registered(state, objects, artifact_id)
            if (
                report.policy_hash != feature_policy_hash
                or report.history_tier != self.policy.history_tier
            ):
                raise ValueError("registered history tier/feature policy does not match training")
            if report.as_of != report.snapshot.as_of:
                raise ValueError("registered feature report time does not match its snapshot")
            for audit in report.series_audits:
                for source_id, digest in zip(
                    audit.source_artifact_ids, audit.source_object_hashes, strict=True
                ):
                    source = state.artifact_record(source_id)
                    if source is None or source["object_hash"] != digest:
                        raise ValueError("training source lineage is missing or changed")
                    objects.get_bytes(digest)
            snapshots.append(report.snapshot)
        model = self.fit(
            snapshots, training_cutoff=training_cutoff, feature_policy_hash=feature_policy_hash
        )
        model = model.model_copy(
            update={
                "training_origin": "REGISTERED_PIT_FEATURES",
                "training_artifact_ids": tuple(artifact_ids),
            }
        )
        return model.model_copy(
            update={
                "parameter_hash": content_hash(
                    model.model_dump(mode="json", exclude={"parameter_hash", "fitted_at"})
                )
            }
        )

    @staticmethod
    def register(model: GaussianRegimeModel, state: StateStore, objects: ObjectStore) -> str:
        model = GaussianRegimeModel.model_validate(model.model_dump())
        model.assert_integrity()
        reference = objects.put_json(model.model_dump(mode="json"))
        artifact_id = "GaussianRegimeModel:" + reference.sha256
        state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="GaussianRegimeModel",
            schema_version=model.schema_version,
            object_hash=reference.sha256,
            input_hashes=[model.training_input_hash, content_hash(model.policy)],
        )
        return artifact_id
