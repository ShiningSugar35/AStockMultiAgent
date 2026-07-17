from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.financial_integrity import (
    FinancialAnomalyEngine,
    load_financial_anomaly_models,
)
from astock.schemas import FinancialAnomalyScope
from tests.helpers import make_financial_anomaly_dataset, make_financial_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_anomaly_models_are_reproducible_and_evidence_bound(
    state, object_store
) -> None:
    lineage = make_financial_facts(
        state,
        object_store,
        source_suffix="m3-3-engine-lineage",
    )[0]
    dataset = make_financial_anomaly_dataset(state, object_store, lineage)
    specs = load_financial_anomaly_models(
        PROJECT_ROOT / "configs" / "financial_anomaly_models.yaml"
    ).models
    created_at = datetime(2026, 7, 17, tzinfo=UTC)
    engine = FinancialAnomalyEngine(object_store)

    first = engine.run(dataset, specs, created_at=created_at)
    second = engine.run(dataset, specs, created_at=created_at)

    assert first == second
    assert object_store.verify(first.dataset_object_hash)
    assert len(first.model_artifacts) == 3
    assert len(first.target_assessments) == 3
    assert len(first.evaluations) == 3
    assert len(first.benign_explanations) == 3
    assert all(item.is_anomaly for item in first.target_assessments)
    target = next(
        sample for sample in dataset.samples if sample.sample_id == dataset.target_sample_id
    )
    assert all(item.evidence_ids == target.evidence_ids for item in first.target_assessments)
    assert all(item.triggered_features for item in first.target_assessments)
    assert all(
        object_store.verify(item.serialized_model_object_hash)
        for item in first.model_artifacts
    )
    assert all(item.true_positive == 1 for item in first.evaluations)
    assert all(item.true_negative == 2 for item in first.evaluations)
    assert all(item.false_positive == 0 for item in first.evaluations)
    assert all(item.false_negative == 0 for item in first.evaluations)
    time_series = next(
        item
        for item in first.model_artifacts
        if item.scope is FinancialAnomalyScope.TIME_SERIES
    )
    assert all(sample_id.startswith("ts-train-") for sample_id in time_series.training_sample_ids)
    peer_models = [
        item
        for item in first.model_artifacts
        if item.scope is FinancialAnomalyScope.PEER
    ]
    assert len(peer_models) == 2
    assert all(
        all(sample_id.startswith("peer-train-") for sample_id in item.training_sample_ids)
        for item in peer_models
    )
    assert all(
        item.library_versions["astock_anomaly_engine"]
        == FinancialAnomalyEngine.ENGINE_VERSION
        for item in first.model_artifacts
    )
