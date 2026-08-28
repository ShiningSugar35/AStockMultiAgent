"""Research-seed contracts for low-cost market and expert-domain discovery."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import Market

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ResearchSeedStatus(StrEnum):
    READY = "READY"
    EMPTY = "EMPTY"
    NEEDS_INFO = "NEEDS_INFO"


class ResearchUniverseCoverageStatus(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ResearchSeedOrigin(StrEnum):
    EXISTING_CANDIDATE = "EXISTING_CANDIDATE"
    MARKET = "MARKET"
    EXPERT_SKILL = "EXPERT_SKILL"


class ExpertDomainEvidence(AStockModel):
    board_code: str = Field(min_length=1)
    board_name: str = Field(min_length=1)
    matched_skill_count: int = Field(ge=1)
    author_skill_count: int = Field(ge=1)
    skill_share: float = Field(gt=0, le=1, allow_inf_nan=False)
    support_skill_ids: list[str] = Field(min_length=1)

    @field_validator("support_skill_ids")
    @classmethod
    def validate_support(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("expert-domain support skills must be sorted and unique")
        return value


class ExpertDomainProfile(AStockModel):
    author_source_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    registry_release_id: str = Field(min_length=1)
    total_admitted_skill_count: int = Field(ge=1)
    domains: list[ExpertDomainEvidence]
    profile_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    board_taxonomy_source: Literal["EASTMONEY_PUBLIC_INDUSTRY_BOARD"] = (
        "EASTMONEY_PUBLIC_INDUSTRY_BOARD"
    )
    formal_company_fact_allowed: Literal[False] = False
    recommendation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_domains(self) -> ExpertDomainProfile:
        identities = [item.board_code for item in self.domains]
        if len(identities) != len(set(identities)):
            raise ValueError("expert-domain board codes must be unique")
        if any(item.author_skill_count != self.total_admitted_skill_count for item in self.domains):
            raise ValueError("expert-domain author skill counts must reconcile")
        return self


class ResearchSeed(AStockModel):
    seed_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    name: str = Field(min_length=1)
    origins: list[ResearchSeedOrigin] = Field(min_length=1)
    research_priority_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    market_liquidity_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    current_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    amount_cny: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    turnover_rate: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    float_market_cap_cny: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    candidate_version_id: str | None = None
    candidate_strength: str | None = None
    expert_author_source_ids: list[str] = Field(default_factory=list)
    expert_domain_names: list[str] = Field(default_factory=list)
    expert_domain_support_skill_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    requires_candidate_evidence: Literal[True] = True
    requires_deep_research: Literal[True] = True
    recommendation_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator(
        "origins",
        "expert_author_source_ids",
        "expert_domain_names",
        "expert_domain_support_skill_ids",
        "reason_codes",
        "source_snapshot_ids",
    )
    @classmethod
    def validate_sets(cls, value: list[object]) -> list[object]:
        if value != sorted(set(value), key=str):
            raise ValueError("research-seed list values must be sorted and unique")
        return value


class ResearchSeedRequest(AStockModel):
    schema_version: str = "research-seed-request-v1"
    as_of: AwareDatetime
    max_total_seeds: int = Field(default=40, ge=5, le=100)
    max_market_seeds: int = Field(default=20, ge=0, le=60)
    max_expert_seeds_per_author: int = Field(default=10, ge=0, le=30)
    market_fetch_workers: int = Field(default=2, ge=1, le=3)
    max_domains_per_author: int = Field(default=5, ge=1, le=10)
    minimum_domain_skill_count: int = Field(default=3, ge=2, le=50)
    expert_overlay_max_priority_bonus: float = Field(default=0.15, ge=0, le=0.25)
    minimum_amount_cny: float = Field(default=20_000_000.0, ge=0)
    minimum_float_market_cap_cny: float = Field(default=2_000_000_000.0, ge=0)
    include_existing_candidates: bool = True
    live: bool = False


class ResearchSeedReport(AStockModel):
    schema_version: str = "research-seed-report-v1"
    report_id: str = Field(min_length=1)
    as_of: AwareDatetime
    data_cutoff_at: AwareDatetime
    status: ResearchSeedStatus
    registry_release_id: str | None = None
    registry_release_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    profiles: list[ExpertDomainProfile]
    seeds: list[ResearchSeed]
    source_snapshot_ids: list[str]
    source_object_hashes: list[str]
    warning_codes: list[str]
    market_coverage_ratios: dict[Market, float] = Field(default_factory=dict)
    universe_coverage_status: ResearchUniverseCoverageStatus = (
        ResearchUniverseCoverageStatus.UNAVAILABLE
    )
    formal_full_market_coverage_allowed: bool = False
    market_seed_count: int = Field(ge=0)
    expert_seed_count: int = Field(ge=0)
    existing_candidate_seed_count: int = Field(ge=0)
    recommendation_allowed: Literal[False] = False
    candidate_record_write_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator("source_snapshot_ids", "source_object_hashes", "warning_codes")
    @classmethod
    def validate_sorted_sets(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("research-seed report lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> ResearchSeedReport:
        market = sum(ResearchSeedOrigin.MARKET in item.origins for item in self.seeds)
        expert = sum(ResearchSeedOrigin.EXPERT_SKILL in item.origins for item in self.seeds)
        existing = sum(ResearchSeedOrigin.EXISTING_CANDIDATE in item.origins for item in self.seeds)
        if (market, expert, existing) != (
            self.market_seed_count,
            self.expert_seed_count,
            self.existing_candidate_seed_count,
        ):
            raise ValueError("research-seed origin counts do not reconcile")
        if self.status is ResearchSeedStatus.READY and not self.seeds:
            raise ValueError("READY research-seed report requires at least one seed")
        if self.status is ResearchSeedStatus.EMPTY and self.seeds:
            raise ValueError("EMPTY research-seed report cannot contain seeds")
        if any(value < 0 or value > 1 for value in self.market_coverage_ratios.values()):
            raise ValueError("market coverage ratios must stay in [0,1]")
        equity_markets = {Market.XSHG, Market.XSHE, Market.BJSE}
        full = (
            set(self.market_coverage_ratios) == equity_markets
            and all(value >= 0.995 for value in self.market_coverage_ratios.values())
        )
        expected_status = (
            ResearchUniverseCoverageStatus.FULL
            if full
            else (
                ResearchUniverseCoverageStatus.PARTIAL
                if self.market_coverage_ratios
                else ResearchUniverseCoverageStatus.UNAVAILABLE
            )
        )
        if self.universe_coverage_status is not expected_status:
            raise ValueError("research-seed universe coverage status does not match ratios")
        if self.status is ResearchSeedStatus.EMPTY and not full:
            raise ValueError("EMPTY research-seed status requires a proven FULL universe")
        if self.status is ResearchSeedStatus.NEEDS_INFO and full and not self.seeds:
            raise ValueError("proven FULL zero-result reports must use EMPTY status")
        if self.formal_full_market_coverage_allowed != full:
            raise ValueError("formal full-market coverage authority must match >=99.5% per market")
        return self


__all__ = [
    "ExpertDomainEvidence",
    "ExpertDomainProfile",
    "ResearchSeed",
    "ResearchSeedOrigin",
    "ResearchSeedReport",
    "ResearchSeedRequest",
    "ResearchSeedStatus",
    "ResearchUniverseCoverageStatus",
]
