"""Position-lifecycle and allowlisted-author coverage contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

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
    gaps: list[dict[str, Any]] = Field(default_factory=list)
