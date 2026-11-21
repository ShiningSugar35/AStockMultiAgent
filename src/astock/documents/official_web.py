"""Deterministic freezing of Agent-acquired official Web PDF documents."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_policy_gate import SourcePolicyGate
from astock.core.state import StateStore
from astock.documents.repository import DocumentRepository
from astock.schemas import (
    AgentSourceProposal,
    DocumentType,
    FetchStatus,
    OfficialWebDocumentCapture,
    SourceAdmissionStatus,
    SourceClass,
    SourceDocument,
    SourceSnapshot,
)

_ALLOWED_DOCUMENT_CAPABILITIES = frozenset(
    {
        "disclosure.document",
        "financial.official_document",
        "corporate_actions.official_evidence",
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
        capture_id = "official-web-capture:" + content_hash(
            {
                "proposal": proposal.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "document_id": document.document_id,
                "snapshot_id": snapshot.snapshot_id,
                "admission_snapshot_id": admission_snapshot.snapshot_id,
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


def load_official_web_capture(
    state: StateStore, objects: ObjectStore, artifact_id: str, *,
    company_id: str, as_of: datetime,
    capabilities: tuple[str, ...] = ("disclosure.document", "financial.official_document"),
) -> tuple[OfficialWebDocumentCapture, SourceDocument, SourceSnapshot]:
    """Verify exact-item admission, immutable PDF and time without network or writes.

    This proves source lineage. Consumers must still establish the document's
    company/period and the meaning of any extracted statement from the original.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("official document cutoff must be timezone-aware")
    prefix = "OfficialWebDocumentCapture:"
    normalized_id = artifact_id if artifact_id.startswith(prefix) else prefix + artifact_id
    artifact = state.artifact_record(normalized_id)
    if artifact is None or artifact["type"] != "OfficialWebDocumentCapture":
        raise ValueError("official document capture is not registered")
    digest = str(artifact["object_hash"])
    if not objects.verify(digest):
        raise ValueError("official document capture object mismatch")
    payload = json.loads(objects.get_bytes(digest))
    if not isinstance(payload, dict):
        raise ValueError("official document capture payload is invalid")
    payload.pop("pit_id", None)
    capture = OfficialWebDocumentCapture.model_validate(payload)
    if (
        normalized_id != prefix + capture.capture_id
        or capture.source_class is not SourceClass.PRIMARY_OFFICIAL_WEB
        or capture.requested_capability not in capabilities
        or not capture.formal_eligible or capture.exhaustive_proof_allowed
    ):
        raise ValueError("official document capture capability or identity mismatch")
    document = DocumentRepository(state).get_model(capture.document_id)
    if (
        document is None or company_id not in document.company_ids
        or document.publisher != capture.source_id
        or document.source_url != str(capture.source_url)
    ):
        raise ValueError("official document company or source mismatch")
    if document.published_at > capture.observed_at or capture.observed_at > as_of:
        raise ValueError("official document was unavailable at cutoff")
    with state.connect() as connection:
        linked = connection.execute(
            "SELECT 1 FROM document_snapshot WHERE document_id=? AND snapshot_id=?",
            (capture.document_id, capture.snapshot_id),
        ).fetchone()
    if linked is None:
        raise ValueError("official document snapshot is not registered to its document")
    snapshot, admission = _verified_capture_snapshots(state, objects, capture, as_of=as_of)
    if artifact.get("input_hashes") != [snapshot.object_sha256, admission.object_sha256]:
        raise ValueError("official document capture input hashes mismatch")
    _verify_capture_admission(objects, capture, admission)
    return capture, document, snapshot


def _verified_capture_snapshots(
    state: StateStore, objects: ObjectStore, capture: OfficialWebDocumentCapture, *,
    as_of: datetime,
) -> tuple[SourceSnapshot, SourceSnapshot]:
    snapshot = state.get_snapshot(capture.snapshot_id)
    admission = state.get_snapshot(capture.admission_snapshot_id)
    if snapshot is None or admission is None:
        raise ValueError("official document source snapshot is missing")
    if (
        snapshot.source_id != capture.source_id
        or admission.source_id != f"{capture.source_id}:admission"
        or snapshot.object_sha256 != capture.object_sha256
    ):
        raise ValueError("official document source snapshot identity mismatch")
    for item in (snapshot, admission):
        if (
            item.source_url != str(capture.source_url)
            or item.fetch_status is not FetchStatus.SUCCEEDED
            or not objects.verify(item.object_sha256)
        ):
            raise ValueError("official document raw source is unavailable")
        if item.available_to_system_at > as_of:
            raise ValueError("official document raw source was unavailable at cutoff")
        if item.available_to_system_at != capture.observed_at:
            raise ValueError("official document visibility differs from its raw source")
    if not objects.get_bytes(snapshot.object_sha256).lstrip().startswith(b"%PDF-"):
        raise ValueError("official document raw source is not a PDF")
    return snapshot, admission


def _verify_capture_admission(
    objects: ObjectStore, capture: OfficialWebDocumentCapture, admission: SourceSnapshot,
) -> None:
    payload = json.loads(objects.get_bytes(admission.object_sha256))
    if not isinstance(payload, dict):
        raise ValueError("official document admission is invalid")
    expected = {
        "schema_version": "official-web-admission-v1",
        "document_id": capture.document_id,
        "document_snapshot_id": capture.snapshot_id,
        "document_object_sha256": capture.object_sha256,
        "exhaustive_proof_allowed": False,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("official document admission binding mismatch")
    proposal = AgentSourceProposal.model_validate(payload.get("proposal"))
    if (
        proposal.requested_capability != capture.requested_capability
        or proposal.candidate_url != capture.source_url
        or not proposal.formal_use or proposal.require_complete
    ):
        raise ValueError("official document admission proposal mismatch")
    recorded = payload.get("decision")
    expected_decision = {
        "requested_capability": capture.requested_capability,
        "allowed": True, "source_id": capture.source_id, "formal_eligible": True,
        "exhaustive_proof_allowed": False, "admission_status": "ADMIT_AFTER_SNAPSHOT",
    }
    if not isinstance(recorded, dict) or any(
        recorded.get(key) != value for key, value in expected_decision.items()
    ):
        raise ValueError("official document admission decision mismatch")
    decision = SourcePolicyGate().validate(proposal)
    if (
        not decision.allowed or not decision.formal_eligible
        or decision.source_id != capture.source_id
        or decision.source_class is not SourceClass.PRIMARY_OFFICIAL_WEB
    ):
        raise ValueError("official document source is not eligible under current policy")


__all__ = ["OfficialWebDocumentCaptureService", "load_official_web_capture"]
