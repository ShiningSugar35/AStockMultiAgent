"""Deterministic freezing of Agent-acquired official Web PDF documents."""

from __future__ import annotations

from datetime import UTC, date, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_policy_gate import SourcePolicyGate
from astock.core.state import StateStore
from astock.documents.repository import DocumentRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AgentSourceProposal,
    AvailabilityBasis,
    DocumentType,
    FetchStatus,
    OfficialWebDocumentCapture,
    PointInTimeStatus,
    SourceAdmissionStatus,
    SourceDocument,
    SourceSnapshot,
)

_ALLOWED_DOCUMENT_CAPABILITIES = frozenset(
    {
        "disclosure.document",
        "financial.official_document",
    }
)


class OfficialWebDocumentCaptureService:
    """Freeze a locally acquired official PDF after deterministic source admission."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        gate: SourcePolicyGate | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.gate = gate or SourcePolicyGate()
        self.documents = DocumentRepository(state)
        self.pit = PointInTimeService(PointInTimeRepository(state), state, objects)

    def capture(
        self,
        proposal: AgentSourceProposal,
        content: bytes,
        *,
        title: str,
        company_ids: list[str],
        published_at: datetime,
        document_type: DocumentType,
        disclosure_id: str | None = None,
        effective_at: datetime | None = None,
        period_end: date | None = None,
        observed_at: datetime | None = None,
    ) -> OfficialWebDocumentCapture:
        if proposal.requested_capability not in _ALLOWED_DOCUMENT_CAPABILITIES:
            raise ValueError("Official Web capture requires a document capability")
        if not proposal.formal_use or proposal.require_complete:
            raise ValueError("Official Web document capture requires bounded formal exact-item use")
        decision = self.gate.validate(proposal)
        if (
            not decision.allowed
            or not decision.formal_eligible
            or decision.source_id is None
            or decision.admission_status is not SourceAdmissionStatus.ADMIT_AFTER_SNAPSHOT
        ):
            raise ValueError("Official Web source proposal is not eligible for document capture")
        if proposal.candidate_url is None:
            raise ValueError("Official Web document capture requires an exact URL")
        if not content.lstrip().startswith(b"%PDF-"):
            raise ValueError("Official Web document content is not a PDF")
        if (
            not title.strip()
            or not company_ids
            or any(
                len(item.strip()) != 6 or not item.strip().isdigit()
                for item in company_ids
            )
        ):
            raise ValueError("Official Web document identity is incomplete")

        observed = observed_at or datetime.now(UTC)
        for label, timestamp in {
            "published_at": published_at,
            "effective_at": effective_at,
            "observed_at": observed,
        }.items():
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError(f"{label} must be timezone-aware")
        observed = observed.astimezone(UTC)
        if published_at.astimezone(UTC) > observed:
            raise ValueError("Official Web document cannot be observed before publication")
        source_url = str(proposal.candidate_url)
        object_ref = self.objects.put_bytes(content)
        resolved_disclosure_id = disclosure_id or content_hash({"source_url": source_url})
        document_key = content_hash(
            {
                "source_id": decision.source_id,
                "disclosure_id": resolved_disclosure_id,
                "source_url": source_url,
            }
        )
        document_id = f"official-web:{decision.source_id}:{document_key}"
        snapshot_key = content_hash(
            {
                "document_id": document_id,
                "object_sha256": object_ref.sha256,
                "observed_at": observed.isoformat(),
            }
        )
        snapshot = SourceSnapshot(
            created_at=observed,
            snapshot_id=f"official-web:{decision.source_id}:document:{snapshot_key}",
            source_id=decision.source_id,
            object_sha256=object_ref.sha256,
            fetched_at=observed,
            available_to_system_at=observed,
            source_url=source_url,
            mime="application/pdf",
            byte_size=object_ref.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_OFFICIAL_WEB",
        )
        self.state.register_snapshot(snapshot)
        admission_payload = {
            "schema_version": "official-web-admission-v1",
            "proposal": proposal.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "document_id": document_id,
            "document_snapshot_id": snapshot.snapshot_id,
            "document_object_sha256": object_ref.sha256,
            "observed_at": observed.isoformat(),
            "exhaustive_proof_allowed": False,
        }
        admission_ref = self.objects.put_json(admission_payload)
        admission_key = content_hash(
            {
                "document_id": document_id,
                "document_snapshot_id": snapshot.snapshot_id,
                "admission_object_sha256": admission_ref.sha256,
            }
        )
        admission_snapshot = SourceSnapshot(
            created_at=observed,
            snapshot_id=f"official-web:{decision.source_id}:admission:{admission_key}",
            source_id=f"{decision.source_id}:admission",
            object_sha256=admission_ref.sha256,
            fetched_at=observed,
            available_to_system_at=observed,
            source_url=source_url,
            mime="application/json",
            byte_size=admission_ref.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_OFFICIAL_WEB_ADMISSION",
        )
        self.state.register_snapshot(admission_snapshot)
        document = SourceDocument(
            created_at=observed,
            document_id=document_id,
            title=title.strip(),
            publisher=decision.source_id,
            document_type=document_type,
            company_ids=list(dict.fromkeys(item.strip() for item in company_ids)),
            published_at=published_at,
            effective_at=effective_at or published_at,
            disclosure_id=resolved_disclosure_id,
            source_url=source_url,
            rights_status="PUBLIC_OFFICIAL_WEB",
        )
        self.documents.register(document, snapshot)
        canonical_snapshot = self.documents.snapshot(snapshot.snapshot_id)
        if canonical_snapshot is None or not self.objects.verify(canonical_snapshot.object_sha256):
            raise ValueError("Official Web snapshot registration is incomplete")
        pit = self.pit.create(
            source_id=f"{document.document_id}:{snapshot.snapshot_id}",
            source_document_id=document.document_id,
            source_snapshot_id=snapshot.snapshot_id,
            period_end=period_end,
            published_at=document.published_at,
            effective_at=document.effective_at,
            ingested_at=snapshot.fetched_at,
            available_to_system_at=snapshot.available_to_system_at,
            point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            availability_basis=AvailabilityBasis.FETCH_OBSERVED,
        )
        capture_id = "official-web-capture:" + content_hash(
            {
                "proposal": proposal.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "document_id": document.document_id,
                "snapshot_id": snapshot.snapshot_id,
                "admission_snapshot_id": admission_snapshot.snapshot_id,
                "pit_id": pit.pit_id,
                "object_sha256": object_ref.sha256,
            }
        )
        capture = OfficialWebDocumentCapture(
            capture_id=capture_id,
            requested_capability=proposal.requested_capability,
            source_id=decision.source_id,
            source_class=decision.source_class,
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            admission_snapshot_id=admission_snapshot.snapshot_id,
            pit_id=pit.pit_id,
            source_url=proposal.candidate_url,
            object_sha256=object_ref.sha256,
            observed_at=observed,
            policy_reason_codes=decision.reason_codes,
        )
        artifact = self.objects.put_json(capture.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"OfficialWebDocumentCapture:{capture.capture_id}",
            artifact_type="OfficialWebDocumentCapture",
            schema_version=capture.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[snapshot.object_sha256, admission_snapshot.object_sha256],
        )
        return capture


__all__ = ["OfficialWebDocumentCaptureService"]
