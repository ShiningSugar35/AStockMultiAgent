"""Strict public and normalized contracts for direct-source Skill distillation."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_EMPTY_BYTES_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_PDF_LOCATOR = re.compile(
    r"^pdf-page-(?P<unit>\d+);normalized-page-text;"
    r"chars=(?P<start>\d+):(?P<end>\d+)$"
)
_DOCX_LOCATOR = re.compile(
    r"^docx-paragraph-(?P<unit>\d+);normalized-paragraph-text;"
    r"chars=(?P<start>\d+):(?P<end>\d+)$"
)
_GENERIC_UNCERTAINTY_REASONS = {
    "needs review",
    "review",
    "uncertain",
    "unknown",
    "不确定",
    "待定",
    "需复核",
}


class DirectSourceModel(BaseModel):
    """Strict base without volatile defaults, suitable for canonical hashing."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class DirectSourceKind(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"


class DirectSkillModule(StrEnum):
    SOURCING_SCREENING = "SOURCING_SCREENING"
    FUNDAMENTAL_RESEARCH = "FUNDAMENTAL_RESEARCH"
    VALUATION_PRICING = "VALUATION_PRICING"
    PORTFOLIO_CONSTRUCTION = "PORTFOLIO_CONSTRUCTION"
    POSITION_RISK_MANAGEMENT = "POSITION_RISK_MANAGEMENT"
    PSYCHOLOGY_BEHAVIOR = "PSYCHOLOGY_BEHAVIOR"


class DirectSkillStatus(StrEnum):
    READY_FOR_SHADOW = "READY_FOR_SHADOW"
    NEEDS_USER_REVIEW = "NEEDS_USER_REVIEW"


class DirectRunStage(StrEnum):
    INITIALIZED = "INITIALIZED"
    PACKETS_EXPORTING = "PACKETS_EXPORTING"
    BATCHES_IMPORTED = "BATCHES_IMPORTED"
    FINALIZED = "FINALIZED"


class DirectBatchStage(StrEnum):
    FROZEN = "FROZEN"
    PACKET_EXPORTED = "PACKET_EXPORTED"
    IMPORTED = "IMPORTED"


class DirectSourceLocator(DirectSourceModel):
    source_kind: DirectSourceKind
    unit_index: int = Field(ge=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> DirectSourceLocator:
        if self.end_offset <= self.start_offset:
            raise ValueError("direct source locator end_offset must be greater than start_offset")
        return self


def parse_direct_source_locator(
    source_kind: DirectSourceKind,
    locator: str,
) -> DirectSourceLocator:
    pattern = _PDF_LOCATOR if source_kind is DirectSourceKind.PDF else _DOCX_LOCATOR
    match = pattern.fullmatch(locator)
    if match is None:
        expected = (
            "pdf-page-N;normalized-page-text;chars=a:b"
            if source_kind is DirectSourceKind.PDF
            else "docx-paragraph-N;normalized-paragraph-text;chars=a:b"
        )
        raise ValueError(f"invalid direct source locator; expected {expected}")
    return DirectSourceLocator(
        source_kind=source_kind,
        unit_index=int(match.group("unit")),
        start_offset=int(match.group("start")),
        end_offset=int(match.group("end")),
    )


class DirectSourceDefinition(DirectSourceModel):
    source_id: str = Field(min_length=1)
    source_kind: DirectSourceKind
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)


class DirectChapterFragment(DirectSourceModel):
    fragment_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    locator: DirectSourceLocator


class DirectAuditedEmptyLocator(DirectSourceModel):
    """Zero-length locator for one frozen, legal empty source unit."""

    source_kind: DirectSourceKind
    unit_index: int = Field(ge=1)
    start_offset: Literal[0] = 0
    end_offset: Literal[0] = 0


class DirectAuditedEmptyUnit(DirectSourceModel):
    """Audited empty source unit; it is never a distillable text fragment."""

    object_hash: Literal[
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ] = _EMPTY_BYTES_SHA256
    locator: DirectAuditedEmptyLocator


class DirectChapterBatchDefinition(DirectSourceModel):
    batch_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    chapter_unit_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    current_fragments: list[DirectChapterFragment] = Field(min_length=1)
    source_unit_start: int | None = Field(default=None, ge=1)
    source_unit_end: int | None = Field(default=None, ge=1)
    audited_empty_units: list[DirectAuditedEmptyUnit] = Field(default_factory=list)
    context_before: list[DirectChapterFragment] = Field(default_factory=list, max_length=2)
    context_after: list[DirectChapterFragment] = Field(default_factory=list, max_length=2)
    visual_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch_identity(self) -> DirectChapterBatchDefinition:
        fragments = self.context_before + self.current_fragments + self.context_after
        fragment_ids = [item.fragment_id for item in fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("chapter fragment ids must be unique within a batch")
        if len(self.visual_evidence_ids) != len(set(self.visual_evidence_ids)):
            raise ValueError("chapter visual evidence ids must be unique")
        if (self.source_unit_start is None) != (self.source_unit_end is None):
            raise ValueError("source unit range must provide both start and end")
        current_indexes = {item.locator.unit_index for item in self.current_fragments}
        empty_indexes = [item.locator.unit_index for item in self.audited_empty_units]
        if len(empty_indexes) != len(set(empty_indexes)):
            raise ValueError("audited empty unit indexes must be unique")
        if current_indexes.intersection(empty_indexes):
            raise ValueError("audited empty units must not overlap non-empty fragments")
        current_kinds = {item.locator.source_kind for item in self.current_fragments}
        empty_kinds = {item.locator.source_kind for item in self.audited_empty_units}
        if len(current_kinds) != 1 or (empty_kinds and empty_kinds != current_kinds):
            raise ValueError("audited empty units must match the batch source kind")
        if self.source_unit_start is None:
            if self.audited_empty_units:
                raise ValueError("audited empty units require an explicit frozen source unit range")
            expected_indexes = set(range(min(current_indexes), max(current_indexes) + 1))
        else:
            assert self.source_unit_end is not None
            if self.source_unit_end < self.source_unit_start:
                raise ValueError("frozen source unit range is reversed")
            expected_indexes = set(range(self.source_unit_start, self.source_unit_end + 1))
        if current_indexes.union(empty_indexes) != expected_indexes:
            raise ValueError(
                "non-empty fragments and audited empty units must exactly cover "
                "the frozen source unit range"
            )
        return self


class DirectRunInitManifest(DirectSourceModel):
    schema_version: Literal["direct-source-run-init-v1"] = "direct-source-run-init-v1"
    run_id: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    sources: list[DirectSourceDefinition] = Field(min_length=1)
    batches: list[DirectChapterBatchDefinition] = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_frozen_scope(self) -> DirectRunInitManifest:
        source_ids = [item.source_id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("direct source ids must be unique")
        source_by_id = {item.source_id: item for item in self.sources}
        batch_ids = [item.batch_id for item in self.batches]
        chapter_ids = [item.chapter_unit_id for item in self.batches]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("direct batch ids must be unique")
        if len(chapter_ids) != len(set(chapter_ids)):
            raise ValueError("direct chapter unit ids must be unique")
        if sorted(item.ordinal for item in self.batches) != list(range(1, len(self.batches) + 1)):
            raise ValueError("direct batch ordinals must be contiguous from one")
        for batch in self.batches:
            source = source_by_id.get(batch.source_id)
            if source is None:
                raise ValueError(f"batch references unknown source: {batch.source_id}")
            for fragment in (
                batch.context_before + batch.current_fragments + batch.context_after
            ):
                if fragment.locator.source_kind is not source.source_kind:
                    raise ValueError("fragment locator source kind does not match its source")
        return self


class DirectPdfBatchLocator(DirectSourceModel):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> DirectPdfBatchLocator:
        if self.page_end < self.page_start:
            raise ValueError("PDF batch locator is reversed")
        return self


class DirectDocxBatchLocator(DirectSourceModel):
    start_paragraph: int = Field(ge=1)
    end_paragraph: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> DirectDocxBatchLocator:
        if self.end_paragraph < self.start_paragraph:
            raise ValueError("DOCX batch locator is reversed")
        return self


class DirectHashContract(DirectSourceModel):
    algorithm: Literal["SHA-256"]
    normalization: str = Field(min_length=1)
    batch_serialization: str = Field(min_length=1)
    source_locator_offsets: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> DirectHashContract:
        if "0-based" not in self.source_locator_offsets or "half-open" not in (
            self.source_locator_offsets
        ):
            raise ValueError("source locator offsets must be 0-based half-open")
        return self


class DirectPdfSourceRef(DirectSourceModel):
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)
    source_kind: Literal[DirectSourceKind.PDF]
    page_number: int = Field(ge=1)
    locator: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=_SHA256_PATTERN)
    visual_evidence_ids: list[str] = Field(default_factory=list)
    paragraph_head: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_locator(self) -> DirectPdfSourceRef:
        parsed = parse_direct_source_locator(DirectSourceKind.PDF, self.locator)
        if parsed.unit_index != self.page_number:
            raise ValueError("PDF locator page does not match page_number")
        if len(self.visual_evidence_ids) != len(set(self.visual_evidence_ids)):
            raise ValueError("source-ref visual evidence ids must be unique")
        return self


class DirectDocxSourceRef(DirectSourceModel):
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)
    source_kind: Literal[DirectSourceKind.DOCX]
    paragraph_number: int = Field(ge=1)
    locator: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=_SHA256_PATTERN)
    visual_evidence_ids: list[str] = Field(default_factory=list)
    paragraph_head: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_locator(self) -> DirectDocxSourceRef:
        parsed = parse_direct_source_locator(DirectSourceKind.DOCX, self.locator)
        if parsed.unit_index != self.paragraph_number:
            raise ValueError("DOCX locator paragraph does not match paragraph_number")
        if len(self.visual_evidence_ids) != len(set(self.visual_evidence_ids)):
            raise ValueError("source-ref visual evidence ids must be unique")
        return self


DirectSolSourceRef = Annotated[
    DirectPdfSourceRef | DirectDocxSourceRef,
    Field(discriminator="source_kind"),
]


class DirectSkillSemantics(DirectSourceModel):
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    secondary_modules: list[DirectSkillModule] = Field(default_factory=list)
    decision_question: str = Field(min_length=1)
    core_principle: str = Field(min_length=1)
    applicable_conditions: list[str] = Field(default_factory=list)
    reasoning_steps: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: DirectSkillStatus
    uncertainty_reason: str | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> DirectSkillSemantics:
        if len(self.secondary_modules) != len(set(self.secondary_modules)):
            raise ValueError("secondary_modules must be de-duplicated")
        if self.primary_module in self.secondary_modules:
            raise ValueError("secondary_modules cannot contain primary_module")
        for field_name in (
            "applicable_conditions",
            "reasoning_steps",
            "required_evidence",
            "positive_signals",
            "negative_signals",
            "invalidation_conditions",
            "failure_modes",
        ):
            values = getattr(self, field_name)
            if any(not item for item in values):
                raise ValueError(f"{field_name} cannot contain empty entries")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} entries must be unique")
        if self.status is DirectSkillStatus.READY_FOR_SHADOW:
            if self.uncertainty_reason is not None:
                raise ValueError("READY_FOR_SHADOW cannot retain uncertainty")
        else:
            if self.uncertainty_reason is None:
                raise ValueError("NEEDS_USER_REVIEW requires a concrete uncertainty reason")
            normalized = self.uncertainty_reason.casefold().strip(" .。")
            if len(normalized) < 8 or normalized in _GENERIC_UNCERTAINTY_REASONS:
                raise ValueError("NEEDS_USER_REVIEW uncertainty reason is not concrete")
        return self


class DirectSolSkill(DirectSkillSemantics):
    source_refs: list[DirectSolSourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lineage(self) -> DirectSolSkill:
        identities = [
            (item.source_file_hash, item.source_object_hash, item.locator)
            for item in self.source_refs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("source_refs must be de-duplicated")
        if self.status is DirectSkillStatus.READY_FOR_SHADOW and not self.source_refs:
            raise ValueError("READY_FOR_SHADOW requires precise source_refs")
        return self


class DirectSolBatchOutput(DirectSourceModel):
    schema_version: Literal["direct-source-skill-batch-v1"]
    source_kind: DirectSourceKind
    batch_id: str = Field(min_length=1)
    section_title: str = Field(min_length=1)
    locator: DirectPdfBatchLocator | DirectDocxBatchLocator
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)
    batch_text_object_hash: str = Field(pattern=_SHA256_PATTERN)
    sol_distillation_version: str = Field(min_length=1)
    hash_contract: DirectHashContract
    visual_evidence_refs: list[str] = Field(default_factory=list)
    skills: list[DirectSolSkill] = Field(default_factory=list)
    no_skill_reason: str | None = None
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch(self) -> DirectSolBatchOutput:
        if self.source_kind is DirectSourceKind.PDF and not isinstance(
            self.locator,
            DirectPdfBatchLocator,
        ):
            raise ValueError("PDF batch requires page_start/page_end locator")
        if self.source_kind is DirectSourceKind.DOCX and not isinstance(
            self.locator,
            DirectDocxBatchLocator,
        ):
            raise ValueError("DOCX batch requires start_paragraph/end_paragraph locator")
        if len(self.visual_evidence_refs) != len(set(self.visual_evidence_refs)):
            raise ValueError("top-level visual_evidence_refs must be unique")
        if self.skills and self.no_skill_reason is not None:
            raise ValueError("a non-empty batch cannot include no_skill_reason")
        if not self.skills and (
            self.no_skill_reason is None or len(self.no_skill_reason.strip()) < 8
        ):
            raise ValueError("an empty batch requires a concrete no_skill_reason")
        if any(not item for item in self.open_questions):
            raise ValueError("open_questions cannot contain empty entries")
        top_visuals = set(self.visual_evidence_refs)
        for skill in self.skills:
            for source_ref in skill.source_refs:
                if source_ref.source_kind is not self.source_kind:
                    raise ValueError("source-ref kind does not match batch source_kind")
                if source_ref.source_file_hash != self.source_file_hash:
                    raise ValueError("source-ref file hash does not match batch source_file_hash")
                if not set(source_ref.visual_evidence_ids).issubset(top_visuals):
                    raise ValueError("source-ref visual IDs must be declared at batch level")
                unit_index = (
                    source_ref.page_number
                    if isinstance(source_ref, DirectPdfSourceRef)
                    else source_ref.paragraph_number
                )
                if isinstance(self.locator, DirectPdfBatchLocator) and not (
                    self.locator.page_start <= unit_index <= self.locator.page_end
                ):
                    raise ValueError("PDF source-ref is outside the batch locator")
                if isinstance(self.locator, DirectDocxBatchLocator) and not (
                    self.locator.start_paragraph
                    <= unit_index
                    <= self.locator.end_paragraph
                ):
                    raise ValueError("DOCX source-ref is outside the batch locator")
        return self


class DirectCandidateSourceRef(DirectSourceModel):
    batch_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)
    chapter_unit_id: str = Field(min_length=1)
    fragment_id: str = Field(min_length=1)
    fragment_object_hash: str = Field(pattern=_SHA256_PATTERN)
    source_object_hash: str = Field(pattern=_SHA256_PATTERN)
    slice_hash: str = Field(pattern=_SHA256_PATTERN)
    locator: DirectSourceLocator
    original_locator: str = Field(min_length=1)
    paragraph_head: str = Field(min_length=1)
    visual_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_slice_hash(self) -> DirectCandidateSourceRef:
        if self.source_object_hash != self.slice_hash:
            raise ValueError("source_object_hash must equal the recomputed slice hash")
        return self


class DirectCandidateVisualRef(DirectSourceModel):
    batch_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    source_kind: DirectSourceKind
    unit_index: int = Field(ge=1)
    evidence_locator: dict[str, object] | list[object] = Field(
        default_factory=dict
    )


class DirectRawSkillCandidate(DirectSkillSemantics):
    candidate_id: str = Field(min_length=1)
    chapter_unit_id: str = Field(min_length=1)
    sol_version_id: str = Field(min_length=1)
    source_refs: list[DirectCandidateSourceRef] = Field(default_factory=list)
    visual_refs: list[DirectCandidateVisualRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_internal_lineage(self) -> DirectRawSkillCandidate:
        source_keys = [
            (item.source_object_hash, item.original_locator) for item in self.source_refs
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("normalized source refs must be de-duplicated")
        visual_ids = [item.evidence_id for item in self.visual_refs]
        if len(visual_ids) != len(set(visual_ids)):
            raise ValueError("normalized visual refs must be de-duplicated")
        if self.status is DirectSkillStatus.READY_FOR_SHADOW and not self.source_refs:
            raise ValueError("READY_FOR_SHADOW requires normalized source refs")
        return self


class DirectNormalizedBatchOutput(DirectSourceModel):
    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    chapter_unit_id: str = Field(min_length=1)
    source_file_hash: str = Field(pattern=_SHA256_PATTERN)
    batch_text_object_hash: str = Field(pattern=_SHA256_PATTERN)
    sol_version_id: str = Field(min_length=1)
    sol_version_hash: str = Field(pattern=_SHA256_PATTERN)
    skills: list[DirectRawSkillCandidate] = Field(default_factory=list)
    no_skill_reason: str | None = None
    visual_evidence_ids: list[str] = Field(default_factory=list)
    formal_committee_weight_allowed: Literal[False] = False


class DirectFinalSkillDraft(DirectSkillSemantics):
    final_skill_id: str = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contributions(self) -> DirectFinalSkillDraft:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("final skill candidate contributions must be unique")
        return self


class DirectDedupManifest(DirectSourceModel):
    schema_version: Literal["direct-source-dedup-manifest-v1"] = (
        "direct-source-dedup-manifest-v1"
    )
    manifest_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sol_version: str = Field(min_length=1)
    sol_version_hash: str = Field(pattern=_SHA256_PATTERN)
    embedding_usage: Literal["POST_GENERATION_ASSIST_ONLY"]
    sol_confirmed: Literal[True]
    final_skills: list[DirectFinalSkillDraft] = Field(default_factory=list)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_manifest_contributions(self) -> DirectDedupManifest:
        final_ids = [item.final_skill_id for item in self.final_skills]
        if len(final_ids) != len(set(final_ids)):
            raise ValueError("final skill ids must be unique")
        contributions = [
            candidate_id
            for final_skill in self.final_skills
            for candidate_id in final_skill.candidate_ids
        ]
        if len(contributions) != len(set(contributions)):
            raise ValueError("each raw candidate may contribute to only one final skill")
        return self


__all__ = [
    "DirectAuditedEmptyLocator",
    "DirectAuditedEmptyUnit",
    "DirectBatchStage",
    "DirectCandidateSourceRef",
    "DirectCandidateVisualRef",
    "DirectChapterBatchDefinition",
    "DirectChapterFragment",
    "DirectDedupManifest",
    "DirectDocxBatchLocator",
    "DirectDocxSourceRef",
    "DirectFinalSkillDraft",
    "DirectHashContract",
    "DirectNormalizedBatchOutput",
    "DirectPdfBatchLocator",
    "DirectPdfSourceRef",
    "DirectRawSkillCandidate",
    "DirectRunInitManifest",
    "DirectRunStage",
    "DirectSkillModule",
    "DirectSkillStatus",
    "DirectSolBatchOutput",
    "DirectSolSkill",
    "DirectSolSourceRef",
    "DirectSourceDefinition",
    "DirectSourceKind",
    "DirectSourceLocator",
    "parse_direct_source_locator",
]
