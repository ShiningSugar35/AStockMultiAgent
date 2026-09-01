"""Deterministic integrity and structural QA for generated reports."""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from astock.core.errors import DataQualityError, FailureClass

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


@dataclass(frozen=True, slots=True)
class DocxValidationReport:
    valid: bool
    file_size: int
    paragraph_count: int
    heading_count: int
    table_count: int
    image_count: int
    image_total_bytes: int
    unique_image_count: int
    estimated_pages: int

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "valid": self.valid,
            "file_size": self.file_size,
            "paragraph_count": self.paragraph_count,
            "heading_count": self.heading_count,
            "table_count": self.table_count,
            "image_count": self.image_count,
            "image_total_bytes": self.image_total_bytes,
            "unique_image_count": self.unique_image_count,
            "estimated_pages": self.estimated_pages,
        }


def validate_docx(path: Path) -> DocxValidationReport:
    if not path.is_file() or path.stat().st_size < 200:
        raise DataQualityError(
            "DOCX output is missing or too small",
            failure_class=FailureClass.DATA_QUALITY,
        )
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            _validate_member_names(names)
            if archive.testzip() is not None:
                raise ValueError("zip member checksum failed")
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
            if not required.issubset(names):
                raise ValueError("required OpenXML parts are missing")
            _validate_relationships(archive, names)
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            paragraphs = document.findall(f".//{{{_WORD_NS}}}p")
            tables = document.findall(f".//{{{_WORD_NS}}}tbl")
            drawings = document.findall(f".//{{{_DRAWING_NS}}}inline") + document.findall(
                f".//{{{_DRAWING_NS}}}anchor"
            )
            headings = 0
            for paragraph in paragraphs:
                style = paragraph.find(f"./{{{_WORD_NS}}}pPr/{{{_WORD_NS}}}pStyle")
                if style is not None:
                    value = style.attrib.get(f"{{{_WORD_NS}}}val", "")
                    if value.casefold().startswith(("heading", "title")):
                        headings += 1
            media = sorted(name for name in names if name.startswith("word/media/"))
            media_bytes = [archive.read(name) for name in media]
            image_total = sum(len(item) for item in media_bytes)
            if any(not item for item in media_bytes):
                raise ValueError("empty media part")
            unique = len(set(media_bytes))
            estimated_pages = max(1, (len(paragraphs) + 34) // 35)
            return DocxValidationReport(
                valid=True,
                file_size=path.stat().st_size,
                paragraph_count=len(paragraphs),
                heading_count=headings,
                table_count=len(tables),
                image_count=max(len(drawings), len(media)),
                image_total_bytes=image_total,
                unique_image_count=unique,
                estimated_pages=estimated_pages,
            )
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError) as exc:
        raise DataQualityError(
            "DOCX OpenXML integrity validation failed",
            failure_class=FailureClass.DATA_QUALITY,
            details={"error_class": type(exc).__name__},
        ) from exc


def visual_qa_summary(path: Path) -> dict[str, int | bool]:
    return validate_docx(path).as_dict()


def validate_pdf(path: Path) -> None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DataQualityError(
            "PDF output is unavailable",
            failure_class=FailureClass.DATA_QUALITY,
        ) from exc
    if len(raw) < 20 or not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
        raise DataQualityError(
            "PDF output failed magic or EOF validation",
            failure_class=FailureClass.DATA_QUALITY,
        )


def _validate_member_names(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("unsafe OpenXML member path")


def _validate_relationships(archive: zipfile.ZipFile, names: list[str]) -> None:
    known = set(names)
    relationship_parts = [name for name in names if name.endswith(".rels")]
    for relationship_part in relationship_parts:
        root = ElementTree.fromstring(archive.read(relationship_part))
        base = _relationship_source_directory(relationship_part)
        for relationship in root.findall(f"{{{_REL_NS}}}Relationship"):
            if relationship.attrib.get("TargetMode") == "External":
                continue
            target = relationship.attrib.get("Target")
            if not target:
                raise ValueError("relationship target is empty")
            normalized = posixpath.normpath(posixpath.join(base, target))
            if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
                raise ValueError("relationship target escapes the package")
            if normalized not in known:
                raise ValueError("relationship target does not exist")


def _relationship_source_directory(relationship_part: str) -> str:
    rel_path = PurePosixPath(relationship_part)
    if rel_path.parent.name != "_rels":
        return str(rel_path.parent)
    source_parent = rel_path.parent.parent
    return str(source_parent) if str(source_parent) != "." else ""


__all__ = [
    "DocxValidationReport",
    "validate_docx",
    "validate_pdf",
    "visual_qa_summary",
]
