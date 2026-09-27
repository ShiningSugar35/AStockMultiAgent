"""Content-based media admission for bounded official documents.

The detector is deliberately independent from source authority. It answers only
whether locally acquired bytes are a supported, structurally safe document
format; source/provenance admission remains the responsibility of
SourcePolicyGate and OfficialWebDocumentCaptureService.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_MIME = "application/msword"
TEXT_MIME = "text/plain"
type SupportedOfficialMediaType = Literal[
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
]

_OLE_COMPOUND_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOCX_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_TEXT_ENCODINGS = ("utf-8-sig", "gb18030")


@dataclass(frozen=True, slots=True)
class OfficialDocumentMedia:
    """Detected media and the parser capability that justified admission."""

    media_type: SupportedOfficialMediaType
    parser_name: str
    parser_version: str
    text_encoding: str | None = None


def inspect_official_document(
    content: bytes,
    *,
    maximum_document_bytes: int,
    maximum_docx_entries: int,
    maximum_docx_uncompressed_bytes: int,
) -> OfficialDocumentMedia:
    """Validate bytes by content, not filename or response Content-Type.

    This function never follows links, executes macros, renders Office content,
    or accesses the network. Legacy .doc is admitted only after the pinned
    pure-Python parser can actually extract visible text from the OLE payload.
    """

    if not content:
        raise ValueError("Official document content is empty")
    if len(content) > maximum_document_bytes:
        raise ValueError("Official document exceeds the configured size limit")

    stripped = content.lstrip()
    if stripped.startswith(b"%PDF-"):
        return OfficialDocumentMedia(
            media_type=PDF_MIME,
            parser_name="pymupdf",
            parser_version="pymupdf-native-text",
        )
    if content.startswith(_OLE_COMPOUND_MAGIC):
        return _inspect_legacy_doc(content)
    if content.startswith(b"PK"):
        return _inspect_docx(
            content,
            maximum_docx_entries=maximum_docx_entries,
            maximum_docx_uncompressed_bytes=maximum_docx_uncompressed_bytes,
        )
    return _inspect_text(content)


def _inspect_legacy_doc(content: bytes) -> OfficialDocumentMedia:
    try:
        from legacy_doc import extract_text
    except ImportError as exc:  # pragma: no cover - deployment-specific
        raise ValueError("Legacy DOC parser is unavailable in the active environment") from exc

    try:
        result = extract_text(content)
    except Exception as exc:  # library exception hierarchy may evolve
        raise ValueError("Legacy DOC failed bounded structural extraction") from exc
    if not str(result.text).strip():
        raise ValueError("Legacy DOC contains no extractable visible text")
    return OfficialDocumentMedia(
        media_type=DOC_MIME,
        parser_name=str(result.parser),
        parser_version=str(result.version),
    )


def _inspect_docx(
    content: bytes,
    *,
    maximum_docx_entries: int,
    maximum_docx_uncompressed_bytes: int,
) -> OfficialDocumentMedia:
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > maximum_docx_entries:
                raise ValueError("Official DOCX contains too many package entries")
            if sum(item.file_size for item in infos) > maximum_docx_uncompressed_bytes:
                raise ValueError("Official DOCX expands beyond the configured size limit")
            for item in infos:
                pure = PurePosixPath(item.filename)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError("Official DOCX contains an unsafe package path")
                if item.flag_bits & 0x1:
                    raise ValueError("Encrypted DOCX packages are not supported")
            names = {item.filename for item in infos}
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise ValueError("Official source is not a WordprocessingML DOCX")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("Macro-enabled Word packages are not supported")
            if archive.testzip() is not None:
                raise ValueError("Official DOCX package integrity check failed")
            root = ET.fromstring(archive.read("[Content_Types].xml"))
            overrides = {
                item.attrib.get("PartName"): item.attrib.get("ContentType")
                for item in root.findall(f"{{{_CT_NS}}}Override")
            }
            if overrides.get("/word/document.xml") != _DOCX_MAIN_CONTENT_TYPE:
                raise ValueError("Official source has an invalid DOCX main-part content type")
    except BadZipFile as exc:
        raise ValueError("Official source is not a valid DOCX package") from exc
    except ET.ParseError as exc:
        raise ValueError("Official DOCX contains malformed OOXML") from exc

    return OfficialDocumentMedia(
        media_type=DOCX_MIME,
        parser_name="wordprocessingml",
        parser_version="wordprocessingml-ecma376+rules-v1",
    )


def _inspect_text(content: bytes) -> OfficialDocumentMedia:
    for encoding in _TEXT_ENCODINGS:
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not text.strip() or "\x00" in text:
            continue
        visible_or_spacing = sum(
            1 for char in text if char.isprintable() or char in {"\r", "\n", "\t"}
        )
        if visible_or_spacing / len(text) < 0.98:
            continue
        normalized_prefix = text.lstrip().lower()
        markup_prefix = re.match(
            r"^(?:<!--|<!doctype\\b|<\\?xml\\b|</?[a-z][a-z0-9:_-]*(?:\\s|/?>))",
            normalized_prefix,
            flags=re.IGNORECASE,
        )
        if normalized_prefix.startswith("{\\rtf") or markup_prefix is not None:
            raise ValueError("Markup/RTF content is not admitted as plain text")
        return OfficialDocumentMedia(
            media_type=TEXT_MIME,
            parser_name="plain-text",
            parser_version="plain-text-v1",
            text_encoding=encoding,
        )
    raise ValueError("Official document format is unsupported or structurally invalid")


__all__ = [
    "DOCX_MIME",
    "DOC_MIME",
    "PDF_MIME",
    "TEXT_MIME",
    "OfficialDocumentMedia",
    "inspect_official_document",
]
