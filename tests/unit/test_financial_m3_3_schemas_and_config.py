from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.financial_integrity import load_financial_anomaly_models
from astock.schemas import (
    FinancialAnomalyDataset,
    FinancialAnomalyModelType,
    FinancialAnomalySample,
    FinancialIndustryProfile,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_versioned_anomaly_registry_has_all_three_m3_3_models() -> None:
    registry = load_financial_anomaly_models(
        PROJECT_ROOT / "configs" / "financial_anomaly_models.yaml"
    )
    assert registry.registry_version == "financial-anomaly-models-m3.3-v1"
    assert registry.compatible_engine_version == "financial-anomaly-m3.3.0"
    assert {model.model_type for model in registry.models} == {
        FinancialAnomalyModelType.ROBUST_Z_SCORE,
        FinancialAnomalyModelType.ISOLATION_FOREST,
        FinancialAnomalyModelType.PYOD_ECOD,
    }
    assert all(model.random_state == 20260717 for model in registry.models)


def test_frozen_dataset_rejects_target_leakage_into_training() -> None:
    sample = FinancialAnomalySample(
        sample_id="target",
        company_id="000001",
        period_end=datetime(2025, 12, 31, tzinfo=UTC).date(),
        available_at=datetime(2026, 3, 20, tzinfo=UTC),
        feature_values={"ratio": Decimal("1")},
        feature_formula_versions={"ratio": "1.0"},
        source_snapshot_ids=["snapshot"],
        pit_ids=["pit"],
        evidence_ids=["evidence"],
    )
    with pytest.raises(ValidationError, match="target sample cannot be part"):
        FinancialAnomalyDataset(
            dataset_id="leaky",
            as_of=datetime(2026, 3, 20, tzinfo=UTC),
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            peer_cohort_id="fixture-cohort",
            feature_names=["ratio"],
            samples=[sample],
            training_sample_ids=["target"],
            target_sample_id="target",
        )


def test_anomaly_sample_requires_formula_version_for_every_feature() -> None:
    with pytest.raises(ValidationError, match="formula version"):
        FinancialAnomalySample(
            sample_id="sample",
            company_id="000001",
            period_end=datetime(2025, 12, 31, tzinfo=UTC).date(),
            available_at=datetime(2026, 3, 20, tzinfo=UTC),
            feature_values={"ratio": Decimal("1")},
            feature_formula_versions={"other_ratio": "1.0"},
            source_snapshot_ids=["snapshot"],
            pit_ids=["pit"],
            evidence_ids=["evidence"],
        )


def test_anomaly_sample_rejects_non_finite_features() -> None:
    with pytest.raises(ValidationError, match="finite number"):
        FinancialAnomalySample(
            sample_id="sample",
            company_id="000001",
            period_end=datetime(2025, 12, 31, tzinfo=UTC).date(),
            available_at=datetime(2026, 3, 20, tzinfo=UTC),
            feature_values={"ratio": Decimal("NaN")},
            feature_formula_versions={"ratio": "1.0"},
            source_snapshot_ids=["snapshot"],
            pit_ids=["pit"],
            evidence_ids=["evidence"],
        )
