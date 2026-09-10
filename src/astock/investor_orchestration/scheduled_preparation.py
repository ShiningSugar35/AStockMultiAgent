"""Deterministic source-plane preparation for pending scheduled research runs.

The clock owns only the wake/bucket identity. This service performs bounded, auditable
source capture after that wake and freezes the evidence cutoff only after acquisition
finishes. It never fabricates semantic results and never writes economic facts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.investor_orchestration.canonical_state import (
    CanonicalStateProjectionReader,
    normalized_instrument,
)
from astock.investor_orchestration.macro import OfficialMacroCaptureService
from astock.investor_orchestration.models import (
    PortfolioLane,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledWindow,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditRequest,
    ScheduledInputCoverageService,
    ScheduledSourceScope,
    load_input_audit_policy,
)
from astock.investor_orchestration.scheduled_market_views import ScheduledIntradayViews
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.investor_orchestration.utils import utc_now
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.market_data.sync import MarketSyncService
from astock.monitoring.news import GdeltNewsLeadProvider, NewsSearchReceipt
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.sina import Sina5mProvider
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    DisclosureExchange,
    DisclosureSearchBatch,
    DisclosureSearchRequest,
    Frequency,
    InstrumentType,
    Market,
)
from astock.schemas.continuous_monitoring import MonitorTargetStatus
from astock.schemas.institutional_research import IndustryProfile

_ROOT = Path(__file__).resolve().parents[3]
_MAX_LOOKBACK = timedelta(days=31)
_BOOTSTRAP_LOOKBACK = timedelta(days=1)
_DEFAULT_DISCLOSURE_MAX_PAGES = 2


@dataclass(frozen=True, slots=True)
class ScheduledSourcePreparationResult:
    request: ScheduledRunRequest
    source_report_ids: tuple[str, ...]
    subject_bindings: tuple[tuple[ScheduledDomain, str], ...]
    interval_start: datetime
    interval_end: datetime
    evidence_cutoff: datetime
    logical_external_call_budget: int


class _NewsProvider(Protocol):
    def search_checked(
        self,
        *,
        names: list[str],
        symbol: str,
        start: datetime,
        end: datetime,
        max_records: int,
    ) -> NewsSearchReceipt: ...


class _DisclosureProvider(Protocol):
    def search_all(
        self,
        request: DisclosureSearchRequest,
        *,
        max_pages: int,
    ) -> list[DisclosureSearchBatch]: ...


class _MacroProvider(Protocol):
    def specs(self) -> tuple[Any, ...]: ...

    def capture_checked(self, spec: Any, *, live: bool) -> Any: ...


class ScheduledSourcePreparationService:
    """Capture and freeze the five required source families for one pending bucket."""

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        *,
        now: Callable[[], datetime] = utc_now,
        market_capture: Callable[[str, Frequency, datetime], str] | None = None,
        news_provider: _NewsProvider | None = None,
        disclosure_provider: _DisclosureProvider | None = None,
        macro_capture: _MacroProvider | None = None,
        industry_name_resolver: Callable[[str, datetime], str] | None = None,
        company_names_resolver: Callable[[str, datetime], list[str]] | None = None,
        max_external_calls: int | None = None,
        disclosure_max_pages: int = _DEFAULT_DISCLOSURE_MAX_PAGES,
    ) -> None:
        self.store = store
        self.now = now
        self.state = StateStore(store.path)
        self.objects = ObjectStore(store.path.parent / "objects" / "sha256")
        self.audit = ScheduledInputCoverageService(store, load_input_audit_policy(), self.objects)
        self.scheduler = ScheduledResearchService(
            store,
            InvestorSessionPreflightService(store),
            source_audit_policy=self.audit.policy,
        )
        self.market_capture = market_capture or self._capture_market
        self.news = news_provider or GdeltNewsLeadProvider(self.objects, self.state)
        self.disclosures = disclosure_provider or CninfoDisclosureProvider(self.objects, self.state)
        self.macro = macro_capture or OfficialMacroCaptureService(store)
        self.industry_name_resolver = industry_name_resolver or self._industry_name
        self.company_names_resolver = company_names_resolver or self._company_names
        config = yaml.safe_load(
            (_ROOT / "configs" / "scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
        )
        configured_budget = int(config.get("max_external_calls_per_run", 40))
        self.max_external_calls = (
            configured_budget if max_external_calls is None else max_external_calls
        )
        if not 1 <= self.max_external_calls <= 1000:
            raise ValueError("scheduled source external-call budget must be bounded and positive")
        if not 1 <= disclosure_max_pages <= 20:
            raise ValueError("scheduled disclosure page budget must be within [1, 20]")
        self.disclosure_max_pages = disclosure_max_pages

    def prepare_pending(
        self,
        binding_id: str,
        schedule_bucket: str,
        *,
        live: bool,
    ) -> ScheduledSourcePreparationResult:
        """Capture five source families and stage only a fully verified source set.

        Network acquisition requires explicit ``live=True`` at this local boundary. The
        binding itself has already passed user-consent and local-execution checks before
        the pending checkpoint can exist.
        """
        if not live:
            raise ValueError("automatic scheduled source acquisition requires explicit live opt-in")
        checkpoint = self.store.get_scheduled_checkpoint(binding_id, schedule_bucket)
        if checkpoint is None:
            raise ValueError("scheduled source preparation requires an existing pending bucket")
        request = ScheduledRunRequest.model_validate(checkpoint["request"])
        if request.source_coverage_artifact_ids:
            return self._existing_result(request)
        if checkpoint["status"] in {"SUCCEEDED", "BLOCKED"}:
            raise ValueError("scheduled bucket is already terminal")
        binding = self.store.get_binding(binding_id)
        if (
            binding is None
            or not binding.active
            or binding.confirmed_at is None
            or not binding.consent_hash
        ):
            raise ValueError("scheduled source preparation requires one active consented binding")
        if binding.execution_surface not in {"LOCAL_DAEMON", "DESKTOP_LOCAL"}:
            raise ValueError("scheduled source preparation requires a local execution surface")
        policy = self.store.get_scheduled_policy(binding.policy_id)
        if policy is None or policy.policy_hash != request.policy_hash:
            raise ValueError("scheduled source preparation policy binding changed")

        capture_started = self._aware_now()
        if capture_started < request.requested_at.astimezone(UTC):
            raise ValueError("scheduled source preparation cannot run before the bucket wake time")
        interval_end = capture_started
        interval_start = self._interval_start(binding_id, interval_end)
        initial_subjects = self._subjects(request, capture_started)
        bindings = self._bindings(initial_subjects)
        if not bindings:
            raise ValueError("scheduled source preparation has no canonical research subjects")
        unique_instruments = tuple(sorted({instrument for _, instrument in bindings}))
        specs = self.macro.specs()
        logical_calls = self._logical_call_budget(len(unique_instruments), len(specs))
        if logical_calls > self.max_external_calls:
            raise ValueError(
                "scheduled source acquisition exceeds the consented external-call budget: "
                f"required={logical_calls}, allowed={self.max_external_calls}"
            )

        policy_captures = tuple(self.macro.capture_checked(spec, live=True) for spec in specs)
        policy_ids = tuple(item.capture_artifact_id for item in policy_captures)
        policy_families = tuple(
            sorted(
                {(item.release.authority, item.release.release_family) for item in policy_captures}
            )
        )
        captured: dict[str, dict[str, Any]] = {}
        frequency = Frequency.M5 if request.window is ScheduledWindow.INTRADAY else Frequency.H1
        for instrument in unique_instruments:
            market, symbol = self._security(instrument)
            company_names = self.company_names_resolver(instrument, capture_started)
            industry_name = self.industry_name_resolver(instrument, capture_started).strip()
            if not industry_name:
                raise ValueError(f"scheduled industry scope is unavailable for {instrument}")
            company_news = self.news.search_checked(
                names=company_names,
                symbol=symbol,
                start=interval_start,
                end=interval_end,
                max_records=50,
            )
            industry_news = self.news.search_checked(
                names=[industry_name],
                symbol="",
                start=interval_start,
                end=interval_end,
                max_records=50,
            )
            if company_news.query == industry_news.query:
                raise ValueError("company and industry source scopes must remain distinct")
            batches = self.disclosures.search_all(
                DisclosureSearchRequest(
                    symbol=symbol,
                    exchange=self._disclosure_exchange(market),
                    start_date=interval_start.astimezone(UTC).date(),
                    end_date=interval_end.astimezone(UTC).date(),
                ),
                max_pages=self.disclosure_max_pages,
            )
            disclosure_ids = self._register_disclosure_batches(batches)
            captured[instrument] = {
                "market": self.market_capture(instrument, frequency, interval_end),
                "company": company_news,
                "industry": industry_news,
                "disclosures": disclosure_ids,
            }

        cutoff = self._aware_now()
        final_subjects = self._subjects(request, cutoff)
        if self._bindings(final_subjects) != bindings:
            raise ValueError(
                "scheduled subject set changed during source acquisition; retry the bucket"
            )

        report_ids: list[str] = []
        source_revisions: dict[str, str] = {}
        for domain, instrument in bindings:
            item = captured[instrument]
            company = item["company"]
            industry = item["industry"]
            assert isinstance(company, NewsSearchReceipt)
            assert isinstance(industry, NewsSearchReceipt)
            audit_request = ScheduledInputAuditRequest(
                run_id=request.run_id,
                domain=domain,
                window=request.window,
                scope=ScheduledSourceScope(
                    instrument_id=instrument,
                    company_query=company.query,
                    industry_query=industry.query,
                    policy_families=policy_families,
                ),
                interval_start=interval_start,
                interval_end=interval_end,
                as_of=cutoff,
                market_references=(str(item["market"]),),
                company_news_snapshot_ids=(company.snapshot_id,),
                industry_news_snapshot_ids=(industry.snapshot_id,),
                disclosure_batch_artifact_ids=tuple(item["disclosures"]),
                policy_capture_artifact_ids=policy_ids,
            )
            report_id = self.audit.register(audit_request)
            report = self.audit.verify_registered(report_id)
            if not report.all_families_checked:
                failures = [
                    f"{check.family.value}:{check.status}:{check.reason or ''}"
                    for check in report.checks
                    if check.status not in {"CHECKED", "BOUNDED_LEADS"}
                ]
                raise ValueError(
                    "scheduled five-family acquisition is incomplete for "
                    f"{domain.value}:{instrument}:" + ",".join(failures)
                )
            report_ids.append(report_id)
            source_revisions[report_id] = report.report_hash

        prepared = request.model_copy(
            update={
                "requested_at": cutoff,
                "source_revision_set": dict(sorted(source_revisions.items())),
                "source_coverage_artifact_ids": tuple(sorted(report_ids)),
                "source_interval_start": interval_start,
                "source_interval_end": interval_end,
            }
        )
        staged = self.scheduler.stage(prepared)
        return ScheduledSourcePreparationResult(
            request=staged,
            source_report_ids=staged.source_coverage_artifact_ids,
            subject_bindings=bindings,
            interval_start=interval_start,
            interval_end=interval_end,
            evidence_cutoff=cutoff,
            logical_external_call_budget=logical_calls,
        )

    def _existing_result(self, request: ScheduledRunRequest) -> ScheduledSourcePreparationResult:
        if request.source_interval_start is None or request.source_interval_end is None:
            raise ValueError("prepared scheduled source checkpoint is missing its interval")
        subjects = self._subjects(request, request.requested_at)
        bindings = self._bindings(subjects)
        for report_id in request.source_coverage_artifact_ids:
            report = self.audit.verify_registered(report_id)
            if not report.all_families_checked:
                raise ValueError("staged scheduled source report is no longer complete")
        return ScheduledSourcePreparationResult(
            request=request,
            source_report_ids=request.source_coverage_artifact_ids,
            subject_bindings=bindings,
            interval_start=request.source_interval_start,
            interval_end=request.source_interval_end,
            evidence_cutoff=request.requested_at,
            logical_external_call_budget=0,
        )

    def _subjects(
        self,
        request: ScheduledRunRequest,
        as_of: datetime,
    ) -> dict[ScheduledDomain, tuple[str, ...]]:
        reader = CanonicalStateProjectionReader(self.store)
        revisions = reader.revision_vector()
        healthy, reason = reader.bounded_health_check()
        actual = reader.lane_snapshot(
            PortfolioLane.ACTUAL,
            revisions["actual"],
            health_ok=healthy,
            health_reason=reason,
            as_of=as_of,
        )
        paper = reader.lane_snapshot(
            PortfolioLane.PAPER,
            revisions["paper"],
            health_ok=healthy,
            health_reason=reason,
            as_of=as_of,
        )
        watchlist = tuple(
            sorted(
                normalized_instrument(item.instrument_id)
                for item in ResearchSubjectRegistryService(self.store).current_watchlist(
                    as_of=as_of
                )
            )
        )
        all_subjects = {
            ScheduledDomain.WATCHLIST: watchlist,
            ScheduledDomain.PAPER_HOLDING: tuple(
                sorted({normalized_instrument(item.instrument_id) for item in paper.positions})
            ),
            ScheduledDomain.ACTUAL_HOLDING: tuple(
                sorted({normalized_instrument(item.instrument_id) for item in actual.positions})
            ),
        }
        return {domain: all_subjects[domain] for domain in request.domains}

    @staticmethod
    def _bindings(
        subjects: Mapping[ScheduledDomain, tuple[str, ...]],
    ) -> tuple[tuple[ScheduledDomain, str], ...]:
        return tuple(
            sorted(
                {
                    (domain, normalized_instrument(instrument))
                    for domain, instruments in subjects.items()
                    for instrument in instruments
                },
                key=lambda item: (item[0].value, item[1]),
            )
        )

    def _interval_start(self, binding_id: str, interval_end: datetime) -> datetime:
        previous = self.store.latest_scheduled_receipt(binding_id)
        candidate = interval_end - _BOOTSTRAP_LOOKBACK
        if previous is not None and previous.next_watermark:
            parsed = datetime.fromisoformat(previous.next_watermark.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("scheduled watermark must be timezone-aware")
            candidate = parsed.astimezone(UTC)
        floor = interval_end - _MAX_LOOKBACK
        if candidate < floor:
            candidate = floor
        if candidate >= interval_end:
            candidate = interval_end - timedelta(seconds=1)
        return candidate

    def _company_names(self, instrument: str, as_of: datetime) -> list[str]:
        del as_of
        market, symbol = self._security(instrument)
        target = ContinuousMonitorRepository(self.state, self.objects).get_target(
            f"monitor-target:{market.value}:{symbol}"
        )
        if target is None or target.status is not MonitorTargetStatus.ACTIVE:
            return [symbol]
        names = [target.display_name, *target.aliases]
        return list(dict.fromkeys(value.strip() for value in names if value.strip())) or [symbol]

    def _industry_name(self, instrument: str, as_of: datetime) -> str:
        _, symbol = self._security(instrument)
        checkpoint = self.state.get_checkpoint("institutional-industry-profile", symbol)
        if checkpoint is None:
            raise ValueError(f"INDUSTRY_PROFILE_REQUIRED:{instrument}")
        artifact_id = checkpoint["cursor"].get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError(f"INDUSTRY_PROFILE_REQUIRED:{instrument}")
        record = self.state.artifact_record(artifact_id)
        if record is None or record["type"] != "IndustryProfile":
            raise ValueError(f"INDUSTRY_PROFILE_REQUIRED:{instrument}")
        profile = IndustryProfile.model_validate_json(
            self.objects.get_bytes(str(record["object_hash"]))
        )
        if profile.company_id != symbol or profile.as_of > as_of:
            raise ValueError(f"INDUSTRY_PROFILE_NOT_AVAILABLE_AS_OF:{instrument}")
        return profile.draft.industry_name

    def _capture_market(self, instrument: str, frequency: Frequency, as_of: datetime) -> str:
        market, symbol = self._security(instrument)
        runtime = self.store.path.parent
        canonical = CanonicalMarketStore(runtime / "data" / "parquet", runtime / "manifests")
        sync = MarketSyncService(
            [
                EastMoney5mProvider(self.objects, self.state),
                Sina5mProvider(self.objects, self.state),
            ],
            self.state,
            ParquetMarketStore(runtime / "data" / "parquet", "market_observation"),
            canonical,
        )
        sync.sync_intraday(
            BarRequest(
                symbol=symbol,
                market=market,
                instrument_type=InstrumentType.STOCK,
                frequency=frequency,
                requested_start=as_of - timedelta(days=1),
                requested_end=as_of,
                adjustment_mode=AdjustmentMode.NONE,
            )
        )
        return (
            ScheduledIntradayViews(self.state, self.objects, canonical)
            .latest(
                instrument,
                frequency,
                as_of=self._aware_now(),
            )
            .locator
        )

    def _register_disclosure_batches(
        self,
        batches: list[DisclosureSearchBatch],
    ) -> tuple[str, ...]:
        if not batches:
            raise ValueError("scheduled disclosure enumeration returned no page receipt")
        identifiers: list[str] = []
        for batch in batches:
            snapshot = self.state.get_snapshot(batch.raw_snapshot_id)
            if snapshot is None:
                raise ValueError("scheduled disclosure batch lost its raw snapshot")
            reference = self.objects.put_json(batch.model_dump(mode="json"))
            artifact_id = f"DisclosureSearchBatch:{batch.batch_id}:{snapshot.object_sha256}"
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="DisclosureSearchBatch",
                schema_version=batch.schema_version,
                object_hash=reference.sha256,
                input_hashes=[
                    snapshot.object_sha256,
                    content_hash(batch.request.model_dump(mode="json")),
                ],
            )
            identifiers.append(artifact_id)
        return tuple(identifiers)

    def _logical_call_budget(self, unique_instruments: int, macro_specs: int) -> int:
        # Per security: two market providers + company news + industry news +
        # CNINFO identity resolution + bounded index pages. Macro releases are shared.
        return macro_specs + unique_instruments * (5 + self.disclosure_max_pages)

    def _aware_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled source clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _security(instrument: str) -> tuple[Market, str]:
        identity = normalized_instrument(instrument)
        parts = identity.split(":")
        if len(parts) != 2 or parts[0] not in {"XSHG", "XSHE", "BJSE"}:
            raise ValueError("scheduled source acquisition requires an explicit A-share security")
        return Market(parts[0]), parts[1]

    @staticmethod
    def _disclosure_exchange(market: Market) -> DisclosureExchange:
        if market is Market.XSHG:
            return DisclosureExchange.SSE
        if market is Market.XSHE:
            return DisclosureExchange.SZSE
        raise ValueError("BJSE official disclosure enumeration is not configured for CNINFO")


__all__ = ["ScheduledSourcePreparationResult", "ScheduledSourcePreparationService"]
