from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentRepository, OfficialWebDocumentCaptureService
from astock.documents.report_identity import is_full_report_title
from astock.schemas import (
    AgentSourceProposal,
    DocumentType,
    FinancialPeriodType,
    SourceClass,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVED = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("测试股份有限公司2025年年度报告", True),
        ("测试股份有限公司2025年年度报告（修订版）", True),
        ("测试股份有限公司2025年年度报告全文", True),
        ("关于测试股份有限公司2025年年度报告信息披露监管问询函的回复报告", False),
        ("测试股份有限公司2025年年度报告独立董事述职报告", False),
        ("关于2025年年度报告的更正公告", False),
        ("关于延期披露2025年年度报告", False),
        ("独立董事关于2025年年度报告", False),
        ("取消审议2025年年度报告", False),
        ("关注函关于2025年年度报告", False),
        ("Notice Regarding 2025 Annual Report", False),
    ],
)
def test_full_report_title_requires_period_report_to_be_primary_document(
    title: str,
    expected: bool,
) -> None:
    assert (
        is_full_report_title(
            title,
            date(2025, 12, 31),
            FinancialPeriodType.ANNUAL,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("title", "period_end", "expected"),
    [
        ("Huahai Pharma 2025 Q1 Report", date(2025, 3, 31), True),
        ("Q1 Report 2025", date(2025, 3, 31), True),
        ("Huahai Pharma 2025 1st Quarter Report", date(2025, 3, 31), True),
        ("First Quarterly Report 2025", date(2025, 3, 31), True),
        ("Huahai Pharma 2025 Q3 Report", date(2025, 3, 31), False),
        ("Huahai Pharma 2025 Q3 Report", date(2025, 9, 30), True),
        ("Q3 Report 2025", date(2025, 9, 30), True),
        ("Huahai Pharma 2025 3rd Quarter Report", date(2025, 9, 30), True),
        ("Third Quarterly Report 2025", date(2025, 9, 30), True),
    ],
)
def test_quarterly_english_aliases_do_not_cross_periods(
    title: str,
    period_end: date,
    expected: bool,
) -> None:
    assert (
        is_full_report_title(
            title,
            period_end,
            FinancialPeriodType.QUARTERLY,
        )
        is expected
    )


def _docx_bytes(*, macro: bool = False) -> bytes:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>测试公司公告正文</w:t></w:r></w:p></w:body>
</w:document>
"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        if macro:
            archive.writestr("word/vbaProject.bin", b"not-executed")
    return buffer.getvalue()


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
    assert snapshot.mime == "application/pdf"
    assert capture.media_type == "application/pdf"
    artifact = state.artifact_record(f"OfficialWebDocumentCapture:{capture.capture_id}")
    assert artifact is not None
    assert objects.verify(str(artifact["object_hash"]))


def test_official_web_duplicate_content_reuses_canonical_snapshot_and_keeps_observation(
    tmp_path: Path,
) -> None:
    service, state, _ = _service(tmp_path)
    kwargs = {
        "title": "测试公司2025年年度报告",
        "company_ids": ["600519"],
        "published_at": PUBLISHED,
        "period_end": date(2025, 12, 31),
        "document_type": DocumentType.ANNUAL_REPORT,
        "disclosure_id": "sse-repeat-001",
    }

    first = service.capture(
        _proposal(capability="financial.official_document"),
        PDF,
        observed_at=OBSERVED,
        **kwargs,
    )
    later = service.capture(
        _proposal(capability="financial.official_document"),
        PDF,
        observed_at=OBSERVED + timedelta(minutes=5),
        **kwargs,
    )

    assert later.snapshot_id == first.snapshot_id
    assert later.capture_id != first.capture_id
    with state.connect() as connection:
        observations = connection.execute(
            "SELECT requested_snapshot_id,canonical_snapshot_id,source_url "
            "FROM source_snapshot_observation "
            "WHERE canonical_snapshot_id=? ORDER BY observed_at",
            (first.snapshot_id,),
        ).fetchall()
        assert len(observations) == 2
        assert all(row["canonical_snapshot_id"] == first.snapshot_id for row in observations)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_official_web_capture_accepts_supported_content_without_extension_trust(
    tmp_path: Path,
) -> None:
    service, state, _ = _service(tmp_path)

    text_capture = service.capture(
        _proposal(),
        "测试公司公告正文\n第二行".encode(),
        title="公告",
        company_ids=["600519"],
        published_at=PUBLISHED,
        document_type=DocumentType.ANNOUNCEMENT,
        observed_at=OBSERVED,
    )
    docx_capture = service.capture(
        _proposal(),
        _docx_bytes(),
        title="公告 DOCX",
        company_ids=["600519"],
        published_at=PUBLISHED,
        document_type=DocumentType.ANNOUNCEMENT,
        disclosure_id="sse-docx-001",
        observed_at=OBSERVED + timedelta(minutes=1),
    )

    assert text_capture.media_type == "text/plain"
    assert text_capture.text_encoding == "utf-8-sig"
    assert docx_capture.media_type.endswith("wordprocessingml.document")
    text_snapshot = state.get_snapshot(text_capture.snapshot_id)
    docx_snapshot = state.get_snapshot(docx_capture.snapshot_id)
    assert text_snapshot is not None and text_snapshot.mime == "text/plain"
    assert docx_snapshot is not None and docx_snapshot.mime == docx_capture.media_type


def test_official_web_capture_rejects_unsupported_markup_macro_and_exhaustive_claim(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    for payload in (
        b"<html>not plain text</html>",
        b"<!-- comment --><div>not plain text</div>",
        b"<div>not plain text</div>",
        b"<report>not plain text</report>",
    ):
        with pytest.raises(ValueError, match="Markup/RTF"):
            service.capture(
                _proposal(),
                payload,
                title="公告",
                company_ids=["600519"],
                published_at=PUBLISHED,
                document_type=DocumentType.ANNOUNCEMENT,
                observed_at=OBSERVED,
            )
    with pytest.raises(ValueError, match="Macro-enabled"):
        service.capture(
            _proposal(),
            _docx_bytes(macro=True),
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
