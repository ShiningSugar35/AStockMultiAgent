"""Strict contracts for owner-reviewed semantic Skill admission overlays."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.direct_source_distillation import DirectSkillModule

_SHA256 = r"^[0-9a-f]{64}$"


class SemanticAdmissionDisposition(StrEnum):
    ADMIT_AS_IS = "ADMIT_AS_IS"
    REWRITE_AND_ADMIT = "REWRITE_AND_ADMIT"


class SemanticAdmissionDecisionValue(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class SemanticAdmissionSkill(AStockModel):
    schema_version: str = "semantic-admission-skill-v1"
    final_skill_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    secondary_modules: list[DirectSkillModule] = Field(default_factory=list)
    decision_question: str = Field(min_length=1)
    core_principle: str = Field(min_length=20)
    applicable_conditions: list[str] = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    failure_modes: list[str] = Field(min_length=1)
    family: str = Field(min_length=1)
    industry_scope: str = Field(min_length=1)
    holding_horizon: str = Field(min_length=1)
    method_categories: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1)
    argument_unit_ids: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_hashes: list[str] = Field(min_length=1)
    source_authors: list[str] = Field(min_length=1)
    same_source_lineage_keys: list[str] = Field(default_factory=list)
    disposition: SemanticAdmissionDisposition
    community_source_only: Literal[True] = True
    factual_use_requires_stronger_source: Literal[True] = True
    formal_committee_weight_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False

    @field_validator(
        "secondary_modules",
        "method_categories",
        "candidate_ids",
        "argument_unit_ids",
        "source_snapshot_ids",
        "source_hashes",
        "source_authors",
        "same_source_lineage_keys",
    )
    @classmethod
    def validate_sorted_unique(cls, value: list[object]) -> list[object]:
        if value != sorted(set(value), key=str):
            raise ValueError(
                "semantic admission lineage/classification lists must be sorted unique"
            )
        return value

    @model_validator(mode="after")
    def validate_skill(self) -> SemanticAdmissionSkill:
        if self.primary_module in self.secondary_modules:
            raise ValueError("primary module cannot be repeated as secondary")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_hashes
        ):
            raise ValueError("semantic admission source hashes must be SHA-256")
        return self


class SemanticAdmissionRun(AStockModel):
    schema_version: str = "semantic-admission-run-v1"
    run_id: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    parent_audited_release_id: str = Field(min_length=1)
    parent_audited_object_hash: str = Field(pattern=_SHA256)
    owner_policy_artifact_hash: str = Field(pattern=_SHA256)
    admitted_groups_artifact_hash: str = Field(pattern=_SHA256)
    raw_candidate_count: int = Field(ge=1)
    exact_group_count: int = Field(ge=1)
    admitted_group_count: int = Field(ge=0)
    rejected_group_count: int = Field(ge=0)
    author_source_ids: list[str] = Field(min_length=1)
    run_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("author_source_ids")
    @classmethod
    def validate_authors(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("semantic admission authors must be sorted unique")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> SemanticAdmissionRun:
        if self.exact_group_count != self.admitted_group_count + self.rejected_group_count:
            raise ValueError("semantic admission group counts do not reconcile")
        return self


class SemanticAdmissionDecision(AStockModel):
    schema_version: str = "semantic-admission-decision-v1"
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256)
    decision: SemanticAdmissionDecisionValue
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=8)
    formal_committee_weight_allowed: Literal[False] = False
    decided_at: AwareDatetime


class SemanticAdmissionAuditReport(AStockModel):
    schema_version: str = "semantic-admission-audit-v1"
    run_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    parent_active_skill_count: int = Field(ge=0)
    raw_candidate_count: int = Field(ge=0)
    mapped_candidate_count: int = Field(ge=0)
    exact_group_count: int = Field(ge=0)
    skill_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    duplicate_candidate_membership_count: int = Field(ge=0)
    broken_object_count: int = Field(ge=0)
    missing_source_hash_count: int = Field(ge=0)
    foreign_key_violation_count: int = Field(ge=0)
    database_integrity: str = Field(min_length=1)
    finding_codes: list[str]
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("finding_codes")
    @classmethod
    def validate_findings(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("semantic admission findings must be sorted unique")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> SemanticAdmissionAuditReport:
        should_pass = (
            self.raw_candidate_count == self.mapped_candidate_count
            and self.exact_group_count == self.skill_count
            and self.skill_count == self.decision_count
            and self.approved_count + self.rejected_count == self.skill_count
            and self.duplicate_candidate_membership_count == 0
            and self.broken_object_count == 0
            and self.missing_source_hash_count == 0
            and self.foreign_key_violation_count == 0
            and self.database_integrity == "ok"
            and not self.finding_codes
        )
        if (self.status == "PASS") != should_pass:
            raise ValueError("semantic admission audit status disagrees with hard gates")
        return self


class SemanticAdmissionMember(AStockModel):
    member_ordinal: int = Field(ge=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256)
    skill_artifact_id: str = Field(min_length=1)
    admission_basis: Literal["APPROVED"] = "APPROVED"
    source_hashes: list[str] = Field(min_length=1)

    @field_validator("source_hashes")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("semantic admission member source hashes must be sorted unique")
        return value


class SemanticAdmissionRelease(AStockModel):
    schema_version: str = "semantic-admission-registry-release-v1"
    release_id: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    admission_run_id: str = Field(min_length=1)
    parent_audited_release_id: str = Field(min_length=1)
    parent_audited_object_hash: str = Field(pattern=_SHA256)
    parent_active_skill_count: int = Field(ge=0)
    raw_candidate_count: int = Field(ge=1)
    exact_group_count: int = Field(ge=1)
    approved_skill_count: int = Field(ge=0)
    rejected_skill_count: int = Field(ge=0)
    active_skill_count: int = Field(ge=0)
    decision_ids: list[str]
    members: list[SemanticAdmissionMember]
    audit_report_artifact_id: str = Field(min_length=1)
    audit_report_object_hash: str = Field(pattern=_SHA256)
    release_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release(self) -> SemanticAdmissionRelease:
        if self.exact_group_count != self.approved_skill_count + self.rejected_skill_count:
            raise ValueError("semantic admission release review counts do not reconcile")
        if self.active_skill_count != self.parent_active_skill_count + self.approved_skill_count:
            raise ValueError("semantic admission active count does not reconcile")
        if self.approved_skill_count != len(self.members):
            raise ValueError("semantic admission member count does not reconcile")
        if self.decision_ids != sorted(set(self.decision_ids)):
            raise ValueError("semantic admission decision IDs must be sorted unique")
        ids = [member.final_skill_id for member in self.members]
        if ids != sorted(set(ids)):
            raise ValueError("semantic admission members must be sorted unique")
        if [member.member_ordinal for member in self.members] != list(
            range(1, len(self.members) + 1)
        ):
            raise ValueError("semantic admission member ordinals must be contiguous")
        return self


__all__ = [
    "SemanticAdmissionAuditReport",
    "SemanticAdmissionDecision",
    "SemanticAdmissionDecisionValue",
    "SemanticAdmissionDisposition",
    "SemanticAdmissionMember",
    "SemanticAdmissionRelease",
    "SemanticAdmissionRun",
    "SemanticAdmissionSkill",
]
