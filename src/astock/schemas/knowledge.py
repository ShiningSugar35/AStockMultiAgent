"""Position-lifecycle and allowlisted-author coverage contracts."""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    AUTHOR_SILENT = "AUTHOR_SILENT"
    INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
    CONFLICTING_SOURCE = "CONFLICTING_SOURCE"
    PARTIAL = "PARTIAL"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"


class KnowledgeAuditStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    NOT_COLLECTED = "NOT_COLLECTED"
    USER_CONFIRMED_COMPLETE_EXPORT = "USER_CONFIRMED_COMPLETE_EXPORT"


class CollectionTerminalCondition(StrEnum):
    PAGINATION_COMPLETE = "PAGINATION_COMPLETE"
    CONFIRMED_EMPTY = "CONFIRMED_EMPTY"
    FETCH_FAILED = "FETCH_FAILED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    CURSOR_UNKNOWN = "CURSOR_UNKNOWN"
    PARTIAL = "PARTIAL"


class KnowledgeIdentityStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING_IDENTITY_CONFIRMATION = "PENDING_IDENTITY_CONFIRMATION"
    LOCAL_EXPORT_USER_CONFIRMED_COMPLETE = "LOCAL_EXPORT_USER_CONFIRMED_COMPLETE"


class KnowledgeAccessStatus(StrEnum):
    LOGGED_IN_ACCESS_VERIFIED = "LOGGED_IN_ACCESS_VERIFIED"
    PENDING_IDENTITY_CONFIRMATION = "PENDING_IDENTITY_CONFIRMATION"
    LOCAL_EXPORT_PARSED_COMPLETE = "LOCAL_EXPORT_PARSED_COMPLETE"


class ZhihuContentType(StrEnum):
    ANSWERS = "answers"
    ARTICLES = "articles"
    THOUGHTS = "thoughts"


class ZhihuTransport(StrEnum):
    PYTHON_HTTP = "PYTHON_HTTP"
    MCP = "MCP"
    CHROME = "CHROME"
    MANUAL_IMPORT = "MANUAL_IMPORT"


class ZhihuResponseKind(StrEnum):
    PROFILE = "PROFILE"
    LISTING = "LISTING"
    CONTENT_DETAIL = "CONTENT_DETAIL"
    ROOT_COMMENTS = "ROOT_COMMENTS"
    CHILD_COMMENTS = "CHILD_COMMENTS"


class ZhihuImportStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"


class ZhihuEndpointTemplateStatus(StrEnum):
    VERIFIED = "VERIFIED"
    PENDING_OBSERVATION = "PENDING_OBSERVATION"


class ZhihuCommentEndpointTemplate(AStockModel):
    template_id: str = Field(min_length=1)
    response_kind: ZhihuResponseKind
    content_types: list[ZhihuContentType] = Field(min_length=1)
    path_template: str | None = None
    default_query: dict[str, str] = Field(default_factory=dict)
    status: ZhihuEndpointTemplateStatus
    observation_evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_template_status(self) -> ZhihuCommentEndpointTemplate:
        if self.response_kind not in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            raise ValueError("comment endpoint templates only cover comment responses")
        if self.status is ZhihuEndpointTemplateStatus.VERIFIED:
            if not self.path_template or not self.path_template.startswith("/api/"):
                raise ValueError("verified endpoint templates require an API path")
        elif self.path_template is not None:
            raise ValueError("pending endpoint templates cannot contain a guessed path")
        if len(self.content_types) != len(set(self.content_types)):
            raise ValueError("endpoint template content types must be unique")
        return self


class ZhihuEndpointTemplateRegistry(AStockModel):
    templates: list[ZhihuCommentEndpointTemplate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_template_ids(self) -> ZhihuEndpointTemplateRegistry:
        template_ids = [item.template_id for item in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("Zhihu endpoint template ids must be unique")
        return self


class ZhihuBrowserResponseEnvelope(AStockModel):
    author_source_id: str = Field(min_length=1)
    response_kind: ZhihuResponseKind
    content_type: ZhihuContentType | None = None
    content_id: str | None = None
    parent_comment_id: str | None = None
    listing_page: int | None = Field(default=None, ge=0)
    comment_page: int | None = Field(default=None, ge=0)
    request_cursor: str | None = None
    requested_url: str = Field(min_length=1)
    status_code: int = Field(ge=100, le=599)
    response_mime: str = Field(min_length=1, max_length=255)
    body_base64: str = Field(min_length=1, max_length=90_000_000)
    transport: ZhihuTransport
    captured_at: AwareDatetime

    @field_validator("body_base64")
    @classmethod
    def validate_body_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("body_base64 must be valid base64") from exc
        if len(decoded) > 64 * 1024 * 1024:
            raise ValueError("decoded Zhihu response exceeds 64 MiB")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> ZhihuBrowserResponseEnvelope:
        if self.transport not in {ZhihuTransport.CHROME, ZhihuTransport.MANUAL_IMPORT}:
            raise ValueError("response envelopes only accept Chrome or manual imports")
        if self.response_kind is ZhihuResponseKind.PROFILE:
            if self.content_type is not None or self.content_id is not None:
                raise ValueError("profile responses cannot identify content")
        elif self.response_kind is ZhihuResponseKind.LISTING:
            if (
                self.content_type is None
                or self.content_id is not None
                or self.listing_page is None
                or self.comment_page is not None
            ):
                raise ValueError("listing responses require content_type and no content_id")
        elif self.response_kind in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            if self.content_type is None or not self.content_id:
                raise ValueError("comment responses require content identity")
            if self.comment_page is None or self.listing_page is not None:
                raise ValueError("comment responses require comment_page only")
        else:
            if self.content_type is None or not self.content_id:
                raise ValueError("content detail responses require content identity")
            if (
                self.listing_page is not None
                or self.comment_page is not None
                or self.request_cursor is not None
            ):
                raise ValueError("content detail responses cannot declare page cursors")
        page_number = self.listing_page if self.listing_page is not None else self.comment_page
        if page_number == 0 and self.request_cursor is not None:
            raise ValueError("the first page cannot declare a prior cursor")
        if page_number is not None and page_number > 0 and not self.request_cursor:
            raise ValueError("continued pages require request_cursor")
        if self.response_kind is ZhihuResponseKind.PROFILE and (
            self.listing_page is not None
            or self.comment_page is not None
            or self.request_cursor is not None
        ):
            raise ValueError("profile responses cannot declare page cursors")
        if (
            self.response_kind is ZhihuResponseKind.CHILD_COMMENTS
            and not self.parent_comment_id
        ):
            raise ValueError("child comment responses require parent_comment_id")
        if (
            self.response_kind is not ZhihuResponseKind.CHILD_COMMENTS
            and self.parent_comment_id is not None
        ):
            raise ValueError("parent_comment_id is only valid for child comments")
        return self

    def decoded_body(self) -> bytes:
        return base64.b64decode(self.body_base64, validate=True)


class ZhihuImportedResponse(AStockModel):
    envelope_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    response_kind: ZhihuResponseKind
    content_type: ZhihuContentType | None = None
    content_id: str | None = None
    parent_comment_id: str | None = None
    listing_page: int | None = Field(default=None, ge=0)
    comment_page: int | None = Field(default=None, ge=0)
    request_cursor: str | None = None
    requested_url: str = Field(min_length=1)
    status_code: int = Field(ge=100, le=599)
    response_mime: str = Field(min_length=1, max_length=255)
    transport: ZhihuTransport
    source_snapshot_id: str = Field(min_length=1)
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_byte_size: int = Field(ge=0)
    import_status: ZhihuImportStatus = ZhihuImportStatus.PENDING
    captured_at: AwareDatetime
    imported_at: AwareDatetime
    consumed_at: AwareDatetime | None = None


class KnowledgeCollectionScope(AStockModel):
    history_mode: str = Field(min_length=1)
    content_types: list[str] = Field(min_length=1)
    include_question_context: bool
    include_required_comment_pages: bool
    include_nested_replies: bool
    derive_author_participation_chains: bool
    incremental_updates: bool

    @model_validator(mode="after")
    def validate_content_types(self) -> KnowledgeCollectionScope:
        if len(self.content_types) != len(set(self.content_types)):
            raise ValueError("knowledge content types must be unique")
        return self


class KnowledgeLocalSeedSource(AStockModel):
    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    file_version: str = Field(min_length=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_block_count: int | None = Field(default=None, ge=0)
    rights_status: str = Field(min_length=1)
    ingestion_scope: str = Field(min_length=1)
    online_history_coverage: str = Field(min_length=1)


class KnowledgeSourceDefinition(AStockModel):
    source_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    platform: str = Field(pattern=r"^zhihu$")
    profile_url: str | None = None
    platform_user_id: str | None = None
    url_token: str | None = None
    identity_status: KnowledgeIdentityStatus
    access_status: KnowledgeAccessStatus
    collection_scope: KnowledgeCollectionScope
    local_seed_sources: list[KnowledgeLocalSeedSource] = Field(default_factory=list)
    online_collection_required: bool = True
    rights_status: str = Field(min_length=1)
    enabled: bool

    @model_validator(mode="after")
    def validate_identity_and_scope(self) -> KnowledgeSourceDefinition:
        if self.identity_status is KnowledgeIdentityStatus.CONFIRMED:
            if not self.profile_url or not self.url_token:
                raise ValueError("confirmed Zhihu sources require profile_url and url_token")
        if self.identity_status is KnowledgeIdentityStatus.PENDING_IDENTITY_CONFIRMATION:
            if self.enabled:
                raise ValueError("pending Zhihu identities cannot be enabled")
            if self.profile_url or self.platform_user_id or self.url_token:
                raise ValueError("pending Zhihu identities cannot contain guessed identifiers")
        if (
            self.identity_status
            is KnowledgeIdentityStatus.LOCAL_EXPORT_USER_CONFIRMED_COMPLETE
        ):
            if self.online_collection_required or not self.local_seed_sources:
                raise ValueError("complete local exports require a seed and no online collection")
        for seed in self.local_seed_sources:
            if seed.author_source_id != self.source_id:
                raise ValueError("local seed author_source_id must match its source")
        return self


class KnowledgeSourceRegistry(AStockModel):
    sources: list[KnowledgeSourceDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_ids(self) -> KnowledgeSourceRegistry:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("knowledge source ids must be unique")
        return self


class ZhihuAuthorIdentity(AStockModel):
    author_source_id: str = Field(min_length=1)
    platform_user_id: str = Field(min_length=1)
    url_token: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    profile_url: str = Field(min_length=1)
    identity_status: KnowledgeIdentityStatus = KnowledgeIdentityStatus.CONFIRMED
    profile_snapshot_id: str = Field(min_length=1)
    profile_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: AwareDatetime


class ZhihuListingPage(AStockModel):
    page_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    listing_page: int = Field(ge=0)
    request_url: str = Field(min_length=1)
    request_cursor: str | None = None
    next_cursor: str | None = None
    is_end: bool
    content_ids: list[str]
    source_snapshot_id: str = Field(min_length=1)
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport: ZhihuTransport
    http_status: int = Field(ge=100, le=599)
    response_structure_version: str = Field(min_length=1)
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def validate_content_ids_and_cursor(self) -> ZhihuListingPage:
        if len(self.content_ids) != len(set(self.content_ids)):
            raise ValueError("listing page content ids must be unique")
        if not self.is_end and not self.next_cursor:
            raise ValueError("non-terminal listing pages require a next cursor")
        return self


class ZhihuContentRecord(AStockModel):
    version_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    platform_author_id: str | None = None
    content_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    canonical_url: str = Field(min_length=1)
    title: str | None = None
    question_id: str | None = None
    question_title: str | None = None
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    collected_at: AwareDatetime
    body_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_source_snapshot_id: str = Field(min_length=1)
    previous_version_id: str | None = None


class ZhihuCommentNode(AStockModel):
    version_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    content_id: str = Field(min_length=1)
    comment_id: str = Field(min_length=1)
    platform_author_id: str | None = None
    author_url_token: str | None = None
    author_display_name: str | None = None
    parent_comment_id: str | None = None
    reply_to_comment_id: str | None = None
    root_comment_id: str = Field(min_length=1)
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    collected_at: AwareDatetime
    like_count: int = Field(default=0, ge=0)
    child_comment_count: int = Field(default=0, ge=0)
    is_target_author: bool
    body_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_source_snapshot_id: str = Field(min_length=1)
    previous_version_id: str | None = None

    @model_validator(mode="after")
    def validate_comment_hierarchy(self) -> ZhihuCommentNode:
        if self.parent_comment_id is None and self.root_comment_id != self.comment_id:
            raise ValueError("root comments must identify themselves as root_comment_id")
        if self.parent_comment_id is not None and self.root_comment_id == self.comment_id:
            raise ValueError("child comments cannot identify themselves as the root")
        return self


class ZhihuCommentPage(AStockModel):
    page_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    content_id: str = Field(min_length=1)
    parent_comment_id: str | None = None
    comment_page: int = Field(ge=0)
    request_url: str = Field(min_length=1)
    request_cursor: str | None = None
    next_cursor: str | None = None
    is_end: bool
    comment_ids: list[str]
    source_snapshot_id: str = Field(min_length=1)
    raw_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport: ZhihuTransport
    http_status: int = Field(ge=100, le=599)
    response_structure_version: str = Field(min_length=1)
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def validate_comment_ids_and_cursor(self) -> ZhihuCommentPage:
        if len(self.comment_ids) != len(set(self.comment_ids)):
            raise ValueError("comment page ids must be unique")
        if not self.is_end and not self.next_cursor:
            raise ValueError("non-terminal comment pages require a next cursor")
        return self


class ZhihuAuthorParticipationChain(AStockModel):
    chain_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    content_id: str = Field(min_length=1)
    root_comment_id: str = Field(min_length=1)
    target_author_comment_ids: list[str] = Field(min_length=1)
    ordered_context_comment_ids: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    selection_rule_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_participation_chain(self) -> ZhihuAuthorParticipationChain:
        if len(self.target_author_comment_ids) != len(set(self.target_author_comment_ids)):
            raise ValueError("target author comment ids must be unique")
        if len(self.ordered_context_comment_ids) != len(
            set(self.ordered_context_comment_ids)
        ):
            raise ValueError("participation context comment ids must be unique")
        if not set(self.target_author_comment_ids).issubset(
            self.ordered_context_comment_ids
        ):
            raise ValueError("participation context must contain every target author comment")
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("participation chain snapshot ids must be unique")
        return self


class ZhihuCollectionGap(AStockModel):
    gap_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: ZhihuContentType
    listing_page: int = Field(ge=0)
    listing_cursor: str | None = None
    failure_class: str = Field(min_length=1)
    retryable: bool
    source_snapshot_id: str | None = None
    status: str = Field(min_length=1)


class CollectionCheckpoint(AStockModel):
    """Fine-grained, resumable cursor for an allowlisted-author collection."""

    author: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    listing_page: int = Field(ge=0)
    listing_cursor: str | None = None
    content_id: str | None = None
    comment_parent_id: str | None = None
    comment_page: int | None = Field(default=None, ge=0)
    comment_cursor: str | None = None
    nested_reply_cursor: str | None = None
    terminal_condition: CollectionTerminalCondition | None = None


class PositionAction(StrEnum):
    HOLD = "HOLD"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    REVIEW = "REVIEW"


class PositionLifecycleStage(StrEnum):
    ENTRY = "ENTRY"
    HOLDING = "HOLDING"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    REVIEW = "REVIEW"


class PositionLifecycleSkillManifest(AStockModel):
    skill_id: str
    version: str
    stage: PositionLifecycleStage
    sources: list[str]
    required_evidence: list[str]
    rules: list[dict[str, Any]]
    invalidation: list[str]
    failure_modes: list[str]
    output_schema: str
    approval_status: str


class AuthorSkillCoverage(AStockModel):
    author_id: str
    source_snapshot_ids: list[str]
    selection_skill_coverage: CoverageStatus
    entry_skill_coverage: CoverageStatus
    holding_skill_coverage: CoverageStatus
    add_skill_coverage: CoverageStatus
    trim_skill_coverage: CoverageStatus
    exit_skill_coverage: CoverageStatus
    risk_skill_coverage: CoverageStatus
    evidence_count_by_stage: dict[str, int]
    coverage_status: CoverageStatus
    missing_stages: list[PositionLifecycleStage]
    review_status: str


class PositionMonitoringPlan(AStockModel):
    plan_id: str | None = None
    position_id: str
    company_id: str
    decision_id: str
    decision_reference_status: str | None = None
    base_case_id: str | None = None
    route_plan_id: str | None = None
    memo_id: str | None = None
    as_of: AwareDatetime | None = None
    rules_version: str | None = None
    thesis_summary: str
    entry_assumptions: list[str]
    holding_horizon: str
    key_value_drivers: list[str]
    validation_metrics: list[dict[str, Any]]
    monitoring_sources: list[str]
    monitoring_cadence: dict[str, str]
    price_rules: list[dict[str, Any]]
    fundamental_rules: list[dict[str, Any]]
    event_rules: list[dict[str, Any]]
    add_conditions: list[str]
    trim_conditions: list[str]
    exit_conditions: list[str]
    invalidation_conditions: list[str]
    manual_information_needs: list[str]
    last_review_at: AwareDatetime | None = None
    next_review_at: AwareDatetime | None = None
    skill_versions: dict[str, str]
    evidence_snapshot_id: str
    baseline_evidence_ids: list[str] = Field(default_factory=list)
    coverage_status: str | None = None

    @model_validator(mode="after")
    def validate_registered_plan(self) -> PositionMonitoringPlan:
        if len(self.baseline_evidence_ids) != len(set(self.baseline_evidence_ids)):
            raise ValueError("monitoring plan baseline evidence ids must be unique")
        if self.plan_id is not None and any(
            value is None
            for value in (
                self.base_case_id,
                self.route_plan_id,
                self.memo_id,
                self.as_of,
                self.rules_version,
                self.decision_reference_status,
                self.coverage_status,
            )
        ):
            raise ValueError("registered monitoring plans require frozen research lineage")
        if (
            self.last_review_at is not None
            and self.next_review_at is not None
            and self.next_review_at <= self.last_review_at
        ):
            raise ValueError("next monitoring review must follow the last review")
        return self


class HoldingEvidenceUpdate(AStockModel):
    update_id: str | None = None
    plan_id: str | None = None
    rules_version: str | None = None
    position_id: str
    from_as_of: AwareDatetime
    to_as_of: AwareDatetime
    added_evidence_ids: list[str]
    changed_claim_ids: list[str]
    invalidated_evidence_ids: list[str]
    unresolved_conflicts: list[str]
    update_hash: str

    @model_validator(mode="after")
    def validate_incremental_window(self) -> HoldingEvidenceUpdate:
        if self.to_as_of <= self.from_as_of:
            raise ValueError("holding evidence update window must move forward")
        for label, values in (
            ("added evidence", self.added_evidence_ids),
            ("changed claim", self.changed_claim_ids),
            ("invalidated evidence", self.invalidated_evidence_ids),
            ("unresolved conflict", self.unresolved_conflicts),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"holding update {label} ids must be unique")
        return self


class HoldingReviewPack(AStockModel):
    review_id: str | None = None
    plan_id: str | None = None
    evidence_update_id: str | None = None
    rules_version: str | None = None
    position_id: str
    as_of: AwareDatetime
    new_market_data: list[dict[str, Any]]
    new_disclosures: list[dict[str, Any]]
    new_regulatory_events: list[dict[str, Any]]
    new_industry_data: list[dict[str, Any]]
    new_news_leads: list[dict[str, Any]]
    manual_evidence_updates: list[dict[str, Any]]
    thesis_strength_change: str
    risk_change: str
    triggered_rules: list[str]
    unresolved_conflicts: list[str]
    recommended_action: PositionAction
    action_confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    next_review_conditions: list[str]
    hard_blocks: list[str] = Field(default_factory=list)
    degradation_codes: list[str] = Field(default_factory=list)
    proposal_id: str | None = None

    @model_validator(mode="after")
    def validate_review_sets(self) -> HoldingReviewPack:
        for label, values in (
            ("triggered rule", self.triggered_rules),
            ("unresolved conflict", self.unresolved_conflicts),
            ("evidence", self.evidence_ids),
            ("next review condition", self.next_review_conditions),
            ("hard block", self.hard_blocks),
            ("degradation code", self.degradation_codes),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"holding review {label} values must be unique")
        return self


class PositionActionProposal(AStockModel):
    proposal_id: str
    position_id: str
    action: PositionAction
    qty_or_weight_limit: str | None = None
    reasons: list[str]
    evidence_ids: list[str]
    hard_blocks: list[str]
    requires_user_confirmation: bool = True
    plan_id: str | None = None
    review_id: str | None = None

    @model_validator(mode="after")
    def enforce_manual_confirmation(self) -> PositionActionProposal:
        if not self.requires_user_confirmation:
            raise ValueError("position action proposals always require user confirmation")
        for label, values in (
            ("reason", self.reasons),
            ("evidence", self.evidence_ids),
            ("hard block", self.hard_blocks),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"position proposal {label} values must be unique")
        return self


class ExitReviewPack(AStockModel):
    position_id: str
    original_thesis: str
    subsequent_facts: list[str]
    correct_judgments: list[str]
    incorrect_judgments: list[str]
    pnl_attribution: dict[str, float]
    beta_attribution: dict[str, float]
    skill_incremental_value: dict[str, float]
    rule_change_proposals: list[str]
    evidence_ids: list[str]


class AuthorCollectionCoverageReport(AStockModel):
    report_id: str | None = None
    author_id: str
    content_type: str
    discovered_count: int = Field(ge=0)
    scheduled_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    restricted_count: int = Field(ge=0)
    skipped_duplicate_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    last_page_or_cursor: str | None = None
    terminal_condition: CollectionTerminalCondition
    coverage_status: CoverageStatus
    source_snapshot_ids: list[str] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage_counts(self) -> AuthorCollectionCoverageReport:
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("coverage snapshot ids must be unique")
        return self


class KnowledgeLocalCoverageReport(AStockModel):
    report_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    seed_source_id: str = Field(min_length=1)
    coverage_basis: str = Field(pattern=r"^USER_CONFIRMED_COMPLETE_EXPORT$")
    expected_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_id: str | None = None
    source_snapshot_id: str | None = None
    parse_report_id: str | None = None
    expected_block_count: int = Field(ge=0)
    registered_block_count: int = Field(ge=0)
    verified_text_object_count: int = Field(ge=0)
    verified_metadata_object_count: int = Field(ge=0)
    missing_object_count: int = Field(ge=0)
    file_hash_matches: bool
    raw_object_verified: bool
    source_snapshot_matches: bool
    parse_report_verified: bool
    block_id_set_matches: bool
    block_set_object_verified: bool
    status: KnowledgeAuditStatus
    findings: list[str] = Field(default_factory=list)
    audited_at: AwareDatetime

    @model_validator(mode="after")
    def validate_complete_local_export(self) -> KnowledgeLocalCoverageReport:
        if self.verified_text_object_count > self.registered_block_count:
            raise ValueError("verified text objects cannot exceed registered blocks")
        if self.verified_metadata_object_count > self.registered_block_count:
            raise ValueError("verified metadata objects cannot exceed registered blocks")
        if self.status is KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT:
            checks = (
                self.file_hash_matches,
                self.raw_object_verified,
                self.source_snapshot_matches,
                self.parse_report_verified,
                self.block_id_set_matches,
                self.block_set_object_verified,
                self.registered_block_count == self.expected_block_count,
                self.verified_text_object_count == self.registered_block_count,
                self.verified_metadata_object_count == self.registered_block_count,
                self.missing_object_count == 0,
                not self.findings,
            )
            if not all(checks):
                raise ValueError("complete local export requires every integrity check")
        return self


class KnowledgeScopeCoverageAudit(AStockModel):
    content_type: str = Field(min_length=1)
    listing_report_id: str | None = None
    listing_terminal_condition: CollectionTerminalCondition | None = None
    listing_coverage_status: CoverageStatus | None = None
    sqlite_content_version_count: int = Field(ge=0)
    parquet_content_version_count: int = Field(ge=0)
    verified_content_body_count: int = Field(ge=0)
    missing_content_body_count: int = Field(ge=0)
    missing_content_parquet_count: int = Field(ge=0)
    orphan_content_parquet_count: int = Field(ge=0)
    content_parquet_hash_mismatch_count: int = Field(ge=0)
    content_parquet_read_error_count: int = Field(ge=0)
    sqlite_comment_version_count: int = Field(ge=0)
    parquet_comment_version_count: int = Field(ge=0)
    verified_comment_body_count: int = Field(ge=0)
    missing_comment_body_count: int = Field(ge=0)
    missing_comment_parquet_count: int = Field(ge=0)
    orphan_comment_parquet_count: int = Field(ge=0)
    comment_parquet_hash_mismatch_count: int = Field(ge=0)
    comment_parquet_read_error_count: int = Field(ge=0)
    root_comment_required_count: int = Field(ge=0)
    root_comment_terminal_count: int = Field(ge=0)
    child_reply_required_count: int = Field(ge=0)
    child_reply_terminal_count: int = Field(ge=0)
    open_gap_count: int = Field(ge=0)
    status: KnowledgeAuditStatus
    findings: list[str] = Field(default_factory=list)


class KnowledgeSourceCoverageAudit(AStockModel):
    source_id: str = Field(min_length=1)
    online_collection_required: bool
    identity_status: KnowledgeIdentityStatus
    identity_registered: bool
    scope_reports: list[KnowledgeScopeCoverageAudit] = Field(default_factory=list)
    local_report_ids: list[str] = Field(default_factory=list)
    pending_import_count: int = Field(ge=0)
    open_gap_count: int = Field(ge=0)
    status: KnowledgeAuditStatus
    findings: list[str] = Field(default_factory=list)


class KnowledgeCoverageAuditReport(AStockModel):
    report_id: str = Field(min_length=1)
    source_reports: list[KnowledgeSourceCoverageAudit]
    total_open_gap_count: int = Field(ge=0)
    total_pending_import_count: int = Field(ge=0)
    stale_running_job_count: int = Field(ge=0)
    missing_object_count: int = Field(ge=0)
    parquet_mismatch_count: int = Field(ge=0)
    status: KnowledgeAuditStatus
    findings: list[str] = Field(default_factory=list)
    audited_at: AwareDatetime
