from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

import astock.cli as cli_module
from astock.cli import app
from astock.core.errors import DataQualityError, FailureClass
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.direct_source_distillation_service import (
    DirectSourceDistillationService,
)
from astock.knowledge.direct_source_real_run_service import (
    _SOURCE_SEMANTIC_RUN_ID,
    DirectSourceRealRunService,
)
from astock.schemas.direct_source_real_run import DirectSourceRealRunImportPlan

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTICLE34_TITLE = "2024-07-16_《经济学原理》阿尔弗雷德.马歇尔"
_ARTICLE34_TITLE_HASH = "2c95c8cccf30d56224f751a3e3c04388294dac09e2666a7520c0801fd1a5c0b6"
_ARTICLE34_METADATA_HASH = "4c45060cffadf8128a47334dd9060b17dac12a41223ab0b7fcc5ca717f0cbda0"
_ARTICLE34_METADATA_BYTES = (
    b'{"block_kind":"PARAGRAPH","cell_index":null,"heading_level":null,'
    b'"hyperlink_targets":[],"paragraph_index":726,"part_kind":"MAIN",'
    b'"part_name":"word/document.xml","part_sequence":0,"row_index":null,'
    b'"schema_version":"1.0","section_path":["2023-01-02_\xe6\x80\xbb\xe7\xbb\x93\xef\xbc\x9a'
    b'\xe7\xbb\x8f\xe9\xaa\x8c\xe4\xb8\x8e\xe6\x95\x99\xe8\xae\xad","2024-06-25_\xe4\xb8\xba\xe4\xbb\x80\xe4\xb9\x88'
    b'\xe6\xaf\x8f\xe4\xb8\x80\xe6\xac\xa1\xe8\x8c\x85\xe5\x8f\xb0\xe6\x9a\xb4\xe8\xb7\x8c\xe4\xb9\x8b\xe5\x90\x8e\xef\xbc\x8c'
    b'\xe8\x82\xa1\xe5\xb8\x82\xe8\xa7\x81\xe5\xba\x95\xef\xbc\x8c\xe7\x86\x8a\xe5\xb8\x82\xe7\xbb\x93\xe6\x9d\x9f\xef\xbc\x9f"],'
    b'"style_id":null,"style_name":null,"table_index":null}'
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _service(root: Path) -> DirectSourceRealRunService:
    return DirectSourceRealRunService(
        StateStore(root / "state.sqlite", PROJECT_ROOT / "migrations"),
        ObjectStore(root / "objects"),
        root,
    )


def _write_source_state(root: Path, *, pdf_hash: str, docx_hash: str) -> Path:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    state = {
        "source_hashes": {
            "pdf": {"path": "docs/current.pdf", "sha256": pdf_hash, "page_count": 249},
            "docx": {
                "path": "docs/current.docx",
                "sha256": docx_hash,
                "article_count": 123,
                "body_paragraph_count": 2032,
            },
        },
        "accepted_batch_ids": [
            "b01",
            "b02",
            "b03",
            "b04",
            "b05",
            "b06",
            "b07.1",
            "b07.2",
            "b08.1",
            "b08.2",
            "b09",
            "b10",
            "b11",
            "b12",
        ],
        "accepted_docx_sections": [],
    }
    path = root / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _write_contract(root: Path, *, pdf_hash: str, docx_hash: str) -> Path:
    articles = _contract_articles()
    payload = {
        "source_fingerprints": {
            "pdf": {
                "sha256": pdf_hash,
                "pages": 249,
                "audited_empty_units": [
                    {
                        "source_kind": "PDF",
                        "unit_index": 2,
                        "start_offset": 0,
                        "end_offset": 0,
                        "object_hash": _sha256(b""),
                    }
                ],
            },
            "docx": {
                "sha256": docx_hash,
                "paragraphs": 2032,
                "sections": 123,
                "paragraph_locator_scheme": "direct-ooxml-body-w:p-1based",
                "title_anchor_rule": (
                    "trim(concatenated descendant w:t) matches ^\\d{4}-\\d{2}-\\d{2}_"
                ),
                "audited_article_boundary_markers": [_article34_marker()],
            },
            "visual_reuse": {
                "book_manifest_id": "book-manifest:test",
                "book_report_id": "book-coverage:test",
                "visual_run_id": "book-visual-run:test",
                "semantic_run_id": "semantic-run:current-test",
                "pdf_sha256": pdf_hash,
                "page_count": 249,
                "image_page_count": 56,
                "placement_count": 73,
                "semantic_ref_count": 71,
                "coverage_status": "COMPLETE",
                "quality_status": "REVIEW_REQUIRED",
                "non_decorative_placement_count": 71,
                "adjudications": _test_visual_adjudications(),
            },
        },
        "docx_batches": articles,
    }
    path = root / "contract.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _test_visual_adjudications() -> list[dict[str, object]]:
    return [
        {
            "action": "NON_SEMANTIC_EXCLUDE",
            "evidence_id": "evidence-36",
            "page_number": 78,
            "placement_index": 2,
            "placement_ordinal": 36,
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "image_object_hash": "a" * 64,
            "evidence_object_hash": "b" * 64,
            "chart_unit_id": "chart-36",
            "original_chart_type": "DECORATIVE",
            "original_decorative_excluded": True,
            "original_review_reason_codes": [],
            "ocr_status": "NO_TEXT",
            "ocr_result_object_hash": "f" * 64,
            "semantic_ref_id": None,
            "semantic_ref_object_hash": None,
        },
        {
            "action": "NON_SEMANTIC_EXCLUDE",
            "evidence_id": "evidence-46",
            "page_number": 97,
            "placement_index": 2,
            "placement_ordinal": 46,
            "bbox": [5.0, 6.0, 7.0, 8.0],
            "image_object_hash": "c" * 64,
            "evidence_object_hash": "d" * 64,
            "chart_unit_id": "chart-46",
            "original_chart_type": "TABLE",
            "original_decorative_excluded": False,
            "original_review_reason_codes": ["OCR_NO_TEXT"],
            "ocr_status": "NO_TEXT",
            "ocr_result_object_hash": "f" * 64,
            "semantic_ref_id": "ref-46",
            "semantic_ref_object_hash": "e" * 64,
        },
    ]


def _contract_articles() -> list[dict[str, object]]:
    articles: list[dict[str, object]] = []
    start = 1
    for ordinal in range(1, 124):
        if ordinal <= 12:
            count = 23
        elif ordinal <= 32:
            count = 22
        elif ordinal == 33:
            count = 9
        elif ordinal == 34:
            count = 3
        elif ordinal == 35:
            count = 13
        elif ordinal == 36:
            count = 15
        elif ordinal <= 114:
            count = 14
        elif ordinal < 123:
            count = 13
        else:
            count = 80
        end = start + count - 1
        articles.append(
            {
                "article_index": ordinal,
                "title": (
                    _ARTICLE34_TITLE
                    if ordinal == 34
                    else f"2025-01-{((ordinal - 1) % 28) + 1:02d}_article-{ordinal}"
                ),
                "start_paragraph": start,
                "end_paragraph": end,
                "paragraph_count": count,
                "paragraph_locator_scheme": "ooxml-body-paragraph-1based",
            }
        )
        start = end + 1
    return articles


def _article34_marker() -> dict[str, object]:
    return {
        "article_index": 34,
        "block_index": 726,
        "title_hash": _ARTICLE34_TITLE_HASH,
        "title_anchor_matches": True,
        "is_heading": False,
        "style_id": None,
        "heading_level": None,
        "metadata_object_hash": _ARTICLE34_METADATA_HASH,
        "parser_version": "wordprocessingml-ecma376+rules-v1",
    }


def _articles(hash_value: str) -> list[dict[str, object]]:
    return [
        {
            "ordinal": ordinal,
            "start": ordinal,
            "end": ordinal,
            "units": [{"index": ordinal, "hash": hash_value, "length": 1}],
            "leading_context": [],
            "trailing_context": [],
        }
        for ordinal in range(1, 124)
    ]


def _release_payloads() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    return (
        {"run_id": "direct-source-real:test", "batches": []},
        {"legacy_freeze_hash": "a" * 64},
        {"run_id": "direct-source-real:test", "completed_only_batch_ids": ["b01"]},
    )


def _create_visual_schema(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE book_source_manifest (
            manifest_id TEXT PRIMARY KEY, document_id TEXT, snapshot_id TEXT,
            file_sha256 TEXT, source_page_count INTEGER, manifest_json TEXT, created_at TEXT
        );
        """
    )
    connection.executescript(
        (PROJECT_ROOT / "migrations" / "0043_book_visual_semantics.sql").read_text(encoding="utf-8")
    )
    return connection


_IGNORED_HISTORY_CHILD_TABLES = (
    "knowledge_review_decision",
    "knowledge_reviewed_semantic_run",
    "knowledge_reviewed_argument_unit",
    "knowledge_reviewed_argument_paragraph_ref",
    "knowledge_reviewed_embedding_manifest",
    "knowledge_reviewed_coverage_report",
    "knowledge_reviewed_visual_ref",
)


def _insert_source_semantic_run(root: Path) -> None:
    connection = sqlite3.connect(root / "state.sqlite")
    connection.execute(
        """
        CREATE TABLE knowledge_semantic_run (
            run_id TEXT PRIMARY KEY,
            author_source_id TEXT NOT NULL,
            input_manifest_hash TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            stage TEXT NOT NULL,
            run_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO knowledge_semantic_run VALUES(?,?,?,?,?,?,?,?)",
        (
            _SOURCE_SEMANTIC_RUN_ID,
            "author",
            "a" * 64,
            "semantic-pipeline-v1",
            "DEEPSEEK_PACKET_READY",
            '{"aggregate":"metadata-only"}',
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:00Z",
        ),
    )
    for table in _IGNORED_HISTORY_CHILD_TABLES:
        connection.execute(f"CREATE TABLE {table} (payload TEXT)")
        connection.execute(f"INSERT INTO {table} VALUES ('ignored')")
    connection.commit()
    connection.close()


def _configure_release_inputs(
    service: DirectSourceRealRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_current_source_hashes",
        lambda _payload: {"PDF": "f" * 64, "DOCX": "b" * 64},
    )
    monkeypatch.setattr(service, "_frozen_docx_contract", lambda *_args: [])
    monkeypatch.setattr(
        service,
        "_frozen_visual_contract",
        lambda *_args: {
            "placement_count": 73,
            "image_page_count": 56,
            "semantic_ref_count": 71,
        },
    )
    monkeypatch.setattr(service, "_frozen_pdf_empty_units", lambda *_args: {2: _sha256(b"")})
    monkeypatch.setattr(
        service,
        "_pdf_source",
        lambda *_args: (
            {
                page: {
                    "hash": _sha256(b"") if page == 2 else "f" * 64,
                    "length": 0 if page == 2 else 1,
                }
                for page in range(1, 250)
            },
            {},
            {
                "image_page_count": 56,
                "image_placement_count": 73,
                "semantic_ref_count": 71,
                "residual_review_count": 0,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_docx_source",
        lambda *_args: (
            _articles("b" * 64),
            {
                "paragraph_count": 2032,
                "article_count": 123,
                "empty_paragraph_count": 123,
            },
        ),
    )


def _insert_docx_registration(root: Path, *, docx_hash: str) -> None:
    connection = sqlite3.connect(root / "state.sqlite")
    connection.executescript(
        """
        CREATE TABLE book_source_manifest (
            manifest_id TEXT PRIMARY KEY, document_id TEXT, snapshot_id TEXT,
            file_sha256 TEXT, source_page_count INTEGER, manifest_json TEXT, created_at TEXT
        );
        CREATE TABLE private_docx_parse_report (
            docx_parse_report_id TEXT PRIMARY KEY, manifest_id TEXT, parser_version TEXT,
            coverage_status TEXT, report_object_hash TEXT, report_json TEXT, created_at TEXT
        );
        CREATE TABLE document_block (
            block_index INTEGER, snapshot_id TEXT, parser_version TEXT, text_object_hash TEXT,
            text_char_count INTEGER, block_json TEXT
        );
        """
    )
    store = ObjectStore(root / "objects")
    report_hash = store.put_json({"coverage_status": "COMPLETE"}).sha256
    empty_hash = store.put_bytes(b"").sha256
    body_hash = store.put_bytes(b"body").sha256
    assert store.put_bytes(_ARTICLE34_METADATA_BYTES).sha256 == _ARTICLE34_METADATA_HASH
    articles = _contract_articles()
    article_by_start = {cast(int, item["start_paragraph"]): item for item in articles}
    empty_indexes = {cast(int, item["start_paragraph"]) + 1 for item in articles}
    connection.execute(
        "INSERT INTO book_source_manifest VALUES(?,?,?,?,?,?,?)",
        ("docx-manifest", "docx-document", "docx-snapshot", docx_hash, 0, "{}", "now"),
    )
    connection.execute(
        "INSERT INTO private_docx_parse_report VALUES(?,?,?,?,?,?,?)",
        (
            "docx-report",
            "docx-manifest",
            "wordprocessingml-ecma376+rules-v1",
            "COMPLETE",
            report_hash,
            "{}",
            "now",
        ),
    )
    rows: list[tuple[object, ...]] = []
    for index in range(1, 2033):
        article = article_by_start.get(index)
        if article is not None:
            text = str(article["title"]).encode("utf-8")
            object_hash = store.put_bytes(text).sha256
            length = len(text.decode("utf-8"))
            is_heading = cast(int, article["article_index"]) != 34
        elif index in empty_indexes:
            object_hash = empty_hash
            length = 0
            is_heading = False
        else:
            object_hash = body_hash
            length = 4
            is_heading = False
        block_json: dict[str, object] = {"is_heading": is_heading}
        if index == 726:
            block_json.update(
                {
                    "metadata_object_sha256": _ARTICLE34_METADATA_HASH,
                    "paragraph_index": 726,
                    "parser_version": "wordprocessingml-ecma376+rules-v1",
                }
            )
        rows.append(
            (
                index,
                "docx-snapshot",
                "wordprocessingml-ecma376+rules-v1",
                object_hash,
                length,
                json.dumps(block_json),
            )
        )
    connection.executemany("INSERT INTO document_block VALUES(?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def _insert_visual_chain(
    connection: sqlite3.Connection,
    *,
    stage: str = "AUDITED",
    coverage_status: str = "COMPLETE",
    quality_status: str = "PASS",
    include_chart: bool = True,
    image_page_count: int = 57,
    run_id: str = "book-visual-run:test",
    manifest_id: str = "book-manifest:test",
) -> None:
    h = "a" * 64
    connection.execute(
        "INSERT INTO book_source_manifest VALUES(?,?,?,?,?,?,?)",
        (manifest_id, "document", "snapshot", h, 249, "{}", "now"),
    )
    connection.execute(
        "INSERT INTO book_visual_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            manifest_id,
            "source",
            "snapshot",
            h,
            "v1",
            "layout-v1",
            "classification-v1",
            stage,
            249,
            image_page_count,
            1,
            1,
            _SOURCE_SEMANTIC_RUN_ID,
            h,
            h,
            "{}",
            "now",
            "now",
        ),
    )
    connection.execute(
        "INSERT INTO book_visual_coverage_report VALUES(?,?,?,?,?,?)",
        ("report", run_id, coverage_status, quality_status, h, "{}"),
    )
    connection.execute(
        "INSERT INTO book_image_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("evidence", run_id, 1, 1, 1, 1, "[]", 1.0, 1.0, h, None, h, "{}"),
    )
    connection.execute(
        "INSERT INTO book_image_evidence_attempt VALUES(?,?,?,?,?,?,?,?,?)",
        ("attempt", "evidence", 1, "XREF_ORIGINAL", "SUCCESS", h, None, h, "{}"),
    )
    connection.execute(
        "INSERT INTO book_image_ocr VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("evidence", run_id, "NO_TEXT", None, None, "engine", "v1", "[]", h, "{}"),
    )
    connection.execute(
        "INSERT INTO book_layout_atom VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("atom", run_id, 1, 1, 1, "IMAGE_EVIDENCE", "[]", None, "evidence", h, "{}"),
    )
    if include_chart:
        connection.execute(
            "INSERT INTO book_chart_unit VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("chart", run_id, "evidence", "TEXT_IMAGE", 1.0, 0, 0, "[]", h, "{}"),
        )
    connection.commit()


def _insert_exact_adjudicated_visual_chain(root: Path) -> dict[str, object]:
    connection = _create_visual_schema(root / "state.sqlite")
    h = ObjectStore(root / "objects").put_bytes(b"frozen-visual-object").sha256
    run_id = "book-visual-run:current"
    manifest_id = "book-manifest:current"
    semantic_run_id = "semantic-run:current"
    connection.execute(
        "INSERT INTO book_source_manifest VALUES(?,?,?,?,?,?,?)",
        (manifest_id, "document", "snapshot", h, 249, "{}", "now"),
    )
    connection.execute(
        "INSERT INTO book_visual_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            manifest_id,
            "source",
            "snapshot",
            h,
            "v1",
            "layout-v1",
            "classification-v1",
            "AUDITED",
            249,
            46,
            46,
            46,
            semantic_run_id,
            h,
            h,
            "{}",
            "now",
            "now",
        ),
    )
    connection.execute(
        "INSERT INTO book_visual_coverage_report VALUES(?,?,?,?,?,?)",
        ("report-current", run_id, "COMPLETE", "REVIEW_REQUIRED", h, "{}"),
    )
    for ordinal in range(1, 47):
        evidence_id = f"evidence-{ordinal}"
        chart_id = f"chart-{ordinal}"
        page = 78 if ordinal == 36 else 97 if ordinal == 46 else ordinal
        bbox = [float(ordinal), 2.0, float(ordinal) + 1.0, 4.0]
        chart_type = "DECORATIVE" if ordinal == 36 else "TABLE" if ordinal == 46 else "TEXT_IMAGE"
        decorative = 1 if ordinal == 36 else 0
        reasons = ["OCR_NO_TEXT"] if ordinal == 46 else []
        connection.execute(
            "INSERT INTO book_image_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_id,
                run_id,
                page,
                1,
                ordinal,
                ordinal,
                json.dumps(bbox),
                612.0,
                792.0,
                h,
                None,
                h,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO book_image_evidence_attempt VALUES(?,?,?,?,?,?,?,?,?)",
            (f"attempt-{ordinal}", evidence_id, 1, "XREF_ORIGINAL", "SUCCESS", h, None, h, "{}"),
        )
        connection.execute(
            "INSERT INTO book_image_ocr VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_id,
                run_id,
                "NO_TEXT" if ordinal in (36, 46) else "SUCCESS",
                None,
                None,
                "engine",
                "v1",
                "[]",
                h,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO book_layout_atom VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"atom-{ordinal}",
                run_id,
                page,
                ordinal,
                ordinal,
                "IMAGE_EVIDENCE",
                "[]",
                None,
                evidence_id,
                h,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO book_chart_unit VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                chart_id,
                run_id,
                evidence_id,
                chart_type,
                1.0,
                decorative,
                0,
                json.dumps(reasons),
                h,
                "{}",
            ),
        )
        if ordinal != 36:
            connection.execute(
                "INSERT INTO book_visual_semantic_ref VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"ref-{ordinal}",
                    run_id,
                    chart_id,
                    semantic_run_id,
                    f"paragraph-{ordinal}",
                    f"argument-{ordinal}",
                    "[]",
                    h,
                    "{}",
                ),
            )
    connection.commit()
    connection.close()
    return {
        "book_manifest_id": manifest_id,
        "book_report_id": "report-current",
        "visual_run_id": run_id,
        "semantic_run_id": semantic_run_id,
        "image_page_count": 46,
        "placement_count": 46,
        "semantic_ref_count": 45,
        "coverage_status": "COMPLETE",
        "quality_status": "REVIEW_REQUIRED",
        "adjudications": [
            {
                "action": "NON_SEMANTIC_EXCLUDE",
                "evidence_id": "evidence-36",
                "page_number": 78,
                "placement_index": 1,
                "placement_ordinal": 36,
                "bbox": [36.0, 2.0, 37.0, 4.0],
                "image_object_hash": h,
                "evidence_object_hash": h,
                "chart_unit_id": "chart-36",
                "original_chart_type": "DECORATIVE",
                "original_decorative_excluded": True,
                "original_review_reason_codes": [],
                "ocr_status": "NO_TEXT",
                "ocr_result_object_hash": h,
                "semantic_ref_id": None,
                "semantic_ref_object_hash": None,
            },
            {
                "action": "NON_SEMANTIC_EXCLUDE",
                "evidence_id": "evidence-46",
                "page_number": 97,
                "placement_index": 1,
                "placement_ordinal": 46,
                "bbox": [46.0, 2.0, 47.0, 4.0],
                "image_object_hash": h,
                "evidence_object_hash": h,
                "chart_unit_id": "chart-46",
                "original_chart_type": "TABLE",
                "original_decorative_excluded": False,
                "original_review_reason_codes": ["OCR_NO_TEXT"],
                "ocr_status": "NO_TEXT",
                "ocr_result_object_hash": h,
                "semantic_ref_id": "ref-46",
                "semantic_ref_object_hash": h,
            },
        ],
    }


def test_prepare_fails_closed_when_current_pdf_object_is_missing(tmp_path: Path) -> None:
    pdf = b"current-pdf"
    docx = b"current-docx"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "current.pdf").write_bytes(pdf)
    (tmp_path / "docs" / "current.docx").write_bytes(docx)
    state_file = _write_source_state(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))
    contract = _write_contract(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))

    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path).prepare(state_file, tmp_path / "release", contract)

    assert raised.value.details["failure_code"] == "CURRENT_SOURCE_OBJECT_MISSING"
    assert raised.value.details["source_kind"] == "PDF"
    assert not (tmp_path / "release").exists()


def test_prepare_does_not_treat_a_legacy_pdf_object_as_the_current_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = b"current-pdf"
    docx = b"current-docx"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "current.pdf").write_bytes(pdf)
    (tmp_path / "docs" / "current.docx").write_bytes(docx)
    state_file = _write_source_state(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))
    contract = _write_contract(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))
    service = _service(tmp_path)
    legacy_pdf_hash = "fd5055" + "0" * 58
    monkeypatch.setattr(service.object_store, "verify", lambda value: value == legacy_pdf_hash)

    with pytest.raises(DataQualityError) as raised:
        service.prepare(state_file, tmp_path / "release", contract)

    assert raised.value.details["failure_code"] == "CURRENT_SOURCE_OBJECT_MISSING"
    assert raised.value.details["source_file_hash"] == _sha256(pdf)


def test_prepare_fails_closed_on_current_source_hash_drift(tmp_path: Path) -> None:
    pdf = b"current-pdf"
    docx = b"current-docx"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "current.pdf").write_bytes(pdf)
    (tmp_path / "docs" / "current.docx").write_bytes(docx)
    state_file = _write_source_state(tmp_path, pdf_hash="0" * 64, docx_hash=_sha256(docx))
    contract = _write_contract(tmp_path, pdf_hash="0" * 64, docx_hash=_sha256(docx))

    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path).prepare(state_file, tmp_path / "release", contract)

    assert raised.value.details["failure_code"] == "CURRENT_SOURCE_HASH_DRIFT"
    assert raised.value.details["source_kind"] == "PDF"


@pytest.mark.parametrize(
    ("stage", "coverage", "expected_code"),
    [
        ("SEMANTIC_MATERIALIZED", "COMPLETE", "CURRENT_VISUAL_RELINEAGE_MISSING"),
        ("AUDITED", "PARTIAL", "CURRENT_VISUAL_COVERAGE_UNAUDITED"),
    ],
)
def test_current_visual_rejects_non_audited_or_incomplete_0043_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    coverage: str,
    expected_code: str,
) -> None:
    connection = _create_visual_schema(tmp_path / "state.sqlite")
    _insert_visual_chain(connection, stage=stage, coverage_status=coverage)
    connection.close()
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_require_object", lambda *_args, **_kwargs: None)
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._current_pdf_visuals(
            readonly,
            "book-manifest:test",
            249,
            {
                "book_manifest_id": "book-manifest:test",
                "book_report_id": "report",
                "visual_run_id": "book-visual-run:test",
                "semantic_run_id": _SOURCE_SEMANTIC_RUN_ID,
                "image_page_count": 1,
                "placement_count": 1,
                "coverage_status": "COMPLETE",
                "quality_status": coverage,
            },
        )
    assert raised.value.details["failure_code"] == expected_code


def test_current_visual_rejects_chart_coverage_drift_0043_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _create_visual_schema(tmp_path / "state.sqlite")
    _insert_visual_chain(connection, include_chart=False, image_page_count=1)
    connection.close()
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_require_object", lambda *_args, **_kwargs: None)
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._current_pdf_visuals(
            readonly,
            "book-manifest:test",
            249,
            {
                "book_manifest_id": "book-manifest:test",
                "book_report_id": "report",
                "visual_run_id": "book-visual-run:test",
                "semantic_run_id": _SOURCE_SEMANTIC_RUN_ID,
                "image_page_count": 1,
                "placement_count": 1,
                "coverage_status": "COMPLETE",
                "quality_status": "PASS",
            },
        )
    assert raised.value.details["failure_code"] == "VISUAL_CHART_COVERAGE_DRIFT"


def test_current_visual_applies_only_two_exact_nonsemantic_adjudications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _insert_exact_adjudicated_visual_chain(tmp_path)
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_require_object", lambda *_args, **_kwargs: None)
    with service._read_only_connection() as readonly:
        by_page, counts = service._current_pdf_visuals(
            readonly, "book-manifest:current", 249, contract
        )
    emitted = {evidence_id for values in by_page.values() for evidence_id in values}
    assert "evidence-36" not in emitted
    assert "evidence-46" not in emitted
    assert len(emitted) == 44
    assert counts == {
        "image_page_count": 46,
        "image_placement_count": 46,
        "semantic_ref_count": 45,
        "emitted_visual_count": 44,
        "residual_review_count": 0,
    }


@pytest.mark.parametrize(
    ("sql", "params", "expected_code"),
    [
        (
            "UPDATE book_image_evidence SET bbox_json=? WHERE evidence_id='evidence-46'",
            ("[0,0,1,1]",),
            "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
        ),
        (
            "UPDATE book_image_evidence SET image_object_hash=? WHERE evidence_id='evidence-46'",
            ("b" * 64,),
            "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
        ),
        (
            "UPDATE book_image_evidence SET placement_index=2 WHERE evidence_id='evidence-46'",
            (),
            "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
        ),
        (
            "UPDATE book_chart_unit SET chart_type='TEXT_IMAGE' WHERE evidence_id='evidence-46'",
            (),
            "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
        ),
        (
            "UPDATE book_visual_semantic_ref SET ref_object_hash=? WHERE ref_id='ref-46'",
            ("b" * 64,),
            "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
        ),
        (
            "UPDATE book_chart_unit SET review_reason_codes_json='[\"REVIEW\"]' "
            "WHERE evidence_id='evidence-1'",
            (),
            "CURRENT_VISUAL_UNADJUDICATED_REVIEW",
        ),
        (
            "UPDATE book_image_ocr SET status='NO_TEXT' WHERE evidence_id='evidence-1'",
            (),
            "CURRENT_VISUAL_UNADJUDICATED_REVIEW",
        ),
        (
            "UPDATE book_image_ocr SET status='LOW_CONFIDENCE' WHERE evidence_id='evidence-1'",
            (),
            "CURRENT_VISUAL_UNADJUDICATED_REVIEW",
        ),
        (
            "UPDATE book_image_ocr SET status='FAILED' WHERE evidence_id='evidence-1'",
            (),
            "CURRENT_VISUAL_UNADJUDICATED_REVIEW",
        ),
        (
            "DELETE FROM book_visual_semantic_ref WHERE ref_id='ref-1'",
            (),
            "CURRENT_VISUAL_SEMANTIC_REF_COUNT_DRIFT",
        ),
        (
            "UPDATE book_visual_run SET image_page_count=45",
            (),
            "CURRENT_VISUAL_COVERAGE_DRIFT",
        ),
    ],
)
def test_current_visual_exact_binding_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    params: tuple[object, ...],
    expected_code: str,
) -> None:
    contract = _insert_exact_adjudicated_visual_chain(tmp_path)
    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute(sql, params)
    writable.commit()
    writable.close()
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_require_object", lambda *_args, **_kwargs: None)
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._current_pdf_visuals(readonly, "book-manifest:current", 249, contract)
    assert raised.value.details["failure_code"] == expected_code


def test_current_visual_rejects_another_valid_adjudication_ocr_result_object(
    tmp_path: Path,
) -> None:
    contract = _insert_exact_adjudicated_visual_chain(tmp_path)
    service = _service(tmp_path)
    other_valid_hash = service.object_store.put_bytes(b"different-valid-ocr-result").sha256
    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute(
        "UPDATE book_image_ocr SET result_object_hash=? WHERE evidence_id='evidence-46'",
        (other_valid_hash,),
    )
    writable.commit()
    writable.close()
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._current_pdf_visuals(readonly, "book-manifest:current", 249, contract)
    assert raised.value.details["failure_code"] == "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT"


def _insert_pdf_pages(root: Path, *, page_two_hash: str, page_two_length: int) -> str:
    connection = sqlite3.connect(root / "state.sqlite")
    connection.executescript(
        """
        CREATE TABLE book_source_manifest (
            manifest_id TEXT PRIMARY KEY, document_id TEXT, snapshot_id TEXT,
            file_sha256 TEXT, source_page_count INTEGER, manifest_json TEXT, created_at TEXT
        );
        CREATE TABLE document_page (
            page_number INTEGER, document_id TEXT, snapshot_id TEXT,
            text_object_hash TEXT, text_char_count INTEGER
        );
        """
    )
    source_hash = "f" * 64
    connection.execute(
        "INSERT INTO book_source_manifest VALUES(?,?,?,?,?,?,?)",
        ("pdf-manifest", "pdf-document", "pdf-snapshot", source_hash, 249, "{}", "now"),
    )
    body_hash = ObjectStore(root / "objects").put_bytes(b"page").sha256
    connection.executemany(
        "INSERT INTO document_page VALUES(?,?,?,?,?)",
        [
            (
                page,
                "pdf-document",
                "pdf-snapshot",
                page_two_hash if page == 2 else body_hash,
                page_two_length if page == 2 else 4,
            )
            for page in range(1, 250)
        ],
    )
    connection.commit()
    connection.close()
    return source_hash


def test_pdf_page_two_audited_empty_covers_b01_without_fragment_or_skill_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    empty_hash = service.object_store.put_bytes(b"").sha256
    source_hash = _insert_pdf_pages(tmp_path, page_two_hash=empty_hash, page_two_length=0)
    monkeypatch.setattr(
        service,
        "_current_pdf_visuals",
        lambda *_: ({}, {"image_page_count": 0, "image_placement_count": 0}),
    )
    with service._read_only_connection() as readonly:
        page_map, _, _ = service._pdf_source(readonly, source_hash, 249, {}, {2: empty_hash})
    manifest = service._build_init_manifest(
        {"PDF": source_hash, "DOCX": "d" * 64}, page_map, {}, _articles("d" * 64)
    )
    b01 = cast(list[dict[str, object]], manifest["batches"])[0]
    current_fragments = cast(list[dict[str, object]], b01["current_fragments"])
    assert [
        cast(dict[str, object], item["locator"])["unit_index"] for item in current_fragments
    ] == [1]
    assert b01["audited_empty_units"] == [
        {
            "object_hash": empty_hash,
            "locator": {
                "source_kind": "PDF",
                "unit_index": 2,
                "start_offset": 0,
                "end_offset": 0,
            },
        }
    ]


@pytest.mark.parametrize(
    ("page_two_length", "contract_empty", "expected_code"),
    [
        (0, False, "CURRENT_SOURCE_FRAGMENT_EMPTY"),
        (4, True, "CURRENT_PDF_AUDITED_EMPTY_DRIFT"),
    ],
)
def test_pdf_audited_empty_contract_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_two_length: int,
    contract_empty: bool,
    expected_code: str,
) -> None:
    service = _service(tmp_path)
    empty_hash = service.object_store.put_bytes(b"").sha256
    body_hash = service.object_store.put_bytes(b"page").sha256
    source_hash = _insert_pdf_pages(
        tmp_path,
        page_two_hash=empty_hash if page_two_length == 0 else body_hash,
        page_two_length=page_two_length,
    )
    monkeypatch.setattr(service, "_current_pdf_visuals", lambda *_: ({}, {}))
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._pdf_source(
            readonly,
            source_hash,
            249,
            {},
            {2: empty_hash} if contract_empty else {},
        )
    assert raised.value.details["failure_code"] == expected_code


def test_frozen_docx_contract_rejects_overlap_and_uses_no_runtime_headings(tmp_path: Path) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    payload = cast(dict[str, object], json.loads(contract.read_text(encoding="utf-8")))
    batches = cast(list[dict[str, object]], payload["docx_batches"])
    batches[1]["start_paragraph"] = 1
    contract.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path)._frozen_docx_contract(
            cast(Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8"))),
            contract,
        )
    assert raised.value.details["failure_code"] == "DOCX_CONTRACT_BOUNDARY_INVALID"


@pytest.mark.parametrize("mutation", ["delete", "add", "duplicate"])
def test_frozen_docx_exact_boundary_marker_count_drift_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    payload = cast(dict[str, object], json.loads(contract.read_text(encoding="utf-8")))
    fingerprints = cast(dict[str, object], payload["source_fingerprints"])
    docx = cast(dict[str, object], fingerprints["docx"])
    markers = cast(list[dict[str, object]], docx["audited_article_boundary_markers"])
    if mutation == "delete":
        markers.clear()
    elif mutation == "add":
        extra = dict(_article34_marker())
        extra["article_index"] = 35
        markers.append(extra)
    else:
        markers.append(dict(markers[0]))
    contract.write_text(json.dumps(payload), encoding="utf-8")
    state = cast(
        Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8"))
    )
    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path)._frozen_docx_contract(state, contract)
    assert raised.value.details["failure_code"] == "DOCX_AUDITED_BOUNDARY_MARKER_COUNT_DRIFT"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("article_index", 35),
        ("block_index", 727),
        ("title_hash", "b" * 64),
        ("title_anchor_matches", False),
        ("is_heading", True),
        ("style_id", "Heading1"),
        ("heading_level", 1),
        ("metadata_object_hash", "b" * 64),
        ("parser_version", "wordprocessingml-other"),
    ],
)
def test_frozen_docx_exact_boundary_marker_field_drift_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    payload = cast(dict[str, object], json.loads(contract.read_text(encoding="utf-8")))
    fingerprints = cast(dict[str, object], payload["source_fingerprints"])
    docx = cast(dict[str, object], fingerprints["docx"])
    markers = cast(list[dict[str, object]], docx["audited_article_boundary_markers"])
    markers[0][field] = value
    contract.write_text(json.dumps(payload), encoding="utf-8")
    state = cast(
        Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8"))
    )
    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path)._frozen_docx_contract(state, contract)
    assert raised.value.details["failure_code"] in {
        "DOCX_AUDITED_BOUNDARY_MARKER_INVALID",
        "DOCX_AUDITED_BOUNDARY_MARKER_DRIFT",
    }


def test_frozen_docx_marker_contract_still_passes_with_no_drift(tmp_path: Path) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    frozen = _service(tmp_path)._frozen_docx_contract(
        cast(Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8"))),
        contract,
    )
    article34 = cast(
        dict[str, object], next(batch for batch in frozen if cast(int, batch["ordinal"]) == 34)
    )
    assert article34["audited_boundary_marker"] == _article34_marker()


@pytest.mark.parametrize(
    "mutation",
    [
        "article33_end",
        "article34_start_and_end",
        "article35_start",
    ],
)
def test_frozen_docx_adjacent_boundary_drift_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    payload = cast(dict[str, object], json.loads(contract.read_text(encoding="utf-8")))
    batches = cast(list[dict[str, object]], payload["docx_batches"])

    article33 = next(item for item in batches if cast(int, item["article_index"]) == 33)
    article34 = next(item for item in batches if cast(int, item["article_index"]) == 34)
    article35 = next(item for item in batches if cast(int, item["article_index"]) == 35)

    if mutation == "article33_end":
        article33["end_paragraph"] = 726
        article33["paragraph_count"] = 10
        article34["start_paragraph"] = 727
        article34["paragraph_count"] = 2
    elif mutation == "article34_start_and_end":
        article33["end_paragraph"] = 726
        article33["paragraph_count"] = 10
        article34["start_paragraph"] = 727
        article34["end_paragraph"] = 729
        article34["paragraph_count"] = 3
        article35["start_paragraph"] = 730
        article35["paragraph_count"] = 12
    else:
        article34["end_paragraph"] = 729
        article34["paragraph_count"] = 4
        article35["start_paragraph"] = 730
        article35["paragraph_count"] = 12

    contract.write_text(json.dumps(payload), encoding="utf-8")
    state = cast(Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8")))

    with pytest.raises(DataQualityError) as raised:
        _service(tmp_path)._frozen_docx_contract(state, contract)
    assert (
        raised.value.details["failure_code"]
        == "DOCX_AUDITED_BOUNDARY_ADJACENCY_DRIFT"
    )


def test_docx_other_non_heading_article_start_remains_rejected(tmp_path: Path) -> None:
    pdf_hash = "a" * 64
    docx_hash = "b" * 64
    state_file = _write_source_state(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    contract = _write_contract(tmp_path, pdf_hash=pdf_hash, docx_hash=docx_hash)
    _insert_docx_registration(tmp_path, docx_hash=docx_hash)
    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute(
        "UPDATE document_block SET block_json=? WHERE block_index=717",
        (json.dumps({"is_heading": False}),),
    )
    writable.commit()
    writable.close()
    service = _service(tmp_path)
    state = cast(
        Mapping[str, object], json.loads(state_file.read_text(encoding="utf-8"))
    )
    frozen = service._frozen_docx_contract(state, contract)
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._docx_source(readonly, docx_hash, 2032, 123, frozen)
    assert raised.value.details["failure_code"] == "CURRENT_DOCX_ARTICLE_BOUNDARY_DRIFT"
    assert raised.value.details["article_ordinal"] == 33


def test_docx_rejects_multiple_parse_reports_before_reading_paragraphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.executescript(
        """
        CREATE TABLE book_source_manifest (
            manifest_id TEXT, document_id TEXT, snapshot_id TEXT, file_sha256 TEXT,
            source_page_count INTEGER, manifest_json TEXT, created_at TEXT
        );
        CREATE TABLE private_docx_parse_report (
            docx_parse_report_id TEXT, manifest_id TEXT, parser_version TEXT,
            coverage_status TEXT, report_object_hash TEXT, report_json TEXT, created_at TEXT
        );
        CREATE TABLE document_block (
            block_index INTEGER, snapshot_id TEXT, parser_version TEXT, text_object_hash TEXT,
            text_char_count INTEGER, block_json TEXT
        );
        """
    )
    h = "a" * 64
    connection.execute(
        "INSERT INTO book_source_manifest VALUES(?,?,?,?,?,?,?)",
        ("docx", "d", "s", h, 0, "{}", "now"),
    )
    connection.executemany(
        "INSERT INTO private_docx_parse_report VALUES(?,?,?,?,?,?,?)",
        [
            ("one", "docx", "wordprocessingml-ecma376+rules-v1", "COMPLETE", h, "{}", "now"),
            ("two", "docx", "other-parser", "COMPLETE", h, "{}", "now"),
        ],
    )
    connection.commit()
    connection.close()
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_require_object", lambda *_args, **_kwargs: None)
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._docx_source(readonly, h, 2032, 123, [])
    assert raised.value.details["failure_code"] == "CURRENT_DOCX_PARSE_MULTIPLE"


def test_prepare_and_verify_accept_123_audited_empty_docx_paragraphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = b"current-pdf"
    docx = b"current-docx"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "current.pdf").write_bytes(pdf)
    (tmp_path / "docs" / "current.docx").write_bytes(docx)
    service = _service(tmp_path)
    service.object_store.put_bytes(pdf)
    service.object_store.put_bytes(docx)
    state_file = _write_source_state(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))
    contract = _write_contract(tmp_path, pdf_hash=_sha256(pdf), docx_hash=_sha256(docx))
    _insert_docx_registration(tmp_path, docx_hash=_sha256(docx))
    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.executescript(
        (PROJECT_ROOT / "migrations" / "0049_direct_source_distillation_state.sql").read_text(
            encoding="utf-8"
        )
    )
    connection.commit()
    connection.close()
    page_hash = service.object_store.put_bytes(b"page").sha256
    monkeypatch.setattr(
        service,
        "_pdf_source",
        lambda *_: (
            {
                page: {
                    "hash": _sha256(b"") if page == 2 else page_hash,
                    "length": 0 if page == 2 else 4,
                }
                for page in range(1, 250)
            },
            {},
            {
                "image_page_count": 56,
                "image_placement_count": 73,
                "semantic_ref_count": 71,
                "residual_review_count": 0,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_legacy_freeze",
        lambda *_args, **_kwargs: {
            "schema_version": "direct-source-legacy-freeze-v3",
            "source_semantic_run": {
                "run_id": _SOURCE_SEMANTIC_RUN_ID,
                "status": "DEEPSEEK_PACKET_READY",
                "canonical_aggregate_fingerprint": "a" * 64,
            },
            "legacy_freeze_hash": "b" * 64,
        },
    )

    prepared = service.prepare(state_file, tmp_path / "release", contract)
    verified = service.verify(state_file, tmp_path / "release", contract)

    assert prepared["status"] == "PREPARED"
    assert verified["status"] == "VERIFIED"
    legacy = json.loads((tmp_path / "release" / "legacy-freeze.json").read_text(encoding="utf-8"))
    block_contract = legacy["current_docx_block_contract"]
    assert block_contract["paragraph_count"] == 2032
    assert block_contract["article_count"] == 123
    assert block_contract["empty_paragraph_count"] == 123
    assert block_contract["zero_length_representation"] == {
        "source_kind": "DOCX",
        "start_offset": 0,
        "end_offset": 0,
        "object_hash": _sha256(b""),
    }
    init = json.loads((tmp_path / "release" / "direct-run-init.json").read_text(encoding="utf-8"))
    docx_batches = [batch for batch in init["batches"] if batch["source_id"] == "direct-docx"]
    assert len(docx_batches) == 123
    assert all(batch["current_fragments"] for batch in docx_batches)
    assert all(
        fragment["locator"]["end_offset"] > fragment["locator"]["start_offset"]
        for batch in docx_batches
        for fragment in batch["current_fragments"]
    )
    assert sum(len(batch["audited_empty_units"]) for batch in docx_batches) == 123
    pdf_batches = [batch for batch in init["batches"] if batch["source_id"] == "direct-pdf"]
    assert pdf_batches[0]["audited_empty_units"] == [
        {
            "object_hash": _sha256(b""),
            "locator": {
                "source_kind": "PDF",
                "unit_index": 2,
                "start_offset": 0,
                "end_offset": 0,
            },
        }
    ]
    assert [
        fragment["locator"]["unit_index"] for fragment in pdf_batches[0]["current_fragments"]
    ] == [1]
    for batch in docx_batches:
        non_empty_indexes = {
            fragment["locator"]["unit_index"] for fragment in batch["current_fragments"]
        }
        empty_indexes = {
            item["locator"]["unit_index"] for item in batch["audited_empty_units"]
        }
        assert non_empty_indexes.isdisjoint(empty_indexes)
        assert non_empty_indexes | empty_indexes == set(
            range(batch["source_unit_start"], batch["source_unit_end"] + 1)
        )

    distillation = DirectSourceDistillationService(service.state, service.object_store)
    initialized = distillation.init_file(tmp_path / "release" / "direct-run-init.json")
    replayed = distillation.init_file(tmp_path / "release" / "direct-run-init.json")
    assert initialized["frozen_batch_count"] == 144
    assert initialized["idempotent_replay"] is False
    assert replayed["frozen_batch_count"] == 144
    assert replayed["idempotent_replay"] is True

    def cloned_manifest() -> dict[str, object]:
        return cast(dict[str, object], json.loads(json.dumps(init)))

    deleted = cloned_manifest()
    deleted_batch = cast(list[dict[str, object]], deleted["batches"])[21]
    cast(list[dict[str, object]], deleted_batch["audited_empty_units"]).pop()

    added = cloned_manifest()
    added_batch = cast(list[dict[str, object]], added["batches"])[21]
    added_empty = cast(list[dict[str, object]], added_batch["audited_empty_units"])
    added_empty.append(
        {
            "object_hash": _sha256(b""),
            "locator": {
                "source_kind": "DOCX",
                "unit_index": cast(int, added_batch["source_unit_end"]) + 1,
                "start_offset": 0,
                "end_offset": 0,
            },
        }
    )

    duplicated = cloned_manifest()
    duplicated_batch = cast(list[dict[str, object]], duplicated["batches"])[21]
    duplicated_empty = cast(
        list[dict[str, object]], duplicated_batch["audited_empty_units"]
    )
    duplicated_empty.append(cast(dict[str, object], dict(duplicated_empty[0])))

    overlapped = cloned_manifest()
    overlapped_batch = cast(list[dict[str, object]], overlapped["batches"])[21]
    overlapped_empty = cast(
        list[dict[str, object]], overlapped_batch["audited_empty_units"]
    )
    cast(dict[str, object], overlapped_empty[0]["locator"])["unit_index"] = overlapped_batch[
        "source_unit_start"
    ]

    for invalid in (deleted, added, duplicated, overlapped):
        with pytest.raises(DataQualityError):
            distillation.init(invalid)


def test_build_manifest_covers_all_pdf_and_docx_batches() -> None:
    object_hash = "a" * 64
    service = _service(Path.cwd())
    manifest = service._build_init_manifest(
        {"PDF": object_hash, "DOCX": "b" * 64},
        {page: {"hash": object_hash, "length": 1} for page in range(1, 250)},
        {154: ["evidence-154"]},
        _articles("b" * 64),
    )
    batches = cast(list[Mapping[str, object]], manifest["batches"])
    assert len(batches) == 144
    assert [batch["batch_id"] for batch in batches[:14]][-1] == "b12"
    assert batches[20]["batch_id"] == "b19"
    assert batches[21]["batch_id"] == "docx-001"
    assert batches[-1]["batch_id"] == "docx-123"


def test_prepare_and_verify_are_idempotent_without_process_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    state_file = tmp_path / "state.json"
    contract = tmp_path / "contract.json"
    state_file.write_text("{}", encoding="utf-8")
    contract.write_text("{}", encoding="utf-8")
    payloads = _release_payloads()
    monkeypatch.setattr(service, "_collect_release", lambda *_args: payloads)
    first = service.prepare(state_file, tmp_path / "release", contract)
    second = service.prepare(state_file, tmp_path / "release", contract)
    verified = service.verify(state_file, tmp_path / "release", contract)
    assert first["run_id"] == second["run_id"] == verified["run_id"]


def test_partial_checkpoint_import_plan_keeps_all_docx_articles_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    object_hash = "a" * 64
    state = {
        "source_hashes": {
            "pdf": {"sha256": object_hash, "path": "docs/p", "page_count": 249},
            "docx": {
                "sha256": "b" * 64,
                "path": "docs/d",
                "article_count": 123,
                "body_paragraph_count": 2032,
            },
        },
        "accepted_batch_ids": [
            "b01",
            "b02",
            "b03",
            "b04",
            "b05",
            "b06",
            "b07.1",
            "b07.2",
            "b08.1",
            "b08.2",
            "b09",
            "b10",
            "b11",
            "b12",
        ],
        "accepted_docx_sections": [],
    }
    monkeypatch.setattr(
        service, "_current_source_hashes", lambda _: {"PDF": object_hash, "DOCX": "b" * 64}
    )
    monkeypatch.setattr(service, "_frozen_docx_contract", lambda *_: [])
    monkeypatch.setattr(
        service,
        "_frozen_visual_contract",
        lambda *_: {"placement_count": 73, "image_page_count": 56, "semantic_ref_count": 71},
    )
    monkeypatch.setattr(service, "_frozen_pdf_empty_units", lambda *_: {2: _sha256(b"")})
    monkeypatch.setattr(service, "_read_only_connection", lambda: nullcontext(None))
    monkeypatch.setattr(
        service,
        "_pdf_source",
        lambda *_: (
            {page: {"hash": object_hash, "length": 1} for page in range(1, 250)},
            {},
            {
                "image_page_count": 56,
                "image_placement_count": 73,
                "semantic_ref_count": 71,
                "residual_review_count": 0,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_docx_source",
        lambda *_: (
            _articles("b" * 64),
            {
                "paragraph_count": 2032,
                "article_count": 123,
                "empty_paragraph_count": 123,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_legacy_freeze",
        lambda *_args, **_kwargs: {
            "schema_version": "direct-source-legacy-freeze-v3",
            "source_semantic_run": {
                "run_id": _SOURCE_SEMANTIC_RUN_ID,
                "status": "DEEPSEEK_PACKET_READY",
                "canonical_aggregate_fingerprint": "a" * 64,
            },
        },
    )
    _, _, plan = service._collect_release(state, tmp_path / "contract.json")
    assert plan["completed_batch_count"] == 14
    assert plan["remaining_pdf_batch_count"] == 7
    assert plan["remaining_docx_batch_count"] == 123


def test_prepare_and_verify_ignore_all_review_history_child_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_source_semantic_run(tmp_path)
    service = _service(tmp_path)
    _configure_release_inputs(service, monkeypatch)
    state_file = _write_source_state(tmp_path, pdf_hash="f" * 64, docx_hash="b" * 64)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    release = tmp_path / "release"
    assert service.prepare(state_file, release, contract)["status"] == "PREPARED"
    legacy_payload = json.loads((release / "legacy-freeze.json").read_text(encoding="utf-8"))
    assert set(legacy_payload) == {
        "schema_version",
        "source_semantic_run",
        "current_docx_block_contract",
        "legacy_freeze_hash",
    }
    source_run = cast(Mapping[str, object], legacy_payload["source_semantic_run"])
    assert source_run["run_id"] == _SOURCE_SEMANTIC_RUN_ID
    assert source_run["status"] == "DEEPSEEK_PACKET_READY"
    assert len(cast(str, source_run["canonical_aggregate_fingerprint"])) == 64
    assert "reviewed" not in json.dumps(legacy_payload)

    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute("UPDATE knowledge_reviewed_semantic_run SET payload='changed'")
    writable.execute("DELETE FROM knowledge_reviewed_argument_unit")
    for table in _IGNORED_HISTORY_CHILD_TABLES:
        if table not in {
            "knowledge_reviewed_semantic_run",
            "knowledge_reviewed_argument_unit",
        }:
            writable.execute(f"DROP TABLE {table}")
    writable.commit()
    writable.close()
    assert service.verify(state_file, release, contract)["status"] == "VERIFIED"


def test_verify_detects_source_semantic_top_level_row_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_source_semantic_run(tmp_path)
    service = _service(tmp_path)
    _configure_release_inputs(service, monkeypatch)
    state_file = _write_source_state(tmp_path, pdf_hash="f" * 64, docx_hash="b" * 64)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    release = tmp_path / "release"
    service.prepare(state_file, release, contract)

    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute(
        "UPDATE knowledge_semantic_run SET input_manifest_hash=? WHERE run_id=?",
        ("b" * 64, _SOURCE_SEMANTIC_RUN_ID),
    )
    writable.commit()
    writable.close()
    with pytest.raises(DataQualityError) as raised:
        service.verify(state_file, release, contract)
    assert raised.value.details["failure_code"] == "RELEASE_ARTIFACT_DRIFT"


def test_source_semantic_stage_drift_still_fails_closed(tmp_path: Path) -> None:
    _insert_source_semantic_run(tmp_path)
    service = _service(tmp_path)
    writable = sqlite3.connect(tmp_path / "state.sqlite")
    writable.execute(
        "UPDATE knowledge_semantic_run SET stage='COMPLETE' WHERE run_id=?",
        (_SOURCE_SEMANTIC_RUN_ID,),
    )
    writable.commit()
    writable.close()
    with service._read_only_connection() as readonly, pytest.raises(DataQualityError) as raised:
        service._legacy_freeze(readonly)
    assert raised.value.details["failure_code"] == "LEGACY_SOURCE_SEMANTIC_STAGE_DRIFT"
    assert raised.value.details["expected_stage"] == "DEEPSEEK_PACKET_READY"


def test_legacy_freeze_sql_is_top_level_semantic_run_only() -> None:
    source = (
        PROJECT_ROOT / "src" / "astock" / "knowledge" / "direct_source_real_run_service.py"
    ).read_text(encoding="utf-8")
    segment = source.split("    def _legacy_freeze(", maxsplit=1)[1].split(
        "    @staticmethod\n    def _fingerprint_complete_rows(", maxsplit=1
    )[0]
    assert "FROM knowledge_semantic_run WHERE run_id=?" in segment
    assert "_legacy_visual_lineage" not in source
    for table in _IGNORED_HISTORY_CHILD_TABLES:
        assert table not in segment


def test_verify_detects_release_artifact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    state_file = tmp_path / "state.json"
    contract = tmp_path / "contract.json"
    state_file.write_text("{}", encoding="utf-8")
    contract.write_text("{}", encoding="utf-8")
    payloads = _release_payloads()
    monkeypatch.setattr(service, "_collect_release", lambda *_args: payloads)
    service.prepare(state_file, tmp_path / "release", contract)
    (tmp_path / "release" / "import-plan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DataQualityError) as raised:
        service.verify(state_file, tmp_path / "release", contract)
    assert raised.value.details["failure_code"] == "RELEASE_ARTIFACT_DRIFT"


def test_load_json_file_rejects_duplicate_keys_and_non_finite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    nonfinite = tmp_path / "nonfinite.json"
    duplicate.write_text('{"x":1,"x":2}', encoding="utf-8")
    nonfinite.write_text('{"x":NaN}', encoding="utf-8")
    with pytest.raises(DataQualityError, match="duplicate JSON key"):
        DirectSourceRealRunService.load_json_file(duplicate)
    with pytest.raises(DataQualityError, match="non-finite JSON number"):
        DirectSourceRealRunService.load_json_file(nonfinite)


def test_import_plan_schema_requires_completed_and_remaining_partition() -> None:
    with pytest.raises(ValueError, match="does not cover every frozen batch"):
        DirectSourceRealRunImportPlan.model_validate(
            {
                "schema_version": "direct-source-real-run-import-plan-v1",
                "run_id": "direct-source-real:test",
                "init_manifest_sha256": "a" * 64,
                "total_batch_count": 2,
                "completed_only_batch_ids": ["b01"],
                "remaining_pdf_batch_ids": [],
                "remaining_docx_batch_ids": [],
                "completed_batch_count": 1,
                "remaining_pdf_batch_count": 0,
                "remaining_docx_batch_count": 0,
                "formal_committee_weight_allowed": False,
            }
        )


def test_cli_prepare_success_and_fail_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state.json"
    contract = tmp_path / "contract.json"
    state.write_text("{}", encoding="utf-8")
    contract.write_text("{}", encoding="utf-8")

    class FakeService:
        def prepare(self, *_args: object) -> dict[str, object]:
            return {"status": "PREPARED"}

        def verify(self, *_args: object) -> dict[str, object]:
            raise DataQualityError(
                "reject",
                failure_class=FailureClass.DATA_QUALITY,
                details={"failure_code": "TEST_REJECT"},
            )

    monkeypatch.setattr(cli_module, "_direct_real_run_service", lambda: FakeService())
    runner = CliRunner()
    prepared = runner.invoke(
        app,
        [
            "knowledge-direct-real-run-prepare",
            str(state),
            "--output",
            str(tmp_path / "release"),
            "--frozen-contract",
            str(contract),
        ],
    )
    rejected = runner.invoke(
        app,
        [
            "knowledge-direct-real-run-verify",
            str(state),
            "--output",
            str(tmp_path),
            "--frozen-contract",
            str(contract),
        ],
    )
    assert prepared.exit_code == 0 and '"PREPARED"' in prepared.output
    assert rejected.exit_code == 3 and '"TEST_REJECT"' in rejected.output


def test_cli_registers_prepare_and_verify_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "knowledge-direct-real-run-prepare" in result.output
    assert "knowledge-direct-real-run-verify" in result.output
