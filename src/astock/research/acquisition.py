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
from astock.providers.eastmoney_financial import EastMoneyFinancialProvider
from astock.providers.sina_financial import SinaFinancialProvider
from astock.schemas import (
    FinancialPeriodType,
    FinancialSourceReleaseStatus,
    ReferenceCoverageStatus,
)
from astock.schemas.documents import DisclosureExchange
from astock.schemas.reference_data import Market, ReferenceSyncReport
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    ExternalAuthority,
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

    def acquire(
        self,
        company_id: str,
        market: Market,
        *,
        lookback_days: int = 120,
    ) -> CurrentResearchAcquisitionReport:
        if market not in {Market.XSHG, Market.XSHE, Market.BJSE}:
            raise ValueError("current company research requires a stock exchange")
        if lookback_days < 30 or lookback_days > 730:
            raise ValueError("current research lookback must be 30..730 days")
        started_at = self.clock()
        current_date = started_at.date()
        start = current_date - timedelta(days=lookback_days)

        identity = self._reference_attempt(
            AcquisitionCapability.INSTRUMENT_IDENTITY,
            lambda: self._market_service().sync_instrument_identity(
                company_id, market, live=True
            ),
        )
        attempts = [identity]

        stage_two: dict[AcquisitionCapability, Callable[[], AcquisitionAttempt]] = {
            AcquisitionCapability.DAILY_MARKET: lambda: self._reference_attempt(
                AcquisitionCapability.DAILY_MARKET,
                lambda: self._market_service().sync_daily(
                    company_id,
                    market,
                    start,
                    current_date,
                    live=True,
                ),
            ),
            AcquisitionCapability.CORPORATE_ACTIONS: lambda: self._reference_attempt(
                AcquisitionCapability.CORPORATE_ACTIONS,
                lambda: self._market_service().sync_corporate_actions(
                    company_id,
                    market,
                    start,
                    current_date,
                    live=True,
                ),
            ),
        }
        financial_specs = self._discover_financial_periods(company_id, market, current_date)
        identity_verified = identity.status is AcquisitionAttemptStatus.SUCCEEDED
        for capability, period_end, period_type in financial_specs:
            stage_two[capability] = lambda c=capability, p=period_end, t=period_type: (
                self._financial_attempt(
                    c, company_id, market, p, t, identity_verified=identity_verified
                )
            )
        attempts.extend(self._run_parallel(stage_two, max_workers=4))

        attempts = sorted(attempts, key=lambda item: item.capability.value)
        external_needs = self._external_needs(company_id, market, attempts)
        core = {
            AcquisitionCapability.INSTRUMENT_IDENTITY,
            AcquisitionCapability.DAILY_MARKET,
            AcquisitionCapability.FINANCIAL_ANNUAL,
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }
        core_attempts = [item for item in attempts if item.capability in core]
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
            attempts=attempts,
            external_research_needs=external_needs,
            manual_actions=[],
            created_at=decision_as_of,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        input_hashes = self._snapshot_object_hashes(attempts)
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
    ) -> list[tuple[AcquisitionCapability, date, FinancialPeriodType]]:
        fallback = _current_financial_periods(current)
        provider = SinaFinancialProvider(
            self.objects,
            self.state,
            self.paths.root / "tests" / "fixtures" / "financial_sources",
        )
        try:
            dates, _ = provider.discover_report_periods(company_id, market, live=True)
        except Exception:
            return fallback
        eligible = sorted({item for item in dates if item <= current}, reverse=True)
        annual_candidates = [item for item in eligible if item.month == 12]
        if not annual_candidates:
            return fallback
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
            return [annual, fallback[1]]
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
        return [annual, interim]

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
        providers = [
            SinaFinancialProvider(
                self.objects,
                self.state,
                self.paths.root / "tests" / "fixtures" / "financial_sources",
            ),
            EastMoneyFinancialProvider(
                self.objects,
                self.state,
                self.paths.root / "tests" / "fixtures" / "financial_sources",
            ),
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
        identity = by_capability[AcquisitionCapability.INSTRUMENT_IDENTITY]
        if identity.status is not AcquisitionAttemptStatus.SUCCEEDED:
            needs.append(
                ExternalResearchNeed(
                    capability=AcquisitionCapability.INSTRUMENT_IDENTITY,
                    research_question=(
                        f"从交易所官方页面确认 {company_id} 的证券身份、市场和当前上市状态。"
                    ),
                    preferred_authorities=[
                        ExternalAuthority.EXCHANGE_OFFICIAL,
                        ExternalAuthority.ISSUER_IR,
                    ],
                )
            )
        daily = by_capability[AcquisitionCapability.DAILY_MARKET]
        if daily.status is AcquisitionAttemptStatus.FAILED:
            needs.append(
                ExternalResearchNeed(
                    capability=AcquisitionCapability.DAILY_MARKET,
                    research_question=f"取得 {company_id} 当前及近期未复权日线价格。",
                    preferred_authorities=[
                        ExternalAuthority.EXCHANGE_OFFICIAL,
                        ExternalAuthority.PUBLIC_MARKET_DATA,
                    ],
                )
            )
        for capability in (
            AcquisitionCapability.FINANCIAL_ANNUAL,
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        ):
            attempt = by_capability[capability]
            if attempt.status is not AcquisitionAttemptStatus.SUCCEEDED:
                description = (
                    "最新年度报告"
                    if capability is AcquisitionCapability.FINANCIAL_ANNUAL
                    else "最新已披露季度/中期报告"
                )
                needs.append(
                    ExternalResearchNeed(
                        capability=capability,
                        research_question=(
                            f"从发行人官网、交易所或法定披露平台取得 {company_id} 的{description}，"
                            "用于核对关键财务事实。"
                        ),
                        preferred_authorities=[
                            ExternalAuthority.ISSUER_IR,
                            ExternalAuthority.EXCHANGE_OFFICIAL,
                            ExternalAuthority.CNINFO_OFFICIAL,
                        ],
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
