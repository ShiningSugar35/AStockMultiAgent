"""Typed enterprise/personnel event observations and deterministic source resolution."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import SourceClass

_SHA256 = r"^[0-9a-f]{64}$"


class EnterpriseEventType(StrEnum):
    MANAGEMENT_APPOINTMENT = "MANAGEMENT_APPOINTMENT"
    MANAGEMENT_RESIGNATION = "MANAGEMENT_RESIGNATION"
    PUBLIC_OFFICE_APPOINTMENT = "PUBLIC_OFFICE_APPOINTMENT"
    CONTROLLER_CHANGE = "CONTROLLER_CHANGE"
    LEGAL_REPRESENTATIVE_CHANGE = "LEGAL_REPRESENTATIVE_CHANGE"
    BUSINESS_REGISTRATION_CHANGE = "BUSINESS_REGISTRATION_CHANGE"
    EQUITY_PLEDGE = "EQUITY_PLEDGE"
    EQUITY_FREEZE = "EQUITY_FREEZE"
    LITIGATION = "LITIGATION"
    ENFORCEMENT = "ENFORCEMENT"
    CREDIT_RISK = "CREDIT_RISK"
    ADMINISTRATIVE_PENALTY = "ADMINISTRATIVE_PENALTY"
    SUBSIDIARY_CHANGE = "SUBSIDIARY_CHANGE"
    PARTNER_CHANGE = "PARTNER_CHANGE"
    KEY_PARTNER_PERSONNEL = "KEY_PARTNER_PERSONNEL"


class EnterpriseRelationScope(StrEnum):
    COMPANY = "COMPANY"
    CONTROLLER = "CONTROLLER"
    SUBSIDIARY = "SUBSIDIARY"
    PARTNER = "PARTNER"
    UPSTREAM = "UPSTREAM"
    DOWNSTREAM = "DOWNSTREAM"
    PUBLIC_OFFICE = "PUBLIC_OFFICE"


class EnterpriseResolutionStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    CONFLICTED = "CONFLICTED"


class EnterpriseIntelligenceObservation(AStockModel):
    schema_version: str = "enterprise-intelligence-observation-v1"
    observation_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    instrument_id: str = Field(min_length=1)
    event_type: EnterpriseEventType
    relation_scope: EnterpriseRelationScope = EnterpriseRelationScope.COMPANY
    event_date: date
    fact_key: str = Field(min_length=1)
    fact_value: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    person_names: tuple[str, ...] = ()
    related_entity_ids: tuple[str, ...] = ()
    source_id: str = Field(min_length=1)
    source_class: SourceClass
    source_independence_group: str | None = Field(default=None, min_length=1)
    evidence_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    evidence_object_hash: str = Field(pattern=_SHA256)
    available_to_system_at: AwareDatetime
    recommendation_allowed: Literal[False] = False
    portfolio_weight_authority_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator("person_names", "related_entity_ids")
    @classmethod
    def sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("enterprise intelligence relation values must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> EnterpriseIntelligenceObservation:
        if not self.instrument_id.endswith(f":{self.company_id}"):
            raise ValueError("enterprise intelligence company/instrument identity mismatch")
        if not self.fact_key.strip() or not self.fact_value.strip():
            raise ValueError("enterprise intelligence fact identity cannot be blank")
        return self


class EnterpriseIntelligenceResolution(AStockModel):
    schema_version: str = "enterprise-intelligence-resolution-v1"
    resolution_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    fact_key: str = Field(min_length=1)
    observation_ids: tuple[str, ...] = Field(min_length=1)
    status: EnterpriseResolutionStatus
    preferred_observation_id: str | None = None
    cross_source_confirmed: bool = False
    investigation_required: bool
    conflict_reason_codes: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = Field(min_length=1)
    source_independence_groups: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    resolved_at: AwareDatetime
    recommendation_allowed: Literal[False] = False
    portfolio_weight_authority_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator(
        "observation_ids",
        "conflict_reason_codes",
        "source_ids",
        "source_independence_groups",
        "evidence_ids",
        "source_snapshot_ids",
    )
    @classmethod
    def sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("enterprise resolution lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_resolution(self) -> EnterpriseIntelligenceResolution:
        if self.status is EnterpriseResolutionStatus.CONFLICTED:
            if not self.investigation_required or self.preferred_observation_id is not None:
                raise ValueError("conflicted enterprise fact must remain unresolved")
            if not self.conflict_reason_codes:
                raise ValueError("conflicted enterprise fact requires reason codes")
        else:
            if self.investigation_required or self.preferred_observation_id is None:
                raise ValueError("confirmed enterprise fact requires one preferred observation")
            if self.preferred_observation_id not in self.observation_ids:
                raise ValueError("preferred enterprise observation must be part of the resolution")
        return self


__all__ = [
    "EnterpriseEventType",
    "EnterpriseIntelligenceObservation",
    "EnterpriseIntelligenceResolution",
    "EnterpriseRelationScope",
    "EnterpriseResolutionStatus",
]
