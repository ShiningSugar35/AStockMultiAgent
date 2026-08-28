"""Agent source proposals and deterministic web/search admission contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl

from astock.schemas.base import AStockModel
from astock.schemas.market import SourceClass


class SourceAdmissionStatus(StrEnum):
    REJECTED = "REJECTED"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    ADMIT_AFTER_SNAPSHOT = "ADMIT_AFTER_SNAPSHOT"


class AgentSourceProposal(AStockModel):
    requested_capability: str = Field(min_length=1)
    query: str = Field(min_length=1)
    candidate_url: HttpUrl | None = None
    expected_fact: str = Field(min_length=1)
    preferred_source_class: SourceClass
    formal_use: bool = False
    require_complete: bool = False
    reason: str = Field(min_length=1)


class SourcePolicyDecision(AStockModel):
    requested_capability: str
    allowed: bool
    source_id: str | None = None
    domain: str | None = None
    source_class: SourceClass
    formal_eligible: bool = False
    exhaustive_proof_allowed: bool = False
    admission_status: SourceAdmissionStatus
    independence_group: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class OfficialWebDocumentCapture(AStockModel):
    capture_id: str
    requested_capability: str
    source_id: str
    source_class: SourceClass
    document_id: str
    snapshot_id: str
    admission_snapshot_id: str
    pit_id: str
    source_url: HttpUrl
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    policy_reason_codes: list[str] = Field(default_factory=list)
    formal_eligible: Literal[True] = True
    exhaustive_proof_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "AgentSourceProposal",
    "OfficialWebDocumentCapture",
    "SourceAdmissionStatus",
    "SourcePolicyDecision",
]
