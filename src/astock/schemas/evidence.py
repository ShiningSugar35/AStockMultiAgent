"""Source snapshot contracts shared by market and knowledge ingestion."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class FetchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FETCH_FAILED = "FETCH_FAILED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    PARTIAL = "PARTIAL"


class SourceSnapshot(AStockModel):
    snapshot_id: str
    source_id: str
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fetched_at: AwareDatetime
    available_to_system_at: AwareDatetime
    source_url: str | None = None
    mime: str
    byte_size: int = Field(ge=0)
    headers_hash: str | None = None
    fetch_status: FetchStatus = FetchStatus.SUCCEEDED
    rights_status: str = "LOCAL_RESEARCH"


class EvidenceGrade(StrEnum):
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    PRIVATE_PRIMARY = "PRIVATE_PRIMARY"
    SECONDARY = "SECONDARY"
    COMMUNITY_LEAD = "COMMUNITY_LEAD"


class FactStatus(StrEnum):
    DIRECT = "DIRECT"
    INFERRED = "INFERRED"
    CONFLICTED = "CONFLICTED"
    UNVERIFIED = "UNVERIFIED"


class ClaimType(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    OPINION = "OPINION"


class ClaimStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    CONFLICTED = "CONFLICTED"
    REJECTED = "REJECTED"


class EvidenceRelation(StrEnum):
    SUPPORT = "SUPPORT"
    REFUTE = "REFUTE"
    CONTEXT = "CONTEXT"


class ReviewerStatus(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    AUTO_VALIDATED = "AUTO_VALIDATED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"


class ConflictResolutionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ACCEPTED_UNCERTAINTY = "ACCEPTED_UNCERTAINTY"


class EvidenceLocator(AStockModel):
    locator_type: str = "PAGE_TEXT"
    page_number: int = Field(ge=1)
    section_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    parser_version: str

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceLocator:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class Evidence(AStockModel):
    evidence_id: str
    document_id: str
    snapshot_id: str
    page_id: str
    locator: EvidenceLocator
    excerpt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_grade: EvidenceGrade
    fact_status: FactStatus
    entity_ids: list[str]
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    available_to_system_at: AwareDatetime
    rights_status: str

    @model_validator(mode="after")
    def validate_validity(self) -> Evidence:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        return self


class Claim(AStockModel):
    claim_id: str
    subject_id: str
    predicate: str
    object_json: dict[str, Any]
    as_of: AwareDatetime
    claim_type: ClaimType
    confidence: float = Field(ge=0, le=1)
    status: ClaimStatus


class ClaimEvidenceLink(AStockModel):
    claim_id: str
    evidence_id: str
    relation: EvidenceRelation
    weight: float = Field(default=1.0, ge=0, le=1)
    reviewer_status: ReviewerStatus = ReviewerStatus.UNREVIEWED


class EvidenceAttachment(AStockModel):
    """Input contract for attaching immutable evidence to a new claim."""

    evidence_id: str
    relation: EvidenceRelation
    weight: float = Field(default=1.0, ge=0, le=1)
    reviewer_status: ReviewerStatus = ReviewerStatus.UNREVIEWED


class EvidenceConflict(AStockModel):
    conflict_id: str
    claim_id: str
    evidence_ids: list[str] = Field(min_length=2)
    conflict_type: str
    resolution_status: ConflictResolutionStatus = ConflictResolutionStatus.OPEN
    resolution_note: str | None = None


class ClaimEvidenceBundle(AStockModel):
    claim: Claim
    links: list[ClaimEvidenceLink]
    conflict: EvidenceConflict | None = None
