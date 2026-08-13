"""Strict Agent proposal contracts for the adaptive edge."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.provider import ProviderHealthStatus
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import AcquisitionCapability, ExternalAuthority


class AdaptiveProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"


class ResearchModule(StrEnum):
    EVIDENCE = "EVIDENCE"
    PIT = "PIT"
    FINANCIAL_INTEGRITY = "FINANCIAL_INTEGRITY"
    FUNDAMENTAL_MODEL = "FUNDAMENTAL_MODEL"
    BASE_CASE = "BASE_CASE"
    SPECIALISTS = "SPECIALISTS"
    KNOWLEDGE = "KNOWLEDGE"
    COMMITTEE = "COMMITTEE"
    TRADING_CLASSIFICATION = "TRADING_CLASSIFICATION"


class ResearchPlannerProposal(AStockModel):
    schema_version: str = "research-planner-proposal-v1"
    proposal_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    requested_modules: list[ResearchModule]
    skipped_optional_reasons: dict[ResearchModule, str] = Field(default_factory=dict)
    requested_acquisition_capabilities: list[AcquisitionCapability] = Field(default_factory=list)
    specialist_budget: int | None = Field(default=None, ge=1, le=32)
    status: Literal[AdaptiveProposalStatus.PROPOSED] = AdaptiveProposalStatus.PROPOSED
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_unique_requests(self) -> ResearchPlannerProposal:
        if len(self.requested_modules) != len(set(self.requested_modules)):
            raise ValueError("research planner requested modules must be unique")
        if len(self.requested_acquisition_capabilities) != len(
            set(self.requested_acquisition_capabilities)
        ):
            raise ValueError("research planner acquisition capabilities must be unique")
        return self


class ValidatedResearchPlan(AStockModel):
    schema_version: str = "validated-research-plan-v1"
    plan_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    ordered_modules: list[ResearchModule] = Field(min_length=1)
    mandatory_modules: list[ResearchModule] = Field(min_length=1)
    acquisition_capabilities: list[AcquisitionCapability] = Field(min_length=1)
    specialist_budget: int = Field(ge=1, le=32)
    policy_version: str = Field(min_length=1)
    status: Literal[AdaptiveProposalStatus.VALIDATED] = AdaptiveProposalStatus.VALIDATED
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


class ProviderFailureDiagnostic(AStockModel):
    provider_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    failure_class: str = Field(min_length=1)
    retryable: bool
    health_status: ProviderHealthStatus
    transport_profile: str | None = None


class ProviderRecoveryProposal(AStockModel):
    schema_version: str = "provider-recovery-proposal-v1"
    proposal_id: str = Field(min_length=1)
    requested_capability: str = Field(min_length=1)
    diagnostics: list[ProviderFailureDiagnostic] = Field(min_length=1)
    proposed_provider_ids: list[str] = Field(default_factory=list)
    authority_fallbacks: list[ExternalAuthority] = Field(default_factory=list)
    status: Literal[AdaptiveProposalStatus.PROPOSED] = AdaptiveProposalStatus.PROPOSED
    manual_last: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_recovery_uniqueness(self) -> ProviderRecoveryProposal:
        if len(self.proposed_provider_ids) != len(set(self.proposed_provider_ids)):
            raise ValueError("recovery provider ids must be unique")
        if len(self.authority_fallbacks) != len(set(self.authority_fallbacks)):
            raise ValueError("recovery authority fallbacks must be unique")
        return self


class ProviderRecoveryValidation(AStockModel):
    schema_version: str = "provider-recovery-validation-v1"
    validation_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    requested_capability: str = Field(min_length=1)
    allowed_provider_ids: list[str]
    authority_fallbacks: list[ExternalAuthority]
    rejection_codes: list[str]
    status: AdaptiveProposalStatus
    manual_last: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_status(self) -> ProviderRecoveryValidation:
        if self.status not in {
            AdaptiveProposalStatus.VALIDATED,
            AdaptiveProposalStatus.REJECTED,
        }:
            raise ValueError("recovery validation status must be VALIDATED or REJECTED")
        if self.status is AdaptiveProposalStatus.VALIDATED and self.rejection_codes:
            raise ValueError("validated recovery cannot carry rejection codes")
        if self.status is AdaptiveProposalStatus.REJECTED and not self.rejection_codes:
            raise ValueError("rejected recovery requires rejection codes")
        return self


class SchemaRepairProposal(AStockModel):
    schema_version: str = "schema-repair-proposal-v1"
    proposal_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    base_dialect_version: str = Field(min_length=1)
    candidate_field_mapping: dict[str, str] = Field(min_length=1)
    candidate_response_paths: dict[str, str] = Field(min_length=1)
    candidate_scope_prefixes: dict[str, list[str]] = Field(default_factory=dict)
    candidate_currency_field: str | None = None
    candidate_native_monetary_unit: str = Field(min_length=1)
    sample_snapshot_ids: list[str] = Field(min_length=2)
    official_evidence_artifact_ids: list[str] = Field(min_length=1)
    contract_test_ids: list[str] = Field(min_length=1)
    status: Literal[AdaptiveProposalStatus.PROPOSED] = AdaptiveProposalStatus.PROPOSED
    formal_fact_write_allowed: Literal[False] = False
    active_runtime_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_repair_uniqueness(self) -> SchemaRepairProposal:
        for values, label in (
            (self.sample_snapshot_ids, "sample snapshots"),
            (self.official_evidence_artifact_ids, "official evidence artifacts"),
            (self.contract_test_ids, "contract tests"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"schema repair {label} must be unique")
        return self


class SchemaRepairValidation(AStockModel):
    schema_version: str = "schema-repair-validation-v1"
    validation_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    verified_snapshot_ids: list[str]
    verified_official_artifact_ids: list[str]
    verified_contract_test_ids: list[str]
    rejection_codes: list[str]
    status: AdaptiveProposalStatus
    formal_fact_write_allowed: Literal[False] = False
    active_runtime_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_status(self) -> SchemaRepairValidation:
        if self.status not in {
            AdaptiveProposalStatus.VALIDATED,
            AdaptiveProposalStatus.REJECTED,
        }:
            raise ValueError("schema repair validation must be VALIDATED or REJECTED")
        if self.status is AdaptiveProposalStatus.VALIDATED and self.rejection_codes:
            raise ValueError("validated schema repair cannot carry rejection codes")
        if self.status is AdaptiveProposalStatus.REJECTED and not self.rejection_codes:
            raise ValueError("rejected schema repair requires rejection codes")
        return self


class AdaptiveArtifactAudit(AStockModel):
    schema_version: str = "adaptive-artifact-audit-v1"
    audit_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_type: str | None = None
    object_hash: str | None = None
    status: Literal["PASS", "FAIL"]
    finding_codes: list[str]


class ProviderDialectRollbackRecord(AStockModel):
    schema_version: str = "provider-dialect-rollback-v1"
    rollback_id: str = Field(min_length=1)
    candidate_release_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    rejected_candidate_dialect_version: str = Field(min_length=1)
    restored_active_dialect_version: str = Field(min_length=1)
    status: Literal[AdaptiveProposalStatus.REJECTED] = AdaptiveProposalStatus.REJECTED
    active_runtime_mutation_allowed: Literal[False] = False
    formal_fact_write_allowed: Literal[False] = False


class ProviderDialectCandidateRelease(AStockModel):
    schema_version: str = "provider-dialect-candidate-release-v1"
    release_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    validation_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    candidate_dialect_version: str = Field(min_length=1)
    candidate_field_mapping: dict[str, str] = Field(min_length=1)
    candidate_response_paths: dict[str, str] = Field(min_length=1)
    status: Literal[AdaptiveProposalStatus.ADMITTED] = AdaptiveProposalStatus.ADMITTED
    explicit_approval_bound: Literal[True] = True
    formal_fact_write_allowed: Literal[False] = False
    active_runtime_mutation_allowed: Literal[False] = False


__all__ = [
    "AdaptiveArtifactAudit",
    "AdaptiveProposalStatus",
    "ProviderDialectCandidateRelease",
    "ProviderDialectRollbackRecord",
    "ProviderFailureDiagnostic",
    "ProviderRecoveryProposal",
    "ProviderRecoveryValidation",
    "ResearchModule",
    "ResearchPlannerProposal",
    "SchemaRepairProposal",
    "SchemaRepairValidation",
    "ValidatedResearchPlan",
]
