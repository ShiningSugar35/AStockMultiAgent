from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.core.hashing import content_hash
from astock.research import EvidenceCollectionRunService
from astock.schemas import EvidenceCollectionRunStatus, EvidenceCollectionTask


def _seed_task(state, objects) -> str:
    task = EvidenceCollectionTask(
        request_artifact_id="ResearchRequest:fixture",
        company="宁德时代",
        ticker="300750",
        required_sources=["financial", "evidence"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    ref = objects.put_json(task.model_dump(mode="json"))
    task_artifact_id = (
        f"EvidenceCollectionTask:{content_hash({'request_artifact_id': task.request_artifact_id})}"
    )
    state.register_artifact(
        artifact_id=task_artifact_id,
        artifact_type="EvidenceCollectionTask",
        schema_version=task.schema_version,
        object_hash=ref.sha256,
        input_hashes=[ref.sha256],
    )
    return task_artifact_id


def test_evidence_collection_run_service_generates_run_and_is_idempotent(
    state,
    object_store,
) -> None:
    task_artifact_id = _seed_task(state, object_store)
    service = EvidenceCollectionRunService(state, object_store)

    first = service.create_run(task_artifact_id)
    assert first.artifact_id == (
        f"EvidenceCollectionRun:{content_hash({'task_artifact_id': task_artifact_id})}"
    )
    assert first.run.task_artifact_id == task_artifact_id
    assert first.run.status == EvidenceCollectionRunStatus.NEEDS_INFO
    assert first.run.collected_items == []
    assert first.run.missing_items == ["evidence", "financial"]
    assert not first.reused_existing

    second = service.create_run(task_artifact_id)
    assert second.reused_existing
    assert second.object_sha256 == first.object_sha256
    assert second.run == first.run


def test_evidence_collection_run_status_requires_collected_items_without_gaps() -> None:
    assert EvidenceCollectionRunService._status(
        ["ClaimEvidenceBundle:claim:recorded"],
        [],
    ) is EvidenceCollectionRunStatus.COMPLETED
    assert EvidenceCollectionRunService._status(
        [],
        ["financial"],
    ) is EvidenceCollectionRunStatus.NEEDS_INFO
    assert EvidenceCollectionRunService._status([], []) is EvidenceCollectionRunStatus.NEEDS_INFO


def test_evidence_collection_run_service_rejects_missing_task_artifact(
    state,
    object_store,
) -> None:
    service = EvidenceCollectionRunService(state, object_store)
    with pytest.raises(ValueError, match="unknown evidence collection task artifact"):
        service.create_run("EvidenceCollectionTask:missing")
