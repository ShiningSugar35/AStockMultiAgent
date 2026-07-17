"""Position-lifecycle and allowlisted-author coverage contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    AUTHOR_SILENT = "AUTHOR_SILENT"
    INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
    CONFLICTING_SOURCE = "CONFLICTING_SOURCE"
    PARTIAL = "PARTIAL"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"


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
    position_id: str
    company_id: str
    decision_id: str
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


class HoldingEvidenceUpdate(AStockModel):
    position_id: str
    from_as_of: AwareDatetime
    to_as_of: AwareDatetime
    added_evidence_ids: list[str]
    changed_claim_ids: list[str]
    invalidated_evidence_ids: list[str]
    unresolved_conflicts: list[str]
    update_hash: str


class HoldingReviewPack(AStockModel):
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


class PositionActionProposal(AStockModel):
    proposal_id: str
    position_id: str
    action: PositionAction
    qty_or_weight_limit: str | None = None
    reasons: list[str]
    evidence_ids: list[str]
    hard_blocks: list[str]
    requires_user_confirmation: bool = True


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
