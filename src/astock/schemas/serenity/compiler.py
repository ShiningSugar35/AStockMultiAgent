"""Schemas for deterministic canonical-to-Serenity compilation."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import AdjustmentMode, Market
from astock.schemas.serenity_v2 import EstimateRevisionV2, FundamentalGrowthV2


class CanonicalDailyTrendCompileRequest(AStockModel):
    target_company_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=32)
    market: Market
    as_of: AwareDatetime
    requested_start: AwareDatetime
    adjustment_mode: AdjustmentMode = AdjustmentMode.NONE
    daily_evidence_ids: list[str] = Field(min_length=1)
    fundamental_growth: list[FundamentalGrowthV2] = Field(default_factory=list)
    estimate_revisions: list[EstimateRevisionV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_compile_scope(self) -> CanonicalDailyTrendCompileRequest:
        if self.market is Market.INDEX:
            raise ValueError("canonical company daily compile requires an equity exchange market")
        if self.requested_start > self.as_of:
            raise ValueError("canonical daily compile start cannot be after as_of")
        if len(self.daily_evidence_ids) != len(set(self.daily_evidence_ids)):
            raise ValueError("canonical daily evidence ids must be unique")
        return self


class CanonicalFundamentalBinding(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    fundamental_model_bundle_artifact_id: str = Field(min_length=1)
    fundamental_model_bundle_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    valuation_pack_artifact_id: str = Field(min_length=1)
    valuation_pack_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_ids: list[str] = Field(min_length=1)
    source_object_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> CanonicalFundamentalBinding:
        if self.source_artifact_ids != sorted(set(self.source_artifact_ids)):
            raise ValueError("canonical binding artifact ids must be sorted and unique")
        if self.source_object_hashes != sorted(set(self.source_object_hashes)):
            raise ValueError("canonical binding object hashes must be sorted and unique")
        if self.fundamental_model_bundle_artifact_id not in self.source_artifact_ids:
            raise ValueError("canonical binding must include the fundamental model bundle")
        if self.valuation_pack_artifact_id not in self.source_artifact_ids:
            raise ValueError("canonical binding must include the valuation pack")
        return self


__all__ = ["CanonicalDailyTrendCompileRequest", "CanonicalFundamentalBinding"]
