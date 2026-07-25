from __future__ import annotations

import pytest

from astock.core.hashing import content_hash
from astock.research import (
    EvidenceCollectionRunService,
    EvidenceCollectionTaskService,
    EvidencePackService,
)
from astock.schemas import ResearchRequest


def _seed_run(state, objects) -> tuple[str, str]:
    request = ResearchRequest(company="宁德时代", ticker="300750")
    request_artifact_id = f"ResearchRequest:{content_hash({
        "company": request.company,
        "ticker": request.ticker,
        "market": request.market,
        "requested_modules": [item.value for item in request.requested_modules],
    })}"
    request_ref = objects.put_json(request.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=request_artifact_id,
        artifact_type="ResearchRequest",
        schema_version=request.schema_version,
        object_hash=request_ref.sha256,
        input_hashes=[content_hash(request)],
    )
    task = EvidenceCollectionTaskService(state, objects).create_task(request_artifact_id)
    run = EvidenceCollectionRunService(state, objects).create_run(task.artifact_id)
    return run.artifact_id, run.object_sha256


def test_evidence_pack_service_generates_pack_and_is_idempotent(
    state,
    object_store,
) -> None:
    run_artifact_id, _ = _seed_run(state, object_store)
    service = EvidencePackService(state, object_store)

    first = service.create_pack(run_artifact_id)
    assert first.artifact_id == (
        f"EvidencePack:{content_hash({'run_artifact_id': run_artifact_id})}"
    )
    assert first.pack.run_artifact_id == run_artifact_id
    assert first.pack.company == "宁德时代"
    assert first.pack.ticker == "300750"
    assert first.pack.evidence_items == []
    assert first.pack.missing_items == ["evidence", "financial", "research"]
    assert not first.reused_existing

    second = service.create_pack(run_artifact_id)
    assert second.reused_existing
    assert second.object_sha256 == first.object_sha256
    assert second.pack == first.pack


def test_evidence_pack_service_rejects_missing_run_artifact(state, object_store) -> None:
    service = EvidencePackService(state, object_store)
    with pytest.raises(ValueError, match="unknown evidence collection run artifact"):
        service.create_pack("EvidenceCollectionRun:missing")


def test_evidence_pack_service_rejects_wrong_artifact_type(state, object_store) -> None:
    service = EvidencePackService(state, object_store)
    request = ResearchRequest(company="Ningde Times", ticker="300750")
    request_ref = object_store.put_json(request.model_dump(mode="json"))
    state.register_artifact(
        artifact_id="ResearchRequest:bad",
        artifact_type="ResearchRequest",
        schema_version=request.schema_version,
        object_hash=request_ref.sha256,
        input_hashes=[request_ref.sha256],
    )
    with pytest.raises(ValueError, match="artifact is not a evidence collection run"):
        service.create_pack("ResearchRequest:bad")


def test_evidence_pack_service_rejects_missing_run_object(state, object_store) -> None:
    run_artifact_id, run_hash = _seed_run(state, object_store)
    object_store.path_for(run_hash).unlink()
    service = EvidencePackService(state, object_store)
    with pytest.raises(ValueError, match="artifact object is unavailable"):
        service.create_pack(run_artifact_id)
