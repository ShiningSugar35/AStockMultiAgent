"""Current-investor acquisition and presentation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.documents import DisclosureExchange
from astock.schemas.reference_data import Market


class AcquisitionCapability(StrEnum):
    INSTRUMENT_IDENTITY = "INSTRUMENT_IDENTITY"
    DAILY_MARKET = "DAILY_MARKET"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    FINANCIAL_ANNUAL = "FINANCIAL_ANNUAL"
    FINANCIAL_LATEST_INTERIM = "FINANCIAL_LATEST_INTERIM"


class AcquisitionAttemptStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CurrentResearchAcquisitionStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NEEDS_EXTERNAL_RESEARCH = "NEEDS_EXTERNAL_RESEARCH"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"


class ExternalAuthority(StrEnum):
    EXCHANGE_OFFICIAL = "EXCHANGE_OFFICIAL"
    CNINFO_OFFICIAL = "CNINFO_OFFICIAL"
    ISSUER_IR = "ISSUER_IR"
    REGULATOR_OFFICIAL = "REGULATOR_OFFICIAL"
    PUBLIC_MARKET_DATA = "PUBLIC_MARKET_DATA"


class AcquisitionAttempt(AStockModel):
    capability: AcquisitionCapability
    status: AcquisitionAttemptStatus
    provider_path: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    record_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)
    internal_reason_codes: list[str] = Field(default_factory=list)
    source_snapshot_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attempt(self) -> AcquisitionAttempt:
        if self.provider_path != list(dict.fromkeys(self.provider_path)):
            raise ValueError("acquisition provider path must be unique and ordered")
        if self.internal_reason_codes != sorted(set(self.internal_reason_codes)):
            raise ValueError("acquisition reason codes must be sorted and unique")
        if self.source_snapshot_ids != list(dict.fromkeys(self.source_snapshot_ids)):
            raise ValueError("acquisition snapshot ids must be unique and ordered")
        return self


class ExternalResearchNeed(AStockModel):
    capability: AcquisitionCapability
    research_question: str = Field(min_length=1)
    preferred_authorities: list[ExternalAuthority] = Field(min_length=1)
    no_api_registration_preferred: Literal[True] = True
    web_search_allowed: Literal[True] = True
    manual_user_action_required: Literal[False] = False


class ManualResearchAction(AStockModel):
    capability: AcquisitionCapability
    instruction: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)


class CurrentResearchAcquisitionReport(AStockModel):
    schema_version: str = "current-research-acquisition-v1"
    report_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    exchange: DisclosureExchange | None = None
    started_at: AwareDatetime
    decision_as_of: AwareDatetime
    status: CurrentResearchAcquisitionStatus
    attempts: list[AcquisitionAttempt]
    external_research_needs: list[ExternalResearchNeed] = Field(default_factory=list)
    manual_actions: list[ManualResearchAction] = Field(default_factory=list)
    question_time_anchor_used: Literal[False] = False
    decision_snapshot_frozen_after_acquisition: Literal[True] = True
    historical_and_prospective_pit_preserved: Literal[True] = True
    parallel_acquisition_used: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> CurrentResearchAcquisitionReport:
        if self.decision_as_of < self.started_at:
            raise ValueError("decision snapshot cannot predate current acquisition")
        capabilities = [item.capability for item in self.attempts]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("current acquisition attempts must be unique by capability")
        if self.status is CurrentResearchAcquisitionStatus.READY and self.external_research_needs:
            raise ValueError("READY acquisition cannot retain external research needs")
        if (
            self.status is CurrentResearchAcquisitionStatus.NEEDS_USER_ACTION
            and not self.manual_actions
        ):
            raise ValueError("NEEDS_USER_ACTION requires an aggregated manual action list")
        return self


class InvestorResearchState(StrEnum):
    EVIDENCE_READY = "EVIDENCE_READY"
    EVIDENCE_STILL_COLLECTING = "EVIDENCE_STILL_COLLECTING"
    DECISION_READY = "DECISION_READY"
    DECISION_NOT_CERTIFIED = "DECISION_NOT_CERTIFIED"


class InvestorResearchView(AStockModel):
    schema_version: str = "investor-research-view-v1"
    company_id: str = Field(pattern=r"^\d{6}$")
    state: InvestorResearchState
    headline: str = Field(min_length=1)
    plain_language_gaps: list[str] = Field(default_factory=list)
    next_step: str = Field(min_length=1)
    diagnostics_available: Literal[True] = True
    internal_codes_exposed: Literal[False] = False
    artifact_ids_exposed: Literal[False] = False
    paper_ledger_write_count: Literal[0] = 0
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "AcquisitionAttempt",
    "AcquisitionAttemptStatus",
    "AcquisitionCapability",
    "CurrentResearchAcquisitionReport",
    "CurrentResearchAcquisitionStatus",
    "ExternalAuthority",
    "ExternalResearchNeed",
    "InvestorResearchState",
    "InvestorResearchView",
    "ManualResearchAction",
]
