"""Current-investor acquisition orchestration with bounded automatic fallback."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from time import perf_counter

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_sources import FinancialSourceParquetStore, FinancialSourceService
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.research.policy import (
    CapabilityGraph,
    load_default_current_research_policy,
    load_default_provider_registry,
)
from astock.schemas import (
    FinancialPeriodType,
    FinancialSourceReleaseStatus,
    ReferenceCoverageStatus,
)
from astock.schemas.adaptation import ValidatedResearchPlan
from astock.schemas.documents import DisclosureExchange
from astock.schemas.reference_data import Market, ReferenceSyncReport
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    ExternalResearchNeed,
)
from astock.settings import ProjectPaths


class CurrentResearchAcquisitionService:
    """Acquire current research inputs first, then freeze one decision timestamp."""

    def __init__(
        self,
        paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.state = state
        self.objects = objects
        self.clock = clock or (lambda: datetime.now(UTC))
        self.policy = load_default_current_research_policy(paths.root)
        self.provider_registry = load_default_provider_registry(paths.root)
        self.capability_graph = CapabilityGraph(self.policy, self.provider_registry, state)

    def acquire(
        self,
        company_id: str,
        market: Market,
        *,
        lookback_days: int | None = None,
        planner_plan_artifact_id: str | None = None,
    ) -> CurrentResearchAcquisitionReport:
        if market not in {Market.XSHG, Market.XSHE, Market.BJSE}:
            raise ValueError("current company research requires a stock exchange")
        started_at = self.clock()
        planner_plan: ValidatedResearchPlan | None = None
        planner_plan_hash: str | None = None
        if planner_plan_artifact_id is not None:
            planner_plan, planner_plan_hash = self._load_planner_plan(
                planner_plan_artifact_id, company_id, market
            )
        resolved_lookback = lookback_days or self.policy.default_lookback_days
        if not (
            self.policy.minimum_lookback_days
            <= resolved_lookback
            <= self.policy.maximum_lookback_days
        ):
            raise ValueError("current research lookback is outside active policy bounds")
        schedule = self.capability_graph.build(
            company_id,
            market,
            lookback_days=resolved_lookback,
            planned_at=started_at,
            capability_filter=(
                set(planner_plan.acquisition_capabilities)
                if planner_plan is not None
                else None
            ),
            planner_plan_artifact_id=planner_plan_artifact_id,
        )
        schedule_ref = self.objects.put_json(schedule.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=schedule.schedule_id,
            artifact_type="CurrentResearchSchedule",
            schema_version=schedule.schema_version,
            object_hash=schedule_ref.sha256,
            input_hashes=[
                schedule.policy_hash,
                *([planner_plan_hash] if planner_plan_hash is not None else []),
            ],
        )
        current_date = started_at.date()
        start = current_date - timedelta(days=resolved_lookback)

        financial_specs, period_discovery_reasons = self._discover_financial_periods(
            company_id, market, current_date
        )
        financial_by_capability = {
            capability: (period_end, period_type)
            for capability, period_end, period_type in financial_specs
        }
        attempts: list[AcquisitionAttempt] = []
        attempt_by_capability: dict[AcquisitionCapability, AcquisitionAttempt] = {}
        for stage in sorted({step.stage for step in schedule.steps}):
            tasks: dict[AcquisitionCapability, Callable[[], AcquisitionAttempt]] = {}
            for step in schedule.steps:
                if step.stage != stage:
                    continue
                identity_attempt = attempt_by_capability.get(
                    AcquisitionCapability.INSTRUMENT_IDENTITY
                )
                identity_verified = (
                    identity_attempt is not None
                    and identity_attempt.status is AcquisitionAttemptStatus.SUCCEEDED
                )
                tasks[step.capability] = self._task_for_capability(
                    step.capability,
                    company_id,
                    market,
                    start,
                    current_date,
                    financial_by_capability,
                    identity_verified=identity_verified,
                )
            stage_results = self._run_parallel(tasks, max_workers=schedule.max_workers)
            if period_discovery_reasons:
                stage_results = [
                    item.model_copy(
                        update={
                            "internal_reason_codes": sorted(
                                set(item.internal_reason_codes) | set(period_discovery_reasons)
                            )
                        }
                    )
                    if item.capability in financial_by_capability
                    else item
                    for item in stage_results
                ]
            attempts.extend(stage_results)
            attempt_by_capability.update({item.capability: item for item in stage_results})

        attempts = sorted(attempts, key=lambda item: item.capability.value)
        external_needs = self._external_needs(company_id, market, attempts)
        core_attempts = [
            item for item in attempts if item.capability in self.policy.core_capabilities
        ]
        if external_needs:
            status = CurrentResearchAcquisitionStatus.NEEDS_EXTERNAL_RESEARCH
        elif all(item.status is AcquisitionAttemptStatus.SUCCEEDED for item in core_attempts):
            if any(item.status is not AcquisitionAttemptStatus.SUCCEEDED for item in attempts):
                status = CurrentResearchAcquisitionStatus.DEGRADED
            else:
                status = CurrentResearchAcquisitionStatus.READY
        else:
            status = CurrentResearchAcquisitionStatus.DEGRADED

        decision_as_of = self.clock()
        identity_payload = {
            "company_id": company_id,
            "market": market.value,
            "started_at": started_at.isoformat(),
            "decision_as_of": decision_as_of.isoformat(),
            "policy_version": schedule.policy_version,
            "policy_hash": schedule.policy_hash,
            "schedule_artifact_id": schedule.schedule_id,
            "planner_plan_artifact_id": planner_plan_artifact_id,
            "attempts": [item.model_dump(mode="json", exclude={"created_at"}) for item in attempts],
            "external_needs": [
                item.model_dump(mode="json", exclude={"created_at"}) for item in external_needs
            ],
        }
        report_id = f"current-research-acquisition:{content_hash(identity_payload)}"
        report = CurrentResearchAcquisitionReport(
            report_id=report_id,
            company_id=company_id,
            market=market,
            exchange=_disclosure_exchange(market),
            started_at=started_at,
            decision_as_of=decision_as_of,
            status=status,
            policy_version=schedule.policy_version,
            policy_hash=schedule.policy_hash,
            schedule_artifact_id=schedule.schedule_id,
            planner_plan_artifact_id=planner_plan_artifact_id,
            automatic_resolution_budget_seconds=schedule.automatic_resolution_budget_seconds,
            attempts=attempts,
            external_research_needs=external_needs,
            manual_actions=[],
            created_at=decision_as_of,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        input_hashes = [schedule_ref.sha256, *self._snapshot_object_hashes(attempts)]
        self.state.register_artifact(
            artifact_id=report_id,
            artifact_type="CurrentResearchAcquisitionReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=input_hashes,
        )
        self.state.set_checkpoint(
            scope_type="current-research-acquisition",
            scope_key=f"{market.value}:{company_id}",
            cursor={
                "report_id": report_id,
                "decision_as_of": decision_as_of.isoformat(),
                "status": status.value,
                "external_research_need_count": len(external_needs),
            },
            status="SUCCEEDED" if not external_needs else "NEEDS_EXTERNAL_RESEARCH",
            object_hash=ref.sha256,
        )
        return report

    def get(self, report_id: str) -> CurrentResearchAcquisitionReport | None:
        record = self.state.artifact_record(report_id)
        if record is None or str(record["type"]) != "CurrentResearchAcquisitionReport":
            return None
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            return None
        return CurrentResearchAcquisitionReport.model_validate_json(
            self.objects.get_bytes(object_hash)
        )

    def _load_planner_plan(
        self,
        artifact_id: str,
        company_id: str,
        market: Market,
    ) -> tuple[ValidatedResearchPlan, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ValidatedResearchPlan":
            raise ValueError("current acquisition requires a frozen validated research plan")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("validated research plan object is unavailable or corrupt")
        plan = ValidatedResearchPlan.model_validate_json(self.objects.get_bytes(object_hash))
        if plan.company_id != company_id or plan.market is not market:
            raise ValueError("validated research plan targets another instrument")
        return plan, object_hash

    def _market_service(self) -> MarketReferenceService:
        return MarketReferenceService(
            self.state,
            self.objects,
            ReferenceParquetStore(self.paths.parquet),
            self.paths.root / "tests" / "fixtures" / "reference",
        )

    def _financial_service(self) -> FinancialSourceService:
        return FinancialSourceService(
            self.state,
            self.objects,
            FinancialSourceParquetStore(self.paths.parquet / "financial_sources"),
            self.paths.root,
        )

    def _discover_financial_periods(
        self,
        company_id: str,
        market: Market,
        current: date,
    ) -> tuple[
        list[tuple[AcquisitionCapability, date, FinancialPeriodType]],
        list[str],
    ]:
        fallback = _current_financial_periods(current)
        financial = self._financial_service()
        definitions = financial.provider_factory.definitions_for_capability(
            "financial.report_period_index"
        )
        reasons: list[str] = []
        for definition in definitions:
            provider = financial.providers.get(definition.provider_id)
            if provider is None:
                candidate = financial.provider_factory.create(definition.provider_id)
                provider = candidate if hasattr(candidate, "discover_report_periods") else None
            discover = getattr(provider, "discover_report_periods", None)
            if not callable(discover):
                reasons.append(
                    f"REPORT_PERIOD_INDEX_UNAVAILABLE:{definition.provider_id}"
                )
                continue
            try:
                discovery_result = discover(company_id, market, live=True)
                if not isinstance(discovery_result, tuple) or len(discovery_result) != 2:
                    raise ValueError("report-period provider returned an invalid contract")
                dates = discovery_result[0]
                if not isinstance(dates, list) or any(not isinstance(item, date) for item in dates):
                    raise ValueError("report-period provider returned invalid dates")
            except Exception as exc:
                reasons.extend(
                    [
                        f"REPORT_PERIOD_INDEX_FAILED:{definition.provider_id}",
                        f"EXCEPTION:{type(exc).__name__}",
                    ]
                )
                continue
            eligible = sorted({item for item in dates if item <= current}, reverse=True)
            annual_candidates = [item for item in eligible if item.month == 12]
            if not annual_candidates:
                reasons.append(
                    f"REPORT_PERIOD_INDEX_NO_ANNUAL:{definition.provider_id}"
                )
                continue
            annual_date = annual_candidates[0]
            annual = (
                AcquisitionCapability.FINANCIAL_ANNUAL,
                annual_date,
                FinancialPeriodType.ANNUAL,
            )
            interim_candidates = [
                item
                for item in eligible
                if item > annual_date and item.month in {3, 6, 9}
            ]
            if not interim_candidates:
                return [annual, fallback[1]], [
                    *reasons,
                    f"REPORT_PERIOD_INDEX_USED:{definition.provider_id}",
                    "CONSERVATIVE_INTERIM_CALENDAR_FALLBACK",
                ]
            interim_date = interim_candidates[0]
            interim_type = (
                FinancialPeriodType.SEMIANNUAL
                if interim_date.month == 6
                else FinancialPeriodType.QUARTERLY
            )
            interim = (
                AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                interim_date,
                interim_type,
            )
            return [annual, interim], [
                *reasons,
                f"REPORT_PERIOD_INDEX_USED:{definition.provider_id}",
            ]
        return fallback, [*reasons, "CONSERVATIVE_REPORT_CALENDAR_FALLBACK"]

    def _task_for_capability(
        self,
        capability: AcquisitionCapability,
        company_id: str,
        market: Market,
        start: date,
        current_date: date,
        financial_by_capability: dict[
            AcquisitionCapability, tuple[date, FinancialPeriodType]
        ],
        *,
        identity_verified: bool,
    ) -> Callable[[], AcquisitionAttempt]:
        if capability is AcquisitionCapability.INSTRUMENT_IDENTITY:
            return lambda: self._reference_attempt(
                capability,
                lambda: self._market_service().sync_instrument_identity(
                    company_id, market, live=True
                ),
            )
        if capability is AcquisitionCapability.DAILY_MARKET:
            return lambda: self._reference_attempt(
                capability,
                lambda: self._market_service().sync_daily(
                    company_id, market, start, current_date, live=True
                ),
            )
        if capability is AcquisitionCapability.CORPORATE_ACTIONS:
            return lambda: self._reference_attempt(
                capability,
                lambda: self._market_service().sync_corporate_actions(
                    company_id, market, start, current_date, live=True
                ),
            )
        if capability in {
            AcquisitionCapability.FINANCIAL_ANNUAL,
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }:
            try:
                period_end, period_type = financial_by_capability[capability]
            except KeyError as exc:
                raise ValueError(
                    f"financial capability has no period specification: {capability}"
                ) from exc
            return lambda: self._financial_attempt(
                capability,
                company_id,
                market,
                period_end,
                period_type,
                identity_verified=identity_verified,
            )
        raise ValueError(f"unsupported acquisition capability executor: {capability}")

    def _run_parallel(
        self,
        tasks: dict[AcquisitionCapability, Callable[[], AcquisitionAttempt]],
        *,
        max_workers: int,
    ) -> list[AcquisitionAttempt]:
        if not tasks:
            return []
        results: dict[AcquisitionCapability, AcquisitionAttempt] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as executor:
            futures = {executor.submit(task): capability for capability, task in tasks.items()}
            for future in as_completed(futures):
                capability = futures[future]
                try:
                    results[capability] = future.result()
                except Exception as exc:
                    results[capability] = AcquisitionAttempt(
                        capability=capability,
                        status=AcquisitionAttemptStatus.FAILED,
                        provider_path=[],
                        fallback_used=False,
                        record_count=0,
                        latency_ms=0,
                        internal_reason_codes=[f"UNHANDLED:{type(exc).__name__}"],
                        source_snapshot_ids=[],
                    )
        return [results[key] for key in sorted(results, key=lambda item: item.value)]

    def _reference_attempt(
        self,
        capability: AcquisitionCapability,
        action: Callable[[], ReferenceSyncReport],
    ) -> AcquisitionAttempt:
        started = perf_counter()
        try:
            report = action()
        except Exception as exc:
            return AcquisitionAttempt(
                capability=capability,
                status=AcquisitionAttemptStatus.FAILED,
                provider_path=[],
                fallback_used=False,
                record_count=0,
                latency_ms=_latency_ms(started),
                internal_reason_codes=[f"EXCEPTION:{type(exc).__name__}"],
                source_snapshot_ids=[],
            )
        provider_path = self._source_path(report.raw_snapshot_ids)
        if report.status is ReferenceCoverageStatus.COMPLETE:
            status = AcquisitionAttemptStatus.SUCCEEDED
        elif report.status in {ReferenceCoverageStatus.PARTIAL, ReferenceCoverageStatus.EMPTY}:
            status = AcquisitionAttemptStatus.PARTIAL
        else:
            status = AcquisitionAttemptStatus.FAILED
        return AcquisitionAttempt(
            capability=capability,
            status=status,
            provider_path=provider_path or ([report.provider_id] if report.provider_id else []),
            fallback_used=len(provider_path) > 1
            or any("FALLBACK" in code for code in report.reason_codes),
            record_count=report.coverage.record_count,
            latency_ms=_latency_ms(started),
            internal_reason_codes=sorted(set(report.reason_codes)),
            source_snapshot_ids=list(dict.fromkeys(report.raw_snapshot_ids)),
        )

    def _financial_attempt(
        self,
        capability: AcquisitionCapability,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        identity_verified: bool = True,
    ) -> AcquisitionAttempt:
        if not identity_verified:
            return self._financial_secondary_hint_attempt(
                capability, company_id, market, period_end
            )
        started = perf_counter()
        try:
            report = self._financial_service().sync(
                company_id,
                market,
                period_end,
                period_type,
                as_of=None,
                live=True,
                cross_check=False,
            )
        except Exception as exc:
            return AcquisitionAttempt(
                capability=capability,
                status=AcquisitionAttemptStatus.FAILED,
                provider_path=[],
                fallback_used=False,
                record_count=0,
                latency_ms=_latency_ms(started),
                internal_reason_codes=[f"EXCEPTION:{type(exc).__name__}"],
                source_snapshot_ids=[],
            )
        provider_path = list(report.provider_ids)
        if report.official_snapshot_id:
            snapshot = self.state.get_snapshot(report.official_snapshot_id)
            if snapshot is not None:
                provider_path.append(snapshot.source_id.split(":", maxsplit=1)[0])
        provider_path = list(dict.fromkeys(provider_path))
        if report.status is FinancialSourceReleaseStatus.CERTIFIED:
            status = AcquisitionAttemptStatus.SUCCEEDED
        elif report.status is FinancialSourceReleaseStatus.NEEDS_INFO:
            status = AcquisitionAttemptStatus.PARTIAL
        else:
            status = AcquisitionAttemptStatus.FAILED
        return AcquisitionAttempt(
            capability=capability,
            status=status,
            provider_path=provider_path,
            fallback_used=(
                len(report.provider_ids) > 1
                or any("FALLBACK" in code for code in report.reason_codes)
            ),
            record_count=report.coverage.certified_fact_count,
            latency_ms=_latency_ms(started),
            internal_reason_codes=sorted(set(report.reason_codes)),
            source_snapshot_ids=list(dict.fromkeys(report.raw_snapshot_ids)),
        )

    def _financial_secondary_hint_attempt(
        self,
        capability: AcquisitionCapability,
        company_id: str,
        market: Market,
        period_end: date,
    ) -> AcquisitionAttempt:
        started = perf_counter()
        financial = self._financial_service()
        providers = [
            financial.providers[provider_id]
            for provider_id in financial.config.provider_order
        ]
        provider_path: list[str] = []
        snapshot_ids: list[str] = []
        reason_codes = {"INSTRUMENT_IDENTITY_UNVERIFIED"}
        record_count = 0
        succeeded_provider_index: int | None = None
        for index, provider in enumerate(providers):
            provider_path.append(provider.provider_id)
            try:
                batch = provider.fetch(company_id, market, period_end, live=True)
            except Exception as exc:
                reason_codes.add(f"{provider.provider_id.upper().replace('-', '_')}_FAILED")
                reason_codes.add(f"EXCEPTION:{type(exc).__name__}")
                continue
            snapshot_ids.extend(snapshot.snapshot_id for snapshot in batch.snapshots)
            current_count = sum(len(rows) for rows in batch.tables.values())
            if current_count:
                record_count = current_count
                succeeded_provider_index = index
                break
        return AcquisitionAttempt(
            capability=capability,
            status=(
                AcquisitionAttemptStatus.PARTIAL
                if record_count
                else AcquisitionAttemptStatus.FAILED
            ),
            provider_path=provider_path,
            fallback_used=bool(succeeded_provider_index and succeeded_provider_index > 0),
            record_count=record_count,
            latency_ms=_latency_ms(started),
            internal_reason_codes=sorted(reason_codes),
            source_snapshot_ids=list(dict.fromkeys(snapshot_ids)),
        )

    def _source_path(self, snapshot_ids: list[str]) -> list[str]:
        result: list[str] = []
        for snapshot_id in snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if snapshot is None:
                continue
            source = snapshot.source_id
            if source.startswith("cninfo-disclosures"):
                source = "cninfo-disclosures"
            if source not in result:
                result.append(source)
        return result

    def _snapshot_object_hashes(self, attempts: list[AcquisitionAttempt]) -> list[str]:
        hashes: list[str] = []
        for attempt in attempts:
            for snapshot_id in attempt.source_snapshot_ids:
                snapshot = self.state.get_snapshot(snapshot_id)
                if snapshot is not None and snapshot.object_sha256 not in hashes:
                    hashes.append(snapshot.object_sha256)
        return hashes

    def _external_needs(
        self,
        company_id: str,
        market: Market,
        attempts: list[AcquisitionAttempt],
    ) -> list[ExternalResearchNeed]:
        by_capability = {item.capability: item for item in attempts}
        needs: list[ExternalResearchNeed] = []
        for capability_policy in sorted(
            self.policy.capabilities.values(), key=lambda item: item.capability.value
        ):
            attempt = by_capability.get(capability_policy.capability)
            status = (
                attempt.status if attempt is not None else AcquisitionAttemptStatus.FAILED
            )
            if status not in capability_policy.external_on:
                continue
            needs.append(
                ExternalResearchNeed(
                    capability=capability_policy.capability,
                    research_question=capability_policy.research_question.format(
                        company_id=company_id,
                        market=market.value,
                    ),
                    preferred_authorities=list(capability_policy.preferred_authorities),
                )
            )
        return needs


def _current_financial_periods(
    current: date,
) -> list[tuple[AcquisitionCapability, date, FinancialPeriodType]]:
    annual = (
        AcquisitionCapability.FINANCIAL_ANNUAL,
        date(current.year - 1, 12, 31),
        FinancialPeriodType.ANNUAL,
    )
    if current.month >= 11:
        interim = (
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
            date(current.year, 9, 30),
            FinancialPeriodType.QUARTERLY,
        )
    elif current.month >= 9:
        interim = (
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
            date(current.year, 6, 30),
            FinancialPeriodType.SEMIANNUAL,
        )
    elif current.month >= 5:
        interim = (
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
            date(current.year, 3, 31),
            FinancialPeriodType.QUARTERLY,
        )
    else:
        interim = (
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
            date(current.year - 1, 9, 30),
            FinancialPeriodType.QUARTERLY,
        )
    return [annual, interim]


def _disclosure_exchange(market: Market) -> DisclosureExchange | None:
    if market is Market.XSHG:
        return DisclosureExchange.SSE
    if market is Market.XSHE:
        return DisclosureExchange.SZSE
    return None


def _latency_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


__all__ = ["CurrentResearchAcquisitionService"]
