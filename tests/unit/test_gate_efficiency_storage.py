"""Registration regressions distinguish immutable content from fetch observations."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.core.state import StateStore
from astock.documents.repository import DocumentRepository
from astock.schemas import DocumentType, SourceDocument, SourceSnapshot


def _snapshot(identifier: str = "observed:same-id") -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=identifier,
        source_id="observation-regression",
        object_sha256="a" * 64,
        fetched_at=datetime(2026, 9, 25, 0, 0, tzinfo=UTC),
        available_to_system_at=datetime(2026, 9, 25, 0, 0, tzinfo=UTC),
        source_url="https://example.invalid/report",
        mime="application/pdf",
        byte_size=100,
        rights_status="TEST_ONLY",
    )


def test_reobserving_stable_snapshot_id_preserves_both_times(state: StateStore) -> None:
    first = _snapshot()
    second = first.model_copy(update={
        "fetched_at": first.fetched_at + timedelta(hours=1),
        "available_to_system_at": first.available_to_system_at + timedelta(hours=1),
    })
    canonical = state.register_snapshot(first)
    again = state.register_snapshot(second)
    assert canonical.snapshot_id == again.snapshot_id == first.snapshot_id
    assert again.fetched_at == first.fetched_at
    with state.connect() as connection:
        observations = connection.execute(
            "SELECT observed_at FROM source_snapshot_observation "
            "WHERE canonical_snapshot_id=? ORDER BY observed_at", (first.snapshot_id,)
        ).fetchall()
        assert [row[0] for row in observations] == [
            first.fetched_at.isoformat(), second.fetched_at.isoformat()
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_registered_alias_cannot_be_rebound_to_other_content(state: StateStore) -> None:
    first = _snapshot("observation:first")
    alias = first.model_copy(update={"snapshot_id": "observation:alias"})
    state.register_snapshot(first)
    state.register_snapshot(alias)
    conflicting = alias.model_copy(update={"object_sha256": "b" * 64})
    with pytest.raises(ValueError, match="collision"):
        state.register_snapshot(conflicting)
    restored = state.get_snapshot(alias.snapshot_id)
    assert restored is not None
    assert restored.snapshot_id == first.snapshot_id
    assert restored.object_sha256 == first.object_sha256
    repository_restored = DocumentRepository(state).snapshot(alias.snapshot_id)
    assert repository_restored is not None
    assert repository_restored.snapshot_id == first.snapshot_id
    assert repository_restored.object_sha256 == first.object_sha256


def test_document_linkage_resolves_registered_snapshot_alias_to_canonical_id(
    state: StateStore,
) -> None:
    first = _snapshot("observation:canonical")
    alias = first.model_copy(
        update={
            "snapshot_id": "observation:document-alias",
            "fetched_at": first.fetched_at + timedelta(minutes=5),
            "available_to_system_at": first.available_to_system_at + timedelta(minutes=5),
        }
    )
    canonical = state.register_snapshot(first)
    state.register_snapshot(alias)
    document = SourceDocument(
        document_id="document:alias-linkage",
        title="测试公司2025年年度报告",
        publisher="observation-regression",
        document_type=DocumentType.ANNUAL_REPORT,
        company_ids=["600000"],
        published_at=first.fetched_at,
        effective_at=first.fetched_at,
        disclosure_id="alias-linkage-001",
        source_url=first.source_url,
        rights_status="TEST_ONLY",
    )

    DocumentRepository(state).register(document, alias)

    with state.connect() as connection:
        linked = connection.execute(
            "SELECT snapshot_id FROM document_snapshot WHERE document_id=?",
            (document.document_id,),
        ).fetchone()
        assert linked is not None
        assert linked["snapshot_id"] == canonical.snapshot_id
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_content_cannot_claim_different_byte_size(state: StateStore) -> None:
    first = _snapshot()
    state.register_snapshot(first)
    conflicting = first.model_copy(update={"snapshot_id": "bad-size", "byte_size": 101})
    with pytest.raises(ValueError, match="size"):
        state.register_snapshot(conflicting)
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM source_snapshot_observation WHERE requested_snapshot_id=?",
            ("bad-size",),
        ).fetchone()[0] == 0
