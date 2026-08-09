"""Strict contracts for direct knowledge admission, retrieval, and Zhihu visuals."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from astock.schemas.direct_source_distillation import DirectSkillModule

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class KnowledgeCompletionModel(BaseModel):
    """Canonical strict base for immutable knowledge completion payloads."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class KnowledgeReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class KnowledgeAdmissionBasis(StrEnum):
    READY = "READY"
    APPROVED = "APPROVED"


class KnowledgeProviderReadiness(StrEnum):
    READY = "READY"
    NEEDS_INFO = "NEEDS_INFO"


class KnowledgeProviderMode(StrEnum):
    SHADOW_ONLY = "SHADOW_ONLY"
    REGISTRY_RELEASE = "REGISTRY_RELEASE"
    BLOCKED = "BLOCKED"


class DirectKnowledgeSkillReviewDecision(KnowledgeCompletionModel):
    schema_version: Literal["direct-knowledge-review-decision-v1"] = (
        "direct-knowledge-review-decision-v1"
    )
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256_PATTERN)
    decision: KnowledgeReviewDecision
    actor: str = Field(min_length=1)
    decided_at: AwareDatetime
    reason: str = Field(min_length=8)
    formal_committee_weight_allowed: Literal[False] = False


class DirectKnowledgeSkillReviewReceipt(KnowledgeCompletionModel):
    decision: DirectKnowledgeSkillReviewDecision
    artifact_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotent_replay: bool


class DirectKnowledgeSkillReviewSpec(KnowledgeCompletionModel):
    skill_name: str = Field(min_length=1)
    decision: KnowledgeReviewDecision
    reason: str = Field(min_length=8)


class DirectKnowledgeSkillReviewBatch(KnowledgeCompletionModel):
    schema_version: Literal["direct-knowledge-review-batch-v1"] = (
        "direct-knowledge-review-batch-v1"
    )
    run_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    expected_pending_count: int = Field(ge=1)
    decisions: list[DirectKnowledgeSkillReviewSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_batch(self) -> DirectKnowledgeSkillReviewBatch:
        names = [item.skill_name for item in self.decisions]
        if len(names) != len(set(names)):
            raise ValueError("knowledge review batch skill names must be unique")
        if len(self.decisions) != self.expected_pending_count:
            raise ValueError("knowledge review batch count does not match expectation")
        return self


class KnowledgeCompletionStatus(KnowledgeCompletionModel):
    schema_version: Literal["knowledge-completion-status-v1"] = (
        "knowledge-completion-status-v1"
    )
    run_id: str = Field(min_length=1)
    source_run_stage: str = Field(min_length=1)
    total_skill_count: int = Field(ge=0)
    ready_skill_count: int = Field(ge=0)
    needs_user_review_count: int = Field(ge=0)
    pending_review_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    review_closed: bool
    registry_version: str | None = None
    registry_release_id: str | None = None
    registry_artifact_id: str | None = None
    registry_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts_and_registry(self) -> KnowledgeCompletionStatus:
        if self.total_skill_count != self.ready_skill_count + self.needs_user_review_count:
            raise ValueError("knowledge completion status total does not reconcile")
        if self.needs_user_review_count != (
            self.pending_review_count + self.approved_count + self.rejected_count
        ):
            raise ValueError("knowledge review decisions do not reconcile")
        if self.review_closed != (self.pending_review_count == 0):
            raise ValueError("review_closed does not match pending_review_count")
        registry_fields = (
            self.registry_version,
            self.registry_release_id,
            self.registry_artifact_id,
            self.registry_object_hash,
        )
        if any(value is not None for value in registry_fields) and not all(
            value is not None for value in registry_fields
        ):
            raise ValueError("registry identity must be complete or absent")
        return self


class KnowledgeSkillRegistryMember(KnowledgeCompletionModel):
    member_ordinal: int = Field(ge=1)
    final_skill_id: str = Field(min_length=1)
    skill_object_hash: str = Field(pattern=_SHA256_PATTERN)
    skill_artifact_id: str = Field(min_length=1)
    admission_basis: KnowledgeAdmissionBasis
    source_hashes: list[str] = Field(min_length=1)

    @field_validator("source_hashes")
    @classmethod
    def validate_source_hashes(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("registry member source_hashes must be sorted and unique")
        if any(re.fullmatch(_SHA256_PATTERN, item) is None for item in value):
            raise ValueError("registry member source hashes must be SHA-256")
        return value


class KnowledgeSkillRegistryRelease(KnowledgeCompletionModel):
    schema_version: Literal["knowledge-skill-registry-release-v1"] = (
        "knowledge-skill-registry-release-v1"
    )
    release_id: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    total_skill_count: int = Field(ge=0)
    ready_skill_count: int = Field(ge=0)
    approved_skill_count: int = Field(ge=0)
    rejected_skill_count: int = Field(ge=0)
    admitted_skill_count: int = Field(ge=0)
    decision_ids: list[str]
    members: list[KnowledgeSkillRegistryMember]
    release_artifact_id: str = Field(min_length=1)
    created_at: AwareDatetime
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release_membership(self) -> KnowledgeSkillRegistryRelease:
        if self.total_skill_count != (
            self.ready_skill_count + self.approved_skill_count + self.rejected_skill_count
        ):
            raise ValueError("knowledge registry total does not reconcile")
        if self.admitted_skill_count != self.ready_skill_count + self.approved_skill_count:
            raise ValueError("knowledge registry admitted count does not reconcile")
        if self.admitted_skill_count != len(self.members):
            raise ValueError("knowledge registry admitted count differs from membership")
        if self.decision_ids != sorted(set(self.decision_ids)):
            raise ValueError("knowledge registry decision_ids must be sorted and unique")
        member_ids = [item.final_skill_id for item in self.members]
        artifact_ids = [item.skill_artifact_id for item in self.members]
        if member_ids != sorted(set(member_ids)):
            raise ValueError("knowledge registry members must be sorted and unique")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("knowledge registry member artifact IDs must be unique")
        if [item.member_ordinal for item in self.members] != list(
            range(1, len(self.members) + 1)
        ):
            raise ValueError("knowledge registry member ordinals must be contiguous")
        return self


class KnowledgeSkillRegistryReleaseRecord(KnowledgeCompletionModel):
    release: KnowledgeSkillRegistryRelease
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotent_replay: bool


class KnowledgeSkillQuery(KnowledgeCompletionModel):
    schema_version: Literal["knowledge-skill-query-v1"] = "knowledge-skill-query-v1"
    query: str = Field(min_length=1, max_length=10_000)
    modules: list[DirectSkillModule] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=50)
    max_context_bytes: int = Field(default=16_384, ge=1, le=1_048_576)
    max_estimated_tokens: int = Field(default=4_096, ge=1, le=262_144)

    @field_validator("modules")
    @classmethod
    def validate_modules(cls, value: list[DirectSkillModule]) -> list[DirectSkillModule]:
        if len(value) != len(set(value)):
            raise ValueError("knowledge query modules must be unique")
        return value


class KnowledgeSkillSummary(KnowledgeCompletionModel):
    final_skill_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    primary_module: DirectSkillModule
    decision_question: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    source_hashes: list[str] = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    admission_basis: KnowledgeAdmissionBasis
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("source_hashes")
    @classmethod
    def validate_summary_sources(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("knowledge summary source hashes must be sorted and unique")
        if any(re.fullmatch(_SHA256_PATTERN, item) is None for item in value):
            raise ValueError("knowledge summary source hashes must be SHA-256")
        return value


class KnowledgeProviderStatus(KnowledgeCompletionModel):
    schema_version: Literal["knowledge-provider-status-v1"] = (
        "knowledge-provider-status-v1"
    )
    run_id: str = Field(min_length=1)
    status: KnowledgeProviderReadiness
    mode: KnowledgeProviderMode
    reason_code: str = Field(min_length=1)
    total_skill_count: int = Field(ge=0)
    ready_skill_count: int = Field(ge=0)
    pending_review_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    eligible_skill_count: int = Field(ge=0)
    registry_release_id: str | None = None
    registry_artifact_id: str | None = None
    registry_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_provider_state(self) -> KnowledgeProviderStatus:
        registry_fields = (
            self.registry_release_id,
            self.registry_artifact_id,
            self.registry_object_hash,
        )
        if self.mode is KnowledgeProviderMode.REGISTRY_RELEASE:
            if self.status is not KnowledgeProviderReadiness.READY:
                raise ValueError("registry provider mode must be ready")
            if not all(value is not None for value in registry_fields):
                raise ValueError("registry provider mode requires immutable registry identity")
        elif any(value is not None for value in registry_fields):
            raise ValueError("non-registry provider modes cannot expose registry identity")
        if self.mode is KnowledgeProviderMode.BLOCKED:
            if self.status is not KnowledgeProviderReadiness.NEEDS_INFO:
                raise ValueError("blocked knowledge provider must report NEEDS_INFO")
            if self.eligible_skill_count != 0:
                raise ValueError("blocked knowledge provider cannot expose eligible skills")
        elif self.status is not KnowledgeProviderReadiness.READY:
            raise ValueError("usable knowledge provider modes must report READY")
        return self


class KnowledgeSkillSelection(KnowledgeCompletionModel):
    schema_version: Literal["knowledge-skill-selection-v1"] = (
        "knowledge-skill-selection-v1"
    )
    query: KnowledgeSkillQuery
    provider_status: KnowledgeProviderStatus
    skills: list[KnowledgeSkillSummary]
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    context_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    cache_key: str = Field(pattern=_SHA256_PATTERN)
    cache_hit: bool
    result_hash: str = Field(pattern=_SHA256_PATTERN)
    reason_code: str = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_selection(self) -> KnowledgeSkillSelection:
        if self.selected_count != len(self.skills):
            raise ValueError("knowledge selection selected_count does not match skills")
        if self.selected_count > self.query.top_k:
            raise ValueError("knowledge selection exceeds top_k")
        if self.selected_count > self.candidate_count:
            raise ValueError("knowledge selection exceeds candidate_count")
        skill_ids = [item.final_skill_id for item in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("knowledge selection skill IDs must be unique")
        if self.context_bytes > self.query.max_context_bytes:
            raise ValueError("knowledge selection exceeds the byte budget")
        if self.estimated_tokens > self.query.max_estimated_tokens:
            raise ValueError("knowledge selection exceeds the token budget")
        return self


class ZhihuVisualOcrStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_TEXT = "NO_TEXT"
    FAILED = "FAILED"


class ZhihuVisualType(StrEnum):
    CHART = "CHART"
    TABLE = "TABLE"
    DIAGRAM = "DIAGRAM"
    SCREENSHOT = "SCREENSHOT"
    DECORATIVE = "DECORATIVE"
    OTHER = "OTHER"


class ZhihuArgumentRebuildStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ZhihuVisualPacketStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ZhihuVisualStage(StrEnum):
    IMAGE_URL_INVENTORIED = "IMAGE_URL_INVENTORIED"
    ACCESS_POLICY_VERIFIED = "ACCESS_POLICY_VERIFIED"
    IMAGE_SNAPSHOT_FROZEN = "IMAGE_SNAPSHOT_FROZEN"
    DOM_LOCATED = "DOM_LOCATED"
    OCR_ATTEMPTED = "OCR_ATTEMPTED"
    VISUAL_CLASSIFIED = "VISUAL_CLASSIFIED"
    CONTEXT_ASSEMBLED = "CONTEXT_ASSEMBLED"
    AFFECTED_AU_REBUILT = "AFFECTED_AU_REBUILT"
    PACKET_READY = "PACKET_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


def validate_zhimg_url(value: str) -> str:
    """Accept only HTTPS zhimg.com hosts with a precise DNS-label boundary."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Zhihu image URL") from exc
    host = parsed.hostname
    if parsed.scheme != "https" or host is None:
        raise ValueError("Zhihu image URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Zhihu image URL cannot contain userinfo")
    if port not in {None, 443}:
        raise ValueError("Zhihu image URL cannot use a non-HTTPS port")
    if host != "zhimg.com" and not host.endswith(".zhimg.com"):
        raise ValueError("Zhihu image URL host is outside the zhimg.com allowlist")
    if host.endswith(".") or parsed.fragment:
        raise ValueError("Zhihu image URL is not canonical")
    return value


class ZhihuDomImageLocator(KnowledgeCompletionModel):
    dom_path: str = Field(min_length=1)
    image_ordinal: int = Field(ge=1)


class ZhihuOcrAttempt(KnowledgeCompletionModel):
    status: ZhihuVisualOcrStatus
    engine_version: str = Field(min_length=1)
    text: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> ZhihuOcrAttempt:
        if self.status is ZhihuVisualOcrStatus.SUCCEEDED:
            if not self.text or self.confidence is None or self.failure_reason is not None:
                raise ValueError("successful OCR requires text/confidence and no failure")
        elif self.text is not None or not self.failure_reason or len(self.failure_reason) < 8:
            raise ValueError("non-successful OCR requires a concrete failure and no text")
        return self


class ZhihuVisualClassification(KnowledgeCompletionModel):
    visual_type: ZhihuVisualType
    classifier_version: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ZhihuParagraphContext(KnowledgeCompletionModel):
    paragraph_id: str = Field(min_length=1)
    paragraph_ordinal: int = Field(ge=1)
    text: str = Field(min_length=1)


class ZhihuAffectedArgumentRebuild(KnowledgeCompletionModel):
    argument_unit_id: str = Field(min_length=1)
    previous_argument_object_hash: str = Field(pattern=_SHA256_PATTERN)
    rebuilt_argument_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    status: ZhihuArgumentRebuildStatus
    reason: str | None = None

    @model_validator(mode="after")
    def validate_rebuild(self) -> ZhihuAffectedArgumentRebuild:
        if self.status is ZhihuArgumentRebuildStatus.READY:
            if self.rebuilt_argument_object_hash is None or self.reason is not None:
                raise ValueError("ready argument rebuild requires an object and no reason")
        elif not self.reason or len(self.reason) < 8:
            raise ValueError("review argument rebuild requires a concrete reason")
        return self


class ZhihuVisualCaptureRequest(KnowledgeCompletionModel):
    schema_version: Literal["zhihu-visual-capture-request-v1"] = (
        "zhihu-visual-capture-request-v1"
    )
    placement_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    image_url: str = Field(min_length=1)
    redirect_chain: list[str] = Field(default_factory=list)
    response_mime: str = Field(min_length=1)
    dom_locator: ZhihuDomImageLocator
    ocr: ZhihuOcrAttempt
    classification: ZhihuVisualClassification
    preceding_context: ZhihuParagraphContext
    following_context: ZhihuParagraphContext
    affected_argument_rebuilds: list[ZhihuAffectedArgumentRebuild] = Field(min_length=1)
    formal_committee_weight_allowed: Literal[False] = False

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return validate_zhimg_url(value)

    @field_validator("redirect_chain")
    @classmethod
    def validate_redirect_chain(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Zhihu image redirect chain must be unique")
        return [validate_zhimg_url(item) for item in value]

    @model_validator(mode="after")
    def validate_visual_context(self) -> ZhihuVisualCaptureRequest:
        if self.preceding_context.paragraph_id == self.following_context.paragraph_id:
            raise ValueError("Zhihu visual requires distinct context paragraphs")
        rebuild_ids = [item.argument_unit_id for item in self.affected_argument_rebuilds]
        if len(rebuild_ids) != len(set(rebuild_ids)):
            raise ValueError("affected argument rebuild IDs must be unique")
        return self


class ZhihuVisualCaptureResult(KnowledgeCompletionModel):
    schema_version: Literal["zhihu-visual-capture-result-v1"] = (
        "zhihu-visual-capture-result-v1"
    )
    packet_id: str = Field(min_length=1)
    placement_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    image_object_hash: str = Field(pattern=_SHA256_PATTERN)
    image_mime: str = Field(min_length=1)
    url_hash: str = Field(pattern=_SHA256_PATTERN)
    host_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    path_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    redirect_chain_hash: str = Field(pattern=_SHA256_PATTERN)
    packet_status: ZhihuVisualPacketStatus
    reason_code: str = Field(min_length=1)
    stages: list[ZhihuVisualStage]
    packet_artifact_id: str = Field(min_length=1)
    packet_object_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotent_replay: bool
    standalone: Literal[False] = False
    merge_policy: Literal["MERGE_WITH_BOTH"] = "MERGE_WITH_BOTH"
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_stages(self) -> ZhihuVisualCaptureResult:
        prefix = [
            ZhihuVisualStage.IMAGE_URL_INVENTORIED,
            ZhihuVisualStage.ACCESS_POLICY_VERIFIED,
            ZhihuVisualStage.IMAGE_SNAPSHOT_FROZEN,
            ZhihuVisualStage.DOM_LOCATED,
            ZhihuVisualStage.OCR_ATTEMPTED,
            ZhihuVisualStage.VISUAL_CLASSIFIED,
            ZhihuVisualStage.CONTEXT_ASSEMBLED,
            ZhihuVisualStage.AFFECTED_AU_REBUILT,
        ]
        terminal = (
            ZhihuVisualStage.PACKET_READY
            if self.packet_status is ZhihuVisualPacketStatus.READY
            else ZhihuVisualStage.REVIEW_REQUIRED
        )
        if self.stages != [*prefix, terminal]:
            raise ValueError("Zhihu visual stages are incomplete or out of order")
        return self


__all__ = [
    "DirectKnowledgeSkillReviewBatch",
    "DirectKnowledgeSkillReviewDecision",
    "DirectKnowledgeSkillReviewReceipt",
    "DirectKnowledgeSkillReviewSpec",
    "KnowledgeAdmissionBasis",
    "KnowledgeCompletionStatus",
    "KnowledgeProviderMode",
    "KnowledgeProviderReadiness",
    "KnowledgeProviderStatus",
    "KnowledgeReviewDecision",
    "KnowledgeSkillQuery",
    "KnowledgeSkillRegistryMember",
    "KnowledgeSkillRegistryRelease",
    "KnowledgeSkillRegistryReleaseRecord",
    "KnowledgeSkillSelection",
    "KnowledgeSkillSummary",
    "ZhihuAffectedArgumentRebuild",
    "ZhihuArgumentRebuildStatus",
    "ZhihuDomImageLocator",
    "ZhihuOcrAttempt",
    "ZhihuParagraphContext",
    "ZhihuVisualCaptureRequest",
    "ZhihuVisualCaptureResult",
    "ZhihuVisualClassification",
    "ZhihuVisualOcrStatus",
    "ZhihuVisualPacketStatus",
    "ZhihuVisualStage",
    "ZhihuVisualType",
    "validate_zhimg_url",
]
