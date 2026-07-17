"""SQLite metadata repository for the Claim--Evidence graph."""

from __future__ import annotations

from datetime import datetime

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.state import StateStore
from astock.schemas import (
    Claim,
    ClaimEvidenceBundle,
    ClaimEvidenceLink,
    Evidence,
    EvidenceConflict,
)


class EvidenceRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM evidence_record WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        return Evidence.model_validate_json(row["evidence_json"]) if row else None

    def register_evidence(self, evidence: Evidence) -> Evidence:
        evidence_json = canonical_json_bytes(evidence.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM evidence_record WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if row is not None:
                existing = Evidence.model_validate_json(row["evidence_json"])
                if content_hash(existing) != content_hash(evidence):
                    raise ValueError(f"Evidence identity collision: {evidence.evidence_id}")
                return existing
            connection.execute(
                "INSERT INTO evidence_record(evidence_id,document_id,snapshot_id,"
                "source_unit_type,source_unit_index,page_id,block_id,excerpt_object_hash,"
                "excerpt_sha256,available_to_system_at,evidence_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence.evidence_id,
                    evidence.document_id,
                    evidence.snapshot_id,
                    "PAGE" if evidence.page_id is not None else "BLOCK",
                    (
                        evidence.locator.page_number
                        if evidence.page_id is not None
                        else evidence.locator.block_index
                    ),
                    evidence.page_id,
                    evidence.block_id,
                    evidence.excerpt_object_sha256,
                    evidence.excerpt_sha256,
                    evidence.available_to_system_at.isoformat(),
                    evidence_json,
                    evidence.created_at.isoformat(),
                ),
            )
        return evidence

    def get_claim_bundle(self, claim_id: str) -> ClaimEvidenceBundle | None:
        with self.state.connect() as connection:
            claim_row = connection.execute(
                "SELECT claim_json FROM claim_record WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if claim_row is None:
                return None
            link_rows = connection.execute(
                "SELECT link_json FROM claim_evidence_link WHERE claim_id=? "
                "ORDER BY evidence_id,relation",
                (claim_id,),
            ).fetchall()
            conflict_row = connection.execute(
                "SELECT conflict_json FROM evidence_conflict WHERE claim_id=? "
                "ORDER BY conflict_id LIMIT 1",
                (claim_id,),
            ).fetchone()
        return ClaimEvidenceBundle(
            claim=Claim.model_validate_json(claim_row["claim_json"]),
            links=[ClaimEvidenceLink.model_validate_json(row["link_json"]) for row in link_rows],
            conflict=(
                EvidenceConflict.model_validate_json(conflict_row["conflict_json"])
                if conflict_row
                else None
            ),
            created_at=Claim.model_validate_json(claim_row["claim_json"]).created_at,
        )

    def claim_bundles_for_subject(
        self,
        subject_id: str,
        *,
        as_of: datetime,
    ) -> list[ClaimEvidenceBundle]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT claim_id,claim_json FROM claim_record WHERE subject_id=? "
                "ORDER BY as_of,claim_id",
                (subject_id,),
            ).fetchall()
        claim_ids = [
            str(row["claim_id"])
            for row in rows
            if Claim.model_validate_json(row["claim_json"]).as_of <= as_of
        ]
        bundles = [self.get_claim_bundle(claim_id) for claim_id in claim_ids]
        return [bundle for bundle in bundles if bundle is not None]

    def register_claim_bundle(self, bundle: ClaimEvidenceBundle) -> ClaimEvidenceBundle:
        with self.state.transaction() as connection:
            existing_row = connection.execute(
                "SELECT claim_json FROM claim_record WHERE claim_id=?",
                (bundle.claim.claim_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._bundle_with_connection(connection, bundle.claim.claim_id)
                if content_hash(existing) != content_hash(bundle):
                    raise ValueError(f"Claim identity collision: {bundle.claim.claim_id}")
                return existing

            evidence_ids = sorted({link.evidence_id for link in bundle.links})
            placeholders = ",".join("?" for _ in evidence_ids)
            found = {
                str(row["evidence_id"])
                for row in connection.execute(
                    "SELECT evidence_id FROM evidence_record "
                    f"WHERE evidence_id IN ({placeholders})",
                    evidence_ids,
                ).fetchall()
            }
            missing = sorted(set(evidence_ids) - found)
            if missing:
                raise ValueError(f"Unknown evidence: {', '.join(missing)}")

            claim = bundle.claim
            claim_json = canonical_json_bytes(claim.model_dump(mode="json")).decode("utf-8")
            connection.execute(
                "INSERT INTO claim_record(claim_id,subject_id,predicate,as_of,claim_type,"
                "confidence,status,claim_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    claim.claim_id,
                    claim.subject_id,
                    claim.predicate,
                    claim.as_of.isoformat(),
                    claim.claim_type.value,
                    claim.confidence,
                    claim.status.value,
                    claim_json,
                    claim.created_at.isoformat(),
                ),
            )
            for link in bundle.links:
                link_json = canonical_json_bytes(link.model_dump(mode="json")).decode("utf-8")
                connection.execute(
                    "INSERT INTO claim_evidence_link(claim_id,evidence_id,relation,weight,"
                    "reviewer_status,link_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        link.claim_id,
                        link.evidence_id,
                        link.relation.value,
                        link.weight,
                        link.reviewer_status.value,
                        link_json,
                        link.created_at.isoformat(),
                    ),
                )
            if bundle.conflict is not None:
                conflict = bundle.conflict
                conflict_json = canonical_json_bytes(conflict.model_dump(mode="json")).decode(
                    "utf-8"
                )
                connection.execute(
                    "INSERT INTO evidence_conflict(conflict_id,claim_id,evidence_ids_json,"
                    "conflict_type,resolution_status,resolution_note,conflict_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        conflict.conflict_id,
                        conflict.claim_id,
                        canonical_json_bytes(conflict.evidence_ids).decode("utf-8"),
                        conflict.conflict_type,
                        conflict.resolution_status.value,
                        conflict.resolution_note,
                        conflict_json,
                        conflict.created_at.isoformat(),
                    ),
                )
        return bundle

    @staticmethod
    def _bundle_with_connection(connection, claim_id: str) -> ClaimEvidenceBundle:
        claim_row = connection.execute(
            "SELECT claim_json FROM claim_record WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if claim_row is None:  # pragma: no cover - caller proves existence in one transaction
            raise ValueError(f"Unknown claim: {claim_id}")
        links = connection.execute(
            "SELECT link_json FROM claim_evidence_link WHERE claim_id=? "
            "ORDER BY evidence_id,relation",
            (claim_id,),
        ).fetchall()
        conflict = connection.execute(
            "SELECT conflict_json FROM evidence_conflict WHERE claim_id=? "
            "ORDER BY conflict_id LIMIT 1",
            (claim_id,),
        ).fetchone()
        claim = Claim.model_validate_json(claim_row["claim_json"])
        return ClaimEvidenceBundle(
            claim=claim,
            links=[ClaimEvidenceLink.model_validate_json(row["link_json"]) for row in links],
            conflict=(
                EvidenceConflict.model_validate_json(conflict["conflict_json"])
                if conflict
                else None
            ),
            created_at=claim.created_at,
        )
