"""Bounded official DOCX/DOC/TXT parsing into stable evidence blocks."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from typing import Any

from docx import Document as OpenXmlDocument
from docx.document import Document as OpenXmlDocumentType
from docx.table import Table
from docx.text.paragraph import Paragraph

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.block_repository import DocumentBlockRepository
from astock.documents.media import DOC_MIME, DOCX_MIME, TEXT_MIME, inspect_official_document
from astock.documents.page_repository import DocumentPageRepository
from astock.schemas import (
    DocumentBlock,
    DocumentBlockKind,
    DocumentPage,
    DocumentPartKind,
    PageExtractionMethod,
    SourceDocument,
    SourceSnapshot,
)

try:
    _PYTHON_DOCX_VERSION = version("python-docx")
except PackageNotFoundError:  # pragma: no cover - declared runtime dependency
    _PYTHON_DOCX_VERSION = "unknown"

try:
    _LEGACY_DOC_VERSION = version("legacy-doc")
except PackageNotFoundError:  # pragma: no cover - optional runtime dependency
    _LEGACY_DOC_VERSION = "unknown"


@dataclass(frozen=True, slots=True)
class OfficialReflowableParseReport:
    snapshot_id: str
    parser_name: str
    parser_version: str
    media_type: str
    parent_object_sha256: str
    block_ids: tuple[str, ...]
    page_ids: tuple[str, ...]
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class _BlockSeed:
    text: str
    block_kind: DocumentBlockKind
    paragraph_index: int
    part_kind: DocumentPartKind = DocumentPartKind.MAIN
    part_sequence: int = 0
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    is_heading: bool = False
    hyperlink_count: int = 0
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


class OfficialReflowableParseService:
    """Parse reflowable official documents without inheriting private-source rights."""

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        blocks: DocumentBlockRepository | None = None,
        pages: DocumentPageRepository | None = None,
        *,
        maximum_document_bytes: int = 500 * 1024 * 1024,
        maximum_docx_uncompressed_bytes: int = 1024 * 1024 * 1024,
        maximum_docx_entries: int = 20_000,
        maximum_text_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.objects = objects
        self.state = state
        self.blocks = blocks or DocumentBlockRepository(state)
        self.pages = pages or DocumentPageRepository(state)
        self.maximum_document_bytes = maximum_document_bytes
        self.maximum_docx_uncompressed_bytes = maximum_docx_uncompressed_bytes
        self.maximum_docx_entries = maximum_docx_entries
        self.maximum_text_bytes = maximum_text_bytes

    def parse(
        self,
        document: SourceDocument,
        snapshot: SourceSnapshot,
    ) -> OfficialReflowableParseReport:
        if snapshot.mime not in {DOCX_MIME, DOC_MIME, TEXT_MIME}:
            raise ValueError(f"Unsupported reflowable official media type: {snapshot.mime}")
        content = self.objects.get_bytes(snapshot.object_sha256)
        media = inspect_official_document(
            content,
            maximum_document_bytes=self.maximum_document_bytes,
            maximum_docx_entries=self.maximum_docx_entries,
            maximum_docx_uncompressed_bytes=self.maximum_docx_uncompressed_bytes,
        )
        if media.media_type != snapshot.mime:
            raise ValueError("Snapshot media type does not match document content")

        if snapshot.mime == DOCX_MIME:
            parser_name = "python-docx"
            parser_version = f"official-docx-blocks-v1+python-docx-{_PYTHON_DOCX_VERSION}"
        elif snapshot.mime == DOC_MIME:
            parser_name = "legacy-doc"
            parser_version = f"official-doc-blocks-v1+legacy-doc-{_LEGACY_DOC_VERSION}"
        else:
            encoding = media.text_encoding
            if encoding is None:
                raise ValueError("Plain-text media admission did not return an encoding")
            parser_name = "plain-text"
            parser_version = f"official-text-blocks-v1+{encoding}"

        cached = self.blocks.blocks_for(snapshot.snapshot_id, parser_version)
        if cached and self._block_objects_available(cached):
            page = self._ensure_page_projection(
                document, snapshot, cached, parser_name, parser_version
            )
            return self._report(
                snapshot,
                parser_name,
                parser_version,
                cached,
                page_ids=(page.page_id,),
                cache_hit=True,
            )

        content_cached = self.blocks.blocks_for_object_hash(
            snapshot.object_sha256,
            parser_version,
        )
        if content_cached and self._block_objects_available(content_cached):
            blocks = self._clone_cached_blocks(document, snapshot, content_cached)
            self._register_parse_artifact(snapshot, parser_name, parser_version, blocks)
            page = self._ensure_page_projection(
                document, snapshot, blocks, parser_name, parser_version
            )
            return self._report(
                snapshot,
                parser_name,
                parser_version,
                blocks,
                page_ids=(page.page_id,),
                cache_hit=True,
            )

        if snapshot.mime == DOCX_MIME:
            seeds = self._docx_seeds(content)
        elif snapshot.mime == DOC_MIME:
            seeds = self._legacy_doc_seeds(content)
        else:
            text, encoding = _decode_text(content)
            seeds = _line_seeds(text, encoding=encoding)

        blocks = self._persist_blocks(
            document=document,
            snapshot=snapshot,
            parser_name=parser_name,
            parser_version=parser_version,
            seeds=seeds,
        )
        self._register_parse_artifact(snapshot, parser_name, parser_version, blocks)
        page = self._ensure_page_projection(
            document, snapshot, blocks, parser_name, parser_version
        )
        return self._report(
            snapshot,
            parser_name,
            parser_version,
            blocks,
            page_ids=(page.page_id,),
            cache_hit=False,
        )

    def _block_objects_available(self, blocks: list[DocumentBlock]) -> bool:
        try:
            return all(
                self.objects.verify(block.text_object_sha256)
                and self.objects.verify(block.metadata_object_sha256)
                for block in blocks
            )
        except ValueError:
            return False

    def _ensure_page_projection(
        self,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        blocks: list[DocumentBlock],
        parser_name: str,
        parser_version: str,
    ) -> DocumentPage:
        """Expose one compatibility page while DocumentBlock remains the source locator."""

        cached = self.pages.get_page(snapshot.snapshot_id, 1, parser_version)
        if cached is not None:
            try:
                projection_available = self.objects.verify(cached.text_object_sha256)
            except ValueError:
                projection_available = False
            if projection_available:
                return cached
        text = "\n".join(
            self.objects.get_bytes(block.text_object_sha256).decode("utf-8")
            for block in blocks
        )
        text_ref = self.objects.put_bytes(text.encode("utf-8"))
        page = DocumentPage(
            page_id=content_hash(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "page_number": 1,
                    "parser_version": parser_version,
                    "text_sha256": text_ref.sha256,
                }
            ),
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            page_number=1,
            width_points=1.0,
            height_points=1.0,
            native_text_char_count=len(text),
            text_char_count=len(text),
            text_sha256=text_ref.sha256,
            text_object_sha256=text_ref.sha256,
            extraction_method=PageExtractionMethod.NATIVE_TEXT,
            ocr_applied=False,
            parser_name=parser_name,
            parser_version=parser_version,
            section_path=[],
            warnings=["REFLOWABLE_COMPATIBILITY_PROJECTION;LOCATORS=DOCUMENT_BLOCK"],
            created_at=snapshot.fetched_at,
        )
        self.pages.register_page(page)
        return page

    def _docx_seeds(self, content: bytes) -> list[_BlockSeed]:
        package = OpenXmlDocument(BytesIO(content))
        seeds: list[_BlockSeed] = []
        paragraph_index = 0
        table_index = 0
        section_path: list[str] = []

        for kind, item in _iter_body_items(package):
            if kind == "paragraph":
                paragraph_index += 1
                paragraph = item
                assert isinstance(paragraph, Paragraph)
                text = paragraph.text.replace("\r\n", "\n").replace("\r", "\n")
                heading_level = _heading_level(paragraph)
                hyperlink_count = _paragraph_hyperlink_count(paragraph)
                if heading_level is not None and text.strip():
                    section_path[:] = section_path[: heading_level - 1]
                    section_path.append(text.strip())
                seeds.append(
                    _BlockSeed(
                        text=text,
                        block_kind=DocumentBlockKind.PARAGRAPH,
                        paragraph_index=paragraph_index,
                        is_heading=heading_level is not None,
                        hyperlink_count=hyperlink_count,
                        warnings=(
                            ("EXTERNAL_LINK_PRESENT_NOT_FOLLOWED",)
                            if hyperlink_count
                            else ()
                        ),
                        metadata={
                            "schema_version": "official-block-location-v1",
                            "part_name": "word/document.xml",
                            "section_path": tuple(section_path),
                            "style_name": paragraph.style.name if paragraph.style else None,
                            "heading_level": heading_level,
                        },
                    )
                )
                continue

            table_index += 1
            table = item
            assert isinstance(table, Table)
            for row_index, row in enumerate(table.rows, start=1):
                row_start_paragraph = paragraph_index + 1
                cells: list[str] = []
                cell_spans: list[dict[str, int]] = []
                row_hyperlinks = 0
                offset = 0
                for cell_index, cell in enumerate(row.cells, start=1):
                    cell_parts: list[str] = []
                    for paragraph in cell.paragraphs:
                        paragraph_index += 1
                        text = paragraph.text.replace("\r\n", "\n").replace("\r", "\n").strip()
                        if text:
                            cell_parts.append(text)
                        row_hyperlinks += _paragraph_hyperlink_count(paragraph)
                    cell_text = " ".join(cell_parts)
                    if cells:
                        offset += 1
                    start = offset
                    cells.append(cell_text)
                    offset += len(cell_text)
                    cell_spans.append(
                        {"cell_index": cell_index, "char_start": start, "char_end": offset}
                    )
                row_text = "\t".join(cells)
                if row_text.strip():
                    seeds.append(
                        _BlockSeed(
                            text=row_text,
                            block_kind=DocumentBlockKind.TABLE_ROW,
                            paragraph_index=row_start_paragraph,
                            table_index=table_index,
                            row_index=row_index,
                            hyperlink_count=row_hyperlinks,
                            warnings=(
                                ("EXTERNAL_LINK_PRESENT_NOT_FOLLOWED",)
                                if row_hyperlinks
                                else ()
                            ),
                            metadata={
                                "schema_version": "official-block-location-v1",
                                "part_name": "word/document.xml",
                                "section_path": tuple(section_path),
                                "cell_spans": cell_spans,
                            },
                        )
                    )
        visible = [seed for seed in seeds if seed.text.strip()]
        if not visible:
            raise ValueError("Official DOCX has no extractable visible text")
        return seeds

    def _legacy_doc_seeds(self, content: bytes) -> list[_BlockSeed]:
        # Keep the legacy parser optional at import time: an unavailable DOC parser
        # must not disable PDF/DOCX/TXT handling in the same process.
        try:
            from legacy_doc import ExtractionOptions, LegacyDocError, extract_text
        except ImportError as exc:  # pragma: no cover - deployment-specific
            raise ValueError("Legacy DOC parser is unavailable in the active environment") from exc
        try:
            result = extract_text(
                content,
                options=ExtractionOptions(
                    max_file_bytes=self.maximum_document_bytes,
                    max_text_bytes=self.maximum_text_bytes,
                ),
            )
        except LegacyDocError as exc:
            raise ValueError("Legacy DOC failed bounded text extraction") from exc
        text = str(result.text)
        if not text.strip():
            raise ValueError("Legacy DOC contains no extractable visible text")
        warnings = tuple(str(item) for item in result.warnings)
        return _line_seeds(
            text,
            encoding=None,
            extra_metadata={"legacy_doc_metadata": dict(result.metadata)},
            warnings=warnings,
        )

    def _clone_cached_blocks(
        self,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        source_blocks: list[DocumentBlock],
    ) -> list[DocumentBlock]:
        cloned: list[DocumentBlock] = []
        for source in source_blocks:
            identity = {
                "snapshot_id": snapshot.snapshot_id,
                "block_index": source.block_index,
                "parser_version": source.parser_version,
                "text_sha256": source.text_sha256,
                "metadata_object_sha256": source.metadata_object_sha256,
            }
            cloned.append(
                source.model_copy(
                    update={
                        "block_id": "block:"
                        + sha256_bytes(canonical_json_bytes(identity)),
                        "document_id": document.document_id,
                        "snapshot_id": snapshot.snapshot_id,
                    }
                )
            )
        self.blocks.register_blocks(cloned)
        return cloned

    def _persist_blocks(
        self,
        *,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        parser_name: str,
        parser_version: str,
        seeds: list[_BlockSeed],
    ) -> list[DocumentBlock]:
        blocks: list[DocumentBlock] = []
        for block_index, seed in enumerate(seeds, start=1):
            text_ref = self.objects.put_bytes(seed.text.encode("utf-8"))
            metadata = {
                "schema_version": "official-block-location-v1",
                "parent_object_sha256": snapshot.object_sha256,
                "media_type": snapshot.mime,
                **(seed.metadata or {}),
            }
            metadata_ref = self.objects.put_json(metadata)
            identity = {
                "snapshot_id": snapshot.snapshot_id,
                "block_index": block_index,
                "parser_version": parser_version,
                "text_sha256": text_ref.sha256,
                "metadata_object_sha256": metadata_ref.sha256,
            }
            blocks.append(
                DocumentBlock(
                    block_id="block:" + sha256_bytes(canonical_json_bytes(identity)),
                    document_id=document.document_id,
                    snapshot_id=snapshot.snapshot_id,
                    block_index=block_index,
                    part_kind=seed.part_kind,
                    part_sequence=seed.part_sequence,
                    block_kind=seed.block_kind,
                    paragraph_index=seed.paragraph_index,
                    table_index=seed.table_index,
                    row_index=seed.row_index,
                    cell_index=seed.cell_index,
                    text_char_count=len(seed.text),
                    text_sha256=text_ref.sha256,
                    text_object_sha256=text_ref.sha256,
                    metadata_object_sha256=metadata_ref.sha256,
                    parser_name=parser_name,
                    parser_version=parser_version,
                    hyperlink_count=seed.hyperlink_count,
                    is_heading=seed.is_heading,
                    warnings=list(seed.warnings),
                )
            )
        self.blocks.register_blocks(blocks)
        return blocks

    def _register_parse_artifact(
        self,
        snapshot: SourceSnapshot,
        parser_name: str,
        parser_version: str,
        blocks: list[DocumentBlock],
    ) -> None:
        scope_hash = content_hash({"scope": "ALL"})
        payload = {
            "schema_version": "official-reflow-parse-v2",
            "snapshot_id": snapshot.snapshot_id,
            "parent_object_sha256": snapshot.object_sha256,
            "scope_hash": scope_hash,
            "media_type": snapshot.mime,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "block_ids": [block.block_id for block in blocks],
        }
        object_ref = self.objects.put_json(payload)
        artifact_id = (
            "OfficialReflowableParse:"
            + sha256_bytes(
                canonical_json_bytes(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "parser_version": parser_version,
                    }
                )
            )
        )
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="OfficialReflowableParse",
            schema_version="2.0",
            object_hash=object_ref.sha256,
            input_hashes=[snapshot.object_sha256, parser_version, scope_hash],
        )

    @staticmethod
    def _report(
        snapshot: SourceSnapshot,
        parser_name: str,
        parser_version: str,
        blocks: list[DocumentBlock],
        *,
        page_ids: tuple[str, ...],
        cache_hit: bool,
    ) -> OfficialReflowableParseReport:
        return OfficialReflowableParseReport(
            snapshot_id=snapshot.snapshot_id,
            parser_name=parser_name,
            parser_version=parser_version,
            media_type=snapshot.mime,
            parent_object_sha256=snapshot.object_sha256,
            block_ids=tuple(block.block_id for block in blocks),
            page_ids=page_ids,
            cache_hit=cache_hit,
        )


def _iter_body_items(
    document: OpenXmlDocumentType,
) -> list[tuple[str, Paragraph | Table]]:
    items: list[tuple[str, Paragraph | Table]] = []
    for child in document.element.body.iterchildren():
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == "p":
            items.append(("paragraph", Paragraph(child, document)))
        elif local_name == "tbl":
            items.append(("table", Table(child, document)))
    return items


def _paragraph_hyperlink_count(paragraph: Paragraph) -> int:
    """Count hyperlink elements without resolving or opening their targets."""
    return len(paragraph._p.xpath(".//w:hyperlink"))


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = paragraph.style.name if paragraph.style else None
    if not style_name:
        return None
    normalized = style_name.casefold().replace(" ", "")
    for prefix in ("heading", "标题"):
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            if suffix.isdigit() and 1 <= int(suffix) <= 9:
                return int(suffix)
    return None


def _decode_text(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text or not text.strip():
            continue
        visible_or_spacing = sum(
            1 for char in text if char.isprintable() or char in {"\r", "\n", "\t"}
        )
        if visible_or_spacing / len(text) >= 0.98:
            return text.replace("\r\n", "\n").replace("\r", "\n"), encoding
    raise ValueError("Official plain-text document has unsupported encoding or binary content")


def _line_seeds(
    text: str,
    *,
    encoding: str | None,
    extra_metadata: dict[str, Any] | None = None,
    warnings: tuple[str, ...] = (),
) -> list[_BlockSeed]:
    seeds: list[_BlockSeed] = []
    offset = 0
    paragraph_index = 0
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [text]
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        line_end = offset + len(line)
        if line.strip():
            paragraph_index += 1
            metadata = {
                "schema_version": "official-block-location-v1",
                "line_start": line_number,
                "line_end": line_number,
                "document_char_start": offset,
                "document_char_end": line_end,
                "text_encoding": encoding,
                **(extra_metadata or {}),
            }
            seeds.append(
                _BlockSeed(
                    text=line,
                    block_kind=DocumentBlockKind.PARAGRAPH,
                    paragraph_index=paragraph_index,
                    warnings=warnings,
                    metadata=metadata,
                )
            )
        offset += len(raw_line)
    if not seeds:
        raise ValueError("Official text document has no extractable visible text")
    return seeds


__all__ = ["OfficialReflowableParseReport", "OfficialReflowableParseService"]
