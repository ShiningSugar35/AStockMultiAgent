from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentRepository, OfficialWebDocumentCaptureService
from astock.pit import PointInTimeRepository
from astock.schemas import (
    AgentSourceProposal,
    AvailabilityBasis,
    DocumentType,
    PointInTimeStatus,
    SourceClass,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVED = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def _proposal(*, capability: str = "disclosure.document") -> AgentSourceProposal:
    return AgentSourceProposal.model_validate(
        {
            "requested_capability": capability,
            "query": "official exchange announcement recovery",
            "candidate_url": "https://www.sse.com.cn/disclosure/listedinfo/example.pdf",
            "expected_fact": "one exact official disclosed fact",
            "preferred_source_class": SourceClass.PRIMARY_OFFICIAL_WEB,
            "formal_use": True,
            "require_complete": False,
            "reason": "recover a known official document without CNINFO",
        }
    )


def _service(tmp_path: Path) -> tuple[OfficialWebDocumentCaptureService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return OfficialWebDocumentCaptureService(state, objects), state, objects


def test_official_exchange_pdf_capture_freezes_snapshot_pit_and_artifact(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)

    capture = service.capture(
        _proposal(),
        PDF,
        title="测试公司重大事项公告",
        company_ids=["600519"],
        published_at=PUBLISHED,
        effective_at=PUBLISHED,
        period_end=date(2026, 6, 30),
        document_type=DocumentType.ANNOUNCEMENT,
        disclosure_id="sse-test-001",
        observed_at=OBSERVED,
    )

    assert capture.source_id == "sse-official-web"
    assert capture.source_class is SourceClass.PRIMARY_OFFICIAL_WEB
    assert capture.formal_eligible
    assert not capture.exhaustive_proof_allowed
    assert not capture.broker_execution_allowed
    assert objects.verify(capture.object_sha256)
    document = DocumentRepository(state).get_model(capture.document_id)
    assert document is not None
    assert document.company_ids == ["600519"]
    snapshot = DocumentRepository(state).snapshot(capture.snapshot_id)
    assert snapshot is not None
    assert snapshot.source_url == str(capture.source_url)
    pit = PointInTimeRepository(state).get(capture.pit_id)
    assert pit is not None
    assert pit.point_in_time_status is PointInTimeStatus.DOCUMENT_RECONSTRUCTED
    assert pit.availability_basis is AvailabilityBasis.FETCH_OBSERVED
    artifact = state.artifact_record(f"OfficialWebDocumentCapture:{capture.capture_id}")
    assert artifact is not None
    assert objects.verify(str(artifact["object_hash"]))


def test_official_web_capture_rejects_non_pdf_and_exhaustive_claim(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="not a PDF"):
        service.capture(
            _proposal(),
            b"<html>not a pdf</html>",
            title="公告",
            company_ids=["600519"],
            published_at=PUBLISHED,
            document_type=DocumentType.ANNOUNCEMENT,
            observed_at=OBSERVED,
        )

    exhaustive = _proposal().model_copy(update={"require_complete": True})
    with pytest.raises(ValueError, match="bounded formal exact-item"):
        service.capture(
            exhaustive,
            PDF,
            title="公告",
            company_ids=["600519"],
            published_at=PUBLISHED,
            document_type=DocumentType.ANNOUNCEMENT,
            observed_at=OBSERVED,
        )


def test_official_web_document_ingest_cli_is_local_and_auditable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    pdf = tmp_path / "official.pdf"
    pdf.write_bytes(PDF)
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    result = CliRunner().invoke(
        app,
        [
            "official-web-document-ingest",
            str(pdf),
            "--url",
            "https://www.sse.com.cn/disclosure/listedinfo/example.pdf",
            "--title",
            "测试公司重大事项公告",
            "--published-at",
            PUBLISHED.isoformat(),
            "--company-id",
            "600519",
            "--disclosure-id",
            "sse-test-cli-001",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["source_id"] == "sse-official-web"
    assert payload["formal_eligible"] is True
    assert payload["exhaustive_proof_allowed"] is False
    assert payload["broker_execution_allowed"] is False
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    assert state.artifact_record(f"OfficialWebDocumentCapture:{payload['capture_id']}") is not None


def test_official_web_document_ingest_cli_failure_is_fixed_json_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    pdf = tmp_path / "official.pdf"
    pdf.write_bytes(PDF)
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    result = CliRunner().invoke(
        app,
        [
            "official-web-document-ingest",
            str(pdf),
            "--url",
            "https://example.invalid/unregistered.pdf",
            "--title",
            "测试公司重大事项公告",
            "--published-at",
            PUBLISHED.isoformat(),
            "--company-id",
            "600519",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "status": "FAILED",
        "failure_code": "OFFICIAL_WEB_DOCUMENT_INGEST_FAILED",
    }
    assert "Traceback" not in result.stdout
    assert "example.invalid" not in result.stdout


def test_exchange_financial_report_url_is_formally_admitted(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    capture = service.capture(
        _proposal(capability="financial.official_document"),
        PDF,
        title="测试公司2025年年度报告",
        company_ids=["600519"],
        published_at=PUBLISHED,
        period_end=date(2025, 12, 31),
        document_type=DocumentType.ANNUAL_REPORT,
        observed_at=OBSERVED,
    )

    assert capture.requested_capability == "financial.official_document"
    assert capture.source_id == "sse-official-web"
