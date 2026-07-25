from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.schemas import EvidenceCollectionRun, EvidenceCollectionRunStatus


def test_evidence_collection_run_validates_time_order() -> None:
    run = EvidenceCollectionRun(
        task_artifact_id="EvidenceCollectionTask:fixture",
        status=EvidenceCollectionRunStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        collected_items=["evidence:1"],
        missing_items=["financial"],
    )
    assert run.status == EvidenceCollectionRunStatus.COMPLETED
    assert run.collected_items == ["evidence:1"]


def test_evidence_collection_run_rejects_invalid_time_window() -> None:
    with pytest.raises(ValueError, match="completed_at"):
        EvidenceCollectionRun(
            task_artifact_id="EvidenceCollectionTask:fixture",
            status=EvidenceCollectionRunStatus.FAILED,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2025, 12, 31, tzinfo=UTC),
        )

