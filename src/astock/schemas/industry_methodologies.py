"""Public-core industry methodology contracts.

These models describe reusable analysis methods only. They never certify company facts,
industry taxonomy, recommendations, portfolio weights, or execution authority.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, HttpUrl, field_validator, model_validator

from astock.schemas.base import AStockModel


class IndustryMethodologySourceKind(StrEnum):
    PROFESSIONAL_STANDARD = "PROFESSIONAL_STANDARD"
    REGULATOR = "REGULATOR"
    INDUSTRY_STANDARD = "INDUSTRY_STANDARD"
    ACADEMIC_DATA = "ACADEMIC_DATA"
    INTERGOVERNMENTAL = "INTERGOVERNMENTAL"


class IndustryMethodologySource(AStockModel):
    schema_version: str = "industry-methodology-source-v1"
    source_id: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    source_kind: IndustryMethodologySourceKind
    methodology_only: Literal[True] = True
    fact_authority_allowed: Literal[False] = False
    recommendation_allowed: Literal[False] = False


class IndustryMethodologySkill(AStockModel):
    schema_version: str = "industry-methodology-skill-v1"
    methodology_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    archetype_ids: list[str] = Field(default_factory=list)
    generic_fallback: bool = False
    source_ids: list[str] = Field(min_length=1)
    analysis_questions: list[str] = Field(min_length=1)
    operating_metrics: list[str] = Field(min_length=1)
    valuation_lenses: list[str] = Field(min_length=1)
    red_flags: list[str] = Field(min_length=1)
    private_skill_required_for_analysis: Literal[False] = False
    specialist_draft_required: Literal[False] = False
    fact_authority_allowed: Literal[False] = False
    recommendation_allowed: Literal[False] = False

    @field_validator("archetype_ids", "source_ids")
    @classmethod
    def sorted_unique_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("industry methodology ids must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> IndustryMethodologySkill:
        if self.generic_fallback == bool(self.archetype_ids):
            raise ValueError(
                "generic methodology must have no archetypes and sector methodology "
                "must have archetypes"
            )
        return self


class IndustryMethodologyBundle(AStockModel):
    schema_version: str = "industry-methodology-bundle-v1"
    query: str = Field(min_length=1)
    status: Literal["MATCHED", "ARCHETYPE_FALLBACK", "GENERIC_FALLBACK"]
    archetype_id: str | None = None
    methodology_ids: list[str] = Field(min_length=1)
    methodologies: list[IndustryMethodologySkill] = Field(min_length=1)
    sources: list[IndustryMethodologySource] = Field(min_length=1)
    private_skill_required_for_analysis: Literal[False] = False
    specialist_draft_required: Literal[False] = False
    fact_authority_allowed: Literal[False] = False
    recommendation_allowed: Literal[False] = False

    @field_validator("methodology_ids")
    @classmethod
    def sorted_unique_methodologies(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("resolved methodology ids must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_bundle(self) -> IndustryMethodologyBundle:
        ids = [item.methodology_id for item in self.methodologies]
        if sorted(ids) != self.methodology_ids or len(ids) != len(set(ids)):
            raise ValueError("industry methodology bundle ids do not reconcile")
        if self.status in {"MATCHED", "ARCHETYPE_FALLBACK"} and self.archetype_id is None:
            raise ValueError("archetype-aware industry methodology requires an archetype")
        if self.status == "GENERIC_FALLBACK" and self.archetype_id is not None:
            raise ValueError("generic fallback cannot claim an archetype")
        if self.status == "ARCHETYPE_FALLBACK" and len(self.methodologies) != 1:
            raise ValueError("archetype fallback must use only the generic core methodology")
        source_ids = {item.source_id for item in self.sources}
        required_source_ids = {
            source_id for item in self.methodologies for source_id in item.source_ids
        }
        if source_ids != required_source_ids:
            raise ValueError("industry methodology source bundle does not reconcile")
        return self


__all__ = [
    "IndustryMethodologyBundle",
    "IndustryMethodologySkill",
    "IndustryMethodologySource",
    "IndustryMethodologySourceKind",
]
