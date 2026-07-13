"""Immutable page evidence and point-in-time-safe claims."""

from __future__ import annotations

from datetime import datetime

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.repository import DocumentRepository
from astock.evidence.repository import EvidenceRepository
from astock.schemas import (
    Claim,
    ClaimEvidenceBundle,
    ClaimEvidenceLink,
    ClaimStatus,
    ClaimType,
    ConflictResolutionStatus,
    Evidence,
    EvidenceAttachment,
    EvidenceConflict,
    EvidenceGrade,
    EvidenceLocator,
    EvidenceRelation,
    FactStatus,
)


class ClaimEvidenceService:
    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        pages: DocumentPageRepository,
        documents: DocumentRepository,
        repository: EvidenceRepository,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.pages = pages
        self.documents = documents
        self.repository = repository

    def create_page_evidence(
        self,
        *,
        page_id: str,
        char_start: int,
        char_end: int,
        evidence_grade: EvidenceGrade,
        fact_status: FactStatus,
        entity_ids: list[str],
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Evidence:
        page = self.pages.get_page_by_id(page_id)
        if page is None:
            raise ValueError(f"Unknown parsed page: {page_id}")
        snapshot = self.documents.snapshot(page.snapshot_id)
        document = self.documents.get_model(page.document_id)
        if snapshot is None or document is None:
            raise ValueError(f"Incomplete source lineage for page: {page_id}")
        text = self.object_store.get_bytes(page.text_object_sha256).decode("utf-8")
        if char_start < 0 or char_end <= char_start or char_end > len(text):
            raise ValueError(f"Evidence range must be within 0..{len(text)}")
        excerpt = text[char_start:char_end]
        if not excerpt.strip():
            raise ValueError("Evidence excerpt must contain visible text")
        excerpt_ref = self.object_store.put_bytes(excerpt.encode("utf-8"))
        locator = EvidenceLocator(
            page_number=page.page_number,
            section_path=page.section_path,
            char_start=char_start,
            char_end=char_end,
            parser_version=page.parser_version,
        )
        identity = {
            "document_id": page.document_id,
            "snapshot_id": page.snapshot_id,
            "page_id": page.page_id,
            "locator": {
                "locator_type": locator.locator_type,
                "page_number": locator.page_number,
                "section_path": locator.section_path,
                "char_start": locator.char_start,
                "char_end": locator.char_end,
                "parser_version": locator.parser_version,
            },
            "excerpt_sha256": excerpt_ref.sha256,
            "evidence_grade": evidence_grade,
            "fact_status": fact_status,
            "entity_ids": sorted(set(entity_ids)),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "available_to_system_at": snapshot.available_to_system_at,
            "rights_status": document.rights_status,
        }
        evidence_id = f"evidence:{sha256_bytes(canonical_json_bytes(identity))}"
        existing = self.repository.get_evidence(evidence_id)
        if existing is not None:
            self._register_evidence_artifact(existing)
            return existing
        evidence = Evidence(
            evidence_id=evidence_id,
            document_id=page.document_id,
            snapshot_id=page.snapshot_id,
            page_id=page.page_id,
            locator=locator,
            excerpt_sha256=excerpt_ref.sha256,
            excerpt_object_sha256=excerpt_ref.sha256,
            evidence_grade=evidence_grade,
            fact_status=fact_status,
            entity_ids=sorted(set(entity_ids)),
            valid_from=valid_from,
            valid_to=valid_to,
            available_to_system_at=snapshot.available_to_system_at,
            rights_status=document.rights_status,
        )
        stored = self.repository.register_evidence(evidence)
        self._register_evidence_artifact(stored)
        return stored

    def create_claim(
        self,
        *,
        subject_id: str,
        predicate: str,
        object_json: dict[str, object],
        as_of: datetime,
        claim_type: ClaimType,
        confidence: float,
        status: ClaimStatus,
        attachments: list[EvidenceAttachment],
    ) -> ClaimEvidenceBundle:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        if not attachments:
            raise ValueError("A claim requires at least one evidence attachment")
        attachment_keys = [(item.evidence_id, item.relation.value) for item in attachments]
        if len(attachment_keys) != len(set(attachment_keys)):
            raise ValueError("Duplicate evidence attachment")
        relations_by_evidence: dict[str, set[EvidenceRelation]] = {}
        evidence_by_id: dict[str, Evidence] = {}
        for attachment in attachments:
            relations_by_evidence.setdefault(attachment.evidence_id, set()).add(
                attachment.relation
            )
            evidence = self.repository.get_evidence(attachment.evidence_id)
            if evidence is None:
                raise ValueError(f"Unknown evidence: {attachment.evidence_id}")
            self._assert_evidence_available(evidence, as_of)
            evidence_by_id[evidence.evidence_id] = evidence
        for evidence_id, relations in relations_by_evidence.items():
            if EvidenceRelation.SUPPORT in relations and EvidenceRelation.REFUTE in relations:
                raise ValueError(f"One evidence cannot both support and refute: {evidence_id}")

        support_ids = sorted(
            {item.evidence_id for item in attachments if item.relation is EvidenceRelation.SUPPORT}
        )
        refute_ids = sorted(
            {item.evidence_id for item in attachments if item.relation is EvidenceRelation.REFUTE}
        )
        has_conflict = bool(support_ids and refute_ids)
        final_status = ClaimStatus.CONFLICTED if has_conflict else status
        normalized_attachments = sorted(
            [
                {
                    "evidence_id": item.evidence_id,
                    "relation": item.relation,
                    "weight": item.weight,
                    "reviewer_status": item.reviewer_status,
                }
                for item in attachments
            ],
            key=lambda item: (str(item["evidence_id"]), str(item["relation"])),
        )
        identity = {
            "subject_id": subject_id,
            "predicate": predicate,
            "object_json": object_json,
            "as_of": as_of,
            "claim_type": claim_type,
            "confidence": confidence,
            "status": final_status,
            "attachments": normalized_attachments,
        }
        claim_id = f"claim:{sha256_bytes(canonical_json_bytes(identity))}"
        existing = self.repository.get_claim_bundle(claim_id)
        if existing is not None:
            self._register_claim_artifact(existing)
            return existing

        claim = Claim(
            claim_id=claim_id,
            subject_id=subject_id,
            predicate=predicate,
            object_json=object_json,
            as_of=as_of,
            claim_type=claim_type,
            confidence=confidence,
            status=final_status,
        )
        links = [
            ClaimEvidenceLink(
                claim_id=claim_id,
                evidence_id=item.evidence_id,
                relation=item.relation,
                weight=item.weight,
                reviewer_status=item.reviewer_status,
            )
            for item in sorted(
                attachments,
                key=lambda item: (item.evidence_id, item.relation.value),
            )
        ]
        conflict = None
        if has_conflict:
            conflict_ids = sorted(set(support_ids + refute_ids))
            conflict_id = "conflict:" + sha256_bytes(
                canonical_json_bytes(
                    {"claim_id": claim_id, "evidence_ids": conflict_ids, "type": "SUPPORT_REFUTE"}
                )
            )
            conflict = EvidenceConflict(
                conflict_id=conflict_id,
                claim_id=claim_id,
                evidence_ids=conflict_ids,
                conflict_type="SUPPORT_REFUTE",
                resolution_status=ConflictResolutionStatus.OPEN,
            )
        bundle = ClaimEvidenceBundle(
            claim=claim,
            links=links,
            conflict=conflict,
            created_at=claim.created_at,
        )
        stored = self.repository.register_claim_bundle(bundle)
        self._register_claim_artifact(stored)
        return stored

    @staticmethod
    def _assert_evidence_available(evidence: Evidence, as_of: datetime) -> None:
        if evidence.available_to_system_at > as_of:
            raise ValueError(
                f"Future evidence is unavailable at claim as_of: {evidence.evidence_id}"
            )
        if evidence.valid_from is not None and evidence.valid_from > as_of:
            raise ValueError(f"Evidence is not yet valid at claim as_of: {evidence.evidence_id}")
        if evidence.valid_to is not None and evidence.valid_to < as_of:
            raise ValueError(f"Evidence is no longer valid at claim as_of: {evidence.evidence_id}")

    def _register_evidence_artifact(self, evidence: Evidence) -> None:
        artifact = self.object_store.put_json(evidence.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"Evidence:{evidence.evidence_id}",
            artifact_type="Evidence",
            schema_version=evidence.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[evidence.page_id, evidence.excerpt_object_sha256],
        )

    def _register_claim_artifact(self, bundle: ClaimEvidenceBundle) -> None:
        artifact = self.object_store.put_json(bundle.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"ClaimEvidenceBundle:{bundle.claim.claim_id}",
            artifact_type="ClaimEvidenceBundle",
            schema_version=bundle.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[link.evidence_id for link in bundle.links],
        )
