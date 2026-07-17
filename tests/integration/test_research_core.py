from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
import pytest

from astock.core.object_store import ObjectStore
from astock.documents import DocumentPageRepository, DocumentRepository, PdfParseService
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.research import ResearchCoreService, load_research_core_config
from astock.schemas import (
    BASE_CASE_SECTIONS,
    AvailabilityBasis,
    BaseCaseBuildRequest,
    BaseCaseDraft,
    ClaimStatus,
    ClaimType,
    DocumentType,
    EvidenceAttachment,
    EvidenceFreezeRequest,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    FetchStatus,
    PointInTimeStatus,
    ResearchCoverageStatus,
    ResearchFindingInput,
    ResearchFindingType,
    SourceDocument,
    SourceSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATEMENT = "Synthetic cited BaseCase statement that must stay out of SQLite."


def _fixture(
    tmp_path: Path,
    state,
    *,
    suffix: str,
    evidence_grade: EvidenceGrade = EvidenceGrade.PRIMARY_OFFICIAL,
    pit_status: PointInTimeStatus = PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
    conflict: bool = False,
):
    text = "Revenue grew. Demand may weaken."
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = pdf.tobytes()
    pdf.close()
    objects = ObjectStore(tmp_path / f"objects-{suffix}")
    raw = objects.put_bytes(pdf_bytes)
    available = datetime(2026, 1, 10, tzinfo=UTC)
    snapshot = SourceSnapshot(
        snapshot_id=f"snapshot:research:{suffix}",
        source_id=f"source:research:{suffix}",
        object_sha256=raw.sha256,
        fetched_at=available,
        available_to_system_at=available,
        mime="application/pdf",
        byte_size=raw.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id=f"document:research:{suffix}",
        title="Synthetic research fixture",
        publisher="TEST",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["company:000001"],
        published_at=available,
        effective_at=available,
        disclosure_id=f"disclosure:research:{suffix}",
        source_url=f"https://example.invalid/{suffix}.pdf",
        rights_status="TEST_FIXTURE",
    )
    documents = DocumentRepository(state)
    documents.register(document, snapshot)
    pages = DocumentPageRepository(state)
    parse = PdfParseService(objects, state, pages).parse(
        document,
        snapshot,
        ocr_enabled=False,
    )
    claim_service = ClaimEvidenceService(
        objects,
        state,
        pages,
        documents,
        EvidenceRepository(state),
    )

    def evidence(phrase: str):
        start = text.index(phrase)
        return claim_service.create_page_evidence(
            page_id=parse.page_ids[0],
            char_start=start,
            char_end=start + len(phrase),
            evidence_grade=evidence_grade,
            fact_status=FactStatus.DIRECT,
            entity_ids=["company:000001"],
        )

    support = evidence("Revenue grew.")
    attachments = [
        EvidenceAttachment(
            evidence_id=support.evidence_id,
            relation=EvidenceRelation.SUPPORT,
        )
    ]
    if conflict:
        refute = evidence("Demand may weaken.")
        attachments.append(
            EvidenceAttachment(
                evidence_id=refute.evidence_id,
                relation=EvidenceRelation.REFUTE,
            )
        )
    bundle = claim_service.create_claim(
        subject_id="company:000001",
        predicate=f"research_fixture_{suffix}",
        object_json={"value": "synthetic"},
        as_of=available + timedelta(seconds=1),
        claim_type=ClaimType.FACT,
        confidence=0.8,
        status=ClaimStatus.VALIDATED,
        attachments=attachments,
    )
    basis = (
        AvailabilityBasis.PROVIDER_CURRENT_VALUE
        if pit_status is PointInTimeStatus.NOT_PIT_SAFE
        else AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP
    )
    PointInTimeService(PointInTimeRepository(state), state, objects).create(
        source_id=f"pit-source:research:{suffix}",
        source_document_id=document.document_id,
        source_snapshot_id=snapshot.snapshot_id,
        published_at=available,
        effective_at=available,
        ingested_at=available,
        available_to_system_at=available,
        point_in_time_status=pit_status,
        availability_basis=basis,
    )
    service = ResearchCoreService(
        state,
        objects,
        load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml"),
    )
    return service, objects, bundle, support, available


def _draft(as_of: datetime, evidence_id: str, *, critical: bool = True) -> BaseCaseDraft:
    return BaseCaseDraft(
        company_id="company:000001",
        as_of=as_of,
        findings_by_section={
            section: [
                ResearchFindingInput(
                    statement=f"{_STATEMENT} {section.value}",
                    finding_type=ResearchFindingType.VERIFIED_FACT,
                    confidence=0.8,
                    critical=critical,
                    evidence_ids=[evidence_id],
                )
            ]
            for section in BASE_CASE_SECTIONS
        },
        evidence_gaps=[],
        specialist_tags=["industrial"],
        requested_base_confidence=0.85,
    )


def test_frozen_evidence_and_base_case_are_idempotent_cited_and_private(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="complete",
    )
    as_of = available + timedelta(seconds=2)
    freeze_request = EvidenceFreezeRequest(
        company_id="company:000001",
        as_of=as_of,
        claim_ids=[bundle.claim.claim_id],
        formal_historical=True,
    )
    frozen = service.freeze_evidence(freeze_request)
    repeated_frozen = service.freeze_evidence(freeze_request)
    assert frozen == repeated_frozen
    assert frozen.pack.coverage_status is ResearchCoverageStatus.COMPLETE

    request = BaseCaseBuildRequest(
        evidence_pack_id=frozen.pack.pack_id,
        draft=_draft(as_of, evidence.evidence_id),
    )
    base = service.build_base_case(request)
    repeated_base = service.build_base_case(request)
    audit = service.audit("company:000001")
    assert base == repeated_base
    assert base.pack.coverage_status is ResearchCoverageStatus.COMPLETE
    assert base.pack.base_confidence == 0.85
    assert base.pack.evidence_ids == [evidence.evidence_id]
    assert audit["status"] == "PASS"
    assert audit["finding_count"] == len(BASE_CASE_SECTIONS)
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM frozen_evidence_pack_index"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM base_case_pack_index"
        ).fetchone()[0] == 1
        safe_metadata = "\n".join(
            str(value)
            for table in ("frozen_evidence_pack_index", "base_case_pack_index")
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert _STATEMENT not in safe_metadata
    assert "Synthetic cited BaseCase" not in safe_metadata


def test_open_conflict_is_preserved_as_blocking_gap_and_caps_confidence(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="conflict",
        conflict=True,
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    assert frozen.pack.open_conflict_ids
    base = service.build_base_case(
        BaseCaseBuildRequest(
            evidence_pack_id=frozen.pack.pack_id,
            draft=_draft(as_of, evidence.evidence_id),
        )
    )
    assert base.pack.coverage_status is ResearchCoverageStatus.INSUFFICIENT
    assert base.pack.confidence_cap == 0.4
    assert base.pack.base_confidence == 0.4
    assert any(
        gap.gap_code.startswith("OPEN_EVIDENCE_CONFLICT:")
        for gap in base.pack.evidence_gaps
    )


def test_critical_finding_cannot_rely_only_on_community_evidence(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="community",
        evidence_grade=EvidenceGrade.COMMUNITY_LEAD,
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    with pytest.raises(ValueError, match="PRIMARY_OFFICIAL"):
        service.build_base_case(
            BaseCaseBuildRequest(
                evidence_pack_id=frozen.pack.pack_id,
                draft=_draft(as_of, evidence.evidence_id),
            )
        )


def test_formal_freeze_rejects_not_pit_safe_and_future_claims(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, _, available = _fixture(
        tmp_path,
        state,
        suffix="not-pit-safe",
        pit_status=PointInTimeStatus.NOT_PIT_SAFE,
    )
    with pytest.raises(ValueError, match="not allowed"):
        service.freeze_evidence(
            EvidenceFreezeRequest(
                company_id="company:000001",
                as_of=available + timedelta(seconds=2),
                claim_ids=[bundle.claim.claim_id],
                formal_historical=True,
            )
        )
    with pytest.raises(ValueError, match="future claim"):
        service.freeze_evidence(
            EvidenceFreezeRequest(
                company_id="company:000001",
                as_of=available,
                claim_ids=[bundle.claim.claim_id],
                formal_historical=False,
            )
        )


def test_base_case_rejects_evidence_outside_frozen_scope(tmp_path: Path, state) -> None:
    service, _, bundle, _, available = _fixture(
        tmp_path,
        state,
        suffix="scope",
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    with pytest.raises(ValueError, match="outside the frozen pack"):
        service.build_base_case(
            BaseCaseBuildRequest(
                evidence_pack_id=frozen.pack.pack_id,
                draft=_draft(as_of, "evidence:not-frozen"),
            )
        )
