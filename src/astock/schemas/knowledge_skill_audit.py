"""Append-only knowledge-Skill audit and curated-registry contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, HttpUrl, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.direct_source_distillation import DirectSkillModule

_SHA256 = r"^[0-9a-f]{64}$"


class KnowledgeSkillAuditVerdict(StrEnum):
    KEEP = "KEEP"
    KEEP_SCOPED = "KEEP_SCOPED"
    REVISE = "REVISE"
    RETIRE = "RETIRE"


class KnowledgeSkillOrigin(StrEnum):
    DIRECT = "DIRECT"
    VISUAL_OVERLAY = "VISUAL_OVERLAY"
    CURATED = "CURATED"
    REVISED = "REVISED"


class KnowledgeSkillAuditStatus(StrEnum):
    PLANNED = "PLANNED"
    DECISIONS_COMPLETE = "DECISIONS_COMPLETE"
    PUBLISHED = "PUBLISHED"


class ExternalEvidenceDefinition(AStockModel):
    schema_version: str = "knowledge-skill-external-evidence-item-v1"
    evidence_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    url: HttpUrl
    tags: list[str] = Field(min_length=1)
    limitation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tags(self) -> ExternalEvidenceDefinition:
        if self.tags != sorted(set(self.tags)):
            raise ValueError("external evidence tags must be sorted and unique")
        return self


class CuratedResearchSkill(AStockModel):
    schema_version: str = "curated-research-skill-v1"
    skill_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    secondary_modules: list[DirectSkillModule] = Field(default_factory=list)
    decision_question: str = Field(min_length=1)
    core_principle: str = Field(min_length=1)
    applicable_conditions: list[str] = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    external_evidence_ids: list[str] = Field(min_length=2)
    source_skill_id: str | None = None
    source_skill_object_hash: str | None = Field(default=None, pattern=_SHA256)
    source_skill_artifact_id: str | None = None
    source_hashes: list[str] = Field(default_factory=list)
    shadow_or_prospective_only: bool
    formal_committee_weight_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_curated_skill(self) -> CuratedResearchSkill:
        normalized_secondary = sorted(
            set(self.secondary_modules), key=lambda item: item.value
        )
        if self.secondary_modules != normalized_secondary:
            raise ValueError("curated Skill secondary modules must be sorted and unique")
        if self.external_evidence_ids != sorted(set(self.external_evidence_ids)):
            raise ValueError("curated Skill evidence IDs must be sorted and unique")
        if self.source_hashes != sorted(set(self.source_hashes)):
            raise ValueError("curated Skill source hashes must be sorted and unique")
        source_fields = (
            self.source_skill_id,
            self.source_skill_object_hash,
            self.source_skill_artifact_id,
        )
        if any(item is not None for item in source_fields) and not all(
            item is not None for item in source_fields
        ):
            raise ValueError("replacement Skill source identity must be complete or absent")
        return self


class KnowledgeSkillAuditDecision(AStockModel):
    schema_version: str = "knowledge-skill-audit-decision-v1"
    decision_id: str = Field(min_length=1)
    audit_run_id: str = Field(min_length=1)
    source_skill_id: str = Field(min_length=1)
    source_skill_object_hash: str = Field(pattern=_SHA256)
    source_skill_artifact_id: str = Field(min_length=1)
    skill_origin: KnowledgeSkillOrigin
    verdict: KnowledgeSkillAuditVerdict
    premise_scope: str = Field(min_length=1)
    risk_codes: list[str]
    conflict_groups: list[str]
    external_evidence_ids: list[str] = Field(min_length=2)
    rationale: str = Field(min_length=1)
    replacement_skill_id: str | None = None
    replacement_skill_object_hash: str | None = Field(default=None, pattern=_SHA256)
    replacement_skill_artifact_id: str | None = None
    formal_committee_weight_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_decision(self) -> KnowledgeSkillAuditDecision:
        for values, label in (
            (self.risk_codes, "risk codes"),
            (self.conflict_groups, "conflict groups"),
            (self.external_evidence_ids, "external evidence IDs"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"knowledge Skill audit {label} must be sorted and unique")
        replacement = (
            self.replacement_skill_id,
            self.replacement_skill_object_hash,
            self.replacement_skill_artifact_id,
        )
        if self.verdict is KnowledgeSkillAuditVerdict.REVISE:
            if not all(replacement):
                raise ValueError("REVISE requires a complete replacement Skill identity")
        elif any(item is not None for item in replacement):
            raise ValueError("only REVISE may carry a replacement Skill identity")
        return self


class KnowledgeSkillAuditRun(AStockModel):
    schema_version: str = "knowledge-skill-audit-run-v1"
    audit_run_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_registry_release_id: str = Field(min_length=1)
    source_registry_object_hash: str = Field(pattern=_SHA256)
    policy_hash: str = Field(pattern=_SHA256)
    evidence_catalog_hash: str = Field(pattern=_SHA256)
    expected_skill_count: int = Field(ge=1)
    decision_count: int = Field(ge=0)
    status: KnowledgeSkillAuditStatus
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts(self) -> KnowledgeSkillAuditRun:
        if self.decision_count > self.expected_skill_count:
            raise ValueError("knowledge Skill audit decision count exceeds source count")
        if self.status is KnowledgeSkillAuditStatus.PLANNED and self.decision_count != 0:
            raise ValueError("PLANNED audit cannot already contain decisions")
        if self.status in {
            KnowledgeSkillAuditStatus.DECISIONS_COMPLETE,
            KnowledgeSkillAuditStatus.PUBLISHED,
        } and self.decision_count != self.expected_skill_count:
            raise ValueError("completed audit must cover every source Skill")
        return self


class AuditedKnowledgeSkillRegistryMember(AStockModel):
    schema_version: str = "audited-knowledge-skill-member-v1"
    member_ordinal: int = Field(ge=1)
    effective_skill_id: str = Field(min_length=1)
    effective_skill_object_hash: str = Field(pattern=_SHA256)
    effective_skill_artifact_id: str = Field(min_length=1)
    source_skill_id: str | None = None
    decision_id: str | None = None
    skill_origin: KnowledgeSkillOrigin
    admission_basis: str = Field(min_length=1)
    source_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_member(self) -> AuditedKnowledgeSkillRegistryMember:
        if self.source_hashes != sorted(set(self.source_hashes)):
            raise ValueError("audited registry source hashes must be sorted and unique")
        if self.skill_origin is not KnowledgeSkillOrigin.CURATED and (
            self.source_skill_id is None or self.decision_id is None
        ):
            raise ValueError("audited non-curated member requires source Skill and decision")
        return self


class AuditedKnowledgeSkillRegistryRelease(AStockModel):
    schema_version: str = "audited-knowledge-skill-registry-v1"
    release_id: str = Field(min_length=1)
    audit_run_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_registry_release_id: str = Field(min_length=1)
    source_registry_object_hash: str = Field(pattern=_SHA256)
    policy_hash: str = Field(pattern=_SHA256)
    evidence_catalog_hash: str = Field(pattern=_SHA256)
    source_skill_count: int = Field(ge=1)
    decision_count: int = Field(ge=1)
    keep_count: int = Field(ge=0)
    keep_scoped_count: int = Field(ge=0)
    revise_count: int = Field(ge=0)
    retire_count: int = Field(ge=0)
    curated_count: int = Field(ge=0)
    active_skill_count: int = Field(ge=0)
    members: list[AuditedKnowledgeSkillRegistryMember]
    release_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release(self) -> AuditedKnowledgeSkillRegistryRelease:
        if self.decision_count != self.source_skill_count:
            raise ValueError("audited registry requires one decision per source Skill")
        if (
            self.keep_count
            + self.keep_scoped_count
            + self.revise_count
            + self.retire_count
            != self.source_skill_count
        ):
            raise ValueError("audited registry verdict counts do not reconcile")
        if self.active_skill_count != (
            self.keep_count + self.keep_scoped_count + self.revise_count + self.curated_count
        ):
            raise ValueError("audited registry active count does not reconcile")
        if self.active_skill_count != len(self.members):
            raise ValueError("audited registry member count does not reconcile")
        if [item.member_ordinal for item in self.members] != list(
            range(1, len(self.members) + 1)
        ):
            raise ValueError("audited registry ordinals must be contiguous")
        ids = [item.effective_skill_id for item in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("audited registry effective Skill IDs must be unique")
        return self


class KnowledgeSkillAuditReport(AStockModel):
    schema_version: str = "knowledge-skill-audit-report-v1"
    audit_run_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    source_skill_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    active_skill_count: int = Field(ge=0)
    conflict_group_count: int = Field(ge=0)
    unresolved_same_premise_conflict_count: int = Field(ge=0)
    missing_external_evidence_count: int = Field(ge=0)
    broken_object_count: int = Field(ge=0)
    finding_codes: list[str]

    @model_validator(mode="after")
    def validate_report(self) -> KnowledgeSkillAuditReport:
        if self.finding_codes != sorted(set(self.finding_codes)):
            raise ValueError("knowledge Skill audit finding codes must be sorted and unique")
        should_pass = (
            self.source_skill_count == self.decision_count
            and self.unresolved_same_premise_conflict_count == 0
            and self.missing_external_evidence_count == 0
            and self.broken_object_count == 0
        )
        if (self.status == "PASS") != should_pass:
            raise ValueError("knowledge Skill audit status disagrees with hard gates")
        return self


__all__ = [
    "AuditedKnowledgeSkillRegistryMember",
    "AuditedKnowledgeSkillRegistryRelease",
    "CuratedResearchSkill",
    "ExternalEvidenceDefinition",
    "KnowledgeSkillAuditDecision",
    "KnowledgeSkillAuditReport",
    "KnowledgeSkillAuditRun",
    "KnowledgeSkillAuditStatus",
    "KnowledgeSkillAuditVerdict",
    "KnowledgeSkillOrigin",
]
