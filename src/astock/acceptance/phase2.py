"""Thirty-document controlled benchmark for Phase 2 PDF and evidence guarantees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentPageRepository, DocumentRepository, PdfParseService
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AvailabilityBasis,
    DocumentType,
    EvidenceGrade,
    FactStatus,
    PointInTimeStatus,
    SourceDocument,
    SourceSnapshot,
)


def run_controlled_document_benchmark(
    root: Path,
    *,
    native_document_count: int = 15,
    scanned_document_count: int = 15,
) -> dict[str, Any]:
    """Run real native extraction and RapidOCR against controlled labeled PDFs."""

    if native_document_count < 1 or scanned_document_count < 1:
        raise ValueError("benchmark requires native and scanned documents")
    root.mkdir(parents=True, exist_ok=True)
    state = StateStore(root / "state.sqlite")
    state.migrate()
    objects = ObjectStore(root / "objects" / "sha256")
    documents = DocumentRepository(state)
    pages = DocumentPageRepository(state)
    parser = PdfParseService(objects, state, pages)
    evidence_service = ClaimEvidenceService(
        objects,
        state,
        pages,
        documents,
        EvidenceRepository(state),
    )
    pit_service = PointInTimeService(PointInTimeRepository(state), state, objects)

    native_expected_characters = 0
    native_recalled_characters = 0
    scanned_expected_fields = 0
    scanned_recalled_fields = 0
    traceable_citations = 0
    idempotent_documents = 0
    extraction_methods: dict[str, int] = {}
    parser_versions: set[str] = set()
    total = native_document_count + scanned_document_count
    for index in range(total):
        scanned = index >= native_document_count
        case_number = index + 1
        fields = _labeled_fields(case_number)
        pdf_bytes = _scanned_pdf(fields) if scanned else _native_pdf(fields)
        available = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
        kind = "scanned" if scanned else "native"
        document_id = f"acceptance:{kind}:{case_number:02d}"
        raw = objects.put_bytes(pdf_bytes)
        snapshot = SourceSnapshot(
            snapshot_id=f"{document_id}:{raw.sha256}",
            source_id=document_id,
            object_sha256=raw.sha256,
            fetched_at=available,
            available_to_system_at=available,
            source_url=None,
            mime="application/pdf",
            byte_size=raw.byte_size,
            rights_status="CONTROLLED_ACCEPTANCE_FIXTURE",
        )
        state.register_snapshot(snapshot)
        document = SourceDocument(
            document_id=document_id,
            title=f"Controlled {kind} acceptance {case_number:02d}",
            publisher="CONTROLLED_ACCEPTANCE",
            document_type=DocumentType.ANNOUNCEMENT,
            company_ids=[f"{600000 + case_number:06d}"],
            published_at=available,
            effective_at=available,
            disclosure_id=document_id,
            source_url=f"acceptance://phase2/{kind}/{case_number:02d}",
            rights_status="CONTROLLED_ACCEPTANCE_FIXTURE",
        )
        documents.register(document, snapshot)
        pit_service.create(
            source_id=document_id,
            source_document_id=document_id,
            source_snapshot_id=snapshot.snapshot_id,
            published_at=available,
            effective_at=available,
            ingested_at=available,
            available_to_system_at=available,
            point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            availability_basis=AvailabilityBasis.FETCH_OBSERVED,
        )
        first = parser.parse(document, snapshot)
        repeated = parser.parse(document, snapshot)
        if first == repeated:
            idempotent_documents += 1
        page = pages.get_page_by_id(first.page_ids[0])
        if page is None:  # pragma: no cover - parser contract
            raise RuntimeError(f"missing controlled page: {document_id}")
        text = objects.get_bytes(page.text_object_sha256).decode("utf-8")
        normalized_text = _normalize(text)
        normalized_expected = _normalize("\n".join(fields))
        if scanned:
            scanned_expected_fields += len(fields)
            scanned_recalled_fields += sum(
                1 for field in fields if _normalize(field) in normalized_text
            )
        else:
            native_expected_characters += len(normalized_expected)
            native_recalled_characters += _matching_character_count(
                normalized_expected, normalized_text
            )
        visible_start = next(
            (position for position, character in enumerate(text) if not character.isspace()),
            None,
        )
        if visible_start is None:
            raise RuntimeError(f"controlled page has no extracted text: {document_id}")
        visible_end = min(len(text), visible_start + 32)
        evidence = evidence_service.create_page_evidence(
            page_id=page.page_id,
            char_start=visible_start,
            char_end=visible_end,
            evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
            fact_status=FactStatus.DIRECT,
            entity_ids=[f"controlled:{case_number:02d}"],
        )
        if (
            objects.verify(snapshot.object_sha256)
            and objects.verify(page.text_object_sha256)
            and objects.verify(evidence.excerpt_object_sha256)
            and evidence.snapshot_id == snapshot.snapshot_id
            and evidence.page_id == page.page_id
            and evidence.locator.page_number == 1
            and evidence.locator.parser_version == page.parser_version
            and evidence.excerpt_sha256
            == sha256_bytes(objects.get_bytes(evidence.excerpt_object_sha256))
        ):
            traceable_citations += 1
        method = page.extraction_method.value
        extraction_methods[method] = extraction_methods.get(method, 0) + 1
        parser_versions.add(page.parser_version)

    native_recall = native_recalled_characters / native_expected_characters
    scanned_recall = scanned_recalled_fields / scanned_expected_fields
    citation_rate = traceable_citations / total
    idempotency_rate = idempotent_documents / total
    thresholds = {
        "native_text_recall": 0.98,
        "scanned_key_field_recall": 0.95,
        "citation_traceability": 1.0,
        "idempotency": 1.0,
    }
    metrics = {
        "native_text_recall": _metric(
            native_recalled_characters,
            native_expected_characters,
            native_recall,
            thresholds["native_text_recall"],
        ),
        "scanned_key_field_recall": _metric(
            scanned_recalled_fields,
            scanned_expected_fields,
            scanned_recall,
            thresholds["scanned_key_field_recall"],
        ),
        "citation_traceability": _metric(
            traceable_citations,
            total,
            citation_rate,
            thresholds["citation_traceability"],
        ),
        "idempotency": _metric(
            idempotent_documents,
            total,
            idempotency_rate,
            thresholds["idempotency"],
        ),
    }
    return {
        "dataset_type": "CONTROLLED_LABELED_NON_PRODUCTION",
        "document_count": total,
        "native_document_count": native_document_count,
        "scanned_document_count": scanned_document_count,
        "extraction_methods": dict(sorted(extraction_methods.items())),
        "parser_versions": sorted(parser_versions),
        "metrics": metrics,
        "all_passed": all(bool(metric["passed"]) for metric in metrics.values()),
        "state_integrity": state.integrity_check(),
    }


def _labeled_fields(case_number: int) -> list[str]:
    return [
        f"ANNUAL REPORT CASE {case_number:02d}",
        f"COMPANY CODE {600000 + case_number:06d}",
        f"REVENUE {100 + case_number} MILLION",
    ]


def _native_pdf(fields: list[str]) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=700, height=500)
    page.insert_textbox(
        pymupdf.Rect(72, 72, 640, 420),
        "\n".join(fields),
        fontsize=24,
    )
    result = document.tobytes()
    document.close()
    return result


def _scanned_pdf(fields: list[str]) -> bytes:
    image = Image.new("RGB", (1800, 1000), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 82)
        if font_path.is_file()
        else ImageFont.load_default()
    )
    for line_number, field in enumerate(fields):
        draw.text((100, 130 + line_number * 230), field, fill="black", font=font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    document = pymupdf.open()
    page = document.new_page(width=900, height=500)
    page.insert_image(page.rect, stream=buffer.getvalue())
    result = document.tobytes()
    document.close()
    return result


def _normalize(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _matching_character_count(expected: str, actual: str) -> int:
    matcher = SequenceMatcher(None, expected, actual, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks())


def _metric(numerator: int, denominator: int, value: float, threshold: float) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(value, 6),
        "threshold": threshold,
        "passed": value >= threshold,
    }
