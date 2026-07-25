from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.schemas import EvidenceCollectionTask


def test_evidence_collection_task_dedupes_and_orders_sources() -> None:
    task = EvidenceCollectionTask(
        request_artifact_id="ResearchRequest:not-existing",
        company="Ningde Times",
        ticker="300750",
        required_sources=["research", "financial", "financial", "evidence"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert task.required_sources == ["evidence", "financial", "research"]


def test_evidence_collection_task_rejects_empty_sources() -> None:
    with pytest.raises(ValueError, match="at least one source"):
        EvidenceCollectionTask(
            request_artifact_id="ResearchRequest:empty-sources",
            company="Ningde Times",
            ticker="300750",
            required_sources=[],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
