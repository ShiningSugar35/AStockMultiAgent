"""Automatic, fail-closed promotion from ResearchSeedReport to Candidate Scan."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq

from astock.candidates.financial_policy import financial_pack_is_candidate_eligible
from astock.candidates.service import CandidateScanService
from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DisclosureEnumerationProvider
from astock.financial_integrity.repository import FinancialIntegrityRepository
from astock.financial_sources.service import FinancialSourceService
from astock.market_data.reference import MarketReferenceService
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas.candidate_promotion import (
    SeedPromotionCompanyResult,
    SeedPromotionCompanyStatus,
    SeedPromotionReport,
    SeedPromotionRequest,
    SeedPromotionStatus,
    SeedPromotionTask,
)
from astock.schemas.candidates import (
    CandidateAnnouncementEventPack,
    CandidateArtifactRole,
    CandidateCompanyInput,
    CandidateCoverageStatus,
    CandidateDailyPoint,
    CandidateEvidenceSeverity,
    CandidateFinancialFlag,
    CandidateInputArtifact,
    CandidateInputRelease,
    CandidateInstrumentUniverseProof,
    CandidatePitStatus,
    CandidateQualityStatus,
    CandidateScanRequest,
    CandidateSourceMode,
    CandidateTradability,
)
from astock.schemas.documents import DisclosureCategory, DisclosureExchange, DisclosureSearchRequest
from astock.schemas.financial import (
    FinancialCoverageStatus,
    FinancialFindingStatus,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialSeverity,
)
from astock.schemas.financial_sources import FinancialSourceReleaseStatus
from astock.schemas.market import (
    AdjustmentMode,
    DataQualityReport,
    Frequency,
    Market,
    ProviderStatus,
    QualityStatus,
    ReplayQuality,
    TimestampSemantics,
    VolumeUnit,
)
from astock.schemas.pit import AvailabilityBasis, PointInTimeStatus
from astock.schemas.reference_data import (
    DailyBarObservation,
    DatasetReleaseManifest,
    InstrumentRecord,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferencePitStatus,
    TradingSession,
)
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin, ResearchSeedReport

if TYPE_CHECKING:
    from astock.research.trading_classification import TradingClassificationService

_EVENT_TITLE_TERMS: dict[str, tuple[str, ...]] = {
    "EARNINGS_PREANNOUNCEMENT": ("业绩预告", "业绩快报"),
    "MERGER_RESTRUCTURING": ("重大资产重组", "发行股份购买资产", "吸收合并", "重组"),
    "MAJOR_CONTRACT": ("重大合同", "中标", "订单", "框架协议"),
    "REGULATORY_PENALTY": ("行政处罚", "监管措施", "立案", "纪律处分"),
    "CONTROLLER_CHANGE": ("实际控制人变更", "控制权变更", "控股股东变更"),
    "DIVIDEND": ("利润分配", "权益分派", "分红"),
    "SUSPENSION_RESUMPTION": ("停牌", "复牌"),
}


class ResearchSeedPromotionService:
    """Promote only evidence-complete seeds and return tasks for the rest."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        reference: MarketReferenceService,
        candidates: CandidateScanService,
        financial_sources: FinancialSourceService,
        trading_classification: TradingClassificationService,
        cninfo: DisclosureEnumerationProvider,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.reference = reference
        self.candidates = candidates
        self.financial_sources = financial_sources
        self.financial = FinancialIntegrityRepository(state, objects)
        self.trading_classification = trading_classification
        self.cninfo = cninfo
        self.pit = PointInTimeService(PointInTimeRepository(state), state, objects)

    def promote(self, request: SeedPromotionRequest) -> SeedPromotionReport:
        seed_record = self.state.artifact_record(request.seed_report_artifact_id)
        if seed_record is None or str(seed_record["type"]) != "ResearchSeedReport":
            raise ValueError("promotion requires a registered ResearchSeedReport")
        seed_hash = str(seed_record["object_hash"])
        if not self.objects.verify(seed_hash):
            raise ValueError("ResearchSeedReport object is unavailable")
        seed_report = ResearchSeedReport.model_validate_json(self.objects.get_bytes(seed_hash))
        promotion_as_of = request.created_at if request.live else seed_report.as_of
        selected = seed_report.seeds[: request.max_seeds]
        source_artifacts: set[str] = {request.seed_report_artifact_id}
        tasks: list[SeedPromotionTask] = []
        results: list[SeedPromotionCompanyResult] = []
        promoted: list[
            tuple[ResearchSeed, CandidateCompanyInput, list[CandidateInputArtifact]]
        ] = []
        instrument_cache: dict[Market, tuple[DatasetReleaseManifest, CandidateInputArtifact]] = {}
        calendar_cache: dict[Market, tuple[DatasetReleaseManifest, CandidateInputArtifact]] = {}

        for seed in selected:
            if ResearchSeedOrigin.EXISTING_CANDIDATE in seed.origins and seed.candidate_version_id:
                results.append(
                    SeedPromotionCompanyResult(
                        company_id=seed.company_id,
                        seed_id=seed.seed_id,
                        status=SeedPromotionCompanyStatus.REUSED_EXISTING_CANDIDATE,
                        candidate_version_id=seed.candidate_version_id,
                        reason_codes=["EXISTING_RESEARCH_READY_CANDIDATE_REUSED"],
                        created_at=promotion_as_of,
                    )
                )
                continue
            try:
                company, artifacts, financial_run_id = self._promote_company(
                    seed,
                    promotion_as_of=promotion_as_of,
                    seed_report_artifact_id=request.seed_report_artifact_id,
                    seed_report_object_hash=seed_hash,
                    request=request,
                    instrument_cache=instrument_cache,
                    calendar_cache=calendar_cache,
                )
            except _PromotionBlocked as blocked:
                task = SeedPromotionTask(
                    task_id="seed-promotion-task:"
                    + content_hash(
                        {
                            "seed": seed.seed_id,
                            "code": blocked.task_code,
                            "reasons": blocked.reason_codes,
                            "sources": blocked.source_artifact_ids,
                        }
                    ),
                    company_id=seed.company_id,
                    task_code=blocked.task_code,
                    reason_codes=sorted(set(blocked.reason_codes)),
                    source_artifact_ids=sorted(set(blocked.source_artifact_ids)),
                    retryable=blocked.retryable,
                    created_at=promotion_as_of,
                )
                tasks.append(task)
                source_artifacts.update(blocked.source_artifact_ids)
                results.append(
                    SeedPromotionCompanyResult(
                        company_id=seed.company_id,
                        seed_id=seed.seed_id,
                        status=SeedPromotionCompanyStatus.NEEDS_INFO,
                        source_artifact_ids=sorted(set(blocked.source_artifact_ids)),
                        reason_codes=sorted(set(blocked.reason_codes)),
                        created_at=promotion_as_of,
                    )
                )
                continue
            promoted.append((seed, company, artifacts))
            source_artifacts.update(item.artifact_id for item in artifacts)
            results.append(
                SeedPromotionCompanyResult(
                    company_id=seed.company_id,
                    seed_id=seed.seed_id,
                    status=SeedPromotionCompanyStatus.PROMOTED,
                    source_artifact_ids=sorted({item.artifact_id for item in artifacts}),
                    reason_codes=["EVIDENCE_COMPLETE_FOR_CANDIDATE_SCAN"],
                    financial_audit_run_id=financial_run_id,
                    created_at=promotion_as_of,
                )
            )

        input_release_id: str | None = None
        input_release_hash: str | None = None
        scan_id: str | None = None
        scan_status: str | None = None
        if promoted:
            release = self._build_release(seed_report, request, promoted)
            input_release_id = release.input_release_id
            input_release_hash = self.candidates.stage_input_release(release)
            source_artifacts.add(f"candidate-input-release:{input_release_id}")
            scan_request = CandidateScanRequest(
                request_id="seed-promotion-scan:"
                + content_hash(
                    {
                        "seed_report": seed_hash,
                        "input_release": input_release_hash,
                        "live": request.live,
                    }
                ),
                input_release_id=input_release_id,
                input_release_object_hash=input_release_hash,
                as_of=release.as_of,
                formal_historical=False,
                live=request.live,
                created_at=release.as_of,
            )
            scan = self.candidates.scan(scan_request)
            scan_id = scan.scan_id
            scan_status = scan.status.value
            for index, result in enumerate(results):
                if result.status is not SeedPromotionCompanyStatus.PROMOTED:
                    continue
                candidate = self.candidates.repository.status_by_company(result.company_id)
                if candidate is not None:
                    results[index] = result.model_copy(
                        update={"candidate_version_id": str(candidate["candidate_version_id"])}
                    )

        promoted_count = sum(item.status is SeedPromotionCompanyStatus.PROMOTED for item in results)
        blocked_count = sum(
            item.status is SeedPromotionCompanyStatus.NEEDS_INFO for item in results
        )
        reused_count = sum(
            item.status is SeedPromotionCompanyStatus.REUSED_EXISTING_CANDIDATE for item in results
        )
        status = (
            SeedPromotionStatus.SUCCEEDED
            if blocked_count == 0 and (promoted_count or reused_count)
            else SeedPromotionStatus.PARTIAL
            if promoted_count or reused_count
            else SeedPromotionStatus.NEEDS_INFO
        )
        identity = {
            "seed_report_object_hash": seed_hash,
            "request": request.model_dump(mode="json", exclude={"created_at"}),
            "company_results": [item.model_dump(mode="json") for item in results],
            "candidate_input_release_object_hash": input_release_hash,
            "candidate_scan_id": scan_id,
        }
        promotion_id = "seed-promotion:" + content_hash(identity)
        report = SeedPromotionReport(
            promotion_id=promotion_id,
            seed_report_artifact_id=request.seed_report_artifact_id,
            seed_report_object_hash=seed_hash,
            as_of=promotion_as_of,
            live=request.live,
            status=status,
            selected_seed_count=len(selected),
            promoted_company_count=promoted_count,
            blocked_company_count=blocked_count,
            reused_candidate_count=reused_count,
            company_results=results,
            tasks=tasks,
            candidate_input_release_id=input_release_id,
            candidate_input_release_object_hash=input_release_hash,
            candidate_scan_id=scan_id,
            candidate_scan_status=scan_status,
            source_artifact_ids=sorted(source_artifacts),
            created_at=promotion_as_of,
        )
        return self._persist(report)

    def status(self) -> dict[str, object]:
        checkpoint = self.state.get_checkpoint("seed-promotion", "latest")
        if checkpoint is None:
            return {"status": "NOT_RUN"}
        return {
            "status": checkpoint["status"],
            "artifact_id": checkpoint["cursor"].get("artifact_id"),
            "promotion_id": checkpoint["cursor"].get("promotion_id"),
            "object_hash": checkpoint.get("object_hash"),
        }

    def audit(self, artifact_id: str) -> dict[str, object]:
        findings: set[str] = set()
        row = self.state.artifact_record(artifact_id)
        if row is None or str(row["type"]) != "SeedPromotionReport":
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_PROMOTION"],
            }
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            findings.add("PROMOTION_REPORT_OBJECT_INVALID")
            report = None
        else:
            report = SeedPromotionReport.model_validate_json(self.objects.get_bytes(object_hash))
        if report is not None:
            seed = self.state.artifact_record(report.seed_report_artifact_id)
            if (
                seed is None
                or str(seed["object_hash"]) != report.seed_report_object_hash
                or not self.objects.verify(report.seed_report_object_hash)
            ):
                findings.add("PROMOTION_SEED_LINEAGE_INVALID")
            for source_id in report.source_artifact_ids:
                source = self.state.artifact_record(source_id)
                if source is not None:
                    if not self.objects.verify(str(source["object_hash"])):
                        findings.add("PROMOTION_SOURCE_ARTIFACT_INVALID")
                    continue
                snapshot = self.state.get_snapshot(source_id)
                if snapshot is None or not self.objects.verify(snapshot.object_sha256):
                    findings.add("PROMOTION_SOURCE_ARTIFACT_INVALID")
            if report.candidate_scan_id:
                scan_audit = self.candidates.audit(report.candidate_scan_id)
                if scan_audit.status.value != "PASS":
                    findings.add("PROMOTION_CANDIDATE_SCAN_AUDIT_FAILED")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": object_hash,
            "finding_codes": sorted(findings),
            "recommendation_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def _promote_company(
        self,
        seed: ResearchSeed,
        *,
        promotion_as_of: datetime,
        seed_report_artifact_id: str,
        seed_report_object_hash: str,
        request: SeedPromotionRequest,
        instrument_cache: dict[Market, tuple[DatasetReleaseManifest, CandidateInputArtifact]],
        calendar_cache: dict[Market, tuple[DatasetReleaseManifest, CandidateInputArtifact]],
    ) -> tuple[CandidateCompanyInput, list[CandidateInputArtifact], str]:
        if seed.market not in {Market.XSHG, Market.XSHE}:
            raise _PromotionBlocked(
                "OFFICIAL_COVERAGE_UNAVAILABLE",
                ["PROMOTION_CURRENTLY_REQUIRES_CNINFO_SSE_OR_SZSE"],
                [],
                retryable=False,
            )
        effective_as_of = promotion_as_of
        instrument_manifest, instrument_artifact = instrument_cache.get(
            seed.market
        ) or self._reference_instruments(seed.market, effective_as_of, request.live)
        instrument_cache[seed.market] = (instrument_manifest, instrument_artifact)
        if request.live:
            effective_as_of = max(effective_as_of, instrument_artifact.available_to_system_at)
        instrument = self._instrument_record(instrument_manifest, seed)
        instrument_artifact = self._instrument_subset_proof(
            seed,
            instrument=instrument,
            parent_manifest=instrument_manifest,
            parent_artifact=instrument_artifact,
            seed_report_artifact_id=seed_report_artifact_id,
            seed_report_object_hash=seed_report_object_hash,
            as_of=effective_as_of,
        )

        start_date = effective_as_of.date() - timedelta(days=request.reference_lookback_days)
        end_date = effective_as_of.date()
        calendar_manifest, calendar_artifact = calendar_cache.get(
            seed.market
        ) or self._reference_calendar(
            seed.market, start_date, end_date, effective_as_of, request.live
        )
        calendar_cache[seed.market] = (calendar_manifest, calendar_artifact)
        if request.live:
            effective_as_of = max(effective_as_of, calendar_artifact.available_to_system_at)
        self._require_calendar(calendar_manifest, start_date, end_date)

        daily_manifest, daily_artifact = self._reference_daily(
            seed, start_date, end_date, effective_as_of, request.live
        )
        if request.live:
            effective_as_of = max(effective_as_of, daily_artifact.available_to_system_at)
        daily_records = [
            item
            for item in self._records(daily_manifest, DailyBarObservation)
            if item.instrument_id == instrument.instrument_id
            and item.available_to_system_at <= effective_as_of
        ]
        if len(daily_records) < 20:
            raise _PromotionBlocked(
                "DAILY_REFERENCE_REQUIRED",
                ["PROMOTION_DAILY_HISTORY_INSUFFICIENT"],
                [daily_artifact.artifact_id],
            )
        quality, quality_artifact = self._daily_quality(seed, daily_records, effective_as_of)

        corporate_artifact = self._corporate_absence(seed, request.live)
        if request.live:
            effective_as_of = max(effective_as_of, corporate_artifact.available_to_system_at)
        announcement_pack, announcement_artifact = self._announcement_pack(
            seed,
            as_of=effective_as_of,
            lookback_days=request.announcement_lookback_days,
            live=request.live,
        )
        if request.live:
            effective_as_of = max(effective_as_of, announcement_artifact.available_to_system_at)
        financial_pack, financial_artifact = self._financial_pack(
            seed, effective_as_of, request.live
        )
        if request.live:
            effective_as_of = max(effective_as_of, financial_artifact.available_to_system_at)

        daily_points = [
            CandidateDailyPoint(
                session_date=item.session_date,
                close=item.close,
                volume=item.volume,
                turnover_cny=item.amount or Decimal("0"),
                source_artifact_id=daily_artifact.artifact_id,
                observed_at=item.session_close_at,
                available_to_system_at=item.available_to_system_at,
                pit_status=daily_artifact.pit_status,
                created_at=effective_as_of,
            )
            for item in sorted(daily_records, key=lambda record: record.session_date)
        ]
        financial_flags = self._financial_flags(financial_pack, financial_artifact)
        company = CandidateCompanyInput(
            company_id=seed.company_id,
            instrument_id=instrument.instrument_id,
            market=instrument.market,
            symbol=instrument.symbol,
            name=instrument.name,
            instrument_type=instrument.instrument_type,
            tradability=(
                CandidateTradability.TRADABLE
                if instrument.tradable
                else CandidateTradability.NON_TRADABLE
            ),
            instrument_artifact_id=instrument_artifact.artifact_id,
            calendar_artifact_id=calendar_artifact.artifact_id,
            daily_artifact_id=daily_artifact.artifact_id,
            corporate_action_artifact_id=corporate_artifact.artifact_id,
            quality_artifact_id=quality_artifact.artifact_id,
            announcement_artifact_id=announcement_artifact.artifact_id,
            financial_artifact_id=financial_artifact.artifact_id,
            quality_status=CandidateQualityStatus(quality.quality_status.value),
            daily_points=daily_points,
            announcement_events=announcement_pack.events,
            financial_flags=financial_flags,
            created_at=effective_as_of,
        )
        return (
            company,
            [
                instrument_artifact,
                calendar_artifact,
                daily_artifact,
                corporate_artifact,
                quality_artifact,
                announcement_artifact,
                financial_artifact,
            ],
            financial_pack.audit_run_id,
        )

    def _reference_instruments(
        self, market: Market, as_of: datetime, live: bool
    ) -> tuple[DatasetReleaseManifest, CandidateInputArtifact]:
        status = self.reference.status(
            ReferenceDatasetKind.INSTRUMENT_MASTER, market.value, as_of=as_of
        )
        if status.get("status") != "AVAILABLE" and live:
            self.reference.sync_instruments(market, live=True)
            status = self.reference.status(ReferenceDatasetKind.INSTRUMENT_MASTER, market.value)
        return self._reference_pair(status, CandidateArtifactRole.INSTRUMENT_TRADABILITY)

    def _reference_calendar(
        self, market: Market, start: date, end: date, as_of: datetime, live: bool
    ) -> tuple[DatasetReleaseManifest, CandidateInputArtifact]:
        status = self.reference.status(
            ReferenceDatasetKind.TRADING_CALENDAR, market.value, as_of=as_of
        )
        if status.get("status") != "AVAILABLE" and live:
            self.reference.sync_calendar(market, start, end, live=True)
            status = self.reference.status(ReferenceDatasetKind.TRADING_CALENDAR, market.value)
        return self._reference_pair(status, CandidateArtifactRole.TRADING_CALENDAR)

    def _reference_daily(
        self, seed: ResearchSeed, start: date, end: date, as_of: datetime, live: bool
    ) -> tuple[DatasetReleaseManifest, CandidateInputArtifact]:
        scope = f"{seed.market.value}:{seed.company_id}"
        status = self.reference.status(ReferenceDatasetKind.DAILY_UNADJUSTED, scope, as_of=as_of)
        if status.get("status") != "AVAILABLE" and live:
            self.reference.sync_daily(seed.company_id, seed.market, start, end, live=True)
            status = self.reference.status(ReferenceDatasetKind.DAILY_UNADJUSTED, scope)
        return self._reference_pair(status, CandidateArtifactRole.DAILY_LOCAL_VERSIONED)

    def _reference_pair(
        self, status: dict[str, Any], role: CandidateArtifactRole
    ) -> tuple[DatasetReleaseManifest, CandidateInputArtifact]:
        if status.get("status") != "AVAILABLE":
            raise _PromotionBlocked(
                "REFERENCE_INPUT_REQUIRED",
                [f"{role.value}_REFERENCE_UNAVAILABLE"],
                [],
            )
        manifest = DatasetReleaseManifest.model_validate(status["release"])
        record = self.state.artifact_record(f"market-reference:{manifest.release_id}")
        if record is None or str(record["object_hash"]) == "":
            raise _PromotionBlocked(
                "REFERENCE_INPUT_REQUIRED", ["REFERENCE_MANIFEST_UNREGISTERED"], []
            )
        coverage = {
            ReferenceCoverageStatus.COMPLETE: CandidateCoverageStatus.COMPLETE,
            ReferenceCoverageStatus.PARTIAL: CandidateCoverageStatus.PARTIAL,
            ReferenceCoverageStatus.CONFLICTED: CandidateCoverageStatus.CONFLICTED,
            ReferenceCoverageStatus.FAILED: CandidateCoverageStatus.FAILED,
            ReferenceCoverageStatus.EMPTY: CandidateCoverageStatus.NOT_AVAILABLE,
        }[manifest.coverage.status]
        pit = {
            ReferencePitStatus.CERTIFIED: CandidatePitStatus.CERTIFIED,
            ReferencePitStatus.RECONSTRUCTED: CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
        }.get(manifest.pit_status, CandidatePitStatus.NOT_PIT_SAFE)
        artifact = CandidateInputArtifact(
            artifact_id=f"market-reference:{manifest.release_id}",
            role=role,
            artifact_type="DatasetReleaseManifest",
            artifact_schema_version=manifest.schema_version,
            dataset_kind=manifest.dataset_kind.value,
            formal_status=pit.value,
            source_family=manifest.provider_id,
            object_hash=str(record["object_hash"]),
            coverage_status=coverage,
            available_to_system_at=manifest.available_to_system_at,
            pit_status=pit,
            source_snapshot_ids=manifest.raw_snapshot_ids,
            created_at=manifest.available_to_system_at,
        )
        return manifest, artifact

    def _instrument_record(
        self, manifest: DatasetReleaseManifest, seed: ResearchSeed
    ) -> InstrumentRecord:
        records = self._records(manifest, InstrumentRecord)
        match = next(
            (
                item
                for item in records
                if item.instrument_id == f"{seed.market.value}:{seed.company_id}"
            ),
            None,
        )
        if match is None:
            raise _PromotionBlocked(
                "INSTRUMENT_IDENTITY_REQUIRED",
                ["SEED_INSTRUMENT_ABSENT_FROM_VERIFIED_MASTER"],
                [f"market-reference:{manifest.release_id}"],
                retryable=False,
            )
        return match

    def _instrument_subset_proof(
        self,
        seed: ResearchSeed,
        *,
        instrument: InstrumentRecord,
        parent_manifest: DatasetReleaseManifest,
        parent_artifact: CandidateInputArtifact,
        seed_report_artifact_id: str,
        seed_report_object_hash: str,
        as_of: datetime,
    ) -> CandidateInputArtifact:
        if (
            parent_artifact.coverage_status is not CandidateCoverageStatus.COMPLETE
            or parent_artifact.pit_status
            not in {
                CandidatePitStatus.CERTIFIED,
                CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
            }
        ):
            raise _PromotionBlocked(
                "INSTRUMENT_IDENTITY_REQUIRED",
                ["INSTRUMENT_MASTER_NOT_COMPLETE_OR_PIT_SAFE"],
                [parent_artifact.artifact_id],
            )
        identity = {
            "seed_report_object_hash": seed_report_object_hash,
            "parent_instrument_object_hash": parent_artifact.object_hash,
            "parent_release_id": parent_manifest.release_id,
            "as_of": as_of,
            "company_ids": [seed.company_id],
            "instrument": instrument.model_dump(mode="json", exclude={"created_at"}),
        }
        proof_id = content_hash(identity)
        proof = CandidateInstrumentUniverseProof(
            proof_id=proof_id,
            seed_report_artifact_id=seed_report_artifact_id,
            seed_report_object_hash=seed_report_object_hash,
            parent_instrument_artifact_id=parent_artifact.artifact_id,
            parent_instrument_object_hash=parent_artifact.object_hash,
            parent_release_id=parent_manifest.release_id,
            as_of=as_of,
            company_ids=[seed.company_id],
            instruments=[instrument],
            source_snapshot_ids=sorted(parent_manifest.raw_snapshot_ids),
            created_at=as_of,
        )
        ref = self.objects.put_json(proof.model_dump(mode="json"))
        artifact_id = f"CandidateInstrumentUniverseProof:{proof.proof_id}"
        existing = self.state.artifact_record(artifact_id)
        if existing is None:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="CandidateInstrumentUniverseProof",
                schema_version=proof.schema_version,
                object_hash=ref.sha256,
                input_hashes=sorted({seed_report_object_hash, parent_artifact.object_hash}),
            )
        elif (
            str(existing["type"]) != "CandidateInstrumentUniverseProof"
            or str(existing["schema_version"]) != proof.schema_version
            or str(existing["object_hash"]) != ref.sha256
        ):
            raise ValueError("CandidateInstrumentUniverseProof identity collision")
        return CandidateInputArtifact(
            artifact_id=artifact_id,
            role=CandidateArtifactRole.INSTRUMENT_TRADABILITY,
            artifact_type="CandidateInstrumentUniverseProof",
            artifact_schema_version=proof.schema_version,
            dataset_kind="INSTRUMENT_TRADABILITY_SUBSET",
            formal_status=parent_artifact.pit_status.value,
            source_family="seed-promotion-instrument-subset",
            object_hash=ref.sha256,
            coverage_status=CandidateCoverageStatus.COMPLETE,
            available_to_system_at=as_of,
            pit_status=parent_artifact.pit_status,
            source_snapshot_ids=proof.source_snapshot_ids,
            created_at=as_of,
        )

    def _require_calendar(self, manifest: DatasetReleaseManifest, start: date, end: date) -> None:
        records = self._records(manifest, TradingSession)
        visible = [item for item in records if start <= item.session_date <= end]
        if not visible or manifest.coverage.status is ReferenceCoverageStatus.FAILED:
            raise _PromotionBlocked(
                "TRADING_CALENDAR_REQUIRED",
                ["PROMOTION_CALENDAR_COVERAGE_INSUFFICIENT"],
                [f"market-reference:{manifest.release_id}"],
            )

    def _daily_quality(
        self, seed: ResearchSeed, records: list[DailyBarObservation], as_of: datetime
    ) -> tuple[DataQualityReport, CandidateInputArtifact]:
        sessions = [item.session_date for item in records]
        duplicates = len(sessions) - len(set(sessions))
        missing_amount = sum(item.amount is None for item in records)
        future = sum(item.available_to_system_at > as_of for item in records)
        status = (
            QualityStatus.PASS
            if not (duplicates or missing_amount or future)
            else QualityStatus.FAIL
        )
        reasons = []
        if duplicates:
            reasons.append("duplicate daily sessions")
        if missing_amount:
            reasons.append("daily amount missing")
        if future:
            reasons.append("future daily observations")
        source_snapshot_ids = sorted({item.source_snapshot_id for item in records})
        payload = {
            "symbol": seed.company_id,
            "sessions": [item.session_date.isoformat() for item in records],
            "source_snapshot_ids": source_snapshot_ids,
            "as_of": as_of.isoformat(),
            "duplicates": duplicates,
            "missing_amount": missing_amount,
            "future": future,
        }
        report_hash = content_hash(payload)
        report = DataQualityReport(
            report_id=report_hash,
            batch_ids=["candidate-promotion-daily:" + report_hash],
            symbol=seed.company_id,
            frequency=Frequency.D1,
            requested_start=records[0].session_close_at,
            requested_end=records[-1].session_close_at,
            actual_start=records[0].session_close_at,
            actual_end=records[-1].session_close_at,
            bar_count=len(records),
            duplicate_bars=duplicates,
            ohlc_errors=0,
            volume_unit=VolumeUnit.SHARE,
            adjustment_mode=AdjustmentMode.NONE,
            timestamp_semantics=TimestampSemantics.DATE_ONLY,
            provider_latency_ms=0,
            provider_status=ProviderStatus.AVAILABLE,
            quality_status=status,
            replay_quality=ReplayQuality.DAILY_CLOSE_MODEL,
            reasons=reasons,
            created_at=as_of,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"DataQualityReport:{report.report_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="DataQualityReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=source_snapshot_ids,
        )
        artifact = CandidateInputArtifact(
            artifact_id=artifact_id,
            role=CandidateArtifactRole.DATA_QUALITY,
            artifact_type="DataQualityReport",
            artifact_schema_version=report.schema_version,
            dataset_kind="DATA_QUALITY",
            formal_status=status.value,
            source_family="market-data-quality",
            object_hash=ref.sha256,
            coverage_status=(
                CandidateCoverageStatus.COMPLETE
                if status is QualityStatus.PASS
                else CandidateCoverageStatus.FAILED
            ),
            available_to_system_at=as_of,
            pit_status=CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
            source_snapshot_ids=sorted({item.source_snapshot_id for item in records}),
            created_at=as_of,
        )
        return report, artifact

    def _corporate_absence(self, seed: ResearchSeed, live: bool) -> CandidateInputArtifact:
        if not live:
            raise _PromotionBlocked(
                "OFFICIAL_CORPORATE_ACTION_BASELINE_REQUIRED",
                ["LIVE_OFFICIAL_CORPORATE_ACTION_ENUMERATION_REQUIRED"],
                [],
            )
        artifact_id, baseline = (
            self.trading_classification.capture_official_corporate_action_baseline(
                seed.company_id,
                live=True,
                provider=self.cninfo,
                sync_instrument_reference=False,
            )
        )
        row = self.state.artifact_record(artifact_id)
        if row is None:
            raise _PromotionBlocked(
                "OFFICIAL_CORPORATE_ACTION_BASELINE_REQUIRED",
                ["CORPORATE_ACTION_BASELINE_NOT_REGISTERED"],
                [],
            )
        if not baseline.absence_is_officially_certified:
            raise _PromotionBlocked(
                "OFFICIAL_CORPORATE_ACTION_REVIEW_REQUIRED",
                ["OFFICIAL_CORPORATE_ACTION_CANDIDATES_FOUND"],
                [artifact_id],
            )
        return CandidateInputArtifact(
            artifact_id=artifact_id,
            role=CandidateArtifactRole.CORPORATE_ACTION,
            artifact_type="TradingClassificationCorporateActionBaseline",
            artifact_schema_version=baseline.schema_version,
            dataset_kind="CORPORATE_ACTION_BASELINE",
            formal_status="CERTIFIED_ABSENCE",
            source_family="cninfo-official-corporate-action-baseline",
            object_hash=str(row["object_hash"]),
            coverage_status=CandidateCoverageStatus.COMPLETE,
            available_to_system_at=baseline.created_at,
            pit_status=CandidatePitStatus.CERTIFIED,
            source_snapshot_ids=baseline.official_query_snapshot_ids,
            created_at=baseline.created_at,
        )

    def _announcement_pack(
        self, seed: ResearchSeed, *, as_of: datetime, lookback_days: int, live: bool
    ) -> tuple[CandidateAnnouncementEventPack, CandidateInputArtifact]:
        if not live:
            raise _PromotionBlocked(
                "ANNOUNCEMENT_ENUMERATION_REQUIRED",
                ["LIVE_CNINFO_ENUMERATION_REQUIRED"],
                [],
            )
        exchange = DisclosureExchange.SSE if seed.market is Market.XSHG else DisclosureExchange.SZSE
        start = as_of.date() - timedelta(days=lookback_days)
        batches = self.cninfo.search_all(
            DisclosureSearchRequest(
                symbol=seed.company_id,
                exchange=exchange,
                start_date=start,
                end_date=as_of.date(),
                category=DisclosureCategory.ALL,
                page_number=1,
                page_size=100,
                created_at=as_of,
            )
        )
        first = batches[0]
        announcement_ids = [
            announcement.announcement_id
            for batch in batches
            for announcement in batch.announcements
        ]
        if (
            len(announcement_ids) != len(set(announcement_ids))
            or len(announcement_ids) != first.total_count
        ):
            raise _PromotionBlocked(
                "ANNOUNCEMENT_ENUMERATION_REQUIRED",
                ["CNINFO_PAGINATION_INCOMPLETE"],
                [item.raw_snapshot_id for item in batches],
            )
        matched = []
        for batch in batches:
            for announcement in batch.announcements:
                for event_type, terms in _EVENT_TITLE_TERMS.items():
                    if any(term in announcement.title for term in terms):
                        matched.append((event_type, announcement))
                        break
        snapshot_ids = sorted({item.raw_snapshot_id for item in batches})
        for snapshot_id in snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if snapshot is None:
                raise _PromotionBlocked(
                    "ANNOUNCEMENT_ENUMERATION_REQUIRED", ["CNINFO_SNAPSHOT_MISSING"], []
                )
            self.pit.create(
                source_id=f"candidate-announcement-enumeration:{seed.company_id}:{snapshot_id}",
                source_snapshot_id=snapshot_id,
                ingested_at=snapshot.fetched_at,
                available_to_system_at=snapshot.available_to_system_at,
                point_in_time_status=PointInTimeStatus.CERTIFIED,
                availability_basis=AvailabilityBasis.FETCH_OBSERVED,
            )
        if matched:
            raise _PromotionBlocked(
                "ANNOUNCEMENT_EVENT_EVIDENCE_REQUIRED",
                sorted({f"CANONICAL_EVENT_TITLE_MATCH:{item[0]}" for item in matched}),
                snapshot_ids,
            )
        created_at = max(
            self.state.get_snapshot(snapshot_id).available_to_system_at  # type: ignore[union-attr]
            for snapshot_id in snapshot_ids
        )
        pack_id = content_hash(
            {"company_id": seed.company_id, "as_of": as_of, "snapshots": snapshot_ids, "events": []}
        )
        pack = CandidateAnnouncementEventPack(
            pack_id=pack_id,
            company_id=seed.company_id,
            as_of=as_of,
            coverage_status=CandidateCoverageStatus.COMPLETE,
            pit_status=CandidatePitStatus.CERTIFIED,
            source_snapshot_ids=snapshot_ids,
            events=[],
            created_at=created_at,
        )
        ref = self.objects.put_json(pack.model_dump(mode="json"))
        artifact_id = f"candidate-announcement-events:{pack.pack_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="CandidateAnnouncementEventPack",
            schema_version=pack.schema_version,
            object_hash=ref.sha256,
            input_hashes=snapshot_ids,
        )
        artifact = CandidateInputArtifact(
            artifact_id=artifact_id,
            role=CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
            artifact_type="CandidateAnnouncementEventPack",
            artifact_schema_version=pack.schema_version,
            dataset_kind="ANNOUNCEMENT_EVENTS",
            formal_status=pack.pit_status.value,
            source_family="official-announcement-classifier",
            object_hash=ref.sha256,
            coverage_status=pack.coverage_status,
            available_to_system_at=pack.created_at,
            pit_status=pack.pit_status,
            source_snapshot_ids=snapshot_ids,
            created_at=pack.created_at,
        )
        return pack, artifact

    def _financial_pack(
        self, seed: ResearchSeed, as_of: datetime, live: bool
    ) -> tuple[Any, CandidateInputArtifact]:
        record = self.financial.latest_succeeded_run(seed.company_id, as_of=as_of)
        pack = self.financial.get_pack(record.audit_run_id) if record is not None else None
        if pack is None and live:
            period_end = date(as_of.year - 1, 12, 31)
            try:
                synced = self.financial_sources.sync(
                    seed.company_id,
                    seed.market,
                    period_end,
                    FinancialPeriodType.ANNUAL,
                    as_of=None,
                    live=True,
                    cross_check=False,
                )
                if synced.status is FinancialSourceReleaseStatus.CERTIFIED:
                    pack = self.financial_sources.run_audit(
                        seed.company_id,
                        period_end,
                        FinancialPeriodType.ANNUAL,
                        as_of=datetime.now(UTC),
                        industry_profile=self._financial_profile(seed),
                    )
            except (AStockError, OSError, RuntimeError, ValueError):
                pack = None
        if pack is None or not financial_pack_is_candidate_eligible(pack):
            raise _PromotionBlocked(
                "FINANCIAL_INTEGRITY_REQUIRED",
                ["NO_SUCCEEDED_FINANCIAL_INTEGRITY_PACK"],
                [],
            )
        artifact_id = f"FinancialIntegrityEvidencePack:{pack.audit_run_id}"
        row = self.state.artifact_record(artifact_id)
        if row is None or not self.objects.verify(str(row["object_hash"])):
            raise _PromotionBlocked(
                "FINANCIAL_INTEGRITY_REQUIRED",
                ["FINANCIAL_INTEGRITY_ARTIFACT_INVALID"],
                [artifact_id],
            )
        coverage = {
            FinancialCoverageStatus.COMPLETE: CandidateCoverageStatus.COMPLETE,
            FinancialCoverageStatus.PARTIAL: CandidateCoverageStatus.PARTIAL,
            FinancialCoverageStatus.BLOCKED: CandidateCoverageStatus.NOT_AVAILABLE,
        }[pack.coverage_status]
        pit_rows = [PointInTimeRepository(self.state).get(item) for item in pack.pit_ids]
        if not pit_rows or any(item is None for item in pit_rows):
            raise _PromotionBlocked(
                "FINANCIAL_INTEGRITY_REQUIRED", ["FINANCIAL_PIT_LINEAGE_MISSING"], [artifact_id]
            )
        pit = (
            CandidatePitStatus.CERTIFIED
            if all(
                item.point_in_time_status is PointInTimeStatus.CERTIFIED
                for item in pit_rows
                if item
            )
            else CandidatePitStatus.DOCUMENT_RECONSTRUCTED
        )
        evidence_ids: set[str] = set(pack.source_snapshot_ids)
        for item in [
            *pack.rule_findings,
            *pack.governance_findings,
            *pack.time_series_anomalies,
            *pack.peer_anomalies,
        ]:
            evidence_ids.update(item.evidence_ids)
        artifact = CandidateInputArtifact(
            artifact_id=artifact_id,
            role=CandidateArtifactRole.FINANCIAL_INTEGRITY,
            artifact_type="FinancialIntegrityEvidencePack",
            artifact_schema_version=pack.schema_version,
            dataset_kind="FINANCIAL_INTEGRITY",
            formal_status=pack.status.value,
            source_family="financial-integrity",
            object_hash=str(row["object_hash"]),
            coverage_status=coverage,
            available_to_system_at=pack.created_at,
            pit_status=pit,
            source_snapshot_ids=pack.source_snapshot_ids,
            evidence_ids=sorted(evidence_ids),
            created_at=pack.created_at,
        )
        return pack, artifact

    @staticmethod
    def _financial_profile(seed: ResearchSeed) -> FinancialIndustryProfile:
        text = " ".join(seed.expert_domain_names)
        if "银行" in text:
            return FinancialIndustryProfile.BANK
        if "保险" in text:
            return FinancialIndustryProfile.INSURANCE
        if "证券" in text or "券商" in text:
            return FinancialIndustryProfile.SECURITIES
        if "房地产" in text or "地产" in text:
            return FinancialIndustryProfile.REAL_ESTATE
        return FinancialIndustryProfile.GENERAL_INDUSTRIAL

    @staticmethod
    def _financial_flags(
        pack: Any, artifact: CandidateInputArtifact
    ) -> list[CandidateFinancialFlag]:
        flags: list[CandidateFinancialFlag] = []
        for finding in [*pack.rule_findings, *pack.governance_findings]:
            if (
                finding.status is FinancialFindingStatus.FLAG
                and finding.severity in {FinancialSeverity.MEDIUM, FinancialSeverity.HIGH}
                and finding.evidence_ids
                and not finding.evidence_gap_ids
            ):
                flags.append(
                    CandidateFinancialFlag(
                        finding_id=finding.finding_id,
                        severity=CandidateEvidenceSeverity(finding.severity.value),
                        evidence_closed=True,
                        source_artifact_id=artifact.artifact_id,
                        observed_at=pack.as_of,
                        available_to_system_at=pack.created_at,
                        pit_status=artifact.pit_status,
                        evidence_ids=sorted(finding.evidence_ids),
                        created_at=pack.created_at,
                    )
                )
        for anomaly in [*pack.time_series_anomalies, *pack.peer_anomalies]:
            if (
                anomaly.is_anomaly
                and anomaly.severity in {FinancialSeverity.MEDIUM, FinancialSeverity.HIGH}
                and anomaly.evidence_ids
                and not anomaly.evidence_gap_ids
            ):
                flags.append(
                    CandidateFinancialFlag(
                        finding_id=anomaly.anomaly_id,
                        severity=CandidateEvidenceSeverity(anomaly.severity.value),
                        evidence_closed=True,
                        source_artifact_id=artifact.artifact_id,
                        observed_at=pack.as_of,
                        available_to_system_at=pack.created_at,
                        pit_status=artifact.pit_status,
                        evidence_ids=sorted(anomaly.evidence_ids),
                        created_at=pack.created_at,
                    )
                )
        return sorted(flags, key=lambda item: item.finding_id)

    def _build_release(
        self,
        seed_report: ResearchSeedReport,
        request: SeedPromotionRequest,
        promoted: list[tuple[ResearchSeed, CandidateCompanyInput, list[CandidateInputArtifact]]],
    ) -> CandidateInputRelease:
        companies = sorted((item[1] for item in promoted), key=lambda item: item.company_id)
        artifact_by_id = {
            artifact.artifact_id: artifact for _, _, artifacts in promoted for artifact in artifacts
        }
        expected = [item.company_id for item in companies]
        identity = {
            "seed_report": seed_report.report_id,
            "companies": expected,
            "artifact_hashes": sorted(item.object_hash for item in artifact_by_id.values()),
            "live": request.live,
        }
        release_id = "seed-promotion-input:" + content_hash(identity)
        return CandidateInputRelease(
            input_release_id=release_id,
            as_of=max(item.available_to_system_at for item in artifact_by_id.values()),
            source_mode=CandidateSourceMode.LIVE if request.live else CandidateSourceMode.LOCAL,
            artifacts=sorted(
                artifact_by_id.values(), key=lambda item: (item.role.value, item.artifact_id)
            ),
            companies=companies,
            expected_company_ids=expected,
            expected_company_count=len(expected),
            company_universe_semantic_hash=content_hash(expected),
            coverage_proof_artifact_ids=sorted({item.instrument_artifact_id for item in companies}),
            created_at=max(item.available_to_system_at for item in artifact_by_id.values()),
        )

    def _records(self, manifest: DatasetReleaseManifest, model: type[Any]) -> list[Any]:
        result: list[Any] = []
        for descriptor in manifest.canonical_files:
            path = (self.reference.parquet.root / descriptor.path).resolve()
            if not path.is_relative_to(self.reference.parquet.root) or not path.is_file():
                raise _PromotionBlocked(
                    "REFERENCE_INPUT_REQUIRED",
                    ["REFERENCE_PARQUET_INVALID"],
                    [f"market-reference:{manifest.release_id}"],
                )
            raw_rows = pq.ParquetFile(path).read(columns=["record_json"]).column(0).to_pylist()
            result.extend(model.model_validate_json(raw) for raw in raw_rows)
        return result

    def _persist(self, report: SeedPromotionReport) -> SeedPromotionReport:
        ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"SeedPromotionReport:{report.promotion_id}"
        inputs: list[str] = [report.seed_report_object_hash]
        for source_id in report.source_artifact_ids:
            row = self.state.artifact_record(source_id)
            if row is not None:
                inputs.append(str(row["object_hash"]))
        existing = self.state.artifact_record(artifact_id)
        if existing is None:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="SeedPromotionReport",
                schema_version=report.schema_version,
                object_hash=ref.sha256,
                input_hashes=sorted(set(inputs)),
            )
        elif str(existing["object_hash"]) != ref.sha256:
            raise ValueError("SeedPromotionReport identity collision")
        self.state.set_checkpoint(
            scope_type="seed-promotion",
            scope_key="latest",
            cursor={"artifact_id": artifact_id, "promotion_id": report.promotion_id},
            status=report.status.value,
            object_hash=ref.sha256,
        )
        return report


class _PromotionBlocked(Exception):
    def __init__(
        self,
        task_code: str,
        reason_codes: list[str],
        source_artifact_ids: list[str],
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(task_code)
        self.task_code = task_code
        self.reason_codes = reason_codes
        self.source_artifact_ids = source_artifact_ids
        self.retryable = retryable


__all__ = ["ResearchSeedPromotionService"]
