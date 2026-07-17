from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    ZhihuBrowserResponseEnvelope,
    ZhihuContentType,
    ZhihuResponseKind,
    ZhihuTransport,
)
from tests.helpers import make_financial_request

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_cli_init_probe_and_context_plan(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "中文运行目录"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    initialized = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert initialized.exit_code == 0, initialized.output
    assert json.loads(initialized.output)["database_integrity"] == "ok"

    repeated = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert repeated.exit_code == 0, repeated.output
    assert not json.loads(repeated.output)["account_initialized"]

    probed = runner.invoke(app, ["probe"])
    assert probed.exit_code == 0, probed.output
    probe = json.loads(probed.output)
    assert probe["python_supported"]
    assert probe["modes"]["OPENAI_COMPATIBLE_MODE"] == "OPTIONAL_DISABLED"
    assert {item["provider_id"] for item in probe["providers"]} == {
        "eastmoney-5m",
        "sina-5m",
    }

    artifact = tmp_path / "中文证据.json"
    artifact.write_text("{}", encoding="utf-8")
    planned = runner.invoke(
        app,
        [
            "context-plan",
            "--skill",
            "holding-monitor",
            "--artifact",
            str(artifact),
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.output)["artifact_byte_size"] == 2

    sources = runner.invoke(app, ["knowledge-source-list"])
    assert sources.exit_code == 0, sources.output
    source_payload = json.loads(sources.output)
    assert {item["display_name"] for item in source_payload["sources"]} == {
        "MR Dang",
        "黄彦臻",
        "派大星皮皮",
        "寒武纪的鳄鱼",
    }
    crocodile = next(
        item for item in source_payload["sources"] if item["display_name"] == "寒武纪的鳄鱼"
    )
    assert not crocodile["online_collection_required"]

    coverage = runner.invoke(
        app,
        ["zhihu-coverage", "zhihu:mr-dang-77", "--content-type", "answers"],
    )
    assert coverage.exit_code == 0, coverage.output
    assert json.loads(coverage.output) == {"report": None, "status": "NOT_COLLECTED"}

    private_marker = b'{"data":[],"paging":{"is_end":true},"private":"cli-secret"}'
    envelope = ZhihuBrowserResponseEnvelope(
        author_source_id="zhihu:mr-dang-77",
        response_kind=ZhihuResponseKind.LISTING,
        content_type=ZhihuContentType.ANSWERS,
        listing_page=0,
        requested_url=(
            "https://www.zhihu.com/api/v4/members/mr-dang-77/"
            "answers?limit=2&offset=0&sort_by=created"
        ),
        status_code=200,
        response_mime="application/json",
        body_base64=base64.b64encode(private_marker).decode("ascii"),
        transport=ZhihuTransport.CHROME,
        captured_at=datetime.now(UTC),
    )
    envelope_path = runtime / "imports" / "response.json"
    envelope_path.parent.mkdir(parents=True)
    envelope_path.write_text(envelope.model_dump_json(), encoding="utf-8")
    imported = runner.invoke(app, ["zhihu-response-import", str(envelope_path)])
    assert imported.exit_code == 0, imported.output
    import_payload = json.loads(imported.output)
    assert import_payload["status"] == "IMPORTED"
    assert import_payload["import_status"] == "PENDING"
    assert "cli-secret" not in imported.output
    assert str(envelope_path) not in imported.output
    replayed = runner.invoke(
        app,
        ["zhihu-import-replay", import_payload["envelope_id"]],
    )
    assert replayed.exit_code == 0, replayed.output
    replay_payload = json.loads(replayed.output)
    assert replay_payload["status"] == "CONSUMED"
    assert replay_payload["coverage_status"] == "COMPLETE"
    assert "cli-secret" not in replayed.output


def test_private_pdf_cli_output_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "private-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    private_text = "This private fixture sentence must never be emitted by the CLI."
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), private_text)
    pdf_path = tmp_path / "secret-local-book.pdf"
    pdf_path.write_bytes(pdf.tobytes())
    pdf.close()

    invoked = runner.invoke(
        app,
        [
            "private-pdf-ingest",
            str(pdf_path),
            "--source-id",
            "book:test:cli",
            "--title",
            "Private CLI fixture",
            "--author-source-id",
            "author:test",
            "--file-version",
            "v1",
            "--page",
            "1",
            "--no-ocr",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["status"] == "INGESTED"
    assert payload["manifest"]["source_page_count"] == 1
    assert payload["parse"]["processed_page_count"] == 1
    assert private_text not in invoked.output
    assert str(pdf_path) not in invoked.output
    assert pdf_path.name not in invoked.output


def test_private_docx_cli_output_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "private-docx-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    private_text = "This private DOCX sentence must never be emitted by the CLI."
    docx_path = tmp_path / "secret-local-export.docx"
    main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{main_type}"/>
</Types>""",
        )
        archive.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{private_text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>""",
        )

    invoked = runner.invoke(
        app,
        [
            "private-docx-ingest",
            str(docx_path),
            "--source-id",
            "docx:test:cli",
            "--title",
            "Private DOCX CLI fixture",
            "--author-source-id",
            "author:test",
            "--file-version",
            "v1",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["status"] == "INGESTED"
    assert payload["parse"]["coverage_status"] == "COMPLETE"
    assert payload["parse"]["processed_block_count"] == 1
    assert private_text not in invoked.output
    assert str(docx_path) not in invoked.output
    assert docx_path.name not in invoked.output


def test_financial_audit_cli_is_idempotent_and_reports_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "financial-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(runtime / "objects" / "sha256")
    request = make_financial_request(state, objects)
    request = request.model_copy(update={"as_of": datetime(2026, 3, 21, tzinfo=UTC)})
    input_path = tmp_path / "financial-audit-input.json"
    input_path.write_text(
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )

    schema = runner.invoke(app, ["financial-audit-schema"])
    assert schema.exit_code == 0, schema.output
    schema_payload = json.loads(schema.output)
    assert set(schema_payload["required"]) >= {"company_id", "as_of", "industry_profile"}

    first = runner.invoke(app, ["financial-audit", str(input_path)])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["status"] == "SUCCEEDED"
    assert payload["pack"]["coverage_status"] == "COMPLETE"
    assert not payload["reused_existing"]
    assert len(payload["artifact_hash"]) == 64
    assert str(input_path) not in first.output

    repeated = runner.invoke(app, ["financial-audit", str(input_path)])
    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["reused_existing"]
    assert repeated_payload["artifact_hash"] == payload["artifact_hash"]

    status = runner.invoke(
        app,
        ["financial-audit-status", payload["pack"]["audit_run_id"]],
    )
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["status"] == "SUCCEEDED"
    assert status_payload["checkpoint_step"] == "COMPLETE"
    assert status_payload["attempt_count"] == 1


def test_invalid_base_case_cli_request_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "research-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    secret = "private-research-statement-must-not-be-echoed"
    request = tmp_path / "private-base-case-request.json"
    request.write_text(json.dumps({"draft": {"statement": secret}}), encoding="utf-8")

    invoked = runner.invoke(app, ["research-base-case-build", str(request)])
    assert invoked.exit_code == 2
    assert json.loads(invoked.output) == {
        "error_code": "INVALID_BASE_CASE_REQUEST",
        "status": "REJECTED",
    }
    assert secret not in invoked.output
    assert str(request) not in invoked.output
