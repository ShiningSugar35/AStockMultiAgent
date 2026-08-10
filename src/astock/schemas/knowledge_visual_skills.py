"""Strict contracts for the visual-enhanced Zhihu Skill overlay."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.direct_source_distillation import DirectSkillModule, DirectSkillStatus

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class VisualSkillReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class VisualEnhancedKnowledgeSkill(AStockModel):
    schema_version: str = "visual-enhanced-knowledge-skill-v1"
    final_skill_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    secondary_modules: list[DirectSkillModule] = Field(default_factory=list)
    decision_question: str = Field(min_length=1)
    core_principle: str = Field(min_length=20)
    applicable_conditions: list[str] = Field(default_factory=list)
    reasoning_steps: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal[DirectSkillStatus.READY_FOR_SHADOW] = DirectSkillStatus.READY_FOR_SHADOW
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    argument_text_object_hash: str = Field(pattern=_SHA256_PATTERN)
    rebuilt_argument_object_hashes: list[str] = Field(min_length=1)
    placement_ids: list[str] = Field(min_length=1)
    visual_packet_artifact_ids: list[str] = Field(min_length=1)
    visual_packet_object_hashes: list[str] = Field(min_length=1)
    image_object_hashes: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    source_hashes: list[str] = Field(min_length=1)
    community_source_only: Literal[True] = True
    factual_use_requires_stronger_source: Literal[True] = True
    standalone_visual_distillation: Literal[False] = False
    merge_policy: Literal["MERGE_WITH_BOTH"] = "MERGE_WITH_BOTH"
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator(
        "secondary_modules",
        "rebuilt_argument_object_hashes",
        "placement_ids",
        "visual_packet_artifact_ids",
        "visual_packet_object_hashes",
        "image_object_hashes",
        "source_snapshot_ids",
        "source_hashes",
    )
    @classmethod
    def validate_unique_lists(cls, value: list[object]) -> list[object]:
        if value != sorted(set(value), key=str):
            raise ValueError("visual Skill lineage lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_skill(self) -> VisualEnhancedKnowledgeSkill:
        if self.primary_module in self.secondary_modules:
            raise ValueError("primary module cannot also be secondary")
        hash_lists = (
            self.rebuilt_argument_object_hashes,
            self.visual_packet_object_hashes,
            self.image_object_hashes,
            self.source_hashes,
        )
        if any(
            any(
                len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
                for item in values
            )
            for values in hash_lists
        ):
            raise ValueError("visual Skill lineage contains a non-SHA256 hash")
        if not set(self.rebuilt_argument_object_hashes).issubset(self.source_hashes):
            raise ValueError("rebuilt AU hashes must be included in source_hashes")
        if not set(self.visual_packet_object_hashes).issubset(self.source_hashes):
            raise ValueError("visual packet hashes must be included in source_hashes")
        if not set(self.image_object_hashes).issubset(self.source_hashes):
            raise ValueError("image hashes must be included in source_hashes")
        return self


class VisualSkillAuditRecord(AStockModel):
    schema_version: str = "visual-enhanced-skill-audit-v1"
    candidate_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    status: Literal["PASS"] = "PASS"
    checks: list[str] = Field(min_length=1)
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    visual_packet_count: int = Field(ge=1)
    source_hash_count: int = Field(ge=1)
    formal_committee_weight_allowed: Literal[False] = False


class VisualSkillNoSkillRecord(AStockModel):
    schema_version: str = "visual-enhanced-no-skill-v1"
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("no-Skill reason codes must be sorted and unique")
        return value


class VisualSkillGenerationRun(AStockModel):
    schema_version: str = "visual-enhanced-skill-generation-run-v1"
    run_id: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    base_registry_release_id: str = Field(min_length=1)
    base_registry_object_hash: str = Field(pattern=_SHA256_PATTERN)
    generation_policy_version: str = Field(min_length=1)
    author_source_ids: list[str] = Field(min_length=1)
    semantic_run_ids: list[str] = Field(min_length=1)
    visual_pack_artifact_ids: list[str] = Field(min_length=1)
    visual_pack_object_hashes: list[str] = Field(min_length=1)
    evaluated_argument_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    no_skill_count: int = Field(ge=0)
    run_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts(self) -> VisualSkillGenerationRun:
        if self.evaluated_argument_count != self.candidate_count + self.no_skill_count:
            raise ValueError("visual Skill generation counts do not reconcile")
        for values in (
            self.author_source_ids,
            self.semantic_run_ids,
            self.visual_pack_artifact_ids,
            self.visual_pack_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("visual Skill generation inputs must be sorted and unique")
        return self


class VisualSkillReviewRecord(AStockModel):
    schema_version: str = "visual-enhanced-skill-review-decision-v1"
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256_PATTERN)
    decision: VisualSkillReviewDecision
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=8)
    formal_committee_weight_allowed: Literal[False] = False


class VisualSkillOverlayMember(AStockModel):
    member_ordinal: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256_PATTERN)
    skill_artifact_id: str = Field(min_length=1)
    admission_basis: Literal["APPROVED"] = "APPROVED"
    source_hashes: list[str] = Field(min_length=1)

    @field_validator("source_hashes")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("visual registry source hashes must be sorted and unique")
        return value


class VisualSkillOverlayRelease(AStockModel):
    schema_version: str = "knowledge-skill-composite-registry-release-v2"
    release_id: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    base_run_id: str = Field(min_length=1)
    generation_run_id: str = Field(min_length=1)
    base_registry_release_id: str = Field(min_length=1)
    base_registry_object_hash: str = Field(pattern=_SHA256_PATTERN)
    base_admitted_skill_count: int = Field(ge=0)
    overlay_candidate_count: int = Field(ge=0)
    overlay_approved_count: int = Field(ge=0)
    overlay_rejected_count: int = Field(ge=0)
    overlay_admitted_skill_count: int = Field(ge=0)
    composite_admitted_skill_count: int = Field(ge=0)
    decision_ids: list[str]
    members: list[VisualSkillOverlayMember]
    release_artifact_id: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release(self) -> VisualSkillOverlayRelease:
        if self.overlay_candidate_count != (
            self.overlay_approved_count + self.overlay_rejected_count
        ):
            raise ValueError("visual overlay review counts do not reconcile")
        if self.overlay_admitted_skill_count != self.overlay_approved_count:
            raise ValueError("visual overlay admitted count does not reconcile")
        if self.composite_admitted_skill_count != (
            self.base_admitted_skill_count + self.overlay_admitted_skill_count
        ):
            raise ValueError("composite registry count does not reconcile")
        if self.overlay_admitted_skill_count != len(self.members):
            raise ValueError("visual overlay member count does not reconcile")
        if self.decision_ids != sorted(set(self.decision_ids)):
            raise ValueError("visual overlay decision IDs must be sorted and unique")
        member_ids = [item.final_skill_id for item in self.members]
        if member_ids != sorted(set(member_ids)):
            raise ValueError("visual overlay member IDs must be sorted and unique")
        if [item.member_ordinal for item in self.members] != list(range(1, len(self.members) + 1)):
            raise ValueError("visual overlay member ordinals must be contiguous")
        return self


__all__ = [
    "VisualEnhancedKnowledgeSkill",
    "VisualSkillAuditRecord",
    "VisualSkillGenerationRun",
    "VisualSkillNoSkillRecord",
    "VisualSkillOverlayMember",
    "VisualSkillOverlayRelease",
    "VisualSkillReviewDecision",
    "VisualSkillReviewRecord",
]
