"""Reproducible M3.3 anomaly training and inference over frozen M3.2 features."""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib.metadata import version
from typing import Any, cast

import numpy as np
from pyod.models.ecod import ECOD
from sklearn.ensemble import IsolationForest
from threadpoolctl import threadpool_limits

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.schemas import (
    FinancialAnomaly,
    FinancialAnomalyDataset,
    FinancialAnomalyEvaluationReport,
    FinancialAnomalyModelArtifact,
    FinancialAnomalyModelSpec,
    FinancialAnomalyModelType,
    FinancialAnomalySample,
    FinancialAnomalyScope,
    FinancialBenignExplanation,
    FinancialSeverity,
)


@dataclass(frozen=True, slots=True)
class FinancialAnomalyExecution:
    dataset_object_hash: str
    model_artifacts: list[FinancialAnomalyModelArtifact]
    target_assessments: list[FinancialAnomaly]
    evaluations: list[FinancialAnomalyEvaluationReport]
    benign_explanations: list[FinancialBenignExplanation]


@dataclass(frozen=True, slots=True)
class _FittedModel:
    serialized: bytes
    score_many: Any
    threshold: Decimal
    limitation_codes: list[str]
    robust_centers: np.ndarray[Any, np.dtype[np.float64]]
    robust_scales: np.ndarray[Any, np.dtype[np.float64]]


class FinancialAnomalyEngine:
    ENGINE_VERSION = "financial-anomaly-m3.3.0"

    def __init__(self, object_store: ObjectStore) -> None:
        self.object_store = object_store

    def run(
        self,
        dataset: FinancialAnomalyDataset,
        specs: list[FinancialAnomalyModelSpec],
        *,
        created_at: datetime,
    ) -> FinancialAnomalyExecution:
        dataset_payload = _strip_created_at(dataset.model_dump(mode="json"))
        dataset_object = self.object_store.put_json(dataset_payload)
        by_id = {sample.sample_id: sample for sample in dataset.samples}
        target = by_id[dataset.target_sample_id]
        target_matrix = _matrix([target], dataset.feature_names)
        artifacts: list[FinancialAnomalyModelArtifact] = []
        assessments: list[FinancialAnomaly] = []
        evaluations: list[FinancialAnomalyEvaluationReport] = []
        explanations: list[FinancialBenignExplanation] = []

        for spec in sorted(specs, key=lambda item: item.model_id):
            training_samples = anomaly_scope_training_samples(dataset, spec.scope)
            if len(training_samples) < spec.minimum_training_samples:
                raise ValueError(
                    f"model {spec.model_id} requires {spec.minimum_training_samples} "
                    f"training samples; received {len(training_samples)}"
                )
            training = _matrix(training_samples, dataset.feature_names)
            fitted = self._fit(spec, training)
            serialized_object = self.object_store.put_bytes(fitted.serialized)
            parameters = {
                "contamination": str(spec.contamination),
                "random_state": spec.random_state,
                **spec.parameters,
            }
            identity = {
                "model_id": spec.model_id,
                "model_type": spec.model_type.value,
                "model_version": spec.model_version,
                "scope": spec.scope.value,
                "dataset_object_hash": dataset_object.sha256,
                "serialized_model_object_hash": serialized_object.sha256,
                "feature_names": spec.feature_names,
                "training_sample_ids": [sample.sample_id for sample in training_samples],
                "parameters": parameters,
            }
            artifact_id = f"financial-model:{sha256_bytes(canonical_json_bytes(identity))}"
            artifact = FinancialAnomalyModelArtifact(
                model_artifact_id=artifact_id,
                model_id=spec.model_id,
                model_type=spec.model_type,
                model_version=spec.model_version,
                scope=spec.scope,
                dataset_id=dataset.dataset_id,
                feature_names=spec.feature_names,
                training_sample_ids=[sample.sample_id for sample in training_samples],
                parameters=parameters,
                library_versions=_library_versions(),
                dataset_object_hash=dataset_object.sha256,
                serialized_model_object_hash=serialized_object.sha256,
                created_at=created_at,
            )
            artifacts.append(artifact)
            target_score = Decimal(str(float(fitted.score_many(target_matrix)[0])))
            is_anomaly = target_score > fitted.threshold
            triggered = _triggered_features(
                target_matrix[0],
                fitted.robust_centers,
                fitted.robust_scales,
                spec.feature_names,
                force_one=is_anomaly,
            )
            anomaly_identity = {
                "model_artifact_id": artifact_id,
                "dataset_id": dataset.dataset_id,
                "sample_id": target.sample_id,
                "score": target_score,
                "threshold": fitted.threshold,
            }
            assessment = FinancialAnomaly(
                anomaly_id=(
                    f"financial-anomaly:"
                    f"{sha256_bytes(canonical_json_bytes(anomaly_identity))}"
                ),
                model_id=spec.model_id,
                model_version=spec.model_version,
                model_artifact_id=artifact_id,
                dataset_id=dataset.dataset_id,
                sample_id=target.sample_id,
                scope=spec.scope,
                score=target_score,
                threshold=fitted.threshold,
                is_anomaly=is_anomaly,
                triggered_features=triggered,
                limitation_codes=fitted.limitation_codes,
                severity=FinancialSeverity.MEDIUM if is_anomaly else FinancialSeverity.INFO,
                evidence_ids=target.evidence_ids,
                created_at=created_at,
            )
            assessments.append(assessment)
            evaluations.append(
                self._evaluate(
                    dataset,
                    spec,
                    artifact,
                    fitted,
                    anomaly_scope_evaluation_samples(dataset, spec.scope),
                    created_at,
                )
            )
            if is_anomaly:
                explanations.extend(
                    self._benign_explanations(target, assessment, created_at)
                )

        return FinancialAnomalyExecution(
            dataset_object_hash=dataset_object.sha256,
            model_artifacts=sorted(artifacts, key=lambda item: item.model_artifact_id),
            target_assessments=sorted(assessments, key=lambda item: item.anomaly_id),
            evaluations=sorted(evaluations, key=lambda item: item.evaluation_id),
            benign_explanations=sorted(
                explanations, key=lambda item: item.explanation_id
            ),
        )

    def _fit(
        self,
        spec: FinancialAnomalyModelSpec,
        training: np.ndarray[Any, np.dtype[np.float64]],
    ) -> _FittedModel:
        centers, scales, constant_features = _robust_location_scale(training)
        if np.all(scales == 0):
            raise ValueError(f"model {spec.model_id} has no varying training features")
        limitations: list[str] = []
        if constant_features:
            limitations.append("CONSTANT_TRAINING_FEATURES_IGNORED")
        if spec.model_type is FinancialAnomalyModelType.ROBUST_Z_SCORE:
            threshold = Decimal(str(spec.parameters.get("z_threshold", "3.5")))

            def score_many(values: np.ndarray[Any, np.dtype[np.float64]]) -> np.ndarray:
                return _robust_scores(values, centers, scales)

            state = {
                "engine_version": self.ENGINE_VERSION,
                "model_type": spec.model_type.value,
                "centers": [str(value) for value in centers],
                "scales": [str(value) for value in scales],
                "threshold": str(threshold),
            }
            serialized = canonical_json_bytes(state)
        elif spec.model_type is FinancialAnomalyModelType.ISOLATION_FOREST:
            n_estimators = int(spec.parameters.get("n_estimators", 200))
            with threadpool_limits(limits=1):
                model = IsolationForest(
                    n_estimators=n_estimators,
                    contamination=cast(Any, float(spec.contamination)),
                    random_state=spec.random_state,
                    n_jobs=1,
                    bootstrap=False,
                ).fit(training)

            def score_many(values: np.ndarray[Any, np.dtype[np.float64]]) -> np.ndarray:
                with threadpool_limits(limits=1):
                    return -model.decision_function(values)

            threshold = Decimal(0)
            serialized = pickle.dumps(model, protocol=5)
            limitations.append("FEATURE_ATTRIBUTION_USES_ROBUST_DISTANCE_HEURISTIC")
        elif spec.model_type is FinancialAnomalyModelType.PYOD_ECOD:
            with threadpool_limits(limits=1):
                model = ECOD(
                    contamination=cast(Any, float(spec.contamination)),
                    n_jobs=1,
                ).fit(training)

            def score_many(values: np.ndarray[Any, np.dtype[np.float64]]) -> np.ndarray:
                with threadpool_limits(limits=1):
                    return np.asarray(model.decision_function(values), dtype=np.float64)

            threshold = Decimal(str(float(model.threshold_)))
            serialized = pickle.dumps(model, protocol=5)
            limitations.append("FEATURE_ATTRIBUTION_USES_ROBUST_DISTANCE_HEURISTIC")
        else:  # pragma: no cover - exhaustive enum handling
            raise ValueError(f"unsupported anomaly model: {spec.model_type}")
        return _FittedModel(
            serialized=serialized,
            score_many=score_many,
            threshold=threshold,
            limitation_codes=limitations,
            robust_centers=centers,
            robust_scales=scales,
        )

    @staticmethod
    def _evaluate(
        dataset: FinancialAnomalyDataset,
        spec: FinancialAnomalyModelSpec,
        artifact: FinancialAnomalyModelArtifact,
        fitted: _FittedModel,
        evaluation_samples: list[FinancialAnomalySample],
        created_at: datetime,
    ) -> FinancialAnomalyEvaluationReport:
        if evaluation_samples:
            values = _matrix(evaluation_samples, dataset.feature_names)
            scores = fitted.score_many(values)
        else:
            scores = np.asarray([], dtype=np.float64)
        tp = tn = fp = fn = 0
        for sample, score in zip(evaluation_samples, scores, strict=True):
            predicted = Decimal(str(float(score))) > fitted.threshold
            expected = bool(sample.expected_anomaly)
            if predicted and expected:
                tp += 1
            elif predicted:
                fp += 1
            elif expected:
                fn += 1
            else:
                tn += 1
        precision = (
            Decimal(tp) / Decimal(tp + fp) if tp + fp else None
        )
        recall = Decimal(tp) / Decimal(tp + fn) if tp + fn else None
        identity = {
            "model_artifact_id": artifact.model_artifact_id,
            "dataset_id": dataset.dataset_id,
            "scope": spec.scope.value,
            "evaluation_sample_ids": [sample.sample_id for sample in evaluation_samples],
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
        }
        return FinancialAnomalyEvaluationReport(
            evaluation_id=(
                f"financial-model-evaluation:"
                f"{sha256_bytes(canonical_json_bytes(identity))}"
            ),
            model_artifact_id=artifact.model_artifact_id,
            dataset_id=dataset.dataset_id,
            scope=spec.scope,
            sample_count=len(evaluation_samples),
            true_positive=tp,
            true_negative=tn,
            false_positive=fp,
            false_negative=fn,
            precision=precision,
            recall=recall,
            created_at=created_at,
        )

    @staticmethod
    def _benign_explanations(
        target: Any,
        assessment: FinancialAnomaly,
        created_at: datetime,
    ) -> list[FinancialBenignExplanation]:
        output: list[FinancialBenignExplanation] = []
        for context in target.benign_contexts:
            identity = {
                "context": context.value,
                "anomaly_id": assessment.anomaly_id,
                "evidence_ids": sorted(target.benign_context_evidence_ids),
            }
            output.append(
                FinancialBenignExplanation(
                    explanation_id=(
                        f"financial-benign-explanation:"
                        f"{sha256_bytes(canonical_json_bytes(identity))}"
                    ),
                    explanation_code=f"EVIDENCE_BACKED_{context.value}",
                    related_anomaly_ids=[assessment.anomaly_id],
                    evidence_ids=sorted(target.benign_context_evidence_ids),
                    created_at=created_at,
                )
            )
        return output


def _matrix(samples: list[Any], feature_names: list[str]) -> np.ndarray[Any, np.dtype[np.float64]]:
    return np.asarray(
        [
            [float(sample.feature_values[feature_name]) for feature_name in feature_names]
            for sample in samples
        ],
        dtype=np.float64,
    )


def anomaly_scope_training_samples(
    dataset: FinancialAnomalyDataset,
    scope: FinancialAnomalyScope,
) -> list[FinancialAnomalySample]:
    by_id = {sample.sample_id: sample for sample in dataset.samples}
    target = by_id[dataset.target_sample_id]
    candidates = [by_id[sample_id] for sample_id in dataset.training_sample_ids]
    if scope is FinancialAnomalyScope.TIME_SERIES:
        selected = [
            sample
            for sample in candidates
            if sample.company_id == target.company_id
            and sample.period_end < target.period_end
            and sample.available_at <= target.available_at
        ]
    else:
        selected = [
            sample
            for sample in candidates
            if sample.company_id != target.company_id
            and sample.period_end == target.period_end
            and sample.available_at <= dataset.as_of
        ]
    return sorted(selected, key=lambda sample: sample.sample_id)


def anomaly_scope_evaluation_samples(
    dataset: FinancialAnomalyDataset,
    scope: FinancialAnomalyScope,
) -> list[FinancialAnomalySample]:
    by_id = {sample.sample_id: sample for sample in dataset.samples}
    target = by_id[dataset.target_sample_id]
    candidates = [by_id[sample_id] for sample_id in dataset.evaluation_sample_ids]
    if scope is FinancialAnomalyScope.TIME_SERIES:
        selected = [sample for sample in candidates if sample.company_id == target.company_id]
    else:
        selected = [sample for sample in candidates if sample.period_end == target.period_end]
    return sorted(selected, key=lambda sample: sample.sample_id)


def _robust_location_scale(
    training: np.ndarray[Any, np.dtype[np.float64]],
) -> tuple[
    np.ndarray[Any, np.dtype[np.float64]],
    np.ndarray[Any, np.dtype[np.float64]],
    list[int],
]:
    centers = np.median(training, axis=0)
    mad = np.median(np.abs(training - centers), axis=0)
    q1 = np.quantile(training, 0.25, axis=0, method="linear")
    q3 = np.quantile(training, 0.75, axis=0, method="linear")
    iqr = q3 - q1
    scales = np.where(mad > 0, mad / 0.6744897501960817, iqr / 1.3489795003921634)
    constant = np.flatnonzero(scales == 0).tolist()
    return centers.astype(np.float64), scales.astype(np.float64), constant


def _robust_scores(
    values: np.ndarray[Any, np.dtype[np.float64]],
    centers: np.ndarray[Any, np.dtype[np.float64]],
    scales: np.ndarray[Any, np.dtype[np.float64]],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    safe_scales = np.where(scales == 0, np.inf, scales)
    return np.max(np.abs((values - centers) / safe_scales), axis=1)


def _triggered_features(
    values: np.ndarray[Any, np.dtype[np.float64]],
    centers: np.ndarray[Any, np.dtype[np.float64]],
    scales: np.ndarray[Any, np.dtype[np.float64]],
    feature_names: list[str],
    *,
    force_one: bool,
) -> list[str]:
    safe_scales = np.where(scales == 0, np.inf, scales)
    distances = np.abs((values - centers) / safe_scales)
    triggered = [
        feature_names[index] for index, distance in enumerate(distances) if distance >= 3.5
    ]
    if force_one and not triggered:
        triggered = [feature_names[int(np.argmax(distances))]]
    return triggered


def _library_versions() -> dict[str, str]:
    return {
        "astock_anomaly_engine": FinancialAnomalyEngine.ENGINE_VERSION,
        "numba": version("numba"),
        "numpy": version("numpy"),
        "scipy": version("scipy"),
        "scikit-learn": version("scikit-learn"),
        "pyod": version("pyod"),
    }


def _strip_created_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_created_at(item)
            for key, item in value.items()
            if key != "created_at"
        }
    if isinstance(value, list):
        return [_strip_created_at(item) for item in value]
    return value
