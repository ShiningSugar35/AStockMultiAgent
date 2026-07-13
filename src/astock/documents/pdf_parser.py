"""Versioned native-text and page-selective OCR parsing for PDF snapshots."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

import pymupdf

from astock.core.errors import DataQualityError, FailureClass
from astock.core.hashing import content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.ocr import OcrEngine, RapidOcrEngine
from astock.documents.page_repository import DocumentPageRepository
from astock.schemas import (
    DocumentPage,
    DocumentParseReport,
    PageExtractionMethod,
    ParseStatus,
    SourceDocument,
    SourceSnapshot,
)

_HEADING_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百零〇0-9]+[章节篇部]\s*\S.{0,35}$"),
    re.compile(r"^[一二三四五六七八九十]+[、.]\s*\S.{0,35}$"),
    re.compile(r"^\d+(?:\.\d+){0,3}\s+\S.{0,35}$"),
)


class PdfParseService:
    parser_name = "pymupdf-page-parser"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        repository: DocumentPageRepository,
        *,
        ocr_engine: OcrEngine | None = None,
        text_threshold: int = 24,
        ocr_dpi: int = 200,
    ) -> None:
        if text_threshold < 0:
            raise ValueError("text_threshold must be non-negative")
        if ocr_dpi < 100 or ocr_dpi > 400:
            raise ValueError("ocr_dpi must be between 100 and 400")
        self.object_store = object_store
        self.state = state
        self.repository = repository
        self._ocr_engine = ocr_engine
        self.text_threshold = text_threshold
        self.ocr_dpi = ocr_dpi

    def parse(
        self,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        *,
        page_numbers: list[int] | None = None,
        ocr_enabled: bool = True,
    ) -> DocumentParseReport:
        parser_version = self._parser_version(ocr_enabled)
        pdf_bytes = self.object_store.get_bytes(snapshot.object_sha256)
        if not pdf_bytes.lstrip().startswith(b"%PDF-"):
            raise DataQualityError(
                "Source snapshot is not a PDF",
                failure_class=FailureClass.DATA_QUALITY,
                details={"snapshot_id": snapshot.snapshot_id},
            )
        try:
            pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            raise DataQualityError(
                "PDF could not be opened",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"snapshot_id": snapshot.snapshot_id},
            ) from exc
        with pdf:
            pages = self._normalize_pages(page_numbers, pdf.page_count)
            page_scope_hash = content_hash(pages)
            cached = self.repository.get_report(
                snapshot.snapshot_id,
                parser_version,
                page_scope_hash,
            )
            if cached is not None:
                return cached
            started_at = datetime.now(UTC).isoformat()
            parsed_pages = self._parse_pages(
                pdf,
                document,
                snapshot,
                pages,
                parser_version,
                ocr_enabled=ocr_enabled,
            )
            report = self._build_report(
                document,
                snapshot,
                parser_version,
                pdf.page_count,
                pages,
                parsed_pages,
            )
        report_ref = self.object_store.put_json(report.model_dump(mode="json"))
        report = report.model_copy(update={"report_object_sha256": report_ref.sha256})
        self.state.register_artifact(
            artifact_id=f"DocumentParseReport:{report_ref.sha256}",
            artifact_type="DocumentParseReport",
            schema_version=report.schema_version,
            object_hash=report_ref.sha256,
            input_hashes=[snapshot.object_sha256, parser_version, page_scope_hash],
        )
        self.repository.register_report(
            report,
            page_scope_hash=page_scope_hash,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
        )
        return report

    def _parse_pages(
        self,
        pdf: pymupdf.Document,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        pages: list[int],
        parser_version: str,
        *,
        ocr_enabled: bool,
    ) -> list[DocumentPage]:
        parsed: list[DocumentPage] = []
        current_section: list[str] = []
        for page_number in pages:
            cached = self.repository.get_page(snapshot.snapshot_id, page_number, parser_version)
            if cached is not None:
                parsed.append(cached)
                if cached.section_path:
                    current_section = cached.section_path
                continue
            page = pdf.load_page(page_number - 1)
            native_text = _normalize_text(cast(str, page.get_text("text", sort=True)))
            native_count = _visible_char_count(native_text)
            text = native_text
            method = PageExtractionMethod.NATIVE_TEXT
            image_hash: str | None = None
            confidence: float | None = None
            warnings: list[str] = []
            ocr_applied = False
            ocr_engine_name: str | None = None
            ocr_engine_version: str | None = None
            if native_count < self.text_threshold:
                method = PageExtractionMethod.EMPTY
                if ocr_enabled:
                    ocr_applied = True
                    pixmap = page.get_pixmap(
                        dpi=self.ocr_dpi,
                        alpha=False,
                        colorspace=pymupdf.csRGB,
                    )
                    image_ref = self.object_store.put_bytes(pixmap.tobytes("png"))
                    image_hash = image_ref.sha256
                    engine = self._get_ocr_engine()
                    ocr_engine_name = engine.name
                    ocr_engine_version = engine.version
                    try:
                        result = engine.recognize(self.object_store.get_bytes(image_ref.sha256))
                        text = _normalize_text(result.text)
                        confidence = result.average_confidence
                        method = (
                            PageExtractionMethod.OCR
                            if _visible_char_count(text) > 0
                            else PageExtractionMethod.EMPTY
                        )
                        if method is PageExtractionMethod.EMPTY:
                            warnings.append("OCR_RETURNED_NO_TEXT")
                    except Exception as exc:
                        text = ""
                        method = PageExtractionMethod.OCR_FAILED
                        warnings.append(f"OCR_FAILED:{type(exc).__name__}")
            heading = _detect_heading(text)
            if heading:
                current_section = [heading]
            text_bytes = text.encode("utf-8")
            text_ref = self.object_store.put_bytes(text_bytes)
            page_id = content_hash(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "page_number": page_number,
                    "parser_version": parser_version,
                    "text_sha256": text_ref.sha256,
                }
            )
            parsed_page = DocumentPage(
                page_id=page_id,
                document_id=document.document_id,
                snapshot_id=snapshot.snapshot_id,
                page_number=page_number,
                width_points=float(page.rect.width),
                height_points=float(page.rect.height),
                native_text_char_count=native_count,
                text_char_count=_visible_char_count(text),
                text_sha256=sha256_bytes(text_bytes),
                text_object_sha256=text_ref.sha256,
                extraction_method=method,
                ocr_applied=ocr_applied,
                ocr_engine=ocr_engine_name,
                ocr_engine_version=ocr_engine_version,
                ocr_average_confidence=confidence,
                page_image_sha256=image_hash,
                parser_name=self.parser_name,
                parser_version=parser_version,
                section_path=current_section,
                warnings=warnings,
                created_at=snapshot.fetched_at,
            )
            self.repository.register_page(parsed_page)
            parsed.append(parsed_page)
        return parsed

    def _build_report(
        self,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        parser_version: str,
        source_page_count: int,
        pages: list[int],
        parsed_pages: list[DocumentPage],
    ) -> DocumentParseReport:
        failed = sum(
            page.extraction_method is PageExtractionMethod.OCR_FAILED for page in parsed_pages
        )
        status = (
            ParseStatus.FAILED
            if parsed_pages and failed == len(parsed_pages)
            else ParseStatus.SUCCEEDED
        )
        if 0 < failed < len(parsed_pages):
            status = ParseStatus.PARTIAL
        parse_run_id = content_hash(
            {
                "snapshot_id": snapshot.snapshot_id,
                "parser_version": parser_version,
                "pages": pages,
            }
        )
        return DocumentParseReport(
            parse_run_id=parse_run_id,
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            parser_name=self.parser_name,
            parser_version=parser_version,
            source_page_count=source_page_count,
            requested_pages=pages,
            processed_page_count=len(parsed_pages),
            native_page_count=sum(
                page.extraction_method is PageExtractionMethod.NATIVE_TEXT for page in parsed_pages
            ),
            ocr_page_count=sum(
                page.extraction_method is PageExtractionMethod.OCR for page in parsed_pages
            ),
            empty_page_count=sum(
                page.extraction_method is PageExtractionMethod.EMPTY for page in parsed_pages
            ),
            failed_page_count=failed,
            total_text_char_count=sum(page.text_char_count for page in parsed_pages),
            page_ids=[page.page_id for page in parsed_pages],
            parse_status=status,
            created_at=snapshot.fetched_at,
        )

    def _parser_version(self, ocr_enabled: bool) -> str:
        ocr = f"rapidocr-{version('rapidocr-onnxruntime')}" if ocr_enabled else "ocr-disabled"
        return (
            f"pymupdf-{version('pymupdf')}+{ocr}+dpi-{self.ocr_dpi}"
            f"+threshold-{self.text_threshold}+rules-v1"
        )

    def _get_ocr_engine(self) -> OcrEngine:
        if self._ocr_engine is None:
            self._ocr_engine = RapidOcrEngine()
        return self._ocr_engine

    @staticmethod
    def _normalize_pages(page_numbers: list[int] | None, page_count: int) -> list[int]:
        pages = (
            list(range(1, page_count + 1))
            if page_numbers is None
            else sorted(set(page_numbers))
        )
        if any(page < 1 or page > page_count for page in pages):
            raise ValueError(f"page_numbers must be within 1..{page_count}")
        if not pages:
            raise ValueError("at least one page must be requested")
        return pages


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _visible_char_count(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _detect_heading(text: str) -> str | None:
    for line in text.splitlines()[:20]:
        candidate = " ".join(line.split())
        if 2 <= len(candidate) <= 40 and any(
            pattern.fullmatch(candidate) for pattern in _HEADING_PATTERNS
        ):
            return candidate
    return None
