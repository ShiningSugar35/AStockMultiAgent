"""Five-family scheduled input audits over existing immutable source records.

This is a metadata audit, not a parallel fact store or a research-completion
certificate. Successful bounded discovery cannot prove that adverse news does
not exist and cannot create a buy/sell decision. Missing checks stay explicit.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from astock.core.errors import AStockError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.investor_orchestration.models import (
    CapabilityExecutionPlan,
    CapabilityRequirement,
    InvestorRequestEnvelope,
    RequestIntent,
    ScheduledDomain,
    ScheduledWindow,
    SideEffectClass,
    StrictModel,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews
from astock.investor_orchestration.scheduled_market_views import ScheduledIntradayViews
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now
from astock.monitoring.news import replay_news_search
from astock.schemas import DisclosureCategory, DisclosureSearchBatch, FetchStatus
from astock.schemas.institutional_research import MarketPriceAnchor
from astock.schemas.reference_data import DailyBarObservation, DatasetReleaseManifest


class ScheduledInputFamily(StrEnum):
    MARKET = "MARKET"
    COMPANY_NEWS = "COMPANY_NEWS"
    INDUSTRY_NEWS = "INDUSTRY_NEWS"
    DISCLOSURES = "DISCLOSURES"
    POLICIES = "POLICIES"


_SCHEDULED_MONITOR_REQUIRED = frozenset(
    {
        "REQUEST_TIME",
        "ENTITY_IDENTITY",
        "SESSION_PREFLIGHT",
        "CURRENT_MARKET",
        "MARKET_REGIME",
        "EVENT_RESEARCH",
        "SUBJECT_REGISTRY",
        "RESPONSE_GATEWAY",
    }
)
_SCHEDULED_MONITOR_PROHIBITED = frozenset({"EXTERNAL_ACCOUNT", "PAPER"})


class ScheduledSourceScope(StrictModel):
    """Explicit query/feed scope, never an assertion of global market coverage."""

    instrument_id: str
    company_query: str = Field(min_length=1, max_length=2000)
    industry_query: str = Field(min_length=1, max_length=2000)
    policy_families: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def explicit_scope(self):
        identity = normalized_instrument(self.instrument_id)
        parts = identity.split(":")
        if (
            len(parts) != 2
            or parts[0] not in {"XSHG", "XSHE", "BJSE"}
            or len(parts[1]) != 6
            or not parts[1].isascii()
            or not parts[1].isdigit()
        ):
            raise ValueError("scheduled source scope needs an explicit canonical security identity")
        if self.company_query.strip() == self.industry_query.strip():
            raise ValueError("company and industry discovery must have distinct explicit scopes")
        if len(set(self.policy_families)) != len(self.policy_families):
            raise ValueError("duplicate policy families do not increase coverage")
        return self


class ScheduledInputAuditRequest(StrictModel):
    schema_version: Literal["scheduled-input-audit-request-v1"] = "scheduled-input-audit-request-v1"
    run_id: str = Field(min_length=1)
    domain: ScheduledDomain
    window: ScheduledWindow
    scope: ScheduledSourceScope
    interval_start: AwareDatetime
    interval_end: AwareDatetime
    as_of: AwareDatetime
    market_references: tuple[str, ...] = ()
    company_news_snapshot_ids: tuple[str, ...] = ()
    industry_news_snapshot_ids: tuple[str, ...] = ()
    disclosure_batch_artifact_ids: tuple[str, ...] = ()
    policy_capture_artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded_request(self):
        if not self.interval_start < self.interval_end <= self.as_of <= utc_now():
            raise ValueError("scheduled input audit has an invalid or future time interval")
        if (self.interval_end - self.interval_start).total_seconds() > 31 * 86400:
            raise ValueError("scheduled source audit exceeds its bounded lookback")
        for values in (
            self.market_references,
            self.company_news_snapshot_ids,
            self.industry_news_snapshot_ids,
            self.disclosure_batch_artifact_ids,
            self.policy_capture_artifact_ids,
        ):
            if len(values) > 256 or len(values) != len(set(values)):
                raise ValueError("scheduled source references must be bounded and unique")
            if any(not value.strip() for value in values):
                raise ValueError("scheduled source reference must not be empty")
        return self


class ScheduledInputAuditPolicy(StrictModel):
    schema_version: Literal["scheduled-input-audit-policy-v1"] = "scheduled-input-audit-policy-v1"
    version: str = Field(min_length=1)
    maximum_price_age_seconds: dict[ScheduledWindow, int]
    maximum_search_age_seconds: int = Field(gt=0, le=7 * 86400)
    maximum_policy_capture_age_seconds: int = Field(gt=0, le=90 * 86400)

    @model_validator(mode="after")
    def validate_windows(self):
        if set(self.maximum_price_age_seconds) != set(ScheduledWindow):
            raise ValueError("input audit policy must cover every scheduled window")
        if any(
            isinstance(value, bool) or not 0 < value <= 14 * 86400
            for value in self.maximum_price_age_seconds.values()
        ):
            raise ValueError("market freshness limits must be finite positive bounded seconds")
        return self


class ScheduledFamilyCheck(StrictModel):
    family: ScheduledInputFamily
    status: Literal["CHECKED", "BOUNDED_LEADS", "MISSING", "STALE", "INCOMPLETE", "INVALID"]
    source_revisions: dict[str, str] = Field(default_factory=dict)
    finding_count: int = Field(default=0, ge=0)
    checked_through: AwareDatetime | None = None
    reason: str | None = None


class ScheduledInputCoverageReport(StrictModel):
    schema_version: Literal["scheduled-input-coverage-report-v1"] = (
        "scheduled-input-coverage-report-v1"
    )
    request: ScheduledInputAuditRequest
    policy: ScheduledInputAuditPolicy
    checks: tuple[ScheduledFamilyCheck, ...]
    all_families_checked: bool
    formal_research_complete: Literal[False] = False
    adverse_news_absence_proven: Literal[False] = False
    report_hash: str


class ScheduledRunSourceCoverage(StrictModel):
    report_ids: tuple[str, ...] = ()
    expected_subject_count: int = Field(ge=0)
    verified_subject_count: int = Field(ge=0)
    all_families_checked: bool = False
    degradation_reasons: tuple[str, ...] = ()
    checked_through: AwareDatetime | None = None


class ScheduledCapabilityCoverageReceipt(StrictModel):
    schema_version: Literal["scheduled-capability-coverage-receipt-v1"] = (
        "scheduled-capability-coverage-receipt-v1"
    )
    receipt_id: str
    policy: ScheduledInputAuditPolicy
    run_id: str
    window: ScheduledWindow
    requested_at: AwareDatetime
    subject_bindings: tuple[tuple[ScheduledDomain, str], ...]
    report_ids: tuple[str, ...]
    interval_start: AwareDatetime | None = None
    interval_end: AwareDatetime | None = None
    expected_subject_count: int = Field(ge=0)
    verified_subject_count: int = Field(ge=0)
    all_families_checked: bool = False
    degradation_reasons: tuple[str, ...] = ()
    checked_through: AwareDatetime | None = None
    semantic_capability_receipt_id: str | None = None
    semantic_coverage_complete: bool = False
    prohibited_call_count: Literal[0] = 0
    economic_write_count: Literal[0] = 0
    receipt_hash: str

    @model_validator(mode="after")
    def validate_coverage_identity(self):
        if self.subject_bindings != tuple(
            sorted(set(self.subject_bindings), key=lambda x: (x[0].value, x[1]))
        ):
            raise ValueError("scheduled capability bindings must be sorted and unique")
        if self.expected_subject_count != len(self.subject_bindings):
            raise ValueError("scheduled expected subject count must match exact bindings")
        if self.verified_subject_count > self.expected_subject_count:
            raise ValueError("scheduled verified subject count exceeds expected subjects")
        if self.all_families_checked and (
            self.expected_subject_count == 0
            or self.verified_subject_count != self.expected_subject_count
            or self.degradation_reasons
            or self.checked_through != self.interval_end
        ):
            raise ValueError("complete scheduled source coverage is internally inconsistent")
        if self.semantic_coverage_complete != bool(self.semantic_capability_receipt_id):
            raise ValueError(
                "scheduled semantic coverage must bind one authenticated capability receipt"
            )
        return self


def load_input_audit_policy() -> ScheduledInputAuditPolicy:
    """Load the one versioned source-check policy; callers cannot silently downgrade it."""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[3] / "configs/scheduled_input_audit_v1.yaml"
    return ScheduledInputAuditPolicy.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


class ScheduledInputCoverageService:
    def __init__(
        self,
        store: InvestorOrchestrationStore,
        policy: ScheduledInputAuditPolicy,
        objects: ObjectStore | None = None,
    ) -> None:
        self.store = store
        self.policy = ScheduledInputAuditPolicy.model_validate(policy.model_dump())
        self.state = StateStore(store.path)
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")
        self.verifier = RegisteredOutputVerifier(store, self.objects)
        self.views = CanonicalRegimeReferenceViews(self.state, self.objects)

    def build(self, request: ScheduledInputAuditRequest) -> ScheduledInputCoverageReport:
        request = ScheduledInputAuditRequest.model_validate(request.model_dump())
        handlers = {
            ScheduledInputFamily.MARKET: self._market,
            ScheduledInputFamily.COMPANY_NEWS: lambda r: self._news(
                r, ScheduledInputFamily.COMPANY_NEWS
            ),
            ScheduledInputFamily.INDUSTRY_NEWS: lambda r: self._news(
                r, ScheduledInputFamily.INDUSTRY_NEWS
            ),
            ScheduledInputFamily.DISCLOSURES: self._disclosures,
            ScheduledInputFamily.POLICIES: self._policies,
        }
        checks = []
        for family in ScheduledInputFamily:
            try:
                check = handlers[family](request)
            except (ValueError, AStockError, OSError) as exc:
                check = ScheduledFamilyCheck(
                    family=family, status="INVALID", reason=type(exc).__name__
                )
            checks.append(check)
        body = {
            "request": request,
            "policy": self.policy,
            "checks": tuple(checks),
            "all_families_checked": all(c.status in {"CHECKED", "BOUNDED_LEADS"} for c in checks),
        }
        return ScheduledInputCoverageReport(**body, report_hash=content_hash(body))

    def register(self, request: ScheduledInputAuditRequest) -> str:
        # The writer accepts source references, not a caller-supplied PASS report.
        report = self.build(request)
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"ScheduledInputCoverageReport:{report.report_hash}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ScheduledInputCoverageReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(
                {
                    content_hash(self.policy),
                    content_hash(report.request),
                    *(digest for c in report.checks for digest in c.source_revisions.values()),
                }
            ),
        )
        return artifact_id

    def verify_registered(self, artifact_id: str) -> ScheduledInputCoverageReport:
        record = self.state.artifact_record(artifact_id)
        if record is None or record["type"] != "ScheduledInputCoverageReport":
            raise ValueError("scheduled source coverage report is not registered")
        report = ScheduledInputCoverageReport.model_validate_json(
            self.objects.get_bytes(record["object_hash"])
        )
        if report.policy != self.policy or self.build(report.request) != report:
            raise ValueError("scheduled coverage no longer matches its canonical sources or policy")
        expected = sorted(
            {
                content_hash(self.policy),
                content_hash(report.request),
                *(v for c in report.checks for v in c.source_revisions.values()),
            }
        )
        if (
            record["input_hashes"] != expected
            or record["schema_version"] != report.schema_version
            or artifact_id != f"ScheduledInputCoverageReport:{report.report_hash}"
        ):
            raise ValueError("scheduled coverage registry lineage or identity was changed")
        return report

    def bind_run(
        self,
        *,
        run_id: str,
        window: ScheduledWindow,
        requested_at: datetime,
        subjects: dict[ScheduledDomain, tuple[str, ...]],
        report_ids: tuple[str, ...],
        interval_start: datetime | None = None,
        interval_end: datetime | None = None,
    ) -> ScheduledRunSourceCoverage:
        """Bind audited scopes to exactly this run, domain, security and interval.

        This result cannot certify semantic research or authorize an economic
        action. Empty runs do not earn complete-source coverage by vacuous truth.
        """
        if (
            requested_at.tzinfo is None
            or requested_at.utcoffset() is None
            or requested_at > utc_now()
        ):
            raise ValueError("scheduled source binding has an invalid request time")
        expected = {
            (domain, normalized_instrument(instrument))
            for domain, instruments in subjects.items()
            for instrument in instruments
        }
        if len(report_ids) > 1000 or len(report_ids) != len(set(report_ids)):
            raise ValueError("scheduled source report references must be bounded and unique")
        if report_ids:
            if interval_start is None or interval_end is None:
                raise ValueError("scheduled source reports require an explicit checked interval")
            if (
                interval_start.tzinfo is None
                or interval_end.tzinfo is None
                or interval_start.utcoffset() is None
                or interval_end.utcoffset() is None
                or not interval_start < interval_end <= requested_at
            ):
                raise ValueError("scheduled source report interval is invalid")
        seen = set()
        passed = set()
        reasons = []
        verified = []
        for artifact_id in sorted(report_ids):
            report = self.verify_registered(artifact_id)
            audit = report.request
            key = (audit.domain, normalized_instrument(audit.scope.instrument_id))
            if (
                key not in expected
                or key in seen
                or audit.run_id != run_id
                or audit.window is not window
                or audit.as_of != requested_at
                or audit.interval_start != interval_start
                or audit.interval_end != interval_end
            ):
                raise ValueError(
                    "scheduled source report belongs to another run, subject or interval"
                )
            seen.add(key)
            verified.append(artifact_id)
            if report.all_families_checked:
                passed.add(key)
            else:
                for check in report.checks:
                    if check.status not in {"CHECKED", "BOUNDED_LEADS"}:
                        reasons.append(
                            f"SOURCE_CHECK_{check.status}:{key[0].value}:{key[1]}:{check.family.value}"
                        )
        for domain, instrument in sorted(
            expected - seen, key=lambda item: (item[0].value, item[1])
        ):
            reasons.append(f"SOURCE_CHECK_MISSING:{domain.value}:{instrument}")
        if not expected:
            reasons.append("SOURCE_CHECK_NO_SUBJECTS")
        complete = bool(expected) and passed == expected
        return ScheduledRunSourceCoverage(
            report_ids=tuple(verified),
            expected_subject_count=len(expected),
            verified_subject_count=len(passed),
            all_families_checked=complete,
            degradation_reasons=tuple(reasons),
            checked_through=interval_end if complete else None,
        )

    def _semantic_coverage_hash(
        self,
        run_id: str,
        receipt_id: str | None,
        *,
        requested_at: datetime,
        subject_bindings: tuple[tuple[ScheduledDomain, str], ...],
    ) -> str | None:
        if receipt_id is None:
            return None
        request_id = f"scheduled:{run_id}"
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT c.receipt_hash, p.payload_json FROM capability_coverage_receipts c "
                "JOIN capability_execution_plans p ON p.plan_id=c.plan_id WHERE c.receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        receipt_hash = str(row["receipt_hash"])
        if not self.verifier.authenticated_coverage(request_id, receipt_id, receipt_hash):
            return None
        request_record = self.state.artifact_record(
            f"InvestorRequestEnvelope:{content_hash(request_id)}"
        )
        if request_record is None or request_record["type"] != "InvestorRequestEnvelope":
            return None
        semantic_request = InvestorRequestEnvelope.model_validate_json(
            self.objects.get_bytes(str(request_record["object_hash"]))
        )
        expected_entities = {instrument for _, instrument in subject_bindings}
        actual_entities = {normalized_instrument(item) for item in semantic_request.entity_ids}
        if (
            semantic_request.request_id != request_id
            or semantic_request.normalized_intent is not RequestIntent.MONITOR
            or semantic_request.side_effect is not SideEffectClass.META
            or semantic_request.question_time != requested_at
            or actual_entities != expected_entities
        ):
            return None
        plan = CapabilityExecutionPlan.model_validate_json(str(row["payload_json"]))
        required = {
            node.capability_id
            for node in plan.nodes
            if node.requirement is CapabilityRequirement.REQUIRED
        }
        prohibited = {
            node.capability_id
            for node in plan.nodes
            if node.requirement is CapabilityRequirement.PROHIBITED
        }
        if required != _SCHEDULED_MONITOR_REQUIRED:
            return None
        if not _SCHEDULED_MONITOR_PROHIBITED.issubset(prohibited):
            return None
        return receipt_hash

    def register_run_coverage(
        self,
        *,
        run_id: str,
        window: ScheduledWindow,
        requested_at: datetime,
        subjects: dict[ScheduledDomain, tuple[str, ...]],
        report_ids: tuple[str, ...],
        interval_start: datetime | None = None,
        interval_end: datetime | None = None,
        semantic_capability_receipt_id: str | None = None,
    ) -> ScheduledCapabilityCoverageReceipt:
        result = self.bind_run(
            run_id=run_id,
            window=window,
            requested_at=requested_at,
            subjects=subjects,
            report_ids=report_ids,
            interval_start=interval_start,
            interval_end=interval_end,
        )
        bindings = tuple(
            sorted(
                {
                    (domain, normalized_instrument(instrument))
                    for domain, instruments in subjects.items()
                    for instrument in instruments
                },
                key=lambda item: (item[0].value, item[1]),
            )
        )
        semantic_hash = self._semantic_coverage_hash(
            run_id,
            semantic_capability_receipt_id,
            requested_at=requested_at,
            subject_bindings=bindings,
        )
        if semantic_capability_receipt_id is not None and semantic_hash is None:
            raise ValueError(
                "scheduled semantic capability receipt is not an authenticated MONITOR coverage"
            )
        body = {
            "policy": self.policy,
            "run_id": run_id,
            "window": window,
            "requested_at": requested_at,
            "subject_bindings": bindings,
            "report_ids": result.report_ids,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "expected_subject_count": result.expected_subject_count,
            "verified_subject_count": result.verified_subject_count,
            "all_families_checked": result.all_families_checked,
            "degradation_reasons": result.degradation_reasons,
            "checked_through": result.checked_through,
            "semantic_capability_receipt_id": semantic_capability_receipt_id,
            "semantic_coverage_complete": semantic_hash is not None,
            "prohibited_call_count": 0,
            "economic_write_count": 0,
        }
        receipt_hash = content_hash(body)
        receipt = ScheduledCapabilityCoverageReceipt(
            receipt_id=f"ScheduledCapabilityCoverageReceipt:{receipt_hash}",
            receipt_hash=receipt_hash,
            **body,
        )
        reference = self.objects.put_json(receipt.model_dump(mode="json"))
        input_hashes = [content_hash(self.policy), content_hash({
            "run_id": run_id,
            "window": window,
            "requested_at": requested_at,
            "subject_bindings": bindings,
            "interval_start": interval_start,
            "interval_end": interval_end,
        })]
        for report_id in result.report_ids:
            record = self.state.artifact_record(report_id)
            if record is None:
                raise ValueError("scheduled source coverage artifact disappeared before binding")
            input_hashes.append(str(record["object_hash"]))
        if semantic_hash is not None:
            input_hashes.append(semantic_hash)
        self.state.register_artifact(
            artifact_id=receipt.receipt_id,
            artifact_type="ScheduledCapabilityCoverageReceipt",
            schema_version=receipt.schema_version,
            object_hash=reference.sha256,
            input_hashes=sorted(set(input_hashes)),
        )
        return receipt

    def verify_registered_run_coverage(
        self, artifact_id: str
    ) -> ScheduledCapabilityCoverageReceipt:
        record = self.state.artifact_record(artifact_id)
        if record is None or record["type"] != "ScheduledCapabilityCoverageReceipt":
            raise ValueError("scheduled capability coverage receipt is not registered")
        object_hash = str(record["object_hash"])
        receipt = ScheduledCapabilityCoverageReceipt.model_validate_json(
            self.objects.get_bytes(object_hash)
        )
        if not self.objects.verify(object_hash):
            raise ValueError("scheduled capability coverage object hash is invalid")
        if (
            receipt.receipt_id != artifact_id
            or receipt.receipt_hash != artifact_id.rsplit(":", 1)[-1]
        ):
            raise ValueError("scheduled capability coverage identity was changed")
        subjects: dict[ScheduledDomain, list[str]] = {}
        for domain, instrument in receipt.subject_bindings:
            subjects.setdefault(domain, []).append(instrument)
        normalized_subjects = {domain: tuple(values) for domain, values in subjects.items()}
        result = self.bind_run(
            run_id=receipt.run_id,
            window=receipt.window,
            requested_at=receipt.requested_at,
            subjects=normalized_subjects,
            report_ids=receipt.report_ids,
            interval_start=receipt.interval_start,
            interval_end=receipt.interval_end,
        )
        if receipt.policy != self.policy:
            raise ValueError("scheduled capability coverage policy differs from verifier policy")
        semantic_hash = self._semantic_coverage_hash(
            receipt.run_id,
            receipt.semantic_capability_receipt_id,
            requested_at=receipt.requested_at,
            subject_bindings=receipt.subject_bindings,
        )
        if receipt.semantic_coverage_complete != (semantic_hash is not None):
            raise ValueError("scheduled semantic capability coverage is no longer authentic")
        body = {
            "policy": receipt.policy,
            "run_id": receipt.run_id,
            "window": receipt.window,
            "requested_at": receipt.requested_at,
            "subject_bindings": receipt.subject_bindings,
            "report_ids": result.report_ids,
            "interval_start": receipt.interval_start,
            "interval_end": receipt.interval_end,
            "expected_subject_count": result.expected_subject_count,
            "verified_subject_count": result.verified_subject_count,
            "all_families_checked": result.all_families_checked,
            "degradation_reasons": result.degradation_reasons,
            "checked_through": result.checked_through,
            "semantic_capability_receipt_id": receipt.semantic_capability_receipt_id,
            "semantic_coverage_complete": semantic_hash is not None,
            "prohibited_call_count": 0,
            "economic_write_count": 0,
        }
        if receipt.receipt_hash != content_hash(body):
            raise ValueError("scheduled capability coverage no longer matches canonical sources")
        input_hashes = [
            content_hash(self.policy),
            content_hash(
                {
                    "run_id": receipt.run_id,
                    "window": receipt.window,
                    "requested_at": receipt.requested_at,
                    "subject_bindings": receipt.subject_bindings,
                    "interval_start": receipt.interval_start,
                    "interval_end": receipt.interval_end,
                }
            ),
        ]
        for report_id in result.report_ids:
            source = self.state.artifact_record(report_id)
            if source is None:
                raise ValueError("scheduled source coverage artifact disappeared after binding")
            input_hashes.append(str(source["object_hash"]))
        if semantic_hash is not None:
            input_hashes.append(semantic_hash)
        if record["input_hashes"] != sorted(set(input_hashes)):
            raise ValueError("scheduled capability coverage lineage was changed")
        return receipt

    @staticmethod
    def _missing(family: ScheduledInputFamily, reason: str) -> ScheduledFamilyCheck:
        return ScheduledFamilyCheck(family=family, status="MISSING", reason=reason)

    def _verified_anchor_time(
        self,
        anchor: MarketPriceAnchor,
        request: ScheduledInputAuditRequest,
        releases: dict[str, tuple[DatasetReleaseManifest, tuple[DailyBarObservation, ...]]],
    ) -> datetime:
        """Bind an identity-less price anchor to a replayed canonical release row.

        A registered hash proves bytes, not the security, price or availability.
        Reuse canonical release/raw/Parquet verification; cache only within this
        one audit so a later audit detects changed or missing source material.
        """
        self.verifier._check_time(anchor.model_dump(mode="json"), request.as_of)
        self.verifier._verify_reference(anchor.source_artifact_id, anchor.source_object_hash)
        if anchor.source_artifact_id not in releases:
            manifest = DatasetReleaseManifest.model_validate(
                self.verifier.load(anchor.source_artifact_id, DatasetReleaseManifest).model_dump()
            )
            row, canonical, observations = self.views._daily_rows(manifest.release_id)
            if (
                manifest != canonical
                or row["manifest_artifact_id"] != anchor.source_artifact_id
                or row["manifest_object_hash"] != anchor.source_object_hash
            ):
                raise ValueError("market anchor parent is not the canonical release identity")
            releases[anchor.source_artifact_id] = canonical, tuple(observations.values())
        manifest, rows = releases[anchor.source_artifact_id]
        if (
            normalized_instrument(manifest.scope_key)
            != normalized_instrument(request.scope.instrument_id)
            or manifest.available_to_system_at > anchor.available_to_system_at
        ):
            raise ValueError("market anchor parent has another security or future availability")
        matches = [
            bar for bar in rows
            if bar.session_close_at == anchor.observed_at
            and bar.close == anchor.price
            and bar.adjustment_mode.value == "NONE"
            and normalized_instrument(bar.instrument_id)
            == normalized_instrument(request.scope.instrument_id)
            and bar.available_to_system_at <= anchor.available_to_system_at
        ]
        if len(matches) != 1:
            raise ValueError("market anchor price/time does not match one canonical unadjusted row")
        return matches[0].session_close_at

    def _market(self, request: ScheduledInputAuditRequest) -> ScheduledFamilyCheck:
        family = ScheduledInputFamily.MARKET
        if not request.market_references:
            return self._missing(family, "CURRENT_MARKET_REFERENCE_UNAVAILABLE")
        sources: dict[str, str] = {}
        observed_times: list[datetime] = []
        anchor_releases: dict[
            str, tuple[DatasetReleaseManifest, tuple[DailyBarObservation, ...]]
        ] = {}
        locators = tuple(ref for ref in request.market_references if self.views.recognizes(ref))
        projected = self.views.load_many(locators)
        intraday = ScheduledIntradayViews(self.state, self.objects)
        intraday_refs = tuple(
            ref for ref in request.market_references if intraday.recognizes(ref)
        )
        expected_identity = normalized_instrument(request.scope.instrument_id)
        for reference in intraday_refs:
            parts = reference.split(":")
            if len(parts) != 6 or f"{parts[1]}:{parts[2]}" != expected_identity:
                raise ValueError("intraday locator belongs to another instrument")
        intraday_prices = intraday.load_many(intraday_refs, as_of=request.as_of)
        for reference in request.market_references:
            if reference in intraday_prices:
                price = intraday_prices[reference]
                identity = f"{price.bar.market.value}:{price.bar.symbol}"
                if identity != normalized_instrument(request.scope.instrument_id):
                    raise ValueError("intraday observation belongs to another instrument")
                sources.update(price.source_revisions)
                observed_times.append(price.observed_at)
                continue
            if reference in projected:
                record, payload = projected[reference]
                if record["type"] != "DailyBarObservation":
                    raise ValueError("market check received a non-price reference")
                bar = DailyBarObservation.model_validate(payload)
                if normalized_instrument(bar.instrument_id) != normalized_instrument(
                    request.scope.instrument_id
                ):
                    raise ValueError("market observation belongs to another instrument")
                if (
                    bar.adjustment_mode.value != "NONE"
                    or bar.available_to_system_at > request.as_of
                    or bar.session_close_at > request.as_of
                    or bar.close <= 0
                ):
                    raise ValueError(
                        "market observation has invalid price basis or future availability"
                    )
                sources[reference] = str(record["object_hash"])
                observed_times.append(bar.session_close_at)
                continue

            record = self.state.artifact_record(reference)
            if record is None or record["type"] != "MarketPriceAnchor":
                raise ValueError(
                    "market audit requires a verified canonical release locator or price anchor"
                )
            anchor = MarketPriceAnchor.model_validate(
                self.verifier.load(reference, MarketPriceAnchor).model_dump()
            )
            if anchor.source_object_hash not in record["input_hashes"]:
                raise ValueError("market anchor lacks its registered canonical source lineage")
            observed_at = self._verified_anchor_time(anchor, request, anchor_releases)
            sources[reference] = str(record["object_hash"])
            sources[anchor.source_artifact_id] = anchor.source_object_hash
            observed_times.append(observed_at)

        latest = max(observed_times)
        if (
            request.as_of - latest
        ).total_seconds() > self.policy.maximum_price_age_seconds[request.window]:
            return ScheduledFamilyCheck(
                family=family,
                status="STALE",
                source_revisions=sources,
                reason="MARKET_OBSERVATION_EXPIRED",
            )
        return ScheduledFamilyCheck(
            family=family,
            status="CHECKED",
            source_revisions=sources,
            checked_through=latest,
        )

    def _news(
        self, request: ScheduledInputAuditRequest, family: ScheduledInputFamily
    ) -> ScheduledFamilyCheck:
        references = (
            request.company_news_snapshot_ids
            if family is ScheduledInputFamily.COMPANY_NEWS
            else request.industry_news_snapshot_ids
        )
        expected = (
            request.scope.company_query
            if family is ScheduledInputFamily.COMPANY_NEWS
            else request.scope.industry_query
        )
        if not references:
            return self._missing(family, "DISCOVERY_CAPTURE_UNAVAILABLE")
        intervals = []
        sources = {}
        found = set()
        for reference in references:
            receipt = replay_news_search(self.state, self.objects, reference)
            if receipt.query != expected or receipt.checked_at > request.as_of:
                raise ValueError("news capture belongs to another query or decision time")
            snapshot = self.state.get_snapshot(reference)
            assert snapshot is not None
            sources[reference] = snapshot.object_sha256
            if (
                request.as_of - receipt.checked_at
            ).total_seconds() > self.policy.maximum_search_age_seconds:
                return ScheduledFamilyCheck(
                    family=family,
                    status="STALE",
                    source_revisions=sources,
                    reason="DISCOVERY_CAPTURE_EXPIRED",
                )
            if not receipt.checked_successfully:
                return ScheduledFamilyCheck(
                    family=family,
                    status="INCOMPLETE",
                    source_revisions=sources,
                    reason="DISCOVERY_LIMIT_OR_INVALID_ROWS",
                )
            intervals.append((receipt.start, receipt.end))
            found.update(item.lead_id for item in receipt.leads)
        cursor = request.interval_start.replace(microsecond=0)
        for start, end in sorted(intervals):
            if start > cursor:
                break
            cursor = max(cursor, end)
        if cursor < request.interval_end.replace(microsecond=0):
            return ScheduledFamilyCheck(
                family=family,
                status="INCOMPLETE",
                source_revisions=sources,
                reason="DISCOVERY_INTERVAL_GAP",
            )
        return ScheduledFamilyCheck(
            family=family,
            status="BOUNDED_LEADS",
            source_revisions=sources,
            finding_count=len(found),
            checked_through=request.interval_end,
        )

    def _disclosures(self, request: ScheduledInputAuditRequest) -> ScheduledFamilyCheck:
        family = ScheduledInputFamily.DISCLOSURES
        if not request.disclosure_batch_artifact_ids:
            return self._missing(family, "OFFICIAL_DISCLOSURE_SEARCH_UNAVAILABLE")
        batches = []
        sources = {}
        for artifact_id in request.disclosure_batch_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError("disclosure search batch is unregistered")
            batch = DisclosureSearchBatch.model_validate(
                self.verifier.load(artifact_id, DisclosureSearchBatch).model_dump()
            )
            sources[artifact_id] = record["object_hash"]
            snapshot = self.state.get_snapshot(batch.raw_snapshot_id)
            if (
                snapshot is None
                or snapshot.source_id != "cninfo-disclosures:index"
                or snapshot.fetch_status is not FetchStatus.SUCCEEDED
                or snapshot.available_to_system_at > request.as_of
            ):
                raise ValueError("official disclosure raw capture is invalid")
            if urlparse(snapshot.source_url).hostname not in {"www.cninfo.com.cn", "cninfo.com.cn"}:
                raise ValueError("disclosure raw source is not official")
            if (
                request.as_of - snapshot.available_to_system_at
            ).total_seconds() > self.policy.maximum_search_age_seconds:
                return ScheduledFamilyCheck(
                    family=family,
                    status="STALE",
                    source_revisions=sources,
                    reason="DISCLOSURE_CHECK_EXPIRED",
                )
            raw = json.loads(self.objects.get_bytes(snapshot.object_sha256))
            if not isinstance(raw, dict) or not isinstance(raw.get("announcements"), list):
                raise ValueError("disclosure response is not an enumerated announcement list")
            if any(
                not isinstance(item, dict)
                or "announcementId" not in item
                or str(item.get("secCode")) != batch.request.symbol
                for item in raw["announcements"]
            ):
                raise ValueError("disclosure raw rows are malformed or belong to another issuer")
            raw_ids = {str(item["announcementId"]) for item in raw["announcements"]}
            actual_ids = {item.announcement_id for item in batch.announcements}
            raw_total = raw.get("totalAnnouncement", raw.get("totalRecordNum"))
            if raw_total is None or isinstance(raw_total, bool):
                raise ValueError("disclosure raw response lacks an explicit total count")
            if int(raw_total) != batch.total_count or int(raw_total) < 0:
                raise ValueError("disclosure raw and typed total counts differ")
            raw_more = raw.get("hasMore")
            if raw_more not in (True, False, "true", "false"):
                raise ValueError("disclosure response lacks an explicit terminal marker")
            if (str(raw_more).lower() == "true") != batch.has_more:
                raise ValueError("disclosure raw and typed terminal markers differ")
            from astock.core.hashing import content_hash as canonical_content_hash

            expected_id = canonical_content_hash(
                {
                    "provider_id": batch.provider_id,
                    "request": batch.request,
                    "announcement_ids": [item.announcement_id for item in batch.announcements],
                    "resolution_snapshot_ids": batch.resolution_snapshot_ids,
                }
            )
            if batch.provider_id != "cninfo-disclosures" or expected_id != batch.batch_id:
                raise ValueError("disclosure batch identity does not match its captured scope")
            if raw_ids != actual_ids or len(raw_ids) != len(raw["announcements"]):
                raise ValueError("disclosure batch does not match its raw announcement identities")
            if snapshot.object_sha256 not in record["input_hashes"]:
                raise ValueError("disclosure batch lacks raw-input lineage")
            sources[batch.raw_snapshot_id] = snapshot.object_sha256
            batches.append(batch)
        batches.sort(key=lambda item: item.request.page_number)
        first = batches[0]
        exchange, symbol = normalized_instrument(request.scope.instrument_id).split(":")
        expected_exchange = {"XSHG": "SSE", "XSHE": "SZSE", "BJSE": "BSE"}[exchange]
        shanghai = ZoneInfo("Asia/Shanghai")
        if (
            first.request.symbol != symbol
            or first.request.exchange.value != expected_exchange
            or first.request.category is not DisclosureCategory.ALL
            or first.request.keyword
            or first.request.start_date > request.interval_start.astimezone(shanghai).date()
            or first.request.end_date < request.interval_end.astimezone(shanghai).date()
        ):
            raise ValueError(
                "disclosure query does not cover the requested instrument and interval"
            )
        base = first.request.model_dump(exclude={"page_number", "created_at"})
        ids = []
        for index, batch in enumerate(batches, 1):
            if (
                batch.request.page_number != index
                or batch.total_count != first.total_count
                or batch.request.model_dump(exclude={"page_number", "created_at"}) != base
            ):
                raise ValueError("disclosure pagination changed scope or skipped a page")
            ids.extend(item.announcement_id for item in batch.announcements)
            if any(
                item.symbol != symbol or item.published_at > request.as_of
                for item in batch.announcements
            ):
                raise ValueError(
                    "disclosure batch contains another issuer or a future announcement"
                )
        if (
            batches[-1].has_more
            or len(set(ids)) != len(ids)
            or len(ids) != first.total_count
            or any(not item.has_more for item in batches[:-1])
        ):
            return ScheduledFamilyCheck(
                family=family,
                status="INCOMPLETE",
                source_revisions=sources,
                reason="DISCLOSURE_PAGINATION_INCOMPLETE",
            )
        return ScheduledFamilyCheck(
            family=family,
            status="CHECKED",
            source_revisions=sources,
            finding_count=len(ids),
            checked_through=request.interval_end,
        )

    def _policies(self, request: ScheduledInputAuditRequest) -> ScheduledFamilyCheck:
        family = ScheduledInputFamily.POLICIES
        if not request.policy_capture_artifact_ids:
            return self._missing(family, "OFFICIAL_POLICY_FEEDS_UNAVAILABLE")
        sources = {}
        seen = set()
        from astock.investor_orchestration.macro import (
            ImmutableRawObjectStore,
            OfficialMacroCaptureService,
        )

        captures = OfficialMacroCaptureService(
            self.store, object_store=ImmutableRawObjectStore(self.objects.root)
        )
        for artifact_id in request.policy_capture_artifact_ids:
            receipt = captures.verified_capture(artifact_id)
            release = self.store.macro_release_by_source(
                authority=receipt.authority,
                release_family=receipt.release_family,
                source_hash=receipt.source_hash,
            )
            if (
                release is None
                or release.parse_status == "FAILED"
                or not request.interval_end <= receipt.checked_at <= request.as_of
            ):
                raise ValueError(
                    "official policy check is unavailable or outside the decision interval"
                )
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError("official policy check is no longer registered")
            sources[artifact_id] = record["object_hash"]
            sources[receipt.source_snapshot_id] = receipt.source_hash
            if (
                request.as_of - receipt.checked_at
            ).total_seconds() > self.policy.maximum_policy_capture_age_seconds:
                return ScheduledFamilyCheck(
                    family=family,
                    status="STALE",
                    source_revisions=sources,
                    reason="POLICY_FEED_CHECK_EXPIRED",
                )
            pair = (receipt.authority, receipt.release_family)
            if pair not in request.scope.policy_families:
                raise ValueError("policy capture is outside the explicit feed scope")
            seen.add(pair)
        if seen != set(request.scope.policy_families):
            return ScheduledFamilyCheck(
                family=family,
                status="INCOMPLETE",
                source_revisions=sources,
                reason="POLICY_FEED_COVERAGE_MISSING",
            )
        return ScheduledFamilyCheck(
            family=family,
            status="CHECKED",
            source_revisions=sources,
            checked_through=request.interval_end,
        )
