"""Canonical contracts for public response presentation and diagnostics."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field


class _PresentationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class ResponseMode(StrEnum):
    INVESTOR = "INVESTOR"
    DEVELOPER = "DEVELOPER"
    REPORT = "REPORT"


class ResponseChannel(StrEnum):
    CHAT = "CHAT"
    CLI = "CLI"
    API = "API"
    REPORT = "REPORT"


class ResponseDetail(StrEnum):
    SHORT = "SHORT"
    STANDARD = "STANDARD"
    DETAILED = "DETAILED"


class ResponseTaskType(StrEnum):
    FACT_QUERY = "FACT_QUERY"
    MARKET_STATUS = "MARKET_STATUS"
    COMPANY_QUICK_VIEW = "COMPANY_QUICK_VIEW"
    PORTFOLIO_DECISION = "PORTFOLIO_DECISION"
    DEEP_RESEARCH = "DEEP_RESEARCH"
    DEVELOPER_DIAGNOSTIC = "DEVELOPER_DIAGNOSTIC"
    FORMAL_REPORT = "FORMAL_REPORT"


class ConclusionStrength(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class FactEquivalenceStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    PASS = "PASS"
    FAIL = "FAIL"


class BudgetStatus(StrEnum):
    WITHIN_BUDGET = "WITHIN_BUDGET"
    EXCEEDED = "EXCEEDED"
    SAFE_FALLBACK = "SAFE_FALLBACK"


class ResponseContext(_PresentationModel):
    schema_version: str = "response-context-v1"
    mode: ResponseMode = ResponseMode.INVESTOR
    channel: ResponseChannel = ResponseChannel.CHAT
    task_type: ResponseTaskType = ResponseTaskType.COMPANY_QUICK_VIEW
    requested_detail: ResponseDetail = ResponseDetail.STANDARD
    request_text: str = ""
    locale: str = "zh-CN"
    diagnostic_intent_detected: bool = False
    system_error_present: bool = False
    broker_execution_allowed: bool = False


class PublicCitation(_PresentationModel):
    schema_version: str = "public-citation-v1"
    label: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)


class PublicReportReference(_PresentationModel):
    schema_version: str = "public-report-reference-v1"
    label: str = Field(default="正式报告", min_length=1, max_length=80)
    file_name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\]+$")


class FactFingerprint(_PresentationModel):
    schema_version: str = "fact-fingerprint-v2"
    entities: list[str] = Field(default_factory=list)
    security_codes: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    times: list[str] = Field(default_factory=list)
    direction_terms: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("direction_terms", "directions"),
    )
    conclusion_strength_terms: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    locked_phrases: list[str] = Field(default_factory=list)

    @property
    def directions(self) -> list[str]:
        """Read-only legacy accessor; canonical serialization uses direction_terms."""

        return self.direction_terms


class ResearchNarrativeBundle(_PresentationModel):
    """Read-only projection of existing frozen research facts for presentation."""

    schema_version: str = "research-narrative-bundle-v1"
    subject: str = Field(min_length=1, max_length=200)
    task_type: ResponseTaskType = ResponseTaskType.COMPANY_QUICK_VIEW
    headline: str = Field(
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("headline", "conclusion"),
    )
    conclusion_strength: ConclusionStrength = ConclusionStrength.UNSPECIFIED
    valuation_or_odds: list[str] = Field(default_factory=list, max_length=8)
    reasons: list[str] = Field(default_factory=list, max_length=32)
    risks: list[str] = Field(default_factory=list, max_length=16)
    change_conditions: list[str] = Field(default_factory=list, max_length=16)
    data_as_of: AwareDatetime | None = None
    citations: list[str] = Field(default_factory=list, max_length=32)
    report_path: str | None = Field(default=None, max_length=2048)
    locked_facts: FactFingerprint | None = None

    @property
    def conclusion(self) -> str:
        """Read-only legacy accessor; canonical serialization uses headline."""

        return self.headline


class InvestorPresentationModel(_PresentationModel):
    schema_version: str = "investor-presentation-v1"
    subject: str = Field(min_length=1, max_length=200)
    headline: str = Field(min_length=1, max_length=4000)
    conclusion_strength: ConclusionStrength = ConclusionStrength.UNSPECIFIED
    valuation_or_odds: list[str] = Field(default_factory=list, max_length=8)
    reasons: list[str] = Field(default_factory=list, max_length=8)
    risk: str | None = Field(default=None, max_length=2000)
    change_condition: str | None = Field(default=None, max_length=2000)
    data_as_of: str | None = Field(default=None, max_length=100)
    citations: list[str] = Field(default_factory=list, max_length=32)
    report_reference: PublicReportReference | None = None


class DeveloperDiagnosticsInput(_PresentationModel):
    schema_version: str = "developer-diagnostics-input-v1"
    user_impact: str = Field(min_length=1, max_length=4000)
    failure_class: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=200)
    stage: str | None = Field(default=None, max_length=256)
    next_action: str | None = Field(default=None, max_length=2000)


class DeveloperDiagnosticsModel(_PresentationModel):
    schema_version: str = "developer-diagnostics-v1"
    user_impact: str = Field(min_length=1, max_length=2000)
    failure_class: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=200)
    stage: str | None = Field(default=None, max_length=256)
    next_action: str | None = Field(default=None, max_length=2000)


class PresentationAudit(_PresentationModel):
    schema_version: str = "presentation-audit-v1"
    status: str = Field(pattern=r"^(PASS|FAIL)$")
    finding_codes: list[str] = Field(default_factory=list)
    character_count: int = Field(ge=0)
    character_budget: int = Field(ge=1)
    budget_status: BudgetStatus
    fact_equivalence_status: FactEquivalenceStatus
    fact_drift_detected: bool = False
    required_content_preserved: bool = True
    secret_exposed: bool = False
    private_path_exposed: bool = False
    internal_implementation_exposed: bool = False
    safe_to_send: bool
    raw_answer_echoed: bool = False


class RenderedResponse(_PresentationModel):
    schema_version: str = "rendered-response-v1"
    mode: ResponseMode
    task_type: ResponseTaskType
    text: str = Field(min_length=1)
    payload: InvestorPresentationModel | DeveloperDiagnosticsModel
    audit: PresentationAudit
    safe_fallback_used: bool = False
    raw_draft_exposed: bool = False


__all__ = [
    "BudgetStatus",
    "ConclusionStrength",
    "DeveloperDiagnosticsInput",
    "DeveloperDiagnosticsModel",
    "FactEquivalenceStatus",
    "FactFingerprint",
    "InvestorPresentationModel",
    "PresentationAudit",
    "PublicCitation",
    "PublicReportReference",
    "RenderedResponse",
    "ResearchNarrativeBundle",
    "ResponseChannel",
    "ResponseContext",
    "ResponseDetail",
    "ResponseMode",
    "ResponseTaskType",
]
