from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from astock.schemas import (
    ResearchPreparationManifest,
    ResearchPreparationRequest,
    ResearchPreparationStatus,
)


def _request_payload() -> dict[str, object]:
    return {
        "research_request_artifact_id": "ResearchRequest:recorded",
        "evidence_pack_artifact_id": "EvidencePack:recorded",
        "financial_audit_run_id": "financial-audit:recorded",
        "claim_ids": ["claim:b", "claim:a", "claim:a"],
        "as_of": "2026-06-30T15:00:00+08:00",
        "formal_historical": True,
        "allow_approximated": False,
    }


def test_research_preparation_request_normalizes_claims_and_requires_aware_as_of() -> None:
    request = ResearchPreparationRequest.model_validate(_request_payload())
    assert request.claim_ids == ["claim:a", "claim:b"]
    assert request.as_of.utcoffset() is not None

    naive = _request_payload()
    naive["as_of"] = "2026-06-30T15:00:00"
    with pytest.raises(ValidationError):
        ResearchPreparationRequest.model_validate(naive)


def test_research_preparation_request_rejects_empty_scope_and_invalid_pit_mode() -> None:
    empty = _request_payload()
    empty["claim_ids"] = []
    with pytest.raises(ValidationError, match="at least 1 item"):
        ResearchPreparationRequest.model_validate(empty)

    invalid_mode = _request_payload()
    invalid_mode["formal_historical"] = False
    invalid_mode["allow_approximated"] = True
    with pytest.raises(ValidationError, match="formal historical"):
        ResearchPreparationRequest.model_validate(invalid_mode)


def test_research_preparation_manifest_enforces_frozen_pack_status_contract() -> None:
    common = {
        "research_request_artifact_id": "ResearchRequest:recorded",
        "evidence_pack_artifact_id": "EvidencePack:recorded",
        "financial_audit_run_id": "financial-audit:recorded",
        "company_id": "300750",
        "ticker": "300750",
        "as_of": datetime(2026, 6, 30, tzinfo=UTC),
        "claim_ids": ["claim:recorded"],
        "financial_manual_task_ids": [],
        "input_object_hashes": ["a" * 64],
    }
    ready = ResearchPreparationManifest(
        **common,
        status=ResearchPreparationStatus.READY_FOR_BASE_CASE,
        blocking_codes=[],
        required_action_codes=[],
        frozen_evidence_pack_id="frozen-evidence:recorded",
        frozen_evidence_pack_artifact_id=(
            "FrozenEvidencePack:frozen-evidence:recorded"
        ),
    )
    assert ready.status is ResearchPreparationStatus.READY_FOR_BASE_CASE

    with pytest.raises(ValidationError, match="requires a frozen evidence pack"):
        ResearchPreparationManifest(
            **common,
            status=ResearchPreparationStatus.READY_FOR_BASE_CASE,
        )
    with pytest.raises(ValidationError, match="cannot reference a frozen pack"):
        ResearchPreparationManifest(
            **common,
            status=ResearchPreparationStatus.NEEDS_INFO,
            frozen_evidence_pack_id="frozen-evidence:recorded",
            frozen_evidence_pack_artifact_id=(
                "FrozenEvidencePack:frozen-evidence:recorded"
            ),
        )
