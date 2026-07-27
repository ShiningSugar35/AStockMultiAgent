from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.schemas import EvidencePack


def test_evidence_pack_normalizes_payload() -> None:
    pack = EvidencePack(
        run_artifact_id="EvidenceCollectionRun:fixture",
        company="Ningde Times",
        ticker="300750",
        evidence_items=["evidence:2", "evidence:1", "evidence:1"],
        missing_items=["missing:2", "missing:1", "missing:1"],
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert pack.evidence_items == ["evidence:1", "evidence:2"]
    assert pack.missing_items == ["missing:1", "missing:2"]


def test_evidence_pack_rejects_invalid_ticker() -> None:
    with pytest.raises(ValueError):
        EvidencePack(
            run_artifact_id="EvidenceCollectionRun:fixture",
            company="Ningde Times",
            ticker="12345",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
