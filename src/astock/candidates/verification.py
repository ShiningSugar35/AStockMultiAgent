"""Typed verification of candidate input artifacts and universe coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pyarrow.parquet as pq
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.pit import PointInTimeRepository
from astock.schemas import (
    DailyBarObservation,
    DataQualityReport,
    DatasetReleaseManifest,
    EvidenceGrade,
    FactStatus,
    FinancialCoverageStatus,
    FinancialFindingStatus,
    FinancialIntegrityEvidencePack,
    FinancialSeverity,
    HoldingReviewPack,
    InstrumentRecord,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferencePitStatus,
)
from astock.schemas.candidates import (
    CandidateAnnouncementEventPack,
    CandidateArtifactRole,
    CandidateCoverageStatus,
    CandidateInputArtifact,
    CandidateInputRelease,
    CandidateInstrumentUniverseProof,
    CandidatePitStatus,
    CandidateQualityStatus,
    CandidateTradability,
)
from astock.schemas.pit import PointInTimeStatus
from astock.schemas.research_runtime import TradingClassificationCorporateActionBaseline
from astock.schemas.research_seeds import ResearchSeedReport


@dataclass(frozen=True, slots=True)
class CandidateVerificationResult:
    issue_codes: tuple[str, ...]
    proven_company_ids: frozenset[str] | None


class CandidateInputVerifier(Protocol):
    def verify(self, release: CandidateInputRelease) -> CandidateVerificationResult: ...


_REFERENCE_KINDS = {
    CandidateArtifactRole.INSTRUMENT_TRADABILITY: ReferenceDatasetKind.INSTRUMENT_MASTER,
    CandidateArtifactRole.TRADING_CALENDAR: ReferenceDatasetKind.TRADING_CALENDAR,
    CandidateArtifactRole.DAILY_LOCAL_VERSIONED: ReferenceDatasetKind.DAILY_UNADJUSTED,
    CandidateArtifactRole.CORPORATE_ACTION: ReferenceDatasetKind.CORPORATE_ACTION,
}

_ROLE_CONTRACTS = {
    **{
        role: ("DatasetReleaseManifest", "market-reference-release-v2", kind.value)
        for role, kind in _REFERENCE_KINDS.items()
    },
    CandidateArtifactRole.DATA_QUALITY: ("DataQualityReport", "1.0", "DATA_QUALITY"),
    CandidateArtifactRole.ANNOUNCEMENT_EVENTS: (
        "CandidateAnnouncementEventPack",
        "candidate-announcement-event-pack-v1",
        "ANNOUNCEMENT_EVENTS",
    ),
    CandidateArtifactRole.FINANCIAL_INTEGRITY: (
        "FinancialIntegrityEvidencePack",
        "1.0",
        "FINANCIAL_INTEGRITY",
    ),
    CandidateArtifactRole.USER_WATCHLIST: (
        "UserWatchlistSnapshot",
        "candidate-watchlist-snapshot-v1",
        "USER_WATCHLIST",
    ),
    CandidateArtifactRole.HOLDING_REVIEW: (
        "HoldingReviewPack",
        "1.0",
        "HOLDING_REVIEW",
    ),
}


class ProductionCandidateInputVerifier:
    """Fail-closed verifier backed by real registries, typed objects, and Parquet."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        reference_root: Path,
        fixture_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.reference_root = reference_root.resolve()
        self.reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(self.reference_root),
            fixture_root,
        )

    def verify(self, release: CandidateInputRelease) -> CandidateVerificationResult:
        issues: list[str] = []
        proven: set[str] = set()
        proof_seen = False
        company_by_artifact: dict[str, set[str]] = {}
        for artifact in release.artifacts:
            corporate_baseline = (
                artifact.role is CandidateArtifactRole.CORPORATE_ACTION
                and artifact.artifact_type == "TradingClassificationCorporateActionBaseline"
            )
            instrument_proof = (
                artifact.role is CandidateArtifactRole.INSTRUMENT_TRADABILITY
                and artifact.artifact_type == "CandidateInstrumentUniverseProof"
            )
            role_contract = _ROLE_CONTRACTS[artifact.role]
            if (
                not corporate_baseline
                and not instrument_proof
                and (
                    artifact.artifact_type,
                    artifact.artifact_schema_version,
                    artifact.dataset_kind,
                )
                != role_contract
            ):
                issues.append(f"ARTIFACT_CONTRACT_MISMATCH:{artifact.artifact_id}")
                continue
            if artifact.artifact_type.startswith("Fixture"):
                issues.append(f"TEST_FIXTURE_FORBIDDEN:{artifact.artifact_id}")
                continue
            if not self.objects.verify(artifact.object_hash):
                issues.append(f"OBJECT_INVALID:{artifact.artifact_id}")
                continue
            try:
                if corporate_baseline:
                    self._verify_corporate_action_baseline(artifact, release)
                elif instrument_proof:
                    companies = self._verify_instrument_universe_proof(artifact, release)
                    company_by_artifact[artifact.artifact_id] = companies
                elif artifact.role in _REFERENCE_KINDS:
                    companies = self._verify_reference(artifact, release)
                    company_by_artifact[artifact.artifact_id] = companies
                elif artifact.role is CandidateArtifactRole.DATA_QUALITY:
                    self._verify_quality(artifact, release)
                elif artifact.role is CandidateArtifactRole.ANNOUNCEMENT_EVENTS:
                    self._verify_announcement_pack(artifact, release)
                elif artifact.role is CandidateArtifactRole.FINANCIAL_INTEGRITY:
                    self._verify_financial(artifact, release)
                elif artifact.role is CandidateArtifactRole.USER_WATCHLIST:
                    self._verify_watchlist(artifact, release)
                elif artifact.role is CandidateArtifactRole.HOLDING_REVIEW:
                    self._verify_holding(artifact, release)
            except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
                issues.append(f"TYPED_ARTIFACT_INVALID:{artifact.artifact_id}")
        for artifact_id in release.coverage_proof_artifact_ids:
            if artifact_id in company_by_artifact:
                proof_seen = True
                proven.update(company_by_artifact[artifact_id])
        return CandidateVerificationResult(
            issue_codes=tuple(sorted(set(issues))),
            proven_company_ids=frozenset(proven) if proof_seen else None,
        )

    def _verify_reference(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> set[str]:
        manifest = DatasetReleaseManifest.model_validate_json(
            self.objects.get_bytes(artifact.object_hash)
        )
        expected_kind = _REFERENCE_KINDS[artifact.role]
        result = self.reference.status(
            expected_kind,
            manifest.scope_key,
            as_of=release.as_of,
        )
        if result.get("status") != "AVAILABLE":
            raise ValueError("reference release is unavailable or corrupt")
        verified = DatasetReleaseManifest.model_validate(result["release"])
        expected_artifact_id = f"market-reference:{verified.release_id}"
        expected_pit = {
            ReferencePitStatus.CERTIFIED: CandidatePitStatus.CERTIFIED,
            ReferencePitStatus.RECONSTRUCTED: CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
        }.get(verified.pit_status, CandidatePitStatus.NOT_PIT_SAFE)
        expected_coverage = {
            ReferenceCoverageStatus.COMPLETE: CandidateCoverageStatus.COMPLETE,
            ReferenceCoverageStatus.PARTIAL: CandidateCoverageStatus.PARTIAL,
            ReferenceCoverageStatus.FAILED: CandidateCoverageStatus.FAILED,
            ReferenceCoverageStatus.EMPTY: CandidateCoverageStatus.NOT_AVAILABLE,
        }[verified.coverage.status]
        if (
            verified.release_id != manifest.release_id
            or verified.dataset_kind is not expected_kind
            or artifact.artifact_id != expected_artifact_id
            or artifact.object_hash != self._registered_hash(expected_artifact_id)
            or artifact.coverage_status is not expected_coverage
            or artifact.pit_status is not expected_pit
            or artifact.formal_status != expected_pit.value
            or artifact.available_to_system_at != verified.available_to_system_at
            or artifact.source_family != verified.provider_id
            or set(artifact.source_snapshot_ids) != set(verified.raw_snapshot_ids)
        ):
            raise ValueError("reference wrapper differs from verified release")
        if artifact.role is CandidateArtifactRole.INSTRUMENT_TRADABILITY:
            instruments = self._reference_records(verified, InstrumentRecord)
            return self._verify_instrument_inputs(artifact, release, instruments)
        if artifact.role is CandidateArtifactRole.DAILY_LOCAL_VERSIONED:
            daily = self._reference_records(verified, DailyBarObservation)
            self._verify_daily_inputs(artifact, release, daily)
        return set()

    def _reference_records(self, manifest: DatasetReleaseManifest, model: type[Any]) -> list[Any]:
        records: list[Any] = []
        for descriptor in manifest.canonical_files:
            path = (self.reference_root / descriptor.path).resolve()
            if not path.is_relative_to(self.reference_root):
                raise ValueError("reference Parquet escapes reference root")
            for raw in pq.ParquetFile(path).read().column("record_json").to_pylist():
                records.append(model.model_validate_json(raw))
        return records

    @staticmethod
    def _expected_tradability(instrument: InstrumentRecord) -> CandidateTradability:
        if instrument.instrument_type.value == "INDEX":
            return CandidateTradability.INDEX_CONTEXT
        if instrument.tradable:
            return CandidateTradability.TRADABLE
        if instrument.delisting_date is not None:
            return CandidateTradability.DELISTED
        return CandidateTradability.NON_TRADABLE

    def _verify_instrument_universe_proof(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> set[str]:
        self._verify_registered_artifact(artifact)
        proof = CandidateInstrumentUniverseProof.model_validate_json(
            self.objects.get_bytes(artifact.object_hash)
        )
        if (
            artifact.artifact_id != f"CandidateInstrumentUniverseProof:{proof.proof_id}"
            or artifact.artifact_schema_version != proof.schema_version
            or artifact.dataset_kind != "INSTRUMENT_TRADABILITY_SUBSET"
            or artifact.source_family != "seed-promotion-instrument-subset"
            or artifact.coverage_status is not CandidateCoverageStatus.COMPLETE
            or artifact.available_to_system_at != proof.as_of
            or artifact.source_snapshot_ids != proof.source_snapshot_ids
            or proof.as_of > release.as_of
        ):
            raise ValueError("instrument universe proof wrapper differs from typed proof")

        seed_record = self.state.artifact_record(proof.seed_report_artifact_id)
        if (
            seed_record is None
            or str(seed_record["type"]) != "ResearchSeedReport"
            or str(seed_record["object_hash"]) != proof.seed_report_object_hash
            or not self.objects.verify(proof.seed_report_object_hash)
        ):
            raise ValueError("instrument universe proof seed lineage is invalid")
        seed_report = ResearchSeedReport.model_validate_json(
            self.objects.get_bytes(proof.seed_report_object_hash)
        )
        if not set(proof.company_ids).issubset({item.company_id for item in seed_report.seeds}):
            raise ValueError("instrument universe proof companies are absent from the seed report")

        parent_record = self.state.artifact_record(proof.parent_instrument_artifact_id)
        if (
            parent_record is None
            or str(parent_record["type"]) != "DatasetReleaseManifest"
            or str(parent_record["object_hash"]) != proof.parent_instrument_object_hash
            or not self.objects.verify(proof.parent_instrument_object_hash)
        ):
            raise ValueError("instrument universe proof parent release is invalid")
        parent = DatasetReleaseManifest.model_validate_json(
            self.objects.get_bytes(proof.parent_instrument_object_hash)
        )
        expected_pit = {
            ReferencePitStatus.CERTIFIED: CandidatePitStatus.CERTIFIED,
            ReferencePitStatus.RECONSTRUCTED: CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
        }.get(parent.pit_status, CandidatePitStatus.NOT_PIT_SAFE)
        if (
            parent.dataset_kind is not ReferenceDatasetKind.INSTRUMENT_MASTER
            or parent.release_id != proof.parent_release_id
            or parent.available_to_system_at > proof.as_of
            or proof.source_snapshot_ids != parent.raw_snapshot_ids
            or artifact.pit_status is not expected_pit
            or artifact.formal_status != expected_pit.value
        ):
            raise ValueError("instrument universe proof parent metadata is invalid")
        current = self.reference.status(
            ReferenceDatasetKind.INSTRUMENT_MASTER,
            parent.scope_key,
            as_of=proof.as_of,
        )
        if current.get("status") != "AVAILABLE":
            raise ValueError("instrument universe proof parent release is unavailable")
        verified_parent = DatasetReleaseManifest.model_validate(current["release"])
        if verified_parent.release_id != parent.release_id:
            raise ValueError("instrument universe proof parent release is not the PIT head")

        parent_by_id = {
            item.instrument_id: item for item in self._reference_records(parent, InstrumentRecord)
        }
        proof_by_id = {item.instrument_id: item for item in proof.instruments}
        if len(parent_by_id) < len(proof_by_id) or not proof_by_id:
            raise ValueError("instrument universe proof is empty or exceeds its parent")
        for instrument_id, bounded in proof_by_id.items():
            parent_instrument = parent_by_id.get(instrument_id)
            if parent_instrument is None or bounded.model_dump(
                mode="json", exclude={"created_at"}
            ) != parent_instrument.model_dump(mode="json", exclude={"created_at"}):
                raise ValueError("instrument universe proof differs from its parent release")

        companies = [
            item
            for item in release.companies
            if item.instrument_artifact_id == artifact.artifact_id
        ]
        if {item.company_id for item in companies} != set(proof.company_ids):
            raise ValueError("candidate companies differ from the bounded instrument proof")
        if {item.instrument_id for item in companies} != set(proof_by_id):
            raise ValueError("candidate instruments differ from the bounded instrument proof")
        for company in companies:
            instrument = proof_by_id[company.instrument_id]
            if (
                company.market is not instrument.market
                or company.symbol != instrument.symbol
                or company.name != instrument.name
                or company.instrument_type is not instrument.instrument_type
                or company.tradability is not self._expected_tradability(instrument)
            ):
                raise ValueError("candidate fields differ from the bounded instrument proof")
        return set(proof.company_ids)

    def _verify_instrument_inputs(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
        instruments: list[InstrumentRecord],
    ) -> set[str]:
        by_id = {item.instrument_id: item for item in instruments}
        if len(by_id) != len(instruments):
            raise ValueError("instrument release contains duplicate identities")
        companies = [
            item
            for item in release.companies
            if item.instrument_artifact_id == artifact.artifact_id
        ]
        candidate_instruments = {item.instrument_id for item in companies}
        if candidate_instruments != set(by_id):
            raise ValueError("candidate company universe differs from instrument release")
        for company in companies:
            instrument = by_id[company.instrument_id]
            if (
                company.market is not instrument.market
                or company.symbol != instrument.symbol
                or company.name != instrument.name
                or company.instrument_type is not instrument.instrument_type
                or company.tradability is not self._expected_tradability(instrument)
                or instrument.available_to_system_at > release.as_of
                or instrument.source_snapshot_id not in artifact.source_snapshot_ids
            ):
                raise ValueError("candidate instrument fields differ from typed reference")
        return {item.company_id for item in companies}

    def _verify_daily_inputs(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
        observations: list[DailyBarObservation],
    ) -> None:
        companies = [
            item for item in release.companies if item.daily_artifact_id == artifact.artifact_id
        ]
        company_by_instrument = {item.instrument_id: item for item in companies}
        if any(item.instrument_id not in company_by_instrument for item in observations):
            raise ValueError("daily release contains an unbound instrument")
        for company in companies:
            typed = {
                item.session_date: item
                for item in observations
                if item.instrument_id == company.instrument_id
            }
            if len(typed) != sum(
                item.instrument_id == company.instrument_id for item in observations
            ):
                raise ValueError("daily release contains duplicate sessions")
            supplied = {item.session_date: item for item in company.daily_points}
            if set(supplied) != set(typed):
                raise ValueError("candidate daily sessions differ from typed reference")
            for session_date, point in supplied.items():
                source = typed[session_date]
                if (
                    source.amount is None
                    or point.close != source.close
                    or point.volume != source.volume
                    or point.turnover_cny != source.amount
                    or point.source_artifact_id != artifact.artifact_id
                    or point.observed_at != source.session_close_at
                    or point.available_to_system_at != source.available_to_system_at
                    or point.pit_status is not artifact.pit_status
                    or source.available_to_system_at > release.as_of
                    or source.source_snapshot_id not in artifact.source_snapshot_ids
                ):
                    raise ValueError("candidate daily values differ from typed reference")

    def _verify_corporate_action_baseline(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        baseline = TradingClassificationCorporateActionBaseline.model_validate_json(
            self.objects.get_bytes(artifact.object_hash)
        )
        companies = [
            item
            for item in release.companies
            if item.corporate_action_artifact_id == artifact.artifact_id
        ]
        if len(companies) != 1:
            raise ValueError("corporate-action baseline must bind exactly one candidate")
        company = companies[0]
        if (
            baseline.company_id != company.company_id
            or baseline.symbol != company.symbol
            or baseline.market is not company.market
            or not baseline.absence_is_officially_certified
            or baseline.candidate_announcement_ids
            or not baseline.official_query_snapshot_ids
            or artifact.artifact_schema_version != baseline.schema_version
            or artifact.dataset_kind != "CORPORATE_ACTION_BASELINE"
            or artifact.formal_status != "CERTIFIED_ABSENCE"
            or artifact.coverage_status is not CandidateCoverageStatus.COMPLETE
            or artifact.pit_status is not CandidatePitStatus.CERTIFIED
            or artifact.source_family != "cninfo-official-corporate-action-baseline"
            or artifact.available_to_system_at != baseline.created_at
            or artifact.source_snapshot_ids != baseline.official_query_snapshot_ids
            or baseline.created_at > release.as_of
        ):
            raise ValueError("corporate-action baseline wrapper differs from certified object")
        for snapshot_id in baseline.official_query_snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if (
                snapshot is None
                or snapshot.source_id != "cninfo-disclosures:index"
                or snapshot.available_to_system_at > release.as_of
                or not self.objects.verify(snapshot.object_sha256)
            ):
                raise ValueError("corporate-action baseline source snapshot is invalid")

    def _verify_quality(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        report = DataQualityReport.model_validate_json(self.objects.get_bytes(artifact.object_hash))
        companies = [
            item for item in release.companies if item.quality_artifact_id == artifact.artifact_id
        ]
        expected_status = {
            "PASS": CandidateCoverageStatus.COMPLETE,
            "PARTIAL": CandidateCoverageStatus.PARTIAL,
            "FAIL": CandidateCoverageStatus.FAILED,
        }[report.quality_status.value]
        expected_quality = CandidateQualityStatus(report.quality_status.value)
        if (
            artifact.artifact_id != f"DataQualityReport:{report.report_id}"
            or artifact.formal_status != report.quality_status.value
            or artifact.coverage_status is not expected_status
            or artifact.available_to_system_at != report.created_at
            or artifact.source_family != "market-data-quality"
            or artifact.pit_status is not CandidatePitStatus.DOCUMENT_RECONSTRUCTED
            or report.actual_end is None
            or report.actual_end > report.created_at
            or any(item.symbol != report.symbol for item in companies)
            or any(item.quality_status is not expected_quality for item in companies)
        ):
            raise ValueError("quality wrapper differs from typed report")

    def _verify_announcement_pack(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        pack = CandidateAnnouncementEventPack.model_validate_json(
            self.objects.get_bytes(artifact.object_hash)
        )
        companies = [
            item
            for item in release.companies
            if item.announcement_artifact_id == artifact.artifact_id
        ]
        if len(companies) != 1 or companies[0].company_id != pack.company_id:
            raise ValueError("announcement pack company binding is invalid")
        company = companies[0]
        supplied = [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in company.announcement_events
        ]
        packed = [item.model_dump(mode="json", exclude={"created_at"}) for item in pack.events]
        evidence_ids = {item for event in pack.events for item in event.evidence_ids}
        if (
            pack.schema_version != "candidate-announcement-event-pack-v1"
            or artifact.artifact_id != f"candidate-announcement-events:{pack.pack_id}"
            or artifact.coverage_status is not pack.coverage_status
            or artifact.pit_status is not pack.pit_status
            or artifact.formal_status != pack.pit_status.value
            or artifact.available_to_system_at != pack.created_at
            or artifact.source_family != "official-announcement-classifier"
            or artifact.source_snapshot_ids != pack.source_snapshot_ids
            or set(artifact.evidence_ids) != evidence_ids
            or supplied != packed
            or any(item.source_artifact_id != artifact.artifact_id for item in pack.events)
            or pack.as_of > pack.created_at
            or pack.created_at > release.as_of
            or any(
                item.available_to_system_at != pack.created_at
                or item.observed_at > item.available_to_system_at
                for item in pack.events
            )
        ):
            raise ValueError("announcement wrapper differs from typed pack")
        evidence_repository = EvidenceRepository(self.state)
        for event in pack.events:
            for evidence_id in event.evidence_ids:
                evidence = evidence_repository.get_evidence(evidence_id)
                if (
                    evidence is None
                    or evidence.snapshot_id not in pack.source_snapshot_ids
                    or evidence.evidence_grade is not EvidenceGrade.PRIMARY_OFFICIAL
                    or evidence.fact_status is not FactStatus.DIRECT
                    or evidence.available_to_system_at > event.available_to_system_at
                    or not {
                        company.company_id,
                        company.symbol,
                        company.instrument_id,
                    }.intersection(evidence.entity_ids)
                    or not self.objects.verify(evidence.excerpt_object_sha256)
                ):
                    raise ValueError("announcement evidence binding is invalid")
        expected_pit = self._verified_snapshot_pit(pack.source_snapshot_ids, release)
        if expected_pit is not pack.pit_status:
            raise ValueError("announcement pack PIT status is invalid")

    def _verify_financial(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        pack = FinancialIntegrityEvidencePack.model_validate_json(
            self.objects.get_bytes(artifact.object_hash)
        )
        evidence = set(pack.source_snapshot_ids)
        for item in [
            *pack.rule_findings,
            *pack.governance_findings,
            *pack.time_series_anomalies,
            *pack.peer_anomalies,
        ]:
            evidence.update(item.evidence_ids)
        companies = {
            item.company_id
            for item in release.companies
            if any(flag.source_artifact_id == artifact.artifact_id for flag in item.financial_flags)
        }
        expected_coverage = {
            FinancialCoverageStatus.COMPLETE: CandidateCoverageStatus.COMPLETE,
            FinancialCoverageStatus.PARTIAL: CandidateCoverageStatus.PARTIAL,
            FinancialCoverageStatus.BLOCKED: CandidateCoverageStatus.NOT_AVAILABLE,
        }[pack.coverage_status]
        pit_metadata = [PointInTimeRepository(self.state).get(item) for item in pack.pit_ids]
        if not pit_metadata or any(item is None for item in pit_metadata):
            raise ValueError("financial pack PIT lineage is missing")
        typed_pit = [item for item in pit_metadata if item is not None]
        if any(
            item.available_to_system_at > release.as_of
            or item.point_in_time_status
            not in {PointInTimeStatus.CERTIFIED, PointInTimeStatus.DOCUMENT_RECONSTRUCTED}
            for item in typed_pit
        ):
            raise ValueError("financial pack PIT lineage is unusable")
        expected_pit = (
            CandidatePitStatus.CERTIFIED
            if all(item.point_in_time_status is PointInTimeStatus.CERTIFIED for item in typed_pit)
            else CandidatePitStatus.DOCUMENT_RECONSTRUCTED
        )
        bound_companies = [
            item for item in release.companies if item.financial_artifact_id == artifact.artifact_id
        ]
        expected_flags: dict[str, tuple[FinancialSeverity, set[str]]] = {}
        for finding in [*pack.rule_findings, *pack.governance_findings]:
            if (
                finding.status is FinancialFindingStatus.FLAG
                and finding.severity in {FinancialSeverity.MEDIUM, FinancialSeverity.HIGH}
                and finding.evidence_ids
                and not finding.evidence_gap_ids
            ):
                expected_flags[finding.finding_id] = (
                    finding.severity,
                    set(finding.evidence_ids),
                )
        for anomaly in [*pack.time_series_anomalies, *pack.peer_anomalies]:
            if (
                anomaly.is_anomaly
                and anomaly.severity in {FinancialSeverity.MEDIUM, FinancialSeverity.HIGH}
                and anomaly.evidence_ids
                and not anomaly.evidence_gap_ids
            ):
                expected_flags[anomaly.anomaly_id] = (
                    anomaly.severity,
                    set(anomaly.evidence_ids),
                )
        supplied_flags = {
            item.finding_id: item
            for company in bound_companies
            for item in company.financial_flags
            if item.source_artifact_id == artifact.artifact_id
        }
        if (
            artifact.artifact_id != f"FinancialIntegrityEvidencePack:{pack.audit_run_id}"
            or artifact.formal_status != pack.status.value
            or artifact.coverage_status is not expected_coverage
            or artifact.available_to_system_at != pack.created_at
            or artifact.source_family != "financial-integrity"
            or artifact.pit_status is not expected_pit
            or set(artifact.source_snapshot_ids) != set(pack.source_snapshot_ids)
            or len(bound_companies) != 1
            or bound_companies[0].company_id != pack.company_id
            or companies - {pack.company_id}
            or not set(artifact.evidence_ids).issubset(evidence)
            or set(supplied_flags) != set(expected_flags)
            or pack.as_of > pack.created_at
            or pack.created_at > release.as_of
        ):
            raise ValueError("financial wrapper differs from typed pack")
        for finding_id, flag in supplied_flags.items():
            severity, finding_evidence = expected_flags[finding_id]
            if (
                flag.severity.value != severity.value
                or not flag.evidence_closed
                or set(flag.evidence_ids) != finding_evidence
                or flag.observed_at != pack.as_of
                or flag.available_to_system_at != pack.created_at
                or flag.pit_status is not artifact.pit_status
            ):
                raise ValueError("candidate financial flag differs from typed finding")

    def _verified_snapshot_pit(
        self,
        snapshot_ids: list[str],
        release: CandidateInputRelease,
    ) -> CandidatePitStatus:
        statuses: list[PointInTimeStatus] = []
        repository = PointInTimeRepository(self.state)
        for snapshot_id in snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            entries = [
                item
                for item in repository.for_snapshot(snapshot_id)
                if item.available_to_system_at <= release.as_of
                and item.point_in_time_status
                in {
                    PointInTimeStatus.CERTIFIED,
                    PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
                }
            ]
            if (
                snapshot is None
                or snapshot.available_to_system_at > release.as_of
                or not self.objects.verify(snapshot.object_sha256)
                or not entries
            ):
                raise ValueError("announcement source snapshot PIT lineage is invalid")
            statuses.extend(item.point_in_time_status for item in entries)
        return (
            CandidatePitStatus.CERTIFIED
            if statuses and all(item is PointInTimeStatus.CERTIFIED for item in statuses)
            else CandidatePitStatus.DOCUMENT_RECONSTRUCTED
        )

    def _verify_watchlist(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        payload = json.loads(self.objects.get_bytes(artifact.object_hash))
        if not isinstance(payload, dict):
            raise ValueError("watchlist root must be an object")
        expected_intents = {
            item.intent_id
            for company in release.companies
            for item in company.watchlist_intents
            if item.source_artifact_id == artifact.artifact_id
        }
        if (
            payload.get("schema_version") != "candidate-watchlist-snapshot-v1"
            or payload.get("confirmed") is not True
            or set(payload.get("intent_ids", [])) != expected_intents
            or payload.get("available_to_system_at") != artifact.available_to_system_at.isoformat()
            or artifact.formal_status != "USER_CONFIRMED"
            or artifact.source_family != "user-watchlist"
            or artifact.pit_status is not CandidatePitStatus.CERTIFIED
            or artifact.source_snapshot_ids
        ):
            raise ValueError("watchlist wrapper differs from confirmed snapshot")

    def _verify_holding(
        self,
        artifact: CandidateInputArtifact,
        release: CandidateInputRelease,
    ) -> None:
        self._verify_registered_artifact(artifact)
        review = HoldingReviewPack.model_validate_json(self.objects.get_bytes(artifact.object_hash))
        expected_ids = {
            item.review_id
            for company in release.companies
            for item in company.holding_observations
            if item.source_artifact_id == artifact.artifact_id
        }
        observations = [
            item
            for company in release.companies
            for item in company.holding_observations
            if item.source_artifact_id == artifact.artifact_id
        ]
        expected_change = (
            "INVALIDATING_EVIDENCE"
            if review.thesis_strength_change == "WEAKENED" or review.risk_change == "HIGHER"
            else "NEW_EVIDENCE"
            if review.evidence_ids
            or review.triggered_rules
            or any(
                [
                    *review.new_market_data,
                    *review.new_disclosures,
                    *review.new_regulatory_events,
                    *review.new_industry_data,
                    *review.new_news_leads,
                    *review.manual_evidence_updates,
                ]
            )
            else "UNCHANGED"
        )
        if (
            review.review_id is None
            or expected_ids != {review.review_id}
            or artifact.artifact_id != f"HoldingReviewPack:{review.review_id}"
            or artifact.formal_status != "VERIFIED"
            or artifact.available_to_system_at != review.created_at
            or artifact.source_family != "holding-review"
            or artifact.pit_status is not CandidatePitStatus.NOT_PIT_SAFE
            or artifact.source_snapshot_ids
            or not set(artifact.evidence_ids).issubset(review.evidence_ids)
            or review.as_of > review.created_at
            or review.created_at > release.as_of
            or len(observations) != 1
            or observations[0].change.value != expected_change
            or set(observations[0].evidence_ids) != set(review.evidence_ids)
            or observations[0].observed_at != review.as_of
            or observations[0].available_to_system_at != review.created_at
            or observations[0].pit_status is not artifact.pit_status
        ):
            raise ValueError("holding wrapper differs from typed review")

    def _verify_registered_artifact(self, artifact: CandidateInputArtifact) -> None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,schema_version,object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()
        if row is None or tuple(row) != (
            artifact.artifact_type,
            artifact.artifact_schema_version,
            artifact.object_hash,
        ):
            raise ValueError("registered artifact contract mismatch")

    def _registered_hash(self, artifact_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return str(row["object_hash"]) if row is not None else None


class CandidateTestInputVerifier:
    """Explicit test-only adapter; production construction never selects it."""

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    def verify(self, release: CandidateInputRelease) -> CandidateVerificationResult:
        issues: list[str] = []
        proven: set[str] = set()
        proof_seen = False
        for artifact in release.artifacts:
            if not artifact.artifact_type.startswith("Fixture"):
                issues.append(f"NON_FIXTURE_IN_TEST_ADAPTER:{artifact.artifact_id}")
            try:
                payload = json.loads(self.objects.get_bytes(artifact.object_hash))
            except (OSError, ValueError, json.JSONDecodeError):
                issues.append(f"OBJECT_INVALID:{artifact.artifact_id}")
                continue
            if artifact.artifact_id in release.coverage_proof_artifact_ids:
                proof_seen = True
                if isinstance(payload, dict) and isinstance(payload.get("company_ids"), list):
                    proven.update(str(item) for item in payload["company_ids"])
                else:
                    issues.append(f"COVERAGE_PROOF_INVALID:{artifact.artifact_id}")
        return CandidateVerificationResult(
            issue_codes=tuple(sorted(set(issues))),
            proven_company_ids=frozenset(proven) if proof_seen else None,
        )


__all__ = [
    "CandidateInputVerifier",
    "CandidateTestInputVerifier",
    "CandidateVerificationResult",
    "ProductionCandidateInputVerifier",
]
