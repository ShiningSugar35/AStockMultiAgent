from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from astock.books import PrivatePdfIngestService
from astock.core.object_store import ObjectStore
from astock.documents import DocumentPageRepository, DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.pit import PointInTimeService
from astock.schemas import DocumentType, EvidenceGrade, FactStatus, PointInTimeStatus


def _private_pdf(path: Path) -> tuple[bytes, str]:
    phrase = "Private methodology evidence remains local and immutable."
    pdf = pymupdf.open()
    first = pdf.new_page()
    first.insert_text((72, 72), phrase)
    second = pdf.new_page()
    second.insert_text((72, 72), "Second page is intentionally outside the Phase 2 sample.")
    data = pdf.tobytes()
    pdf.close()
    path.write_bytes(data)
    return data, phrase


def test_private_book_is_content_addressed_idempotent_and_uses_shared_evidence(
    tmp_path: Path, state
) -> None:
    path = tmp_path / "private-fixture.pdf"
    raw, phrase = _private_pdf(path)
    objects = ObjectStore(tmp_path / "objects")
    service = PrivatePdfIngestService(objects, state)
    first = service.ingest(
        path,
        source_id="book:test:private-method",
        display_name="Private method fixture",
        author_source_id="author:test",
        file_version="v1",
        document_type=DocumentType.PRIVATE_BOOK,
        sample_pages=[1],
        ocr_enabled=False,
    )
    repeated = service.ingest(
        path,
        source_id="book:test:private-method",
        display_name="Private method fixture",
        author_source_id="author:test",
        file_version="v1",
        document_type=DocumentType.PRIVATE_BOOK,
        sample_pages=[1],
        ocr_enabled=False,
    )
    manifest = first.manifest
    assert manifest.manifest_id == repeated.manifest.manifest_id
    assert manifest.raw_object_sha256 == repeated.manifest.raw_object_sha256
    assert objects.get_bytes(manifest.raw_object_sha256) == raw
    assert manifest.file_sha256 == manifest.raw_object_sha256
    assert manifest.source_page_count == 2
    assert manifest.git_policy == "EXCLUDED"
    assert manifest.external_republication_policy == "PROHIBITED"
    assert manifest.raw_retention_policy == "PERMANENT"
    assert manifest.cleaning_reconstructable
    assert first.pit_metadata.point_in_time_status is PointInTimeStatus.NOT_PIT_SAFE
    with pytest.raises(ValueError, match="not allowed"):
        PointInTimeService.assert_usable(
            first.pit_metadata,
            datetime.now(UTC),
        )

    parse_report = first.parse_report
    assert parse_report is not None
    assert parse_report.processing_status.value == "SAMPLE_ONLY"
    assert parse_report.requested_pages == [1]
    assert parse_report.processed_page_count == 1
    assert parse_report.parser_version == parse_report.pages[0].parser_version

    documents = DocumentRepository(state)
    document = documents.get_model(manifest.document_id)
    snapshot = documents.snapshot(manifest.snapshot_id)
    assert document is not None and snapshot is not None
    assert snapshot.source_url is None
    assert str(path) not in document.source_url
    assert path.name not in document.source_url

    page = DocumentPageRepository(state).get_page_by_id(parse_report.pages[0].page_id)
    assert page is not None
    extracted = objects.get_bytes(page.text_object_sha256).decode("utf-8")
    start = extracted.index(phrase)
    evidence = ClaimEvidenceService(
        objects,
        state,
        DocumentPageRepository(state),
        documents,
        EvidenceRepository(state),
    ).create_page_evidence(
        page_id=page.page_id,
        char_start=start,
        char_end=start + len(phrase),
        evidence_grade=EvidenceGrade.PRIVATE_PRIMARY,
        fact_status=FactStatus.DIRECT,
        entity_ids=["book:test:private-method"],
    )
    assert evidence.locator.page_number == 1
    assert evidence.rights_status == "LOCAL_PRIVATE_RESEARCH"
    assert objects.get_bytes(evidence.excerpt_object_sha256).decode("utf-8") == phrase

    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM book_source_manifest").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM book_parse_report").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM point_in_time_metadata").fetchone()[0] == 1
        stored_json = "\n".join(
            [
                connection.execute(
                    "SELECT manifest_json FROM book_source_manifest"
                ).fetchone()[0],
                connection.execute("SELECT report_json FROM book_parse_report").fetchone()[0],
                connection.execute("SELECT page_json FROM document_page").fetchone()[0],
                connection.execute("SELECT evidence_json FROM evidence_record").fetchone()[0],
            ]
        )
    assert phrase not in stored_json
    assert str(path) not in stored_json
    assert path.name not in stored_json


def test_private_pdf_without_sample_does_not_parse_and_sample_limit_is_enforced(
    tmp_path: Path, state
) -> None:
    path = tmp_path / "private-fixture.pdf"
    _private_pdf(path)
    service = PrivatePdfIngestService(
        ObjectStore(tmp_path / "objects"),
        state,
        maximum_sample_pages=1,
    )
    result = service.ingest(
        path,
        source_id="pdf:test:no-auto-parse",
        display_name="No auto parse fixture",
        author_source_id="author:test",
        file_version="v1",
        document_type=DocumentType.PRIVATE_PDF,
    )
    assert result.parse_report is None
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_page").fetchone()[0] == 0
    with pytest.raises(ValueError, match="limited to 1"):
        service.ingest(
            path,
            source_id="pdf:test:no-auto-parse",
            display_name="No auto parse fixture",
            author_source_id="author:test",
            file_version="v1",
            document_type=DocumentType.PRIVATE_PDF,
            sample_pages=[1, 2],
        )


def test_invalid_private_pdf_is_rejected_before_registration(tmp_path: Path, state) -> None:
    path = tmp_path / "not-a-pdf.pdf"
    path.write_bytes(b"not a private PDF")
    objects = ObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="not a PDF"):
        PrivatePdfIngestService(objects, state).ingest(
            path,
            source_id="pdf:test:invalid",
            display_name="Invalid fixture",
            author_source_id="author:test",
            file_version="v1",
            document_type=DocumentType.PRIVATE_PDF,
        )
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] == 0
    assert list(objects.root.rglob("*")) == []


def test_private_pdf_explicit_full_parse_covers_every_page(tmp_path: Path, state) -> None:
    path = tmp_path / "private-full.pdf"
    _private_pdf(path)
    service = PrivatePdfIngestService(ObjectStore(tmp_path / "objects"), state)
    result = service.ingest(
        path,
        source_id="pdf:test:full",
        display_name="Full parse fixture",
        author_source_id="author:test",
        file_version="v1",
        document_type=DocumentType.PRIVATE_PDF,
        full_parse=True,
        ocr_enabled=False,
    )
    assert result.parse_report is not None
    assert result.parse_report.parse_scope.value == "FULL_SOURCE"
    assert result.parse_report.processing_status.value == "COMPLETE"
    assert result.parse_report.requested_pages == [1, 2]
    assert result.parse_report.processed_page_count == 2
    with pytest.raises(ValueError, match="cannot be combined"):
        service.ingest(
            path,
            source_id="pdf:test:full",
            display_name="Full parse fixture",
            author_source_id="author:test",
            file_version="v1",
            document_type=DocumentType.PRIVATE_PDF,
            sample_pages=[1],
            full_parse=True,
            ocr_enabled=False,
        )
