"""Evidence-bounded, research-only candidate registry contracts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import InstrumentType, Market
from astock.schemas.reference_data import InstrumentRecord


class CandidateArtifactRole(StrEnum):
    INSTRUMENT_TRADABILITY = "INSTRUMENT_TRADABILITY"
    TRADING_CALENDAR = "TRADING_CALENDAR"
    DAILY_LOCAL_VERSIONED = "DAILY_LOCAL_VERSIONED"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    DATA_QUALITY = "DATA_QUALITY"
    ANNOUNCEMENT_EVENTS = "ANNOUNCEMENT_EVENTS"
    FINANCIAL_INTEGRITY = "FINANCIAL_INTEGRITY"
    USER_WATCHLIST = "USER_WATCHLIST"
    HOLDING_REVIEW = "HOLDING_REVIEW"


class CandidateCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class CandidatePitStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    DOCUMENT_RECONSTRUCTED = "DOCUMENT_RECONSTRUCTED"
    NOT_PIT_SAFE = "NOT_PIT_SAFE"


class CandidateSourceMode(StrEnum):
    LOCAL = "LOCAL"
    RECORDED = "RECORDED"
    LIVE = "LIVE"


class CandidateTradability(StrEnum):
    TRADABLE = "TRADABLE"
    NON_TRADABLE = "NON_TRADABLE"
    DELISTED = "DELISTED"
    INDEX_CONTEXT = "INDEX_CONTEXT"


class CandidateQualityStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


class CandidateEvidenceSeverity(StrEnum):
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CandidateHoldingChange(StrEnum):
    NEW_EVIDENCE = "NEW_EVIDENCE"
    INVALIDATING_EVIDENCE = "INVALIDATING_EVIDENCE"
    UNCHANGED = "UNCHANGED"


class CandidateSignalType(StrEnum):
    ANNOUNCEMENT_EVENT = "ANNOUNCEMENT_EVENT"
    FINANCIAL_ANOMALY = "FINANCIAL_ANOMALY"
    PRICE_VOLUME_CLUE = "PRICE_VOLUME_CLUE"
    USER_WATCHLIST = "USER_WATCHLIST"
    HOLDING_REVIEW = "HOLDING_REVIEW"
    QUALITY_GATE = "QUALITY_GATE"
    LIQUIDITY_GATE = "LIQUIDITY_GATE"
    TRADABILITY_GATE = "TRADABILITY_GATE"


class CandidateSignalDisposition(StrEnum):
    SUPPORT = "SUPPORT"
    WEAK_CLUE = "WEAK_CLUE"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    GATE_PASS = "GATE_PASS"
    GATE_DEGRADED = "GATE_DEGRADED"
    GATE_FAIL = "GATE_FAIL"
    EXCLUDED_FUTURE = "EXCLUDED_FUTURE"
    EXCLUDED_NOT_PIT_SAFE = "EXCLUDED_NOT_PIT_SAFE"
    EXCLUDED_DUPLICATE = "EXCLUDED_DUPLICATE"


class CandidateStrength(StrEnum):
    NONE = "NONE"
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class CandidateLifecycleStatus(StrEnum):
    OBSERVATION = "OBSERVATION"
    RESEARCH_READY = "RESEARCH_READY"
    REVIEW_DUE = "REVIEW_DUE"
    CLOSED = "CLOSED"


class CandidateEvaluationStatus(StrEnum):
    EVALUATED = "EVALUATED"
    NEEDS_INFO = "NEEDS_INFO"


class CandidateScanStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_INFO = "NEEDS_INFO"
    FAILED = "FAILED"


class CandidateCheckpointStep(StrEnum):
    INPUT_REGISTERED = "INPUT_REGISTERED"
    INPUTS_VALIDATED = "INPUTS_VALIDATED"
    SIGNALS_WRITTEN = "SIGNALS_WRITTEN"
    CANDIDATES_WRITTEN = "CANDIDATES_WRITTEN"
    REGISTRY_COMMITTED = "REGISTRY_COMMITTED"
    COMPLETE = "COMPLETE"


class CandidateAuditStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class CandidateInputArtifact(AStockModel):
    artifact_id: str = Field(min_length=1)
    role: CandidateArtifactRole
    artifact_type: str = Field(min_length=1)
    artifact_schema_version: str = Field(min_length=1)
    dataset_kind: str = Field(min_length=1)
    formal_status: str = Field(min_length=1)
    source_family: str = Field(min_length=1)
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_status: CandidateCoverageStatus
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    source_snapshot_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sets(self) -> CandidateInputArtifact:
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("source_snapshot_ids must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")
        return self


class CandidateInstrumentUniverseProof(AStockModel):
    """Exact bounded instrument subset derived from one frozen ResearchSeedReport."""

    schema_version: str = "candidate-instrument-universe-proof-v1"
    proof_id: str = Field(min_length=1)
    seed_report_artifact_id: str = Field(min_length=1)
    seed_report_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_instrument_artifact_id: str = Field(min_length=1)
    parent_instrument_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_release_id: str = Field(min_length=1)
    as_of: AwareDatetime
    company_ids: list[str] = Field(min_length=1)
    instruments: list[InstrumentRecord] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    completeness_basis: Literal["RESEARCH_SEED_REPORT"] = "RESEARCH_SEED_REPORT"
    recommendation_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_proof(self) -> CandidateInstrumentUniverseProof:
        for label, values in (
            ("company", self.company_ids),
            ("snapshot", self.source_snapshot_ids),
        ):
            if values != sorted(set(values)):
                raise ValueError(
                    f"candidate instrument proof {label} values must be sorted and unique"
                )
        instruments = sorted(self.instruments, key=lambda item: item.instrument_id)
        if self.instruments != instruments:
            raise ValueError("candidate instrument proof instruments must be sorted")
        if len({item.instrument_id for item in self.instruments}) != len(self.instruments):
            raise ValueError("candidate instrument proof instruments must be unique")
        if self.company_ids != sorted(item.symbol for item in self.instruments):
            raise ValueError("candidate instrument proof company ids must match instrument symbols")
        if any(
            item.available_to_system_at > self.as_of
            or item.source_snapshot_id not in self.source_snapshot_ids
            for item in self.instruments
        ):
            raise ValueError(
                "candidate instrument proof contains future or unbound instrument facts"
            )
        return self


class CandidateDailyPoint(AStockModel):
    session_date: date
    close: Decimal = Field(gt=0, allow_inf_nan=False)
    volume: Decimal = Field(ge=0, allow_inf_nan=False)
    turnover_cny: Decimal = Field(ge=0, allow_inf_nan=False)
    source_artifact_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus

    @model_validator(mode="after")
    def validate_availability(self) -> CandidateDailyPoint:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("daily point cannot be available before it is observed")
        return self


class CandidateAnnouncementEvent(AStockModel):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    severity: CandidateEvidenceSeverity
    source_artifact_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_availability(self) -> CandidateAnnouncementEvent:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("announcement cannot be available before it is observed")
        return self


class CandidateAnnouncementEventPack(AStockModel):
    """Registered deterministic classification of official announcement events."""

    schema_version: str = "candidate-announcement-event-pack-v1"
    pack_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    coverage_status: CandidateCoverageStatus
    pit_status: CandidatePitStatus
    source_snapshot_ids: list[str] = Field(min_length=1)
    events: list[CandidateAnnouncementEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pack(self) -> CandidateAnnouncementEventPack:
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("announcement pack source snapshots must be unique")
        if len({item.event_id for item in self.events}) != len(self.events):
            raise ValueError("announcement pack event ids must be unique")
        return self


class CandidateFinancialFlag(AStockModel):
    finding_id: str = Field(min_length=1)
    severity: CandidateEvidenceSeverity
    evidence_closed: bool
    source_artifact_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_availability(self) -> CandidateFinancialFlag:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("financial flag cannot be available before it is observed")
        return self


class CandidateWatchlistIntent(AStockModel):
    intent_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_availability(self) -> CandidateWatchlistIntent:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("watchlist intent cannot be available before it is observed")
        return self


class CandidateHoldingObservation(AStockModel):
    review_id: str = Field(min_length=1)
    change: CandidateHoldingChange
    source_artifact_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_availability(self) -> CandidateHoldingObservation:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("holding review cannot be available before it is observed")
        return self


class CandidateCompanyInput(AStockModel):
    company_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    instrument_type: InstrumentType
    tradability: CandidateTradability
    instrument_artifact_id: str = Field(min_length=1)
    calendar_artifact_id: str = Field(min_length=1)
    daily_artifact_id: str = Field(min_length=1)
    corporate_action_artifact_id: str = Field(min_length=1)
    quality_artifact_id: str = Field(min_length=1)
    announcement_artifact_id: str = Field(min_length=1)
    financial_artifact_id: str = Field(min_length=1)
    quality_status: CandidateQualityStatus
    daily_points: list[CandidateDailyPoint] = Field(default_factory=list)
    announcement_events: list[CandidateAnnouncementEvent] = Field(default_factory=list)
    financial_flags: list[CandidateFinancialFlag] = Field(default_factory=list)
    watchlist_intents: list[CandidateWatchlistIntent] = Field(default_factory=list)
    holding_observations: list[CandidateHoldingObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity(self) -> CandidateCompanyInput:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("instrument_id must be market:symbol")
        if self.instrument_type is InstrumentType.INDEX:
            if self.tradability is not CandidateTradability.INDEX_CONTEXT:
                raise ValueError("indices must be INDEX_CONTEXT")
        elif self.tradability is CandidateTradability.INDEX_CONTEXT:
            raise ValueError("only indices may be INDEX_CONTEXT")
        if len({item.session_date for item in self.daily_points}) != len(self.daily_points):
            raise ValueError("daily point session dates must be unique")
        return self


class CandidateInputRelease(AStockModel):
    """Immutable normalized scan input with complete upstream lineage."""

    schema_version: str = "candidate-input-release-v1"
    input_release_id: str = Field(min_length=1)
    as_of: AwareDatetime
    source_mode: CandidateSourceMode = CandidateSourceMode.LOCAL
    artifacts: list[CandidateInputArtifact] = Field(min_length=1)
    companies: list[CandidateCompanyInput] = Field(min_length=1)
    expected_company_ids: list[str] = Field(min_length=1)
    expected_company_count: int = Field(gt=0)
    company_universe_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_proof_artifact_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_release(self) -> CandidateInputRelease:
        artifact_by_id = {item.artifact_id: item for item in self.artifacts}
        if len(artifact_by_id) != len(self.artifacts):
            raise ValueError("candidate input artifact ids must be unique")
        if len({item.company_id for item in self.companies}) != len(self.companies):
            raise ValueError("candidate company ids must be unique within a release")
        if len({item.instrument_id for item in self.companies}) != len(self.companies):
            raise ValueError("candidate instrument ids must be unique within a release")
        if len(self.expected_company_ids) != len(set(self.expected_company_ids)):
            raise ValueError("expected company ids must be unique")
        if len(self.coverage_proof_artifact_ids) != len(set(self.coverage_proof_artifact_ids)):
            raise ValueError("coverage proof artifact ids must be unique")
        required_roles = {
            CandidateArtifactRole.INSTRUMENT_TRADABILITY,
            CandidateArtifactRole.TRADING_CALENDAR,
            CandidateArtifactRole.DAILY_LOCAL_VERSIONED,
            CandidateArtifactRole.CORPORATE_ACTION,
            CandidateArtifactRole.DATA_QUALITY,
            CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
            CandidateArtifactRole.FINANCIAL_INTEGRITY,
        }
        if not required_roles.issubset({item.role for item in self.artifacts}):
            raise ValueError("candidate input release is missing required artifact roles")
        expected_fields = {
            "instrument_artifact_id": CandidateArtifactRole.INSTRUMENT_TRADABILITY,
            "calendar_artifact_id": CandidateArtifactRole.TRADING_CALENDAR,
            "daily_artifact_id": CandidateArtifactRole.DAILY_LOCAL_VERSIONED,
            "corporate_action_artifact_id": CandidateArtifactRole.CORPORATE_ACTION,
            "quality_artifact_id": CandidateArtifactRole.DATA_QUALITY,
            "announcement_artifact_id": CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
            "financial_artifact_id": CandidateArtifactRole.FINANCIAL_INTEGRITY,
        }
        for company in self.companies:
            for field_name, role in expected_fields.items():
                artifact = artifact_by_id.get(str(getattr(company, field_name)))
                if artifact is None or artifact.role is not role:
                    raise ValueError(f"{field_name} does not bind a {role.value} artifact")
            nested = [
                *company.daily_points,
                *company.announcement_events,
                *company.financial_flags,
                *company.watchlist_intents,
                *company.holding_observations,
            ]
            for item in nested:
                if item.source_artifact_id not in artifact_by_id:
                    raise ValueError("candidate input references an unknown artifact")
            role_bindings = [
                *(
                    (item, CandidateArtifactRole.DAILY_LOCAL_VERSIONED)
                    for item in company.daily_points
                ),
                *(
                    (item, CandidateArtifactRole.ANNOUNCEMENT_EVENTS)
                    for item in company.announcement_events
                ),
                *(
                    (item, CandidateArtifactRole.FINANCIAL_INTEGRITY)
                    for item in company.financial_flags
                ),
                *(
                    (item, CandidateArtifactRole.USER_WATCHLIST)
                    for item in company.watchlist_intents
                ),
                *(
                    (item, CandidateArtifactRole.HOLDING_REVIEW)
                    for item in company.holding_observations
                ),
            ]
            for item, role in role_bindings:
                artifact = artifact_by_id[item.source_artifact_id]
                if artifact.role is not role:
                    raise ValueError("candidate unit binds the wrong artifact role")
                item_evidence = set(getattr(item, "evidence_ids", []))
                if not item_evidence.issubset(artifact.evidence_ids):
                    raise ValueError("candidate unit evidence is absent from its source artifact")
        for artifact_id in self.coverage_proof_artifact_ids:
            artifact = artifact_by_id.get(artifact_id)
            if (
                artifact is None
                or artifact.role is not CandidateArtifactRole.INSTRUMENT_TRADABILITY
            ):
                raise ValueError("coverage proof must bind an instrument artifact")
        return self


class CandidateScanRequest(AStockModel):
    schema_version: str = "candidate-scan-request-v1"
    request_id: str = Field(min_length=1)
    input_release_id: str = Field(min_length=1)
    input_release_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: AwareDatetime
    rules_version: Literal["candidate-scan-v1"] = "candidate-scan-v1"
    formal_historical: bool = True
    live: bool = False


class CandidateSignal(AStockModel):
    schema_version: str = "candidate-signal-v1"
    signal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    signal_type: CandidateSignalType
    rule_version: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    source_unit_id: str = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_family: str = Field(min_length=1)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    pit_status: CandidatePitStatus
    evidence_ids: list[str] = Field(default_factory=list)
    disposition: CandidateSignalDisposition
    severity: CandidateEvidenceSeverity | None = None
    reason_codes: list[str] = Field(default_factory=list)


class CandidateRecord(AStockModel):
    schema_version: str = "candidate-record-v1"
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_release_id: str = Field(min_length=1)
    rules_version: Literal["candidate-scan-v1"] = "candidate-scan-v1"
    company_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    as_of: AwareDatetime
    lifecycle_status: CandidateLifecycleStatus
    evaluation_status: CandidateEvaluationStatus
    strength: CandidateStrength
    signal_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    quality_status: CandidateQualityStatus
    tradability: CandidateTradability
    liquidity_gate_passed: bool
    miss_count: int = Field(ge=0)
    reactivation_count: int = Field(ge=0)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_research_ready(self) -> CandidateRecord:
        if self.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY:
            if self.evaluation_status is not CandidateEvaluationStatus.EVALUATED:
                raise ValueError("RESEARCH_READY candidates must be evaluated")
            if self.strength not in {CandidateStrength.MODERATE, CandidateStrength.STRONG}:
                raise ValueError("RESEARCH_READY requires MODERATE or STRONG evidence")
            if not self.evidence_ids or not self.liquidity_gate_passed:
                raise ValueError("RESEARCH_READY requires evidence and a liquidity pass")
            if self.quality_status is CandidateQualityStatus.FAIL:
                raise ValueError("RESEARCH_READY cannot pass a failed quality gate")
            if self.tradability is not CandidateTradability.TRADABLE:
                raise ValueError("RESEARCH_READY requires TRADABLE")
        return self


class CandidateUniverseMember(AStockModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class CandidateUniverseSnapshot(AStockModel):
    schema_version: str = "candidate-universe-snapshot-v1"
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_release_id: str = Field(min_length=1)
    rules_version: Literal["candidate-scan-v1"] = "candidate-scan-v1"
    as_of: AwareDatetime
    members: list[CandidateUniverseMember]
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_members(self) -> CandidateUniverseSnapshot:
        if len({item.company_id for item in self.members}) != len(self.members):
            raise ValueError("universe member companies must be unique")
        return self


class CandidateFileDescriptor(AStockModel):
    schema_version: str = "candidate-file-descriptor-v1"
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    logical_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateSignalManifest(AStockModel):
    schema_version: str = "candidate-signal-manifest-v1"
    signal_manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_release_id: str = Field(min_length=1)
    rules_version: Literal["candidate-scan-v1"] = "candidate-scan-v1"
    signal_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor: CandidateFileDescriptor
    signal_ids: list[str]


class CandidateScanReport(AStockModel):
    schema_version: str = "candidate-scan-report-v1"
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_release_id: str = Field(min_length=1)
    rules_version: Literal["candidate-scan-v1"] = "candidate-scan-v1"
    as_of: AwareDatetime
    status: CandidateScanStatus
    checkpoint_step: CandidateCheckpointStep
    signal_manifest_id: str | None = None
    candidate_version_ids: list[str] = Field(default_factory=list)
    universe_snapshot_id: str | None = None
    needs_info_codes: list[str] = Field(default_factory=list)
    interrupted_attempt_ids: list[str] = Field(default_factory=list)


class CandidateAuditReport(AStockModel):
    schema_version: str = "candidate-audit-report-v1"
    audit_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CandidateAuditStatus
    checked_object_hashes: list[str]
    checked_parquet_paths: list[str]
    failure_codes: list[str]
    candidate_count: int = Field(ge=0)
    universe_member_count: int = Field(ge=0)


__all__ = [
    "CandidateAnnouncementEvent",
    "CandidateAnnouncementEventPack",
    "CandidateArtifactRole",
    "CandidateAuditReport",
    "CandidateAuditStatus",
    "CandidateCheckpointStep",
    "CandidateCompanyInput",
    "CandidateCoverageStatus",
    "CandidateDailyPoint",
    "CandidateEvaluationStatus",
    "CandidateEvidenceSeverity",
    "CandidateFileDescriptor",
    "CandidateFinancialFlag",
    "CandidateHoldingChange",
    "CandidateHoldingObservation",
    "CandidateInputArtifact",
    "CandidateInstrumentUniverseProof",
    "CandidateInputRelease",
    "CandidateLifecycleStatus",
    "CandidatePitStatus",
    "CandidateQualityStatus",
    "CandidateRecord",
    "CandidateScanReport",
    "CandidateScanRequest",
    "CandidateScanStatus",
    "CandidateSignal",
    "CandidateSignalDisposition",
    "CandidateSignalManifest",
    "CandidateSignalType",
    "CandidateSourceMode",
    "CandidateStrength",
    "CandidateTradability",
    "CandidateUniverseMember",
    "CandidateUniverseSnapshot",
    "CandidateWatchlistIntent",
]
