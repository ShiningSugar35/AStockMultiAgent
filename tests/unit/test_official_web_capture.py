from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentRepository, OfficialWebDocumentCaptureService
from astock.documents.official_web import load_official_web_capture
from astock.schemas import (
    AgentSourceProposal,
    DocumentType,
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
    assert objects.verify(capture.object_sha256)
    document = DocumentRepository(state).get_model(capture.document_id)
    assert document is not None
    assert document.company_ids == ["600519"]
    snapshot = DocumentRepository(state).snapshot(capture.snapshot_id)
    assert snapshot is not None
    assert snapshot.source_url == str(capture.source_url)
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


def _readable_capture(service: OfficialWebDocumentCaptureService):
    return service.capture(
        _proposal(), PDF, title="测试公司2026年度业绩预告", company_ids=["600519"],
        published_at=PUBLISHED, document_type=DocumentType.ANNOUNCEMENT, observed_at=OBSERVED,
    )


def test_verified_official_reader_is_local_and_obeys_identity_and_cutoff(tmp_path, monkeypatch):
    service, state, objects = _service(tmp_path)
    capture = _readable_capture(service)

    def no_write(*args, **kwargs):
        raise AssertionError("verified original reader must not persist or recapture")

    monkeypatch.setattr(objects, "put_json", no_write)
    monkeypatch.setattr(objects, "put_bytes", no_write)
    monkeypatch.setattr(state, "register_artifact", no_write)
    monkeypatch.setattr(state, "register_snapshot", no_write)
    loaded, document, snapshot = load_official_web_capture(
        state, objects, capture.capture_id, company_id="600519", as_of=OBSERVED,
    )
    assert loaded == capture and document.document_id == capture.document_id
    assert snapshot.object_sha256 == capture.object_sha256
    with pytest.raises(ValueError, match="cutoff"):
        load_official_web_capture(
            state, objects, capture.capture_id, company_id="600519",
            as_of=OBSERVED - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="company"):
        load_official_web_capture(
            state, objects, capture.capture_id, company_id="000001", as_of=OBSERVED,
        )
    with pytest.raises(ValueError, match="capability"):
        load_official_web_capture(
            state, objects, capture.capture_id, company_id="600519", as_of=OBSERVED,
            capabilities=("financial.official_document",),
        )


@pytest.mark.parametrize("source_kind", ["original", "admission"])
def test_verified_official_reader_rejects_corrupted_original_or_admission(tmp_path, source_kind):
    service, state, objects = _service(tmp_path)
    capture = _readable_capture(service)
    snapshot_id = (
        capture.snapshot_id if source_kind == "original" else capture.admission_snapshot_id
    )
    snapshot = state.get_snapshot(snapshot_id)
    assert snapshot is not None
    objects.path_for(snapshot.object_sha256).write_bytes(b"altered source")
    with pytest.raises(ValueError, match="raw source"):
        load_official_web_capture(
            state, objects, capture.capture_id, company_id="600519", as_of=OBSERVED,
        )


def test_verified_official_reader_rechecks_frozen_admission_bindings(tmp_path):
    service, state, objects = _service(tmp_path)
    capture = _readable_capture(service)
    admission = state.get_snapshot(capture.admission_snapshot_id)
    assert admission is not None
    payload = json.loads(objects.get_bytes(admission.object_sha256))
    payload["document_object_sha256"] = "f" * 64
    changed = objects.put_json(payload)
    other_admission = admission.model_copy(update={
        "snapshot_id": admission.snapshot_id + ":wrong-original",
        "object_sha256": changed.sha256, "byte_size": changed.byte_size,
    })
    state.register_snapshot(other_admission)
    forged = capture.model_copy(update={
        "capture_id": capture.capture_id + ":wrong-original",
        "admission_snapshot_id": other_admission.snapshot_id,
    })
    frozen = objects.put_json(forged.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"OfficialWebDocumentCapture:{forged.capture_id}",
        artifact_type="OfficialWebDocumentCapture", schema_version=forged.schema_version,
        object_hash=frozen.sha256,
        input_hashes=[capture.object_sha256, other_admission.object_sha256],
    )
    with pytest.raises(ValueError, match="admission binding"):
        load_official_web_capture(
            state, objects, forged.capture_id, company_id="600519", as_of=OBSERVED,
        )
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM document_snapshot WHERE document_id=? AND snapshot_id=?",
            (capture.document_id, capture.snapshot_id),
        )
    with pytest.raises(ValueError, match="not registered to its document"):
        load_official_web_capture(
            state, objects, capture.capture_id, company_id="600519", as_of=OBSERVED,
        )
