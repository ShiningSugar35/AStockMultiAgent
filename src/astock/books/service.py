"""Safe, content-addressed local ingestion for private books and PDFs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from astock.books.repository import BookRepository
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.pdf_parser import PdfParseService
from astock.documents.repository import DocumentRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AvailabilityBasis,
    BookPageReference,
    BookParseReport,
    BookParseScope,
    BookProcessingStatus,
    BookSourceManifest,
    DocumentType,
    PointInTimeStatus,
    PrivatePdfIngestResult,
    SourceDocument,
    SourceSnapshot,
)


class PrivatePdfIngestService:
    pipeline_version = "private-pdf-ingest-v1"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        documents: DocumentRepository | None = None,
        pages: DocumentPageRepository | None = None,
        books: BookRepository | None = None,
        parser: PdfParseService | None = None,
        pit_service: PointInTimeService | None = None,
        maximum_pdf_bytes: int = 500 * 1024 * 1024,
        maximum_sample_pages: int = 12,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.documents = documents or DocumentRepository(state)
        self.pages = pages or DocumentPageRepository(state)
        self.books = books or BookRepository(state)
        self.parser = parser or PdfParseService(object_store, state, self.pages)
        self.pit_service = pit_service or PointInTimeService(
            PointInTimeRepository(state), state, object_store
        )
        self.maximum_pdf_bytes = maximum_pdf_bytes
        self.maximum_sample_pages = maximum_sample_pages

    def ingest(
        self,
        path: Path,
        *,
        source_id: str,
        display_name: str,
        author_source_id: str,
        file_version: str,
        document_type: DocumentType = DocumentType.PRIVATE_BOOK,
        sample_pages: list[int] | None = None,
        ocr_enabled: bool = True,
    ) -> PrivatePdfIngestResult:
        if not source_id.strip() or not display_name.strip() or not file_version.strip():
            raise ValueError("source_id, display_name, and file_version are required")
        if document_type not in {DocumentType.PRIVATE_BOOK, DocumentType.PRIVATE_PDF}:
            raise ValueError("private ingestion accepts only PRIVATE_BOOK or PRIVATE_PDF")
        data = self._read_and_validate(path)
        page_count = self._page_count(data)
        raw_ref = self.object_store.put_bytes(data)
        source_token = sha256_bytes(source_id.encode("utf-8"))[:16]
        snapshot_id = f"private-pdf:{source_token}:{raw_ref.sha256}"
        document_id = "private:" + sha256_bytes(
            canonical_json_bytes(
                {
                    "source_id": source_id,
                    "file_version": file_version,
                    "file_sha256": raw_ref.sha256,
                    "document_type": document_type,
                }
            )
        )
        now = datetime.now(UTC)
        candidate_snapshot = SourceSnapshot(
            snapshot_id=snapshot_id,
            source_id=source_id,
            object_sha256=raw_ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=None,
            mime="application/pdf",
            byte_size=raw_ref.byte_size,
            rights_status="LOCAL_PRIVATE_RESEARCH",
        )
        self.state.register_snapshot(candidate_snapshot)
        snapshot = self.documents.snapshot(snapshot_id)
        if snapshot is None:  # pragma: no cover - register_snapshot guarantees this
            raise ValueError("Private source snapshot registration failed")

        document = self.documents.get_model(document_id)
        if document is None:
            document = SourceDocument(
                document_id=document_id,
                title=display_name,
                publisher="LOCAL_PRIVATE",
                document_type=document_type,
                company_ids=[],
                published_at=snapshot.fetched_at,
                effective_at=None,
                disclosure_id=f"{source_id}:{file_version}:{raw_ref.sha256}",
                source_url=f"private://local-research/{source_token}",
                rights_status="LOCAL_PRIVATE_RESEARCH",
            )
        self.documents.register(document, snapshot)
        pit = self.pit_service.create(
            source_id=document_id,
            source_document_id=document_id,
            source_snapshot_id=snapshot_id,
            published_at=document.published_at,
            effective_at=document.effective_at,
            ingested_at=snapshot.fetched_at,
            available_to_system_at=snapshot.available_to_system_at,
            point_in_time_status=PointInTimeStatus.NOT_PIT_SAFE,
            availability_basis=AvailabilityBasis.FETCH_OBSERVED,
        )

        manifest_fields = {
            "source_id": source_id,
            "display_name": display_name,
            "author_source_id": author_source_id,
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "pit_id": pit.pit_id,
            "document_type": document_type,
            "file_sha256": raw_ref.sha256,
            "file_name_sha256": sha256_bytes(path.name.encode("utf-8")),
            "file_version": file_version,
            "byte_size": raw_ref.byte_size,
            "source_page_count": page_count,
            "parser_pipeline_version": self.pipeline_version,
        }
        manifest_identity = {
            key: value for key, value in manifest_fields.items() if key != "file_name_sha256"
        }
        manifest_id = "book-manifest:" + sha256_bytes(canonical_json_bytes(manifest_identity))
        manifest = BookSourceManifest(
            manifest_id=manifest_id,
            raw_object_sha256=raw_ref.sha256,
            rights_status="LOCAL_PRIVATE_RESEARCH",
            **manifest_fields,
        )
        stored_manifest = self.books.register_manifest(manifest)
        self._register_manifest_artifact(stored_manifest)
        parse_report = self._parse_samples(
            stored_manifest,
            document,
            snapshot,
            page_count,
            sample_pages,
            ocr_enabled=ocr_enabled,
        )
        return PrivatePdfIngestResult(
            manifest=stored_manifest,
            pit_metadata=pit,
            parse_report=parse_report,
            created_at=stored_manifest.created_at,
        )

    def _read_and_validate(self, path: Path) -> bytes:
        try:
            if not path.is_file():
                raise ValueError("Private PDF source is not a readable file")
            byte_size = path.stat().st_size
            if byte_size > self.maximum_pdf_bytes:
                raise ValueError("Private PDF exceeds the configured size limit")
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("Private PDF could not be read") from exc
        if len(data) > self.maximum_pdf_bytes:
            raise ValueError("Private PDF exceeds the configured size limit")
        if not data.lstrip().startswith(b"%PDF-"):
            raise ValueError("Private source is not a PDF")
        return data

    @staticmethod
    def _page_count(data: bytes) -> int:
        try:
            pdf = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise ValueError("Private PDF could not be opened") from exc
        with pdf:
            page_count = pdf.page_count
        if page_count < 1:
            raise ValueError("Private PDF has no pages")
        return page_count

    def _parse_samples(
        self,
        manifest: BookSourceManifest,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        page_count: int,
        sample_pages: list[int] | None,
        *,
        ocr_enabled: bool,
    ) -> BookParseReport | None:
        if not sample_pages:
            return None
        if len(sample_pages) != len(set(sample_pages)):
            raise ValueError("Private PDF sample pages must be unique")
        pages = sorted(sample_pages)
        if len(pages) > self.maximum_sample_pages:
            raise ValueError(
                f"Private PDF sample is limited to {self.maximum_sample_pages} pages"
            )
        report = self.parser.parse(
            document,
            snapshot,
            page_numbers=pages,
            ocr_enabled=ocr_enabled,
        )
        if report.report_object_sha256 is None:  # pragma: no cover - parser guarantees storage
            raise ValueError("Underlying PDF parse report was not persisted")
        page_models = []
        for page_id in report.page_ids:
            page = self.pages.get_page_by_id(page_id)
            if page is None:  # pragma: no cover - parser registers every returned page
                raise ValueError(f"Parsed private page is missing: {page_id}")
            page_models.append(page)
        parse_scope = (
            BookParseScope.FULL_SOURCE
            if report.processed_page_count == page_count
            else BookParseScope.SAMPLE_PAGES
        )
        processing_status = (
            BookProcessingStatus.COMPLETE
            if parse_scope is BookParseScope.FULL_SOURCE
            else BookProcessingStatus.SAMPLE_ONLY
        )
        report_identity = {
            "manifest_id": manifest.manifest_id,
            "underlying_parse_report_sha256": report.report_object_sha256,
            "parser_version": report.parser_version,
            "requested_pages": report.requested_pages,
            "parse_scope": parse_scope,
        }
        report_id = "book-parse:" + sha256_bytes(canonical_json_bytes(report_identity))
        existing = self.books.get_parse_report(report_id)
        if existing is not None:
            self._register_parse_artifact(existing)
            return existing
        book_report = BookParseReport(
            book_parse_report_id=report_id,
            manifest_id=manifest.manifest_id,
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            file_sha256=manifest.file_sha256,
            parser_name=report.parser_name,
            parser_version=report.parser_version,
            parse_scope=parse_scope,
            processing_status=processing_status,
            source_page_count=page_count,
            requested_pages=report.requested_pages,
            processed_page_count=report.processed_page_count,
            native_page_count=report.native_page_count,
            ocr_page_count=report.ocr_page_count,
            empty_page_count=report.empty_page_count,
            failed_page_count=report.failed_page_count,
            parsed_text_char_count=report.total_text_char_count,
            pages=[
                BookPageReference(
                    page_id=page.page_id,
                    page_number=page.page_number,
                    section_path=page.section_path,
                    parser_version=page.parser_version,
                    extraction_method=page.extraction_method,
                    text_char_count=page.text_char_count,
                    text_sha256=page.text_sha256,
                    text_object_sha256=page.text_object_sha256,
                )
                for page in page_models
            ],
            underlying_parse_report_sha256=report.report_object_sha256,
        )
        object_ref = self.object_store.put_json(book_report.model_dump(mode="json"))
        book_report = book_report.model_copy(update={"report_object_sha256": object_ref.sha256})
        stored = self.books.register_parse_report(book_report)
        self._register_parse_artifact(stored)
        return stored

    def _register_manifest_artifact(self, manifest: BookSourceManifest) -> None:
        artifact = self.object_store.put_json(manifest.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"BookSourceManifest:{manifest.manifest_id}",
            artifact_type="BookSourceManifest",
            schema_version=manifest.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[manifest.raw_object_sha256, manifest.pit_id],
        )

    def _register_parse_artifact(self, report: BookParseReport) -> None:
        if report.report_object_sha256 is None:  # pragma: no cover - schema/repository invariant
            raise ValueError("Book parse artifact hash is missing")
        self.state.register_artifact(
            artifact_id=f"BookParseReport:{report.book_parse_report_id}",
            artifact_type="BookParseReport",
            schema_version=report.schema_version,
            object_hash=report.report_object_sha256,
            input_hashes=[report.file_sha256, report.underlying_parse_report_sha256],
        )
