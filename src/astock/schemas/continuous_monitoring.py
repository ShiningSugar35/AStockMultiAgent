"""Durable contracts for continuous investment monitoring and event-triggered research."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.adaptation import ResearchModule
from astock.schemas.base import AStockModel
from astock.schemas.reference_data import Market


class MonitorTargetReason(StrEnum):
    ANALYZED = "ANALYZED"
    RECOMMENDED = "RECOMMENDED"
    PAPER_POSITION = "PAPER_POSITION"
    OPEN_PAPER_ORDER = "OPEN_PAPER_ORDER"
    MANUAL = "MANUAL"
    CATALYST = "CATALYST"


class MonitorTargetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REMOVED = "REMOVED"


class MonitorSource(StrEnum):
    MARKET_60M = "MARKET_60M"
    CNINFO = "CNINFO"
    GDELT = "GDELT"
    CATALYST = "CATALYST"
    SCHEDULE = "SCHEDULE"
    PAPER = "PAPER"


class MonitorSeverity(StrEnum):
    INFO = "INFO"
    WATCH = "WATCH"
    MATERIAL = "MATERIAL"
    CRITICAL = "CRITICAL"


class MonitorEventType(StrEnum):
    PRICE_BAR_UPDATED = "PRICE_BAR_UPDATED"
    PRICE_TRIGGER = "PRICE_TRIGGER"
    DRAWDOWN_TRIGGER = "DRAWDOWN_TRIGGER"
    OFFICIAL_DISCLOSURE = "OFFICIAL_DISCLOSURE"
    NEWS_LEAD = "NEWS_LEAD"
    CATALYST_DUE = "CATALYST_DUE"
    CATALYST_CHANGED = "CATALYST_CHANGED"
    SCHEDULED_REVIEW_DUE = "SCHEDULED_REVIEW_DUE"
    PAPER_REPLAY_DUE = "PAPER_REPLAY_DUE"
    DATA_SOURCE_DEGRADED = "DATA_SOURCE_DEGRADED"
    RESEARCH_TASK_CREATED = "RESEARCH_TASK_CREATED"


class MonitorMetric(StrEnum):
    LAST_PRICE = "LAST_PRICE"
    RETURN_1D = "RETURN_1D"
    RETURN_5D = "RETURN_5D"
    DRAWDOWN_FROM_WATCH_HIGH = "DRAWDOWN_FROM_WATCH_HIGH"
    VOLUME_RATIO = "VOLUME_RATIO"
    DAYS_SINCE_REVIEW = "DAYS_SINCE_REVIEW"


class MonitorComparison(StrEnum):
    GT = "GT"
    GE = "GE"
    LT = "LT"
    LE = "LE"
    EQ = "EQ"


class MonitorRuleAction(StrEnum):
    OBSERVE = "OBSERVE"
    REVIEW = "REVIEW"
    ENTER_PAPER_CANDIDATE = "ENTER_PAPER_CANDIDATE"
    ADD_REVIEW = "ADD_REVIEW"
    TRIM_REVIEW = "TRIM_REVIEW"
    EXIT_REVIEW = "EXIT_REVIEW"


class MonitorTaskPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class MonitorTaskStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MonitorRunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class MonitorDaemonState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class MonitorTargetEnrollRequest(AStockModel):
    schema_version: str = "continuous-monitor-target-enroll-request-v1"
    symbol: str = Field(pattern=r"^\d{6}$")
    market: Market
    company_id: str = Field(pattern=r"^\d{6}$")
    display_name: str = Field(min_length=1, max_length=120)
    reason: MonitorTargetReason
    aliases: list[str] = Field(default_factory=list, max_length=8)
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_aliases(self) -> MonitorTargetEnrollRequest:
        cleaned = [item.strip() for item in self.aliases if item.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("monitor target aliases must be unique")
        if any(len(item) > 120 for item in cleaned):
            raise ValueError("monitor target aliases are too long")
        object.__setattr__(self, "aliases", cleaned)
        return self


class ContinuousMonitorTarget(AStockModel):
    schema_version: str = "continuous-monitor-target-v1"
    target_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}$")
    market: Market
    company_id: str = Field(pattern=r"^\d{6}$")
    display_name: str = Field(min_length=1, max_length=120)
    reasons: list[MonitorTargetReason] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    status: MonitorTargetStatus = MonitorTargetStatus.ACTIVE
    enrolled_at: AwareDatetime
    updated_at: AwareDatetime
    last_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    high_watermark_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    last_market_at: AwareDatetime | None = None
    last_review_at: AwareDatetime | None = None
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_target(self) -> ContinuousMonitorTarget:
        if self.reasons != sorted(set(self.reasons), key=lambda item: item.value):
            raise ValueError("monitor target reasons must be sorted and unique")
        if self.aliases != sorted(set(self.aliases)):
            raise ValueError("monitor target aliases must be sorted and unique")
        return self


class MonitorRuleRequest(AStockModel):
    schema_version: str = "continuous-monitor-rule-request-v1"
    target_id: str = Field(min_length=1)
    metric: MonitorMetric
    comparison: MonitorComparison
    threshold: float = Field(allow_inf_nan=False)
    action: MonitorRuleAction
    severity: MonitorSeverity = MonitorSeverity.WATCH
    cooldown_seconds: int = Field(default=3600, ge=0, le=30 * 24 * 3600)
    affected_modules: list[ResearchModule] = Field(default_factory=list)
    natural_language_rule_execution_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_modules(self) -> MonitorRuleRequest:
        expected = sorted(set(self.affected_modules), key=lambda item: item.value)
        if self.affected_modules != expected:
            raise ValueError("monitor rule affected modules must be sorted and unique")
        if self.action is not MonitorRuleAction.OBSERVE and not self.affected_modules:
            raise ValueError("actionable monitor rules require affected modules")
        return self


class MonitorRule(MonitorRuleRequest):
    schema_version: str = "continuous-monitor-rule-v1"
    rule_id: str = Field(min_length=1)
    active: bool = True
    last_triggered_at: AwareDatetime | None = None


class MonitorEvent(AStockModel):
    schema_version: str = "continuous-monitor-event-v1"
    event_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    event_type: MonitorEventType
    severity: MonitorSeverity
    observed_at: AwareDatetime
    available_at: AwareDatetime
    source: MonitorSource
    source_ref: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dedupe_key: str = Field(min_length=1)
    affected_modules: list[ResearchModule] = Field(default_factory=list)
    requires_research: bool = False
    news_lead_only: bool = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_event(self) -> MonitorEvent:
        if self.available_at < self.observed_at:
            raise ValueError("monitor event cannot be available before it was observed")
        if self.affected_modules != sorted(set(self.affected_modules), key=lambda item: item.value):
            raise ValueError("monitor event affected modules must be sorted and unique")
        if self.event_type is MonitorEventType.NEWS_LEAD and not self.news_lead_only:
            raise ValueError("news events must remain lead-only")
        if self.event_type is not MonitorEventType.NEWS_LEAD and self.news_lead_only:
            raise ValueError("only news events may be lead-only")
        return self


class MonitorResearchTask(AStockModel):
    schema_version: str = "continuous-monitor-research-task-v1"
    task_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    requested_modules: list[ResearchModule] = Field(min_length=1)
    priority: MonitorTaskPriority
    status: MonitorTaskStatus = MonitorTaskStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: AwareDatetime
    updated_at: AwareDatetime
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_modules(self) -> MonitorResearchTask:
        if self.requested_modules != sorted(
            set(self.requested_modules), key=lambda item: item.value
        ):
            raise ValueError("monitor research task modules must be sorted and unique")
        return self


class MonitorRunReport(AStockModel):
    schema_version: str = "continuous-monitor-run-report-v1"
    run_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    status: MonitorRunStatus
    live: bool
    target_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    source_success: dict[MonitorSource, int] = Field(default_factory=dict)
    source_failure: dict[MonitorSource, int] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    broker_execution_allowed: Literal[False] = False


class MonitorDaemonStatus(AStockModel):
    schema_version: str = "continuous-monitor-daemon-status-v1"
    owner_id: str | None = None
    pid: int | None = Field(default=None, ge=1)
    state: MonitorDaemonState
    started_at: AwareDatetime | None = None
    heartbeat_at: AwareDatetime | None = None
    stop_requested: bool = False
    last_run_id: str | None = None
    details: dict[str, object] = Field(default_factory=dict)
    updated_at: AwareDatetime
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "ContinuousMonitorTarget",
    "MonitorComparison",
    "MonitorDaemonState",
    "MonitorDaemonStatus",
    "MonitorEvent",
    "MonitorEventType",
    "MonitorMetric",
    "MonitorResearchTask",
    "MonitorRule",
    "MonitorRuleAction",
    "MonitorRuleRequest",
    "MonitorRunReport",
    "MonitorRunStatus",
    "MonitorSeverity",
    "MonitorSource",
    "MonitorTargetEnrollRequest",
    "MonitorTargetReason",
    "MonitorTargetStatus",
    "MonitorTaskPriority",
    "MonitorTaskStatus",
]
