"""Strict contracts for the reviewed semantic-candidate Skill overlay."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.direct_source_distillation import DirectSkillModule

_SHA256 = r"^[0-9a-f]{64}$"


class SemanticSkillReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class SemanticEffectiveSkill(AStockModel):
    schema_version: str = "semantic-effective-skill-v1"
    final_skill_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    secondary_modules: list[DirectSkillModule] = Field(default_factory=list)
    decision_question: str = Field(min_length=1)
    core_principle: str = Field(min_length=20)
    applicable_conditions: list[str] = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(min_length=1)
    failure_modes: list[str] = Field(min_length=1)
    method_categories: list[str] = Field(min_length=1)
    applicable_industries: list[str] = Field(min_length=1)
    holding_horizon: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    author_source_ids: list[str] = Field(min_length=1)
    semantic_run_ids: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1)
    argument_unit_ids: list[str] = Field(min_length=1)
    source_hashes: list[str] = Field(min_length=1)
    community_source_only: Literal[True] = True
    factual_use_requires_stronger_source: Literal[True] = True
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator(
        "secondary_modules",
        "method_categories",
        "applicable_industries",
        "holding_horizon",
        "author_source_ids",
        "semantic_run_ids",
        "candidate_ids",
        "argument_unit_ids",
        "source_hashes",
    )
    @classmethod
    def sorted_unique(cls, value: list[object]) -> list[object]:
        if value != sorted(set(value), key=str):
            raise ValueError(
                "semantic Skill lineage/classification lists must be sorted and unique"
            )
        return value

    @model_validator(mode="after")
    def validate_skill(self) -> SemanticEffectiveSkill:
        if self.primary_module in self.secondary_modules:
            raise ValueError("primary module cannot also be secondary")
        if any(
            len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
            for item in self.source_hashes
        ):
            raise ValueError("semantic Skill source hashes must be SHA-256")
        return self


class SemanticSkillGenerationAudit(AStockModel):
    schema_version: str = "semantic-skill-generation-audit-v1"
    group_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    status: Literal["PASS"] = "PASS"
    checks: list[str] = Field(min_length=1)
    raw_candidate_count: int = Field(ge=1)
    source_hash_count: int = Field(ge=1)
    formal_committee_weight_allowed: Literal[False] = False


class SemanticSkillGenerationRun(AStockModel):
    schema_version: str = "semantic-skill-generation-run-v1"
    run_id: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    parent_registry_release_id: str = Field(min_length=1)
    parent_registry_object_hash: str = Field(pattern=_SHA256)
    semantic_run_id: str = Field(min_length=1)
    llm_batch_id: str = Field(min_length=1)
    generation_policy_version: str = Field(min_length=1)
    raw_candidate_count: int = Field(ge=1)
    effective_skill_count: int = Field(ge=1)
    author_source_ids: list[str] = Field(min_length=1)
    run_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("author_source_ids")
    @classmethod
    def validate_authors(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("semantic Skill authors must be sorted and unique")
        return value


class SemanticSkillReviewRecord(AStockModel):
    schema_version: str = "semantic-skill-review-decision-v1"
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256)
    decision: SemanticSkillReviewDecision
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=8)
    formal_committee_weight_allowed: Literal[False] = False
    decided_at: AwareDatetime


class SemanticSkillOverlayMember(AStockModel):
    member_ordinal: int = Field(ge=1)
    group_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256)
    skill_artifact_id: str = Field(min_length=1)
    admission_basis: Literal["SEMANTIC_REVIEW_APPROVED"] = "SEMANTIC_REVIEW_APPROVED"
    source_hashes: list[str] = Field(min_length=1)

    @field_validator("source_hashes")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("semantic overlay source hashes must be sorted and unique")
        return value


class SemanticSkillAuditReport(AStockModel):
    schema_version: str = "semantic-skill-overlay-audit-v1"
    run_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    raw_candidate_count: int = Field(ge=0)
    mapped_candidate_count: int = Field(ge=0)
    effective_skill_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    broken_object_count: int = Field(ge=0)
    duplicate_membership_count: int = Field(ge=0)
    missing_source_hash_count: int = Field(ge=0)
    finding_codes: list[str]
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> SemanticSkillAuditReport:
        if self.finding_codes != sorted(set(self.finding_codes)):
            raise ValueError("semantic audit findings must be sorted and unique")
        should_pass = (
            self.raw_candidate_count == self.mapped_candidate_count
            and self.effective_skill_count == self.decision_count
            and self.approved_count + self.rejected_count == self.effective_skill_count
            and self.broken_object_count == 0
            and self.duplicate_membership_count == 0
            and self.missing_source_hash_count == 0
        )
        if (self.status == "PASS") != should_pass:
            raise ValueError("semantic audit status disagrees with hard gates")
        return self


class SemanticSkillOverlayRelease(AStockModel):
    schema_version: str = "knowledge-semantic-overlay-registry-v1"
    release_id: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    generation_run_id: str = Field(min_length=1)
    parent_registry_release_id: str = Field(min_length=1)
    parent_registry_object_hash: str = Field(pattern=_SHA256)
    parent_admitted_skill_count: int = Field(ge=0)
    raw_candidate_count: int = Field(ge=1)
    effective_skill_count: int = Field(ge=1)
    overlay_approved_count: int = Field(ge=0)
    overlay_rejected_count: int = Field(ge=0)
    overlay_admitted_skill_count: int = Field(ge=0)
    composite_admitted_skill_count: int = Field(ge=0)
    decision_ids: list[str]
    members: list[SemanticSkillOverlayMember]
    audit_report_artifact_id: str = Field(min_length=1)
    audit_report_object_hash: str = Field(pattern=_SHA256)
    release_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release(self) -> SemanticSkillOverlayRelease:
        if self.effective_skill_count != self.overlay_approved_count + self.overlay_rejected_count:
            raise ValueError("semantic overlay review counts do not reconcile")
        if self.overlay_admitted_skill_count != self.overlay_approved_count:
            raise ValueError("semantic overlay admitted count does not reconcile")
        if (
            self.composite_admitted_skill_count
            != self.parent_admitted_skill_count + self.overlay_admitted_skill_count
        ):
            raise ValueError("semantic composite count does not reconcile")
        if self.overlay_admitted_skill_count != len(self.members):
            raise ValueError("semantic overlay member count does not reconcile")
        if self.decision_ids != sorted(set(self.decision_ids)):
            raise ValueError("semantic overlay decision IDs must be sorted and unique")
        ids = [item.final_skill_id for item in self.members]
        if ids != sorted(set(ids)):
            raise ValueError("semantic overlay member IDs must be sorted and unique")
        if [item.member_ordinal for item in self.members] != list(range(1, len(self.members) + 1)):
            raise ValueError("semantic overlay ordinals must be contiguous")
        return self


__all__ = [
    "SemanticEffectiveSkill",
    "SemanticSkillAuditReport",
    "SemanticSkillGenerationAudit",
    "SemanticSkillGenerationRun",
    "SemanticSkillOverlayMember",
    "SemanticSkillOverlayRelease",
    "SemanticSkillReviewDecision",
    "SemanticSkillReviewRecord",
]
