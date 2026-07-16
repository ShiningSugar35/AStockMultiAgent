"""Safe, content-addressed ingestion for private WordprocessingML DOCX sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from astock.books.docx_repository import PrivateDocxRepository
from astock.books.repository import BookRepository
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.block_repository import DocumentBlockRepository
from astock.documents.repository import DocumentRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AvailabilityBasis,
    BookProcessingStatus,
    BookSourceManifest,
    CoverageStatus,
    DocumentBlock,
    DocumentBlockKind,
    DocumentPartKind,
    DocumentType,
    PointInTimeStatus,
    PrivateDocxIngestResult,
    PrivateDocxParseReport,
    SourceDocument,
    SourceSnapshot,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_W = f"{{{_W_NS}}}"
_R = f"{{{_R_NS}}}"
_CT = f"{{{_CT_NS}}}"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_WRAPPER_TAGS = {
    f"{_W}sdt",
    f"{_W}sdtContent",
    f"{_W}customXml",
    f"{_W}smartTag",
    f"{_W}ins",
    f"{_W}moveFrom",
    f"{_W}moveTo",
}
_TEXT_TAGS = {f"{_W}t", f"{_W}delText", f"{_W}instrText"}


@dataclass(slots=True)
class _PartCounters:
    paragraph_count: int = 0
    table_count: int = 0
    table_cell_count: int = 0


@dataclass(frozen=True, slots=True)
class _ParagraphLocation:
    element: ET.Element
    paragraph_index: int
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None


@dataclass(frozen=True, slots=True)
class _PartContainer:
    part_name: str
    part_kind: DocumentPartKind
    part_sequence: int
    element: ET.Element
    relationships: dict[str, str]


@dataclass(frozen=True, slots=True)
class _RawBlock:
    part_name: str
    part_kind: DocumentPartKind
    part_sequence: int
    block_kind: DocumentBlockKind
    paragraph_index: int
    table_index: int | None
    row_index: int | None
    cell_index: int | None
    text: str
    style_id: str | None
    style_name: str | None
    heading_level: int | None
    section_path: tuple[str, ...]
    hyperlink_targets: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedPackage:
    blocks: tuple[_RawBlock, ...]
    source_part_count: int
    table_count: int
    table_cell_count: int
    embedded_visual_count: int
    unsupported_object_count: int
    gaps: tuple[dict[str, object], ...]


@dataclass(slots=True)
class _PackageBuilder:
    blocks: list[_RawBlock] = field(default_factory=list)
    table_count: int = 0
    table_cell_count: int = 0


class PrivateDocxIngestService:
    pipeline_version = "private-docx-ingest-v1"
    parser_name = "wordprocessingml"
    parser_version = "wordprocessingml-ecma376+rules-v1"

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        documents: DocumentRepository | None = None,
        blocks: DocumentBlockRepository | None = None,
        books: BookRepository | None = None,
        reports: PrivateDocxRepository | None = None,
        pit_service: PointInTimeService | None = None,
        maximum_docx_bytes: int = 500 * 1024 * 1024,
        maximum_uncompressed_bytes: int = 1024 * 1024 * 1024,
        maximum_zip_entries: int = 20_000,
        maximum_paragraphs: int = 1_000_000,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.documents = documents or DocumentRepository(state)
        self.blocks = blocks or DocumentBlockRepository(state)
        self.books = books or BookRepository(state)
        self.reports = reports or PrivateDocxRepository(state)
        self.pit_service = pit_service or PointInTimeService(
            PointInTimeRepository(state), state, object_store
        )
        self.maximum_docx_bytes = maximum_docx_bytes
        self.maximum_uncompressed_bytes = maximum_uncompressed_bytes
        self.maximum_zip_entries = maximum_zip_entries
        self.maximum_paragraphs = maximum_paragraphs

    def ingest(
        self,
        path: Path,
        *,
        source_id: str,
        display_name: str,
        author_source_id: str,
        file_version: str,
    ) -> PrivateDocxIngestResult:
        required_values = (source_id, display_name, author_source_id, file_version)
        if not all(value.strip() for value in required_values):
            raise ValueError(
                "source_id, display_name, author_source_id, and file_version are required"
            )
        data, package = self._read_validate_and_parse(path)
        raw_ref = self.object_store.put_bytes(data)
        source_token = sha256_bytes(source_id.encode("utf-8"))[:16]
        snapshot_id = f"private-docx:{source_token}:{raw_ref.sha256}"
        document_id = "private:" + sha256_bytes(
            canonical_json_bytes(
                {
                    "source_id": source_id,
                    "file_version": file_version,
                    "file_sha256": raw_ref.sha256,
                    "document_type": DocumentType.PRIVATE_DOCX,
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
            mime=_DOCX_MIME,
            byte_size=raw_ref.byte_size,
            rights_status="LOCAL_PRIVATE_RESEARCH",
        )
        self.state.register_snapshot(candidate_snapshot)
        snapshot = self.documents.snapshot(snapshot_id)
        if snapshot is None:  # pragma: no cover - register_snapshot guarantees this
            raise ValueError("Private DOCX snapshot registration failed")

        document = self.documents.get_model(document_id)
        if document is None:
            document = SourceDocument(
                document_id=document_id,
                title=display_name,
                publisher="LOCAL_PRIVATE",
                document_type=DocumentType.PRIVATE_DOCX,
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
            "document_type": DocumentType.PRIVATE_DOCX,
            "file_sha256": raw_ref.sha256,
            "file_name_sha256": sha256_bytes(path.name.encode("utf-8")),
            "file_version": file_version,
            "byte_size": raw_ref.byte_size,
            "source_page_count": 0,
            "parser_pipeline_version": self.pipeline_version,
        }
        manifest_identity = {
            key: value for key, value in manifest_fields.items() if key != "file_name_sha256"
        }
        manifest = BookSourceManifest(
            manifest_id="book-manifest:"
            + sha256_bytes(canonical_json_bytes(manifest_identity)),
            raw_object_sha256=raw_ref.sha256,
            rights_status="LOCAL_PRIVATE_RESEARCH",
            **manifest_fields,
        )
        stored_manifest = self.books.register_manifest(manifest)
        self._register_manifest_artifact(stored_manifest)
        report = self._persist_parse(stored_manifest, document, snapshot, package)
        return PrivateDocxIngestResult(
            manifest=stored_manifest,
            pit_metadata=pit,
            parse_report=report,
            created_at=stored_manifest.created_at,
        )

    def _read_validate_and_parse(self, path: Path) -> tuple[bytes, _ParsedPackage]:
        try:
            if not path.is_file():
                raise ValueError("Private DOCX source is not a readable file")
            if path.stat().st_size > self.maximum_docx_bytes:
                raise ValueError("Private DOCX exceeds the configured size limit")
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("Private DOCX could not be read") from exc
        if len(data) > self.maximum_docx_bytes:
            raise ValueError("Private DOCX exceeds the configured size limit")
        if not data.startswith(b"PK"):
            raise ValueError("Private source is not a DOCX package")
        try:
            with ZipFile(BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > self.maximum_zip_entries:
                    raise ValueError("Private DOCX contains too many package entries")
                if sum(item.file_size for item in infos) > self.maximum_uncompressed_bytes:
                    raise ValueError("Private DOCX expands beyond the configured size limit")
                for item in infos:
                    pure = PurePosixPath(item.filename)
                    if pure.is_absolute() or ".." in pure.parts:
                        raise ValueError("Private DOCX contains an unsafe package path")
                    if item.flag_bits & 0x1:
                        raise ValueError("Encrypted DOCX packages are not supported")
                names = {item.filename for item in infos}
                required = {"[Content_Types].xml", "word/document.xml"}
                if not required.issubset(names):
                    raise ValueError("Private source is not a WordprocessingML DOCX")
                if archive.testzip() is not None:
                    raise ValueError("Private DOCX package integrity check failed")
                self._validate_content_type(archive)
                package = self._parse_package(archive, names)
        except BadZipFile as exc:
            raise ValueError("Private source is not a valid DOCX package") from exc
        except ET.ParseError as exc:
            raise ValueError("Private DOCX contains malformed OOXML") from exc
        if len(package.blocks) > self.maximum_paragraphs:
            raise ValueError("Private DOCX exceeds the configured paragraph limit")
        if not package.blocks:
            raise ValueError("Private DOCX has no eligible text paragraphs")
        return data, package

    @staticmethod
    def _validate_content_type(archive: ZipFile) -> None:
        root = ET.fromstring(archive.read("[Content_Types].xml"))
        overrides = {
            item.attrib.get("PartName"): item.attrib.get("ContentType")
            for item in root.findall(f"{_CT}Override")
        }
        if overrides.get("/word/document.xml") != _DOCX_MAIN_CONTENT_TYPE:
            raise ValueError("Private source has an invalid DOCX main-part content type")

    def _parse_package(self, archive: ZipFile, names: set[str]) -> _ParsedPackage:
        part_names = ["word/document.xml"]
        part_names.extend(
            sorted(
                name
                for name in names
                if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            )
        )
        part_names.extend(
            name
            for name in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml")
            if name in names
        )
        roots = {name: ET.fromstring(archive.read(name)) for name in part_names}
        styles = self._styles(archive, names)
        relationships = {
            name: self._relationships(archive, names, name) for name in part_names
        }
        containers: list[_PartContainer] = []
        main_body = roots["word/document.xml"].find(f"{_W}body")
        if main_body is None:
            raise ValueError("Private DOCX main document has no body")
        containers.append(
            _PartContainer(
                part_name="word/document.xml",
                part_kind=DocumentPartKind.MAIN,
                part_sequence=0,
                element=main_body,
                relationships=relationships["word/document.xml"],
            )
        )
        for name in part_names[1:]:
            root = roots[name]
            rels = relationships[name]
            sequence_match = re.search(r"(\d+)\.xml$", name)
            sequence = int(sequence_match.group(1)) if sequence_match else 0
            if "/header" in name:
                containers.append(
                    _PartContainer(name, DocumentPartKind.HEADER, sequence, root, rels)
                )
            elif "/footer" in name:
                containers.append(
                    _PartContainer(name, DocumentPartKind.FOOTER, sequence, root, rels)
                )
            elif name.endswith("footnotes.xml"):
                containers.extend(
                    self._note_containers(name, root, DocumentPartKind.FOOTNOTE, rels)
                )
            elif name.endswith("endnotes.xml"):
                containers.extend(
                    self._note_containers(name, root, DocumentPartKind.ENDNOTE, rels)
                )
            elif name.endswith("comments.xml"):
                for comment in root.findall(f"{_W}comment"):
                    comment_id = self._nonnegative_id(comment)
                    if comment_id is not None:
                        containers.append(
                            _PartContainer(
                                name,
                                DocumentPartKind.COMMENT,
                                comment_id,
                                comment,
                                rels,
                            )
                        )

        builder = _PackageBuilder()
        for container in containers:
            counters = _PartCounters()
            section_path: list[str] = []
            for location in self._iter_paragraphs(container.element, counters):
                raw = self._raw_block(container, location, styles, section_path)
                if raw.heading_level is not None and raw.text.strip():
                    level = raw.heading_level
                    section_path[:] = section_path[: level - 1]
                    section_path.append(raw.text.strip())
                    raw = replace(raw, section_path=tuple(section_path))
                builder.blocks.append(raw)
            builder.table_count += counters.table_count
            builder.table_cell_count += counters.table_cell_count

        embedded_visual_count = sum(
            len(root.findall(f".//{_W}drawing"))
            + len(root.findall(f".//{_W}pict"))
            + len(root.findall(f".//{_W}object"))
            for root in roots.values()
        )
        unsupported_object_count = sum(
            len(root.findall(f".//{_W}altChunk"))
            + len(root.findall(f".//{_W}txbxContent"))
            + len(root.findall(f".//{_W}object"))
            for root in roots.values()
        )
        gaps: list[dict[str, object]] = []
        if embedded_visual_count:
            gaps.append(
                {
                    "gap_type": "EMBEDDED_VISUAL_NOT_TEXT_PARSED",
                    "count": embedded_visual_count,
                }
            )
        if unsupported_object_count:
            gaps.append(
                {
                    "gap_type": "UNSUPPORTED_OOXML_OBJECT",
                    "count": unsupported_object_count,
                }
            )
        return _ParsedPackage(
            blocks=tuple(builder.blocks),
            source_part_count=len(part_names),
            table_count=builder.table_count,
            table_cell_count=builder.table_cell_count,
            embedded_visual_count=embedded_visual_count,
            unsupported_object_count=unsupported_object_count,
            gaps=tuple(gaps),
        )

    @staticmethod
    def _note_containers(
        part_name: str,
        root: ET.Element,
        part_kind: DocumentPartKind,
        relationships: dict[str, str],
    ) -> list[_PartContainer]:
        tag = f"{_W}footnote" if part_kind is DocumentPartKind.FOOTNOTE else f"{_W}endnote"
        result: list[_PartContainer] = []
        for note in root.findall(tag):
            note_type = note.attrib.get(f"{_W}type")
            note_id = PrivateDocxIngestService._nonnegative_id(note)
            if note_type in {"separator", "continuationSeparator"} or note_id is None:
                continue
            result.append(_PartContainer(part_name, part_kind, note_id, note, relationships))
        return result

    @staticmethod
    def _nonnegative_id(element: ET.Element) -> int | None:
        value = element.attrib.get(f"{_W}id")
        try:
            parsed = int(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and parsed >= 0 else None

    @staticmethod
    def _styles(archive: ZipFile, names: set[str]) -> dict[str, tuple[str | None, int | None]]:
        if "word/styles.xml" not in names:
            return {}
        root = ET.fromstring(archive.read("word/styles.xml"))
        result: dict[str, tuple[str | None, int | None]] = {}
        for style in root.findall(f"{_W}style"):
            if style.attrib.get(f"{_W}type") != "paragraph":
                continue
            style_id = style.attrib.get(f"{_W}styleId")
            if not style_id:
                continue
            name_element = style.find(f"{_W}name")
            name = name_element.attrib.get(f"{_W}val") if name_element is not None else None
            outline_element = style.find(f"{_W}pPr/{_W}outlineLvl")
            outline: int | None = None
            if outline_element is not None:
                try:
                    candidate = int(outline_element.attrib.get(f"{_W}val", ""))
                except ValueError:
                    candidate = -1
                if 0 <= candidate <= 8:
                    outline = candidate + 1
            result[style_id] = (name, outline)
        return result

    @staticmethod
    def _relationships(
        archive: ZipFile,
        names: set[str],
        part_name: str,
    ) -> dict[str, str]:
        pure = PurePosixPath(part_name)
        rels_name = str(pure.parent / "_rels" / f"{pure.name}.rels")
        if rels_name not in names:
            return {}
        root = ET.fromstring(archive.read(rels_name))
        return {
            item.attrib["Id"]: item.attrib["Target"]
            for item in root
            if "Id" in item.attrib and "Target" in item.attrib
        }

    def _iter_paragraphs(
        self,
        container: ET.Element,
        counters: _PartCounters,
        table_location: tuple[int, int, int] | None = None,
    ):
        for child in list(container):
            if child.tag == f"{_W}p":
                counters.paragraph_count += 1
                yield _ParagraphLocation(
                    element=child,
                    paragraph_index=counters.paragraph_count,
                    table_index=table_location[0] if table_location else None,
                    row_index=table_location[1] if table_location else None,
                    cell_index=table_location[2] if table_location else None,
                )
            elif child.tag == f"{_W}tbl":
                counters.table_count += 1
                table_index = counters.table_count
                for row_index, row in enumerate(child.findall(f"{_W}tr"), start=1):
                    for cell_index, cell in enumerate(row.findall(f"{_W}tc"), start=1):
                        counters.table_cell_count += 1
                        yield from self._iter_paragraphs(
                            cell,
                            counters,
                            (table_index, row_index, cell_index),
                        )
            elif child.tag in _WRAPPER_TAGS:
                yield from self._iter_paragraphs(child, counters, table_location)

    def _raw_block(
        self,
        container: _PartContainer,
        location: _ParagraphLocation,
        styles: dict[str, tuple[str | None, int | None]],
        section_path: list[str],
    ) -> _RawBlock:
        paragraph = location.element
        style_element = paragraph.find(f"{_W}pPr/{_W}pStyle")
        style_id = style_element.attrib.get(f"{_W}val") if style_element is not None else None
        style_name, heading_level = styles.get(style_id or "", (None, None))
        if heading_level is None:
            heading_level = self._heading_level_from_name(style_id, style_name)
        hyperlinks: list[str] = []
        warnings: list[str] = []
        for hyperlink in paragraph.iter(f"{_W}hyperlink"):
            relationship_id = hyperlink.attrib.get(f"{_R}id")
            anchor = hyperlink.attrib.get(f"{_W}anchor")
            if relationship_id:
                target = container.relationships.get(relationship_id)
                if target is None:
                    warnings.append("UNRESOLVED_HYPERLINK_RELATIONSHIP")
                else:
                    hyperlinks.append(target)
            elif anchor:
                hyperlinks.append(f"#{anchor}")
        table_location = (location.table_index, location.row_index, location.cell_index)
        return _RawBlock(
            part_name=container.part_name,
            part_kind=container.part_kind,
            part_sequence=container.part_sequence,
            block_kind=(
                DocumentBlockKind.TABLE_CELL_PARAGRAPH
                if all(value is not None for value in table_location)
                else DocumentBlockKind.PARAGRAPH
            ),
            paragraph_index=location.paragraph_index,
            table_index=location.table_index,
            row_index=location.row_index,
            cell_index=location.cell_index,
            text=self._paragraph_text(paragraph),
            style_id=style_id,
            style_name=style_name,
            heading_level=heading_level,
            section_path=tuple(section_path),
            hyperlink_targets=tuple(hyperlinks),
            warnings=tuple(sorted(set(warnings))),
        )

    @staticmethod
    def _heading_level_from_name(style_id: str | None, style_name: str | None) -> int | None:
        for candidate in (style_name, style_id):
            if not candidate:
                continue
            normalized = candidate.casefold().replace(" ", "")
            match = re.fullmatch(r"(?:heading|标题)([1-9])", normalized)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _paragraph_text(paragraph: ET.Element) -> str:
        pieces: list[str] = []

        def walk(element: ET.Element) -> None:
            if element.tag == f"{_W}txbxContent":
                return
            if element.tag in _TEXT_TAGS:
                pieces.append(element.text or "")
                return
            if element.tag == f"{_W}tab":
                pieces.append("\t")
                return
            if element.tag in {f"{_W}br", f"{_W}cr"}:
                pieces.append("\n")
                return
            if element.tag == f"{_W}noBreakHyphen":
                pieces.append("\u2011")
                return
            if element.tag == f"{_W}softHyphen":
                pieces.append("\u00ad")
                return
            for child in list(element):
                walk(child)

        walk(paragraph)
        return "".join(pieces).replace("\r\n", "\n").replace("\r", "\n")

    def _persist_parse(
        self,
        manifest: BookSourceManifest,
        document: SourceDocument,
        snapshot: SourceSnapshot,
        package: _ParsedPackage,
    ) -> PrivateDocxParseReport:
        blocks: list[DocumentBlock] = []
        for block_index, raw in enumerate(package.blocks, start=1):
            text_ref = self.object_store.put_bytes(raw.text.encode("utf-8"))
            metadata_ref = self.object_store.put_json(
                {
                    "schema_version": "1.0",
                    "part_name": raw.part_name,
                    "part_kind": raw.part_kind,
                    "part_sequence": raw.part_sequence,
                    "block_kind": raw.block_kind,
                    "paragraph_index": raw.paragraph_index,
                    "table_index": raw.table_index,
                    "row_index": raw.row_index,
                    "cell_index": raw.cell_index,
                    "style_id": raw.style_id,
                    "style_name": raw.style_name,
                    "heading_level": raw.heading_level,
                    "section_path": raw.section_path,
                    "hyperlink_targets": raw.hyperlink_targets,
                }
            )
            identity = {
                "snapshot_id": snapshot.snapshot_id,
                "block_index": block_index,
                "parser_version": self.parser_version,
                "text_sha256": text_ref.sha256,
                "metadata_object_sha256": metadata_ref.sha256,
            }
            blocks.append(
                DocumentBlock(
                    block_id="block:" + sha256_bytes(canonical_json_bytes(identity)),
                    document_id=document.document_id,
                    snapshot_id=snapshot.snapshot_id,
                    block_index=block_index,
                    part_kind=raw.part_kind,
                    part_sequence=raw.part_sequence,
                    block_kind=raw.block_kind,
                    paragraph_index=raw.paragraph_index,
                    table_index=raw.table_index,
                    row_index=raw.row_index,
                    cell_index=raw.cell_index,
                    text_char_count=len(raw.text),
                    text_sha256=text_ref.sha256,
                    text_object_sha256=text_ref.sha256,
                    metadata_object_sha256=metadata_ref.sha256,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    hyperlink_count=len(raw.hyperlink_targets),
                    is_heading=raw.heading_level is not None,
                    warnings=list(raw.warnings),
                    created_at=snapshot.fetched_at,
                )
            )
        self.blocks.register_blocks(blocks)
        block_ids = [block.block_id for block in blocks]
        block_set_ref = self.object_store.put_json(block_ids)
        gaps = list(package.gaps)
        coverage = CoverageStatus.PARTIAL if gaps else CoverageStatus.COMPLETE
        processing = (
            BookProcessingStatus.PARTIAL if gaps else BookProcessingStatus.COMPLETE
        )
        report_identity = {
            "manifest_id": manifest.manifest_id,
            "parser_version": self.parser_version,
            "block_set_sha256": block_set_ref.sha256,
            "gaps": gaps,
        }
        report_id = "docx-parse:" + sha256_bytes(canonical_json_bytes(report_identity))
        existing = self.reports.get_parse_report(report_id)
        if existing is not None:
            self._register_parse_artifact(existing)
            return existing
        report = PrivateDocxParseReport(
            docx_parse_report_id=report_id,
            manifest_id=manifest.manifest_id,
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            file_sha256=manifest.file_sha256,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            processing_status=processing,
            coverage_status=coverage,
            source_part_count=package.source_part_count,
            source_paragraph_count=len(package.blocks),
            processed_block_count=len(blocks),
            nonempty_block_count=sum(bool(raw.text.strip()) for raw in package.blocks),
            empty_block_count=sum(not raw.text.strip() for raw in package.blocks),
            table_count=package.table_count,
            table_cell_count=package.table_cell_count,
            hyperlink_count=sum(len(raw.hyperlink_targets) for raw in package.blocks),
            embedded_visual_count=package.embedded_visual_count,
            unsupported_object_count=package.unsupported_object_count,
            parsed_text_char_count=sum(len(raw.text) for raw in package.blocks),
            block_ids=block_ids,
            block_set_sha256=block_set_ref.sha256,
            gaps=gaps,
            created_at=snapshot.fetched_at,
        )
        report_ref = self.object_store.put_json(report.model_dump(mode="json"))
        report = report.model_copy(update={"report_object_sha256": report_ref.sha256})
        stored = self.reports.register_parse_report(report)
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

    def _register_parse_artifact(self, report: PrivateDocxParseReport) -> None:
        if report.report_object_sha256 is None:  # pragma: no cover - repository invariant
            raise ValueError("Private DOCX parse artifact hash is missing")
        self.state.register_artifact(
            artifact_id=f"PrivateDocxParseReport:{report.docx_parse_report_id}",
            artifact_type="PrivateDocxParseReport",
            schema_version=report.schema_version,
            object_hash=report.report_object_sha256,
            input_hashes=[report.file_sha256, report.block_set_sha256],
        )
