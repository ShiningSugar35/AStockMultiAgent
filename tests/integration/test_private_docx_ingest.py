from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from astock.books import PrivateDocxIngestService
from astock.core.object_store import ObjectStore
from astock.documents import DocumentBlockRepository, DocumentPageRepository, DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.schemas import (
    BookProcessingStatus,
    ClaimStatus,
    ClaimType,
    CoverageStatus,
    EvidenceAttachment,
    EvidenceGrade,
    EvidenceLocatorType,
    EvidenceRelation,
    FactStatus,
)

_PRIVATE_HEADING = "Private heading must remain outside SQLite."
_PRIVATE_PHRASE = "Private methodology evidence is block-addressable."
_PRIVATE_CELL = "Private table cell content."
_PRIVATE_FOOTNOTE = "Private footnote content."
_PRIVATE_TARGET = "https://example.invalid/private-source"


def _private_docx(path: Path) -> bytes:
    main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    styles_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
    footnotes_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
    content_types = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{main_type}"/>
  <Override PartName="/word/styles.xml" ContentType="{styles_type}"/>
  <Override PartName="/word/footnotes.xml" ContentType="{footnotes_type}"/>
</Types>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{_PRIVATE_HEADING}</w:t></w:r></w:p>
    <w:p><w:hyperlink r:id="rId5"><w:r><w:t>{_PRIVATE_PHRASE}</w:t></w:r></w:hyperlink></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>{_PRIVATE_CELL}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p/>
    <w:sectPr/>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="Heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  </w:style>
</w:styles>"""
    hyperlink_type = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
    )
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId5" Type="{hyperlink_type}" Target="{_PRIVATE_TARGET}" TargetMode="External"/>
</Relationships>"""
    footnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t>control</w:t></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:r><w:t>{_PRIVATE_FOOTNOTE}</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/footnotes.xml", footnotes)
    return path.read_bytes()


def test_private_docx_is_fully_parsed_idempotent_and_block_evidence_is_claim_usable(
    tmp_path: Path, state
) -> None:
    path = tmp_path / "private-source.docx"
    raw = _private_docx(path)
    objects = ObjectStore(tmp_path / "objects")
    service = PrivateDocxIngestService(objects, state)
    first = service.ingest(
        path,
        source_id="docx:test:private-source",
        display_name="Private DOCX fixture",
        author_source_id="author:test",
        file_version="v1",
    )
    repeated = service.ingest(
        path,
        source_id="docx:test:private-source",
        display_name="Private DOCX fixture",
        author_source_id="author:test",
        file_version="v1",
    )

    assert first.manifest.manifest_id == repeated.manifest.manifest_id
    assert first.parse_report == repeated.parse_report
    assert objects.get_bytes(first.manifest.raw_object_sha256) == raw
    assert first.manifest.source_page_count == 0
    assert first.manifest.git_policy == "EXCLUDED"
    report = first.parse_report
    assert report.processing_status is BookProcessingStatus.COMPLETE
    assert report.coverage_status is CoverageStatus.COMPLETE
    assert report.source_part_count == 2
    assert report.source_paragraph_count == 5
    assert report.processed_block_count == 5
    assert report.nonempty_block_count == 4
    assert report.empty_block_count == 1
    assert report.table_count == 1
    assert report.table_cell_count == 1
    assert report.hyperlink_count == 1
    assert report.embedded_visual_count == 0
    assert report.unsupported_object_count == 0
    assert report.gaps == []

    block_repository = DocumentBlockRepository(state)
    blocks = block_repository.blocks_for(report.snapshot_id, report.parser_version)
    assert [block.block_index for block in blocks] == [1, 2, 3, 4, 5]
    phrase_block = next(
        block
        for block in blocks
        if objects.get_bytes(block.text_object_sha256).decode("utf-8") == _PRIVATE_PHRASE
    )
    heading_block = blocks[0]
    heading_metadata = objects.get_bytes(heading_block.metadata_object_sha256).decode("utf-8")
    phrase_metadata = objects.get_bytes(phrase_block.metadata_object_sha256).decode("utf-8")
    assert _PRIVATE_HEADING in heading_metadata
    assert _PRIVATE_HEADING in phrase_metadata
    assert _PRIVATE_TARGET in phrase_metadata

    evidence_service = ClaimEvidenceService(
        objects,
        state,
        DocumentPageRepository(state),
        DocumentRepository(state),
        EvidenceRepository(state),
        block_repository,
    )
    evidence = evidence_service.create_block_evidence(
        block_id=phrase_block.block_id,
        char_start=0,
        char_end=len(_PRIVATE_PHRASE),
        evidence_grade=EvidenceGrade.PRIVATE_PRIMARY,
        fact_status=FactStatus.DIRECT,
        entity_ids=["docx:test:private-source"],
    )
    assert evidence.locator.locator_type is EvidenceLocatorType.BLOCK_TEXT
    assert evidence.locator.block_index == phrase_block.block_index
    assert evidence.locator.page_number is None
    assert evidence.page_id is None
    assert evidence.block_id == phrase_block.block_id
    claim = evidence_service.create_claim(
        subject_id="method:test",
        predicate="has_block_evidence",
        object_json={"value": True},
        as_of=first.pit_metadata.available_to_system_at + timedelta(seconds=1),
        claim_type=ClaimType.FACT,
        confidence=1.0,
        status=ClaimStatus.VALIDATED,
        attachments=[
            EvidenceAttachment(
                evidence_id=evidence.evidence_id,
                relation=EvidenceRelation.SUPPORT,
            )
        ],
    )
    assert claim.links[0].evidence_id == evidence.evidence_id

    with state.connect() as connection:
        sqlite_metadata = "\n".join(
            str(row[0])
            for query in (
                "SELECT manifest_json FROM book_source_manifest",
                "SELECT report_json FROM private_docx_parse_report",
                "SELECT block_json FROM document_block",
                "SELECT evidence_json FROM evidence_record",
            )
            for row in connection.execute(query).fetchall()
        )
        assert connection.execute("SELECT COUNT(*) FROM document_block").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM evidence_record").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM claim_evidence_link").fetchone()[0] == 1
    for secret in (
        _PRIVATE_HEADING,
        _PRIVATE_PHRASE,
        _PRIVATE_CELL,
        _PRIVATE_FOOTNOTE,
        _PRIVATE_TARGET,
        str(path),
        path.name,
    ):
        assert secret not in sqlite_metadata


def test_invalid_private_docx_is_rejected_before_object_or_metadata_registration(
    tmp_path: Path, state
) -> None:
    path = tmp_path / "invalid.docx"
    path.write_bytes(b"PK-not-a-real-docx")
    objects = ObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="DOCX"):
        PrivateDocxIngestService(objects, state).ingest(
            path,
            source_id="docx:test:invalid",
            display_name="Invalid fixture",
            author_source_id="author:test",
            file_version="v1",
        )
    assert list(objects.root.rglob("*")) == []
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] == 0
