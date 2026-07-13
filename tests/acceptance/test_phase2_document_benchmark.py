from __future__ import annotations

import os
from pathlib import Path

import pytest

from astock.acceptance import run_controlled_document_benchmark

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        os.environ.get("ASTOCK_RUN_ACCEPTANCE") != "1",
        reason="set ASTOCK_RUN_ACCEPTANCE=1 for the 30-document real OCR benchmark",
    ),
]


def test_thirty_document_pdf_and_evidence_benchmark(tmp_path: Path) -> None:
    report = run_controlled_document_benchmark(tmp_path / "phase2-acceptance")
    assert report["document_count"] == 30
    assert report["native_document_count"] == 15
    assert report["scanned_document_count"] == 15
    assert report["metrics"]["native_text_recall"]["value"] >= 0.98
    assert report["metrics"]["scanned_key_field_recall"]["value"] >= 0.95
    assert report["metrics"]["citation_traceability"]["value"] == 1.0
    assert report["metrics"]["idempotency"]["value"] == 1.0
    assert report["state_integrity"] == "ok"
    assert report["all_passed"]
