from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.financial_integrity import (
    FinancialIntegrityService,
    load_financial_anomaly_models,
)
from astock.schemas import (
    FinancialAuditRequest,
    FinancialGapType,
    FinancialIndustryProfile,
    FinancialRiskLevel,
    RunStatus,
)
from tests.helpers import make_financial_anomaly_dataset, make_financial_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(state, object_store) -> FinancialIntegrityService:
    return FinancialIntegrityService(
        state,
        object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    )


def _request(state, object_store) -> FinancialAuditRequest:
    facts = make_financial_facts(
        state,
        object_store,
        source_suffix="m3-3-service-lineage",
    )
    registry = load_financial_anomaly_models(
        PROJECT_ROOT / "configs" / "financial_anomaly_models.yaml"
    )
    return FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=facts,
        anomaly_dataset=make_financial_anomaly_dataset(state, object_store, facts[0]),
        anomaly_model_specs=registry.models,
    )


def test_m3_3_service_freezes_models_evaluations_and_lineage(state, object_store) -> None:
    request = _request(state, object_store)
    service = _service(state, object_store)

    first = service.run(request)

    assert first.pack.status is RunStatus.SUCCEEDED
    assert first.pack.risk_level is FinancialRiskLevel.MEDIUM
    assert len(first.pack.time_series_anomalies) == 1
    assert len(first.pack.peer_anomalies) == 2
    assert len(first.pack.anomaly_model_artifacts) == 3
    assert len(first.pack.anomaly_evaluations) == 3
    assert len(first.pack.benign_explanations) == 3
    assert not first.pack.evidence_gaps
    assert first.pack.capability_status["time_series_anomalies"] == "EXECUTED_M3_3"
    assert first.pack.capability_status["peer_anomalies"] == "EXECUTED_M3_3"
    assert first.pack.capability_status["pyod"] == "AVAILABLE_M3_3_PYOD_ECOD"
    assert all(
        object_store.verify(item.dataset_object_hash)
        and object_store.verify(item.serialized_model_object_hash)
        for item in first.pack.anomaly_model_artifacts
    )
    with state.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_anomaly_dataset_manifest"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_anomaly_model_manifest"
            ).fetchone()[0]
            == 3
        )
        assert (
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_registry "
                    "WHERE type='FinancialAnomalyDataset'"
                ).fetchone()[0]
            == 1
        )

    repeated = service.run(request)
    assert repeated.reused_existing
    assert repeated.artifact_hash == first.artifact_hash
    assert repeated.pack == first.pack


def test_m3_3_insufficient_training_sample_is_explicit_needs_info(
    state, object_store
) -> None:
    request = _request(state, object_store)
    too_large = [
        spec.model_copy(update={"minimum_training_samples": 30})
        for spec in request.anomaly_model_specs
    ]

    pack = _service(state, object_store).run(
        request.model_copy(update={"anomaly_model_specs": too_large})
    ).pack

    assert pack.status is RunStatus.NEEDS_INFO
    assert not pack.time_series_anomalies
    assert not pack.peer_anomalies
    assert not pack.anomaly_model_artifacts
    assert not pack.anomaly_evaluations
    assert any(
        gap.gap_type is FinancialGapType.MODEL_SAMPLE_INSUFFICIENT
        for gap in pack.evidence_gaps
    )
    assert (
        pack.capability_status["time_series_anomalies"]
        == "REQUESTED_M3_3_NEEDS_INFO"
    )
    assert pack.capability_status["peer_anomalies"] == "REQUESTED_M3_3_NEEDS_INFO"
