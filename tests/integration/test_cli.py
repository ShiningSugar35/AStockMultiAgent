from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from typer.testing import CliRunner

from astock.cli import app

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
