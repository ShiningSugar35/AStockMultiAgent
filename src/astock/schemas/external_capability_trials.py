"""Tracked evidence bundles that compile into canonical M-06 qualification reports."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.external_capabilities import (
    CapabilityQualificationChecks,
    CapabilityQualificationReport,
    ExternalCapabilityStage,
    QualificationCheckStatus,
    qualification_report_id,
)
from astock.schemas.market import CompletenessSemantics, SourceClass


class CapabilityEvidenceSource(AStockModel):
    """One immutable, reviewable input used by an E-02 qualification decision."""

    source_ref: str = Field(min_length=1)
    observed_at: AwareDatetime
    assertion: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CapabilityQualificationEvidence(AStockModel):
    """Non-authoritative evidence input compiled into the existing M-06 report model."""

    evidence_version: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    requested_stage: ExternalCapabilityStage
    admitted_stage: ExternalCapabilityStage
    checks: CapabilityQualificationChecks
    recorded_validation: QualificationCheckStatus
    controlled_live_validation: QualificationCheckStatus
    source_class_ceiling: SourceClass
    completeness_ceiling: CompletenessSemantics
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    reason_codes: list[str] = Field(default_factory=list)
    sources: list[CapabilityEvidenceSource] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> CapabilityQualificationEvidence:
        refs = [item.source_ref for item in self.sources]
        if len(refs) != len(set(refs)):
            raise ValueError("qualification evidence source_ref values must be unique")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("qualification evidence reason codes must be sorted and unique")
        if self.expires_at <= self.valid_from:
            raise ValueError("qualification evidence must expire after valid_from")
        if self.admitted_stage is ExternalCapabilityStage.PRODUCTION_BACKUP:
            if not self.checks.all_pass():
                raise ValueError("production backup evidence requires every M-06 check to pass")
            if self.recorded_validation is not QualificationCheckStatus.PASS:
                raise ValueError("production backup evidence requires recorded validation")
            if self.controlled_live_validation is not QualificationCheckStatus.PASS:
                raise ValueError("production backup evidence requires controlled-live validation")
        return self

    def to_report(self, evidence_object_hash: str) -> CapabilityQualificationReport:
        evidence_hashes = [evidence_object_hash]
        report_id = qualification_report_id(
            capability_id=self.capability_id,
            candidate_version=self.candidate_version,
            requested_stage=self.requested_stage,
            admitted_stage=self.admitted_stage,
            checks=self.checks,
            recorded_validation=self.recorded_validation,
            controlled_live_validation=self.controlled_live_validation,
            source_class_ceiling=self.source_class_ceiling,
            completeness_ceiling=self.completeness_ceiling,
            evidence_object_hashes=evidence_hashes,
            valid_from=self.valid_from,
            expires_at=self.expires_at,
            reason_codes=self.reason_codes,
        )
        return CapabilityQualificationReport(
            report_id=report_id,
            capability_id=self.capability_id,
            candidate_version=self.candidate_version,
            requested_stage=self.requested_stage,
            admitted_stage=self.admitted_stage,
            checks=self.checks,
            recorded_validation=self.recorded_validation,
            controlled_live_validation=self.controlled_live_validation,
            source_class_ceiling=self.source_class_ceiling,
            completeness_ceiling=self.completeness_ceiling,
            evidence_object_hashes=evidence_hashes,
            valid_from=self.valid_from,
            expires_at=self.expires_at,
            reason_codes=self.reason_codes,
        )


__all__ = ["CapabilityEvidenceSource", "CapabilityQualificationEvidence"]
