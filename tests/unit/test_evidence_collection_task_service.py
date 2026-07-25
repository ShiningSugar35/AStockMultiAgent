from __future__ import annotations

import pytest

from astock.core.hashing import content_hash
from astock.research import EvidenceCollectionTaskService
from astock.schemas import ResearchRequest


def _research_request_artifact_id(request: ResearchRequest) -> str:
    request_identity = {
        "company": request.company,
        "ticker": request.ticker,
        "market": request.market,
        "requested_modules": [item.value for item in request.requested_modules],
    }
    return f"ResearchRequest:{content_hash(request_identity)}"


def _seed_research_request(state, objects) -> tuple[str, str]:
    request = ResearchRequest(company="宁德时代", ticker="300750")
    ref = objects.put_json(request.model_dump(mode="json"))
    artifact_id = _research_request_artifact_id(request)
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="ResearchRequest",
        schema_version=request.schema_version,
        object_hash=ref.sha256,
        input_hashes=[content_hash(request)],
    )
    return artifact_id, ref.sha256


def test_evidence_collection_task_service_generates_task_and_is_idempotent(
    state,
    object_store,
) -> None:
    request_artifact_id, _ = _seed_research_request(state, object_store)
    service = EvidenceCollectionTaskService(state, object_store)

    first = service.create_task(request_artifact_id)
    assert first.artifact_id == (
        f"EvidenceCollectionTask:{content_hash({'request_artifact_id': request_artifact_id})}"
    )
    assert first.task.company == "宁德时代"
    assert first.task.ticker == "300750"
    assert first.task.required_sources == ["evidence", "financial", "research"]
    assert not first.reused_existing

    second = service.create_task(request_artifact_id)
    assert second.reused_existing
    assert second.object_sha256 == first.object_sha256
    assert second.task == first.task


def test_evidence_collection_task_service_rejects_missing_request_artifact(
    state,
    object_store,
) -> None:
    service = EvidenceCollectionTaskService(state, object_store)
    with pytest.raises(ValueError, match="unknown research request artifact"):
        service.create_task("ResearchRequest:missing")
