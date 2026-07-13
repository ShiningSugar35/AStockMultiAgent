from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from astock.core.object_store import ObjectStore
from astock.documents.ocr import OcrResult, RapidOcrEngine
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.pdf_parser import PdfParseService
from astock.documents.repository import DocumentRepository
from astock.schemas import (
    DocumentType,
    PageExtractionMethod,
    SourceDocument,
    SourceSnapshot,
)


class FakeOcrEngine:
    name = "fake-ocr"
    version = "1.0"

    def __init__(self, text: str = "1 Scanned Method\nEvidence from scan") -> None:
        self.text = text
        self.calls = 0

    def recognize(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes.startswith(b"\x89PNG")
        self.calls += 1
        return OcrResult(self.text, 0.98)


def native_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(72, 72, 540, 400),
        "1 Introduction\nThis annual report contains enough native text for extraction.",
        fontsize=18,
    )
    result = document.tobytes()
    document.close()
    return result


def scanned_pdf_bytes(text: str = "SCANNED ANNUAL REPORT 2025") -> bytes:
    image = Image.new("RGB", (1400, 500), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 72)
        if font_path.is_file()
        else ImageFont.load_default()
    )
    draw.text((80, 180), text, fill="black", font=font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    document = pymupdf.open()
    page = document.new_page(width=700, height=250)
    page.insert_image(page.rect, stream=buffer.getvalue())
    result = document.tobytes()
    document.close()
    return result


def register_pdf(
    state,
    objects: ObjectStore,
    pdf_bytes: bytes,
    *,
    document_id: str,
) -> tuple[SourceDocument, SourceSnapshot]:
    object_ref = objects.put_bytes(pdf_bytes)
    now = datetime(2026, 7, 13, tzinfo=UTC)
    snapshot = SourceSnapshot(
        snapshot_id=f"fixture:{object_ref.sha256}",
        source_id="fixture-pdf",
        object_sha256=object_ref.sha256,
        fetched_at=now,
        available_to_system_at=now,
        source_url=None,
        mime="application/pdf",
        byte_size=object_ref.byte_size,
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id=document_id,
        title="Fixture PDF",
        publisher="TEST",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["000001"],
        published_at=now,
        effective_at=now,
        disclosure_id=document_id,
        source_url="https://example.invalid/fixture.pdf",
        rights_status="TEST_FIXTURE",
    )
    DocumentRepository(state).register(document, snapshot)
    return document, snapshot


def test_native_page_uses_text_layer_and_recovers_heading(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    document, snapshot = register_pdf(
        state,
        objects,
        native_pdf_bytes(),
        document_id="fixture:native",
    )
    fake_ocr = FakeOcrEngine()
    pages = DocumentPageRepository(state)
    report = PdfParseService(objects, state, pages, ocr_engine=fake_ocr).parse(
        document,
        snapshot,
    )
    assert report.native_page_count == 1
    assert report.ocr_page_count == 0
    assert fake_ocr.calls == 0
    stored = pages.get_page(snapshot.snapshot_id, 1, report.parser_version)
    assert stored is not None
    assert stored.extraction_method is PageExtractionMethod.NATIVE_TEXT
    assert stored.section_path == ["1 Introduction"]
    assert b"annual report" in objects.get_bytes(stored.text_object_sha256)


def test_scanned_page_uses_ocr_once_and_reuses_versioned_cache(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    document, snapshot = register_pdf(
        state,
        objects,
        scanned_pdf_bytes(),
        document_id="fixture:scanned",
    )
    fake_ocr = FakeOcrEngine()
    pages = DocumentPageRepository(state)
    parser = PdfParseService(objects, state, pages, ocr_engine=fake_ocr)
    first = parser.parse(document, snapshot)
    repeated = parser.parse(document, snapshot)
    assert first == repeated
    assert fake_ocr.calls == 1
    assert first.ocr_page_count == 1
    stored = pages.get_page(snapshot.snapshot_id, 1, first.parser_version)
    assert stored is not None
    assert stored.ocr_applied
    assert stored.page_image_sha256 is not None
    assert objects.verify(stored.page_image_sha256)
    assert stored.ocr_average_confidence == 0.98


def test_parser_version_change_keeps_both_page_versions(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    document, snapshot = register_pdf(
        state,
        objects,
        native_pdf_bytes(),
        document_id="fixture:versions",
    )
    pages = DocumentPageRepository(state)
    first = PdfParseService(objects, state, pages, text_threshold=24).parse(
        document, snapshot, ocr_enabled=False
    )
    second = PdfParseService(objects, state, pages, text_threshold=1000).parse(
        document, snapshot, ocr_enabled=False
    )
    assert first.parser_version != second.parser_version
    assert len(pages.page_rows(document.document_id)) == 2


def test_invalid_page_number_is_rejected(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    document, snapshot = register_pdf(
        state,
        objects,
        native_pdf_bytes(),
        document_id="fixture:invalid-page",
    )
    with pytest.raises(ValueError, match="within 1..1"):
        PdfParseService(objects, state, DocumentPageRepository(state)).parse(
            document,
            snapshot,
            page_numbers=[2],
        )


def test_real_rapidocr_reads_local_scanned_page(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    document, snapshot = register_pdf(
        state,
        objects,
        scanned_pdf_bytes(),
        document_id="fixture:real-ocr",
    )
    pages = DocumentPageRepository(state)
    report = PdfParseService(
        objects,
        state,
        pages,
        ocr_engine=RapidOcrEngine(),
        ocr_dpi=200,
    ).parse(document, snapshot)
    stored = pages.get_page(snapshot.snapshot_id, 1, report.parser_version)
    assert stored is not None
    text = objects.get_bytes(stored.text_object_sha256).decode("utf-8").upper()
    assert "ANNUAL" in text
    assert "REPORT" in text
