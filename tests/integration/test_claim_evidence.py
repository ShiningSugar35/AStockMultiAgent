from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
import pytest

from astock.core.object_store import ObjectStore
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.pdf_parser import PdfParseService
from astock.documents.repository import DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.schemas import (
    ClaimStatus,
    ClaimType,
    DocumentType,
    EvidenceAttachment,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    SourceDocument,
    SourceSnapshot,
)


def _service(tmp_path: Path, state) -> tuple[ClaimEvidenceService, str, str]:
    text = "Revenue grew by 25 percent. Management expects stable demand."
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = pdf.tobytes()
    pdf.close()

    objects = ObjectStore(tmp_path / "objects")
    raw = objects.put_bytes(pdf_bytes)
    available = datetime(2026, 7, 13, tzinfo=UTC)
    snapshot = SourceSnapshot(
        snapshot_id=f"fixture:{raw.sha256}",
        source_id="fixture-claim-evidence",
        object_sha256=raw.sha256,
        fetched_at=available,
        available_to_system_at=available,
        source_url=None,
        mime="application/pdf",
        byte_size=raw.byte_size,
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id="fixture:claim-evidence",
        title="Claim evidence fixture",
        publisher="TEST",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["000001"],
        published_at=available,
        effective_at=available,
        disclosure_id="fixture:claim-evidence",
        source_url="https://example.invalid/evidence.pdf",
        rights_status="TEST_FIXTURE",
    )
    documents = DocumentRepository(state)
    documents.register(document, snapshot)
    pages = DocumentPageRepository(state)
    report = PdfParseService(objects, state, pages).parse(document, snapshot, ocr_enabled=False)
    service = ClaimEvidenceService(
        objects,
        state,
        pages,
        documents,
        EvidenceRepository(state),
    )
    return service, report.page_ids[0], text


def _evidence(service: ClaimEvidenceService, page_id: str, text: str, phrase: str):
    start = text.index(phrase)
    return service.create_page_evidence(
        page_id=page_id,
        char_start=start,
        char_end=start + len(phrase),
        evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
        fact_status=FactStatus.DIRECT,
        entity_ids=["company:000001"],
    )


def test_page_evidence_is_exact_idempotent_and_keeps_plaintext_out_of_sqlite(
    tmp_path: Path, state
) -> None:
    service, page_id, text = _service(tmp_path, state)
    phrase = "Revenue grew by 25 percent."
    first = _evidence(service, page_id, text, phrase)
    repeated = _evidence(service, page_id, text, phrase)
    assert first == repeated
    assert service.object_store.get_bytes(first.excerpt_object_sha256).decode() == phrase
    with state.connect() as connection:
        row = connection.execute(
            "SELECT evidence_json FROM evidence_record WHERE evidence_id=?",
            (first.evidence_id,),
        ).fetchone()
        assert row is not None
        assert phrase not in row["evidence_json"]
        assert connection.execute("SELECT COUNT(*) FROM evidence_record").fetchone()[0] == 1


def test_invalid_or_blank_evidence_ranges_are_rejected(tmp_path: Path, state) -> None:
    service, page_id, text = _service(tmp_path, state)
    with pytest.raises(ValueError, match="within"):
        service.create_page_evidence(
            page_id=page_id,
            char_start=0,
            char_end=len(text) + 1,
            evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
            fact_status=FactStatus.DIRECT,
            entity_ids=[],
        )
    space = text.index(" ")
    with pytest.raises(ValueError, match="visible"):
        service.create_page_evidence(
            page_id=page_id,
            char_start=space,
            char_end=space + 1,
            evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
            fact_status=FactStatus.DIRECT,
            entity_ids=[],
        )


def test_many_to_many_claim_links_and_future_information_gate(tmp_path: Path, state) -> None:
    service, page_id, text = _service(tmp_path, state)
    growth = _evidence(service, page_id, text, "Revenue grew by 25 percent.")
    demand = _evidence(service, page_id, text, "Management expects stable demand.")
    attachment_growth = EvidenceAttachment(
        evidence_id=growth.evidence_id, relation=EvidenceRelation.SUPPORT
    )
    with pytest.raises(ValueError, match="Future evidence"):
        service.create_claim(
            subject_id="company:000001",
            predicate="revenue_growth",
            object_json={"percent": 25},
            as_of=datetime(2026, 7, 12, tzinfo=UTC),
            claim_type=ClaimType.FACT,
            confidence=0.9,
            status=ClaimStatus.VALIDATED,
            attachments=[attachment_growth],
        )

    as_of = datetime(2026, 7, 13, tzinfo=UTC) + timedelta(seconds=1)
    first = service.create_claim(
        subject_id="company:000001",
        predicate="outlook",
        object_json={"growth": 25, "demand": "stable"},
        as_of=as_of,
        claim_type=ClaimType.FACT,
        confidence=0.9,
        status=ClaimStatus.VALIDATED,
        attachments=[
            attachment_growth,
            EvidenceAttachment(
                evidence_id=demand.evidence_id, relation=EvidenceRelation.SUPPORT
            ),
        ],
    )
    repeated = service.create_claim(
        subject_id="company:000001",
        predicate="outlook",
        object_json={"growth": 25, "demand": "stable"},
        as_of=as_of,
        claim_type=ClaimType.FACT,
        confidence=0.9,
        status=ClaimStatus.VALIDATED,
        attachments=[
            attachment_growth,
            EvidenceAttachment(
                evidence_id=demand.evidence_id, relation=EvidenceRelation.SUPPORT
            ),
        ],
    )
    assert first == repeated
    second = service.create_claim(
        subject_id="company:000001",
        predicate="historical_growth",
        object_json={"percent": 25},
        as_of=as_of,
        claim_type=ClaimType.FACT,
        confidence=0.8,
        status=ClaimStatus.VALIDATED,
        attachments=[attachment_growth],
    )
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_record").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM claim_evidence_link").fetchone()[0] == 3
        linked_claims = connection.execute(
            "SELECT COUNT(DISTINCT claim_id) FROM claim_evidence_link WHERE evidence_id=?",
            (growth.evidence_id,),
        ).fetchone()[0]
    assert linked_claims == 2
    assert first.claim.claim_id != second.claim.claim_id


def test_support_and_refute_create_open_conflict(tmp_path: Path, state) -> None:
    service, page_id, text = _service(tmp_path, state)
    support = _evidence(service, page_id, text, "Revenue grew by 25 percent.")
    refute = _evidence(service, page_id, text, "Management expects stable demand.")
    bundle = service.create_claim(
        subject_id="company:000001",
        predicate="demand_is_stable",
        object_json={"value": True},
        as_of=datetime(2026, 7, 13, 0, 0, 1, tzinfo=UTC),
        claim_type=ClaimType.INFERENCE,
        confidence=0.5,
        status=ClaimStatus.PROPOSED,
        attachments=[
            EvidenceAttachment(
                evidence_id=support.evidence_id, relation=EvidenceRelation.SUPPORT
            ),
            EvidenceAttachment(
                evidence_id=refute.evidence_id, relation=EvidenceRelation.REFUTE
            ),
        ],
    )
    assert bundle.claim.status is ClaimStatus.CONFLICTED
    assert bundle.conflict is not None
    assert bundle.conflict.resolution_status.value == "OPEN"
    assert set(bundle.conflict.evidence_ids) == {support.evidence_id, refute.evidence_id}
