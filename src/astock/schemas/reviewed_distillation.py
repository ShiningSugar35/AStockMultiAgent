"""Schemas for strict, review-based method-rule distillation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel


class RuleDraftStatus(StrEnum):
    READY_FOR_SHADOW = "READY_FOR_SHADOW"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"
    MECHANICAL_DRAFT = "MECHANICAL_DRAFT"


class DistilledSourceRef(AStockModel):
    argument_unit_id: str = Field(min_length=1)
    paragraph_ids: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    text_object_sha256: str = ""

    @model_validator(mode="after")
    def validate_projection(self) -> DistilledSourceRef:
        if any(not item for item in self.paragraph_ids):
            raise ValueError("distilled source ref paragraph ids must be non-empty")
        if any(item <= 0 for item in self.page_numbers):
            raise ValueError("distilled source ref page numbers must be positive")
        if len(set(self.paragraph_ids)) != len(self.paragraph_ids):
            raise ValueError("distilled source ref paragraph ids must be unique")
        if self.page_numbers != sorted(set(self.page_numbers)):
            raise ValueError("distilled source ref page numbers must be sorted and unique")
        if self.text_object_sha256 and len(self.text_object_sha256) != 64:
            raise ValueError("distilled source ref text object hash must be sha256")
        return self


class MechanicalDraft(AStockModel):
    status: RuleDraftStatus = RuleDraftStatus.MECHANICAL_DRAFT
    decision_question: str = ""
    applicable_conditions: list[str] = Field(default_factory=list)
    reasoning_steps: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    known_failure_modes: list[str] = Field(default_factory=list)
    applicable_industries: list[str] = Field(default_factory=list)
    holding_horizon: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def prevent_mechanical_promotion(self) -> MechanicalDraft:
        if self.status is not RuleDraftStatus.MECHANICAL_DRAFT:
            raise ValueError("mechanical candidates must remain mechanical drafts")
        return self


class DistillationAUContext(AStockModel):
    run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    topics: list[str] = Field(default_factory=list)
    source_refs: list[DistilledSourceRef] = Field(default_factory=list)
    mechanical_draft: MechanicalDraft | None = None


class RuleDraftOrigin(StrEnum):
    MECHANICAL_DRAFT = "MECHANICAL_DRAFT"
    CODEX_NATURAL_LANGUAGE = "CODEX_NATURAL_LANGUAGE"


class MethodRuleDraft(AStockModel):
    decision_question: str = Field(min_length=1)
    applicable_conditions: list[str] = Field(min_length=1)
    reasoning_steps: list[str] = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)
    positive_signals: list[str] = Field(min_length=1)
    negative_signals: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    known_failure_modes: list[str] = Field(min_length=1)
    applicable_industries: list[str] = Field(min_length=1)
    holding_horizon: list[str] = Field(min_length=1)
    source_refs: list[DistilledSourceRef] = Field(default_factory=list)
    status: RuleDraftStatus
    origin: RuleDraftOrigin | None = None
    argument_unit_id: str = ""
    batch_id: int = Field(default=1, ge=1)
    input_object_hash: str = ""
    uncertainty_reason: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rule_contract(self) -> MethodRuleDraft:
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and not self.source_refs:
            raise ValueError("ready rules require source refs")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and (
            self.origin is not RuleDraftOrigin.CODEX_NATURAL_LANGUAGE
        ):
            raise ValueError("ready rules must be natural language authored")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and not self.argument_unit_id:
            raise ValueError("ready rules require argument_unit_id")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and self.batch_id < 1:
            raise ValueError("ready rules require positive batch_id")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and (
            not self.input_object_hash or len(self.input_object_hash) != 64
        ):
            raise ValueError("ready rules require input_object_hash")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and not all(
            getattr(item, "argument_unit_id", None) for item in self.source_refs
        ):
            raise ValueError("ready rules require valid source refs")
        if self.status is RuleDraftStatus.READY_FOR_SHADOW:
            argument_unit_ids = [item.argument_unit_id for item in self.source_refs]
            if len(set(argument_unit_ids)) != 1:
                raise ValueError("ready rules require a single argument unit id")
        if self.status is RuleDraftStatus.MECHANICAL_DRAFT and self.origin is None:
            object.__setattr__(self, "origin", RuleDraftOrigin.MECHANICAL_DRAFT)
        if self.status is RuleDraftStatus.READY_FOR_SHADOW and self.uncertainty_reason:
            raise ValueError("ready rules cannot have uncertainty reasons")
        if self.status is RuleDraftStatus.NEEDS_USER_REVIEW and not self.uncertainty_reason:
            raise ValueError("non-ready rules must include uncertainty reasons")
        if (
            self.origin is RuleDraftOrigin.MECHANICAL_DRAFT
            and self.status is RuleDraftStatus.READY_FOR_SHADOW
        ):
            raise ValueError("mechanical-origin rules cannot be final ready status")
        return self


class DistillationBatchInput(AStockModel):
    run_id: str = Field(min_length=1)
    batch_id: int = Field(ge=1)
    arguments: list[DistillationAUContext] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_batch_arguments(self) -> DistillationBatchInput:
        argument_ids = [item.argument_unit_id for item in self.arguments]
        if len(argument_ids) != len(set(argument_ids)):
            raise ValueError("distillation batch argument ids must be unique")
        if any(item.run_id != self.run_id for item in self.arguments):
            raise ValueError("distillation batch arguments must belong to the batch run")
        return self


class DistillationBatchOutput(AStockModel):
    run_id: str = Field(min_length=1)
    batch_id: int = Field(ge=1)
    rules: list[MethodRuleDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch_totals(self) -> DistillationBatchOutput:
        if self.batch_id < 1:
            raise ValueError("batch id must be positive")
        return self


class DistillationBatchManifestEntry(AStockModel):
    batch_id: int = Field(ge=1)
    au_count: int = Field(ge=1)
    au_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_entry_count(self) -> DistillationBatchManifestEntry:
        if self.au_count != len(self.au_ids):
            raise ValueError("batch entry count must match AU id list")
        if len(set(self.au_ids)) != len(self.au_ids):
            raise ValueError("batch AU ids must be unique")
        return self


class DistillationBatchManifest(AStockModel):
    generated_by: str = Field(min_length=1)
    reviewed_run_id: str = Field(min_length=1)
    total_au: int = Field(ge=1)
    batches: list[DistillationBatchManifestEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> DistillationBatchManifest:
        if self.total_au <= 0:
            raise ValueError("total_au must be positive")
        listed = sum(len(item.au_ids) for item in self.batches)
        if listed != self.total_au:
            raise ValueError("manifest total_au mismatches listed batch size")
        return self


class DistillationBatchManifestValidation(AStockModel):
    duplicate_au_ids: list[str] = Field(default_factory=list)
    missing_au_ids: list[str] = Field(default_factory=list)
    is_complete: bool = False
    processed_count: int = 0
    expected_count: int = 0


__all__ = [
    "DistilledSourceRef",
    "DistillationAUContext",
    "DistillationBatchInput",
    "DistillationBatchManifest",
    "DistillationBatchManifestEntry",
    "DistillationBatchManifestValidation",
    "DistillationBatchOutput",
    "MethodRuleDraft",
    "MechanicalDraft",
    "RuleDraftStatus",
    "RuleDraftOrigin",
]
