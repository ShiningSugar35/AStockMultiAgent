"""Qualification contracts for optional external Providers, libraries, MCPs, crawlers and Skills."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas.market import CompletenessSemantics, SourceClass

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ExternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class ExternalCapabilityKind(StrEnum):
    API = "API"
    PYTHON_LIBRARY = "PYTHON_LIBRARY"
    MCP = "MCP"
    CRAWLER = "CRAWLER"
    PARSER = "PARSER"
    SKILL = "SKILL"


class ExternalCapabilityStage(StrEnum):
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    SHADOW = "SHADOW"
    PRODUCTION_BACKUP = "PRODUCTION_BACKUP"
    REJECT = "REJECT"


class QualificationCheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ExternalCapabilityDefinition(_ExternalModel):
    capability_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    display_name: str = Field(min_length=1, max_length=160)
    kind: ExternalCapabilityKind
    logical_capabilities: list[str] = Field(min_length=1)
    default_stage: ExternalCapabilityStage = ExternalCapabilityStage.DISCOVERY_ONLY
    maximum_stage: ExternalCapabilityStage = ExternalCapabilityStage.SHADOW
    provider_id: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    source_class_ceiling: SourceClass = SourceClass.UNKNOWN
    completeness_ceiling: CompletenessSemantics = CompletenessSemantics.NOT_APPLICABLE
    qualification_validity_days: int = Field(default=90, ge=1, le=365)
    fixed_version_required: bool = True
    optional_dependency: bool = True
    exit_contract: str = Field(min_length=1, max_length=1000)
    broker_execution_capable: bool = False

    @field_validator("logical_capabilities")
    @classmethod
    def _unique_capabilities(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not item.strip() for item in value):
            raise ValueError("logical_capabilities must be sorted, unique and non-empty")
        return value

    @model_validator(mode="after")
    def _safe_stage(self) -> ExternalCapabilityDefinition:
        if self.broker_execution_capable or any(
            _looks_like_broker_execution(x) for x in self.logical_capabilities
        ):
            if self.maximum_stage is not ExternalCapabilityStage.REJECT:
                raise ValueError("broker execution capabilities are permanently rejected")
        rank = {
            ExternalCapabilityStage.DISCOVERY_ONLY: 0,
            ExternalCapabilityStage.SHADOW: 1,
            ExternalCapabilityStage.PRODUCTION_BACKUP: 2,
            ExternalCapabilityStage.REJECT: -1,
        }
        if self.maximum_stage is ExternalCapabilityStage.REJECT:
            if self.default_stage is not ExternalCapabilityStage.REJECT:
                raise ValueError("rejected capability must default to REJECT")
        elif rank[self.default_stage] > rank[self.maximum_stage]:
            raise ValueError("default stage cannot exceed maximum stage")
        return self


class ExternalCapabilityRegistry(_ExternalModel):
    schema_version: str = "external-capability-registry-v1"
    registry_version: str = Field(min_length=1)
    capabilities: list[ExternalCapabilityDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> ExternalCapabilityRegistry:
        ids = [item.capability_id for item in self.capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("external capability ids must be unique")
        provider_ids = [item.provider_id for item in self.capabilities if item.provider_id]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("external capability provider ids must be unique")
        return self


class CapabilityQualificationChecks(_ExternalModel):
    license: QualificationCheckStatus
    terms_of_service: QualificationCheckStatus
    data_rights: QualificationCheckStatus
    pit: QualificationCheckStatus
    provenance: QualificationCheckStatus
    credential_handling: QualificationCheckStatus
    sbom: QualificationCheckStatus
    security_review: QualificationCheckStatus
    maintenance: QualificationCheckStatus
    cost: QualificationCheckStatus
    latency: QualificationCheckStatus
    cache_behavior: QualificationCheckStatus
    offline_behavior: QualificationCheckStatus
    failure_behavior: QualificationCheckStatus
    exit_uninstall: QualificationCheckStatus

    def all_pass(self) -> bool:
        return all(value is QualificationCheckStatus.PASS for value in self.model_dump().values())


class CapabilityQualificationRequest(_ExternalModel):
    schema_version: str = "capability-qualification-request-v1"
    request_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    candidate_version: str = Field(min_length=1, max_length=160)
    requested_stage: ExternalCapabilityStage
    evidence_object_hashes: list[str] = Field(default_factory=list)
    requested_at: AwareDatetime

    @field_validator("evidence_object_hashes")
    @classmethod
    def _hashes(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("qualification evidence hashes must be sorted and unique")
        if any(
            len(item) != 64 or any(ch not in "0123456789abcdef" for ch in item) for item in value
        ):
            raise ValueError("qualification evidence hashes must be lowercase sha256")
        return value


class CapabilityQualificationReport(_ExternalModel):
    schema_version: str = "capability-qualification-report-v1"
    report_id: str = Field(pattern=_SHA256_PATTERN)
    capability_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    candidate_version: str = Field(min_length=1, max_length=160)
    requested_stage: ExternalCapabilityStage
    admitted_stage: ExternalCapabilityStage
    checks: CapabilityQualificationChecks
    recorded_validation: QualificationCheckStatus
    controlled_live_validation: QualificationCheckStatus
    source_class_ceiling: SourceClass
    completeness_ceiling: CompletenessSemantics
    evidence_object_hashes: list[str] = Field(default_factory=list)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("evidence_object_hashes")
    @classmethod
    def _evidence_hashes(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("qualification evidence hashes must be sorted and unique")
        if any(
            len(item) != 64 or any(ch not in "0123456789abcdef" for ch in item) for item in value
        ):
            raise ValueError("qualification evidence hashes must be lowercase sha256")
        return value

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("qualification reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _consistent_report(self) -> CapabilityQualificationReport:
        if self.expires_at <= self.valid_from:
            raise ValueError("qualification expiry must follow valid_from")
        if self.admitted_stage is ExternalCapabilityStage.PRODUCTION_BACKUP:
            if not self.checks.all_pass():
                raise ValueError("production backup requires every qualification check to PASS")
            if self.recorded_validation is not QualificationCheckStatus.PASS:
                raise ValueError("production backup requires recorded validation PASS")
            if self.controlled_live_validation is not QualificationCheckStatus.PASS:
                raise ValueError("production backup requires controlled live validation PASS")
        expected = qualification_report_id(
            capability_id=self.capability_id,
            candidate_version=self.candidate_version,
            requested_stage=self.requested_stage,
            admitted_stage=self.admitted_stage,
            checks=self.checks,
            recorded_validation=self.recorded_validation,
            controlled_live_validation=self.controlled_live_validation,
            source_class_ceiling=self.source_class_ceiling,
            completeness_ceiling=self.completeness_ceiling,
            evidence_object_hashes=self.evidence_object_hashes,
            valid_from=self.valid_from,
            expires_at=self.expires_at,
            reason_codes=self.reason_codes,
        )
        if self.report_id != expected:
            raise ValueError("qualification report id does not match canonical identity")
        return self


class CapabilityRevocation(_ExternalModel):
    schema_version: str = "capability-revocation-v1"
    revocation_id: str = Field(pattern=_SHA256_PATTERN)
    capability_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    report_id: str = Field(pattern=_SHA256_PATTERN)
    revoked_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _identity(self) -> CapabilityRevocation:
        expected = capability_revocation_id(
            capability_id=self.capability_id,
            report_id=self.report_id,
            revoked_at=self.revoked_at,
            reason=self.reason,
        )
        if self.revocation_id != expected:
            raise ValueError("revocation id does not match canonical identity")
        return self


def qualification_report_id(
    *,
    capability_id: str,
    candidate_version: str,
    requested_stage: ExternalCapabilityStage,
    admitted_stage: ExternalCapabilityStage,
    checks: CapabilityQualificationChecks,
    recorded_validation: QualificationCheckStatus,
    controlled_live_validation: QualificationCheckStatus,
    source_class_ceiling: SourceClass,
    completeness_ceiling: CompletenessSemantics,
    evidence_object_hashes: list[str],
    valid_from: datetime,
    expires_at: datetime,
    reason_codes: list[str],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "capability-qualification-report-v1",
                "capability_id": capability_id,
                "candidate_version": candidate_version,
                "requested_stage": requested_stage.value,
                "admitted_stage": admitted_stage.value,
                "checks": checks.model_dump(mode="json"),
                "recorded_validation": recorded_validation.value,
                "controlled_live_validation": controlled_live_validation.value,
                "source_class_ceiling": source_class_ceiling.value,
                "completeness_ceiling": completeness_ceiling.value,
                "evidence_object_hashes": evidence_object_hashes,
                "valid_from": valid_from.isoformat(),
                "expires_at": expires_at.isoformat(),
                "reason_codes": reason_codes,
            }
        )
    )


def capability_revocation_id(
    *, capability_id: str, report_id: str, revoked_at: datetime, reason: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "capability-revocation-v1",
                "capability_id": capability_id,
                "report_id": report_id,
                "revoked_at": revoked_at.isoformat(),
                "reason": reason,
            }
        )
    )


def _looks_like_broker_execution(capability: str) -> bool:
    normalized = capability.lower().replace("-", ".").replace("_", ".")
    tokens = ("broker", "place.order", "order.execute", "trade.execute", "live.execution")
    return any(token in normalized for token in tokens)


__all__ = [
    "CapabilityQualificationChecks",
    "CapabilityQualificationReport",
    "CapabilityQualificationRequest",
    "CapabilityRevocation",
    "ExternalCapabilityDefinition",
    "ExternalCapabilityKind",
    "ExternalCapabilityRegistry",
    "ExternalCapabilityStage",
    "QualificationCheckStatus",
    "capability_revocation_id",
    "qualification_report_id",
]
