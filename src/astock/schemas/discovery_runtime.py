"""Contracts for immutable verified discovery Seed releases.

A release contains only ResearchSeed rows already derived from fully satisfied,
evidence-bound DiscoveryThesisResult objects. It is research-only and cannot
authorize recommendations, candidate writes, or trading.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.evidence import ClaimType
from astock.schemas.market import Market
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin

_SHA256 = r"^[0-9a-f]{64}$"
_VERIFIED_MARKER = "VERIFIED_DISCOVERY_THESIS"


class VerifiedDiscoverySeedRelease(AStockModel):
    schema_version: str = "verified-discovery-seed-release-v1"
    release_id: str = Field(min_length=1)
    as_of: AwareDatetime
    active_registry_release_id: str = Field(min_length=1)
    active_registry_object_hash: str = Field(pattern=_SHA256)
    discovery_method_catalog_hash: str = Field(pattern=_SHA256)
    seeds: list[ResearchSeed]
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)
    producer: str = Field(default="verified-discovery-runtime-v1", min_length=1)
    recommendation_allowed: Literal[False] = False
    candidate_record_write_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False

    @field_validator("source_artifact_ids", "source_object_hashes")
    @classmethod
    def validate_sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("verified discovery release lineage must be sorted unique")
        return value

    @model_validator(mode="after")
    def validate_seeds(self) -> VerifiedDiscoverySeedRelease:
        company_ids = [seed.company_id for seed in self.seeds]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("verified discovery release cannot duplicate a company")
        if self.seeds != sorted(
            self.seeds,
            key=lambda item: (-item.research_priority_score, item.company_id),
        ):
            raise ValueError(
                "verified discovery release Seeds must use deterministic priority order"
            )
        for seed in self.seeds:
            if seed.origins != [ResearchSeedOrigin.EXPERT_SKILL]:
                raise ValueError(
                    "verified discovery release accepts discovery-only EXPERT_SKILL Seeds"
                )
            if _VERIFIED_MARKER not in seed.reason_codes:
                raise ValueError("verified discovery release requires the exact verified marker")
            if not any(code.startswith("DISCOVERY_THESIS:") for code in seed.reason_codes):
                raise ValueError("verified discovery release requires a thesis identity")
            if not any(code.startswith("DISCOVERY_CHANNEL:") for code in seed.reason_codes):
                raise ValueError("verified discovery release requires a discovery channel")
            if not any(code.startswith("DISCOVERY_METHOD:") for code in seed.reason_codes):
                raise ValueError("verified discovery release requires a reviewed method")
        return self


class DiscoveryRuntimeContextSpec(AStockModel):
    company_id: str = Field(pattern=r"^[0-9]{6}$")
    market: Market
    name: str = Field(min_length=1)
    industry_id: str = Field(min_length=1)
    business_profile: str = Field(min_length=1)
    industry_label: str | None = None


class DiscoveryRefreshRequest(AStockModel):
    schema_version: str = "discovery-refresh-request-v1"
    as_of: AwareDatetime
    contexts: list[DiscoveryRuntimeContextSpec]
    max_verified_discovery: int = Field(default=8, ge=0, le=8)
    execute_existing_recovery: bool = True
    recovery_budget_seconds: int = Field(default=300, ge=60, le=1800)
    recovery_max_companies: int = Field(default=12, ge=1, le=12)

    @model_validator(mode="after")
    def validate_contexts(self) -> DiscoveryRefreshRequest:
        identities = [(item.market.value, item.company_id) for item in self.contexts]
        if identities != sorted(set(identities)):
            raise ValueError("discovery refresh contexts must be sorted and unique")
        return self


class DiscoveryConditionClaimAdmissionRequest(AStockModel):
    schema_version: str = "discovery-condition-claim-admission-v1"
    as_of: AwareDatetime
    company_id: str = Field(pattern=r"^[0-9]{6}$")
    industry_id: str = Field(min_length=1)
    channel: Literal["BOTTLENECK", "EVENT", "CYCLE"]
    role: str = Field(min_length=1)
    state: Literal["SATISFIED", "NOT_SATISFIED"]
    subject_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    claim_type: ClaimType = ClaimType.FACT
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not item.strip() for item in value):
            raise ValueError("discovery evidence ids must be non-empty sorted unique")
        return value


__all__ = [
    "DiscoveryConditionClaimAdmissionRequest",
    "DiscoveryRefreshRequest",
    "DiscoveryRuntimeContextSpec",
    "VerifiedDiscoverySeedRelease",
]
