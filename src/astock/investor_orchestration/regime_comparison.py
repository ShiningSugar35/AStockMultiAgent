"""Read-only comparison of the transparent baseline and a fitted challenger."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MarketRegimeSnapshotV2, StrictModel
from astock.investor_orchestration.regime_challenger import (
    GaussianMarkovChallenger,
    GaussianRegimeModel,
    RegimeFilteredPrediction,
)
from astock.investor_orchestration.regime_features import (
    RegimeFeatureBuilder,
    RegimeFeatureBuildReport,
)


class RegimeShadowComparison(StrictModel):
    schema_version: Literal["regime-shadow-comparison-v1"] = "regime-shadow-comparison-v1"
    model_artifact_id: str
    model_parameter_hash: str
    feature_artifact_ids: tuple[str, ...]
    baseline_snapshots: tuple[MarketRegimeSnapshotV2, ...]
    challenger_predictions: tuple[RegimeFilteredPrediction, ...]
    disagreement_count: int
    admission_status: Literal["SHADOW_ONLY"] = "SHADOW_ONLY"
    calibration_status: Literal["NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"] = (
        "NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME"
    )
    broker_execution_allowed: Literal[False] = False


def read_registered_predictions(
    state: StateStore,
    objects: ObjectStore,
    *,
    model_artifact_id: str,
    feature_artifact_ids: Sequence[str],
) -> tuple[
    GaussianRegimeModel, tuple[RegimeFeatureBuildReport, ...], tuple[RegimeFilteredPrediction, ...]
]:
    record = state.artifact_record(model_artifact_id)
    if record is None or record["type"] != "GaussianRegimeModel":
        raise ValueError("challenger comparison requires a registered fitted model")
    model = GaussianRegimeModel.model_validate_json(objects.get_bytes(record["object_hash"]))
    model.assert_integrity()
    if record["schema_version"] != model.schema_version:
        raise ValueError("challenger schema version differs from its registry record")
    if model.training_origin != "REGISTERED_PIT_FEATURES":
        raise ValueError("unregistered diagnostic training is not a registered comparison")
    reports = tuple(
        RegimeFeatureBuilder.load_registered(state, objects, item) for item in feature_artifact_ids
    )
    if any(report.history_tier != model.policy.history_tier for report in reports):
        raise ValueError("challenger comparison cannot mix history tiers")
    predictions = GaussianMarkovChallenger(model.policy).filter(
        model, [report.snapshot for report in reports]
    )
    return model, reports, predictions
