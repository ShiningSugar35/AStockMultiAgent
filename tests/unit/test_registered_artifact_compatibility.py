"""Legacy reads must not weaken immutable evidence or current publication checks."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.artifact_reading import ArtifactReadError, decode_registered_artifact
from astock.core.object_store import ObjectStore
from astock.schemas import FinancialIntegrityEvidencePack
from astock.schemas.full_research import RecommendationResearchReceipt


def financial_payload(*, count: int = 1) -> dict:
    now = datetime(2026, 9, 17, tzinfo=UTC).isoformat()
    number = {
        "schema_version": "1.0", "created_at": now,
        "field_code": "TOTAL_ASSETS", "statement_type": "BALANCE_SHEET",
        "period_end": "2025-12-31", "period_type": "ANNUAL",
        "value_cny": "100000.00123456789", "reporting_quantum_cny": "0.01",
        "fact_ids": ["fact:assets"], "source_snapshot_ids": ["source:statement"],
        "evidence_ids": ["evidence:statement"], "pit_ids": ["pit:old-annotation"],
    }
    return {
        "schema_version": "1.0", "created_at": now,
        "audit_run_id": "financial-audit:legacy-test", "request_hash": "a" * 64,
        "status": "SUCCEEDED", "coverage_status": "COMPLETE", "company_id": "000001",
        "as_of": now, "industry_profile": "GENERAL_INDUSTRIAL", "periods": ["2025-12-31"],
        "input_fact_ids": ["fact:assets"], "source_snapshot_ids": ["source:statement"],
        "verified_numbers": [dict(number) for _ in range(count)], "recalculated_metrics": [],
        "rule_findings": [], "risk_level": "LOW", "hard_blocks": [],
        "rule_versions": {}, "model_versions": {}, "capability_status": {},
        "pit_ids": ["pit:old-annotation"],
    }


def decode(payload: dict) -> FinancialIntegrityEvidencePack:
    return decode_registered_artifact(
        json.dumps(payload).encode(), FinancialIntegrityEvidencePack, registry_schema_version="1.0"
    )


@pytest.mark.parametrize("count", [1, 17])
def test_old_financial_annotations_read_without_rewriting_bytes(tmp_path: Path, count: int) -> None:
    payload = financial_payload(count=count)
    objects = ObjectStore(tmp_path / "objects")
    raw = json.dumps(payload).encode()
    ref = objects.put_bytes(raw)
    with pytest.raises(ValidationError):
        FinancialIntegrityEvidencePack.model_validate_json(raw)
    restored = decode_registered_artifact(
        objects.get_bytes(ref.sha256), FinancialIntegrityEvidencePack, registry_schema_version="1.0"
    )
    assert len(restored.verified_numbers) == count
    assert str(restored.verified_numbers[0].value_cny) == "100000.00123456789"
    assert restored.source_snapshot_ids == ["source:statement"]
    assert all(
        item.source_snapshot_ids == ["source:statement"] for item in restored.verified_numbers
    )
    assert "pit_ids" not in restored.model_dump_json()
    assert objects.get_bytes(ref.sha256) == raw
    assert objects.verify(ref.sha256)
    assert payload["pit_ids"] == ["pit:old-annotation"]


def test_current_financial_roundtrip_is_unchanged() -> None:
    current = decode(financial_payload())
    restored = decode_registered_artifact(
        current.model_dump_json().encode(), FinancialIntegrityEvidencePack,
        registry_schema_version="1.0",
    )
    assert restored == current


@pytest.mark.parametrize("bad", [True, "pit:single", [1], {"secret": "private"}, [""]])
def test_retired_metadata_is_not_a_general_unknown_field_escape(bad) -> None:
    payload = financial_payload()
    payload["verified_numbers"][0]["pit_ids"] = bad
    with pytest.raises(ArtifactReadError, match="UNSUPPORTED_LEGACY_FINANCIAL_METADATA"):
        decode(payload)


def test_unknown_fields_still_fail_and_never_echo_secrets() -> None:
    payload = financial_payload()
    payload["verified_numbers"][0]["secret-in-field-name"] = "secret-in-value"
    with pytest.raises(ArtifactReadError) as captured:
        decode(payload)
    diagnostic = json.dumps(captured.value.diagnostic())
    assert "SCHEMA_VALIDATION_FAILED" in diagnostic
    assert "verified_numbers" in diagnostic
    assert "<unknown-field>" in diagnostic
    assert "secret-in" not in diagnostic
    assert "input" not in diagnostic


def test_source_snapshot_ids_are_never_invented_from_pit_ids() -> None:
    payload = financial_payload()
    del payload["verified_numbers"][0]["source_snapshot_ids"]
    with pytest.raises(ArtifactReadError, match="UNSUPPORTED_LEGACY_FINANCIAL_METADATA"):
        decode(payload)
    payload = financial_payload()
    payload["verified_numbers"][0]["source_snapshot_ids"] = []
    with pytest.raises(ArtifactReadError, match="SCHEMA_VALIDATION_FAILED"):
        decode(payload)


def test_registry_version_and_nested_legacy_version_remain_strict() -> None:
    payload = financial_payload()
    payload["schema_version"] = "future-v9"
    with pytest.raises(ArtifactReadError, match="REGISTRY_SCHEMA_VERSION_MISMATCH"):
        decode(payload)
    payload = financial_payload()
    payload["verified_numbers"][0]["schema_version"] = "2.0"
    with pytest.raises(ArtifactReadError, match="UNSUPPORTED_LEGACY_FINANCIAL_METADATA"):
        decode(payload)


@pytest.mark.parametrize("raw", [b"{bad", b"[]", b"null", b"\xff"])
def test_invalid_bytes_are_classified_without_echo(raw: bytes) -> None:
    with pytest.raises(ArtifactReadError) as captured:
        decode_registered_artifact(
            raw, FinancialIntegrityEvidencePack, registry_schema_version="1.0"
        )
    assert captured.value.code in {"INVALID_ARTIFACT_JSON", "INVALID_ARTIFACT_SHAPE"}


def test_historical_sealed_receipt_never_becomes_current_authority() -> None:
    payload = {"schema_version": "recommendation-research-receipt-v1", "pit_snapshot": {},
               "receipt_hash": "a" * 64, "broker_execution_allowed": False}
    raw = json.dumps(payload).encode()
    with pytest.raises(ArtifactReadError) as captured:
        decode_registered_artifact(raw, RecommendationResearchReceipt,
                                   registry_schema_version="recommendation-research-receipt-v1")
    assert captured.value.code == "HISTORICAL_RECEIPT_REQUIRES_REFRESH"
    assert captured.value.diagnostic()["next_action"] == "RESUME_CURRENT_RESEARCH"
    assert payload["receipt_hash"] == "a" * 64


def test_tampered_original_object_still_rejected(tmp_path: Path) -> None:
    from astock.core.errors import StorageError

    objects = ObjectStore(tmp_path / "objects")
    ref = objects.put_json(financial_payload())
    ref.path.write_bytes(b"tampered")
    with pytest.raises(StorageError):
        decode_registered_artifact(objects.get_bytes(ref.sha256), FinancialIntegrityEvidencePack,
                                   registry_schema_version="1.0")
