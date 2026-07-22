"""Deterministic, PIT-safe candidate scan and audit service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from astock.candidates.config import CandidateScanConfig
from astock.candidates.repository import CandidateRepository
from astock.candidates.storage import CandidateParquetStore
from astock.candidates.verification import (
    CandidateInputVerifier,
    ProductionCandidateInputVerifier,
)
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.schemas.candidates import (
    CandidateArtifactRole,
    CandidateAuditReport,
    CandidateAuditStatus,
    CandidateCheckpointStep,
    CandidateCompanyInput,
    CandidateCoverageStatus,
    CandidateDailyPoint,
    CandidateEvaluationStatus,
    CandidateEvidenceSeverity,
    CandidateFileDescriptor,
    CandidateHoldingChange,
    CandidateInputArtifact,
    CandidateInputRelease,
    CandidateLifecycleStatus,
    CandidatePitStatus,
    CandidateQualityStatus,
    CandidateRecord,
    CandidateScanReport,
    CandidateScanRequest,
    CandidateScanStatus,
    CandidateSignal,
    CandidateSignalDisposition,
    CandidateSignalManifest,
    CandidateSignalType,
    CandidateSourceMode,
    CandidateStrength,
    CandidateTradability,
    CandidateUniverseMember,
    CandidateUniverseSnapshot,
    CandidateWatchlistIntent,
)


class CandidateInterrupted(RuntimeError):
    """Testable process-interruption boundary; the attempt remains recoverable."""


class CandidateScanService:
    def __init__(
        self,
        repository: CandidateRepository,
        objects: ObjectStore,
        parquet: CandidateParquetStore,
        config: CandidateScanConfig,
        input_verifier: CandidateInputVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.objects = objects
        self.parquet = parquet
        self.config = config
        self.input_verifier = input_verifier or ProductionCandidateInputVerifier(
            repository.state,
            objects,
            parquet.root.parent,
            Path.cwd() / "tests" / "fixtures" / "reference",
        )

    def stage_input_release(self, release: CandidateInputRelease) -> str:
        """Store and register a release assembled by upstream deterministic services."""

        self._verify_upstream_objects(release)
        ref = self.objects.put_json(release.model_dump(mode="json"))
        self.repository.register_input_release(release, ref.sha256)
        return ref.sha256

    def scan(
        self,
        request: CandidateScanRequest,
        *,
        interrupt_after: CandidateCheckpointStep | None = None,
    ) -> CandidateScanReport:
        if request.rules_version != self.config.rules_version:
            raise ValueError("Candidate rules version does not match configuration")
        release = CandidateInputRelease.model_validate_json(
            self.objects.get_bytes(request.input_release_object_hash)
        )
        self._validate_request_binding(request, release)
        self.repository.register_input_release(release, request.input_release_object_hash)
        request_hash = content_hash(
            {
                "input_release_id": request.input_release_id,
                "input_release_object_hash": request.input_release_object_hash,
                "as_of": request.as_of,
                "rules_version": request.rules_version,
                "formal_historical": request.formal_historical,
                "live": request.live,
            }
        )
        scan_id = content_hash(
            {
                "request_hash": request_hash,
                "input_release_id": release.input_release_id,
                "rules_version": self.config.rules_version,
            }
        )
        attempt_id, interrupted_ids, terminal = self.repository.begin_scan(
            scan_id=scan_id,
            request_id=request.request_id,
            request_hash=request_hash,
            input_release_id=release.input_release_id,
            rules_version=request.rules_version,
            as_of=request.as_of,
            formal_historical=request.formal_historical,
            live=request.live,
        )
        if terminal:
            return self._load_report(scan_id)
        try:
            current = self.repository.get_scan(scan_id)
            if (
                current is not None
                and current["checkpoint_step"]
                == CandidateCheckpointStep.REGISTRY_COMMITTED.value
            ):
                needs_info = self._validate_inputs(request, release)
                signal_manifest, _signals, records, snapshot = self._load_committed_state(
                    scan_id,
                    request,
                )
                recovered_report = CandidateScanReport(
                    created_at=request.as_of,
                    scan_id=scan_id,
                    request_id=str(current["request_id"]),
                    request_hash=request_hash,
                    input_release_id=release.input_release_id,
                    as_of=request.as_of,
                    status=(
                        CandidateScanStatus.NEEDS_INFO
                        if needs_info
                        else CandidateScanStatus.SUCCEEDED
                    ),
                    checkpoint_step=CandidateCheckpointStep.COMPLETE,
                    signal_manifest_id=signal_manifest.signal_manifest_id,
                    candidate_version_ids=[
                        item.candidate_version_id for item in records
                    ],
                    universe_snapshot_id=snapshot.snapshot_id,
                    needs_info_codes=needs_info,
                    interrupted_attempt_ids=interrupted_ids,
                )
                report_object = self.objects.put_json(
                    recovered_report.model_dump(mode="json")
                )
                self.repository.publish_report(
                    recovered_report,
                    report_object.sha256,
                    attempt_id,
                )
                return recovered_report
            self._interrupt_if_requested(
                interrupt_after, CandidateCheckpointStep.INPUT_REGISTERED
            )
            needs_info = self._validate_inputs(request, release)
            self.repository.set_checkpoint(scan_id, CandidateCheckpointStep.INPUTS_VALIDATED)
            self._interrupt_if_requested(
                interrupt_after, CandidateCheckpointStep.INPUTS_VALIDATED
            )

            signals = self._build_signals(scan_id, request, release)
            signal_object = self.objects.put_json(
                [item.model_dump(mode="json") for item in signals]
            )
            signal_descriptor = self.parquet.write_signals(scan_id, request.as_of, signals)
            signal_manifest_id = content_hash(
                {
                    "scan_id": scan_id,
                    "signal_object_hash": signal_object.sha256,
                    "descriptor": signal_descriptor,
                    "rules_version": request.rules_version,
                }
            )
            signal_manifest = CandidateSignalManifest(
                created_at=request.as_of,
                signal_manifest_id=signal_manifest_id,
                scan_id=scan_id,
                input_release_id=release.input_release_id,
                signal_object_hash=signal_object.sha256,
                descriptor=signal_descriptor,
                signal_ids=[item.signal_id for item in signals],
            )
            signal_manifest_object = self.objects.put_json(
                signal_manifest.model_dump(mode="json")
            )
            self.repository.publish_signal_manifest(
                signal_manifest, signal_manifest_object.sha256
            )
            self._interrupt_if_requested(
                interrupt_after, CandidateCheckpointStep.SIGNALS_WRITTEN
            )

            complete_release = not needs_info
            records = self._build_records(scan_id, request, release, signals, complete_release)
            record_hashes: dict[str, str] = {}
            for record in records:
                record_hashes[record.candidate_version_id] = self.objects.put_json(
                    record.model_dump(mode="json")
                ).sha256
            ready_records = [
                item
                for item in records
                if item.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY
                and item.evaluation_status is CandidateEvaluationStatus.EVALUATED
            ]
            member_descriptor = self.parquet.write_members(
                scan_id, request.as_of, ready_records
            )
            self.repository.set_checkpoint(scan_id, CandidateCheckpointStep.CANDIDATES_WRITTEN)
            self._interrupt_if_requested(
                interrupt_after, CandidateCheckpointStep.CANDIDATES_WRITTEN
            )
            members = [
                CandidateUniverseMember(
                    created_at=request.as_of,
                    candidate_id=item.candidate_id,
                    candidate_version_id=item.candidate_version_id,
                    company_id=item.company_id,
                    instrument_id=item.instrument_id,
                    evidence_ids=item.evidence_ids,
                )
                for item in ready_records
            ]
            semantic_hash = content_hash(
                [item.model_dump(mode="json", exclude={"created_at"}) for item in members]
            )
            snapshot_id = content_hash(
                {
                    "scan_id": scan_id,
                    "input_release_id": release.input_release_id,
                    "rules_version": request.rules_version,
                    "as_of": request.as_of,
                    "semantic_hash": semantic_hash,
                }
            )
            snapshot = CandidateUniverseSnapshot(
                created_at=request.as_of,
                snapshot_id=snapshot_id,
                scan_id=scan_id,
                input_release_id=release.input_release_id,
                as_of=request.as_of,
                members=members,
                semantic_hash=semantic_hash,
            )
            snapshot_object = self.objects.put_json(snapshot.model_dump(mode="json"))
            self.repository.publish_candidates(
                records,
                record_hashes,
                snapshot,
                snapshot_object.sha256,
                canonical_json_bytes(member_descriptor).decode(),
            )
            self._interrupt_if_requested(
                interrupt_after, CandidateCheckpointStep.REGISTRY_COMMITTED
            )
            status = (
                CandidateScanStatus.NEEDS_INFO
                if needs_info
                else CandidateScanStatus.SUCCEEDED
            )
            report = CandidateScanReport(
                created_at=request.as_of,
                scan_id=scan_id,
                request_id=request.request_id,
                request_hash=request_hash,
                input_release_id=release.input_release_id,
                as_of=request.as_of,
                status=status,
                checkpoint_step=CandidateCheckpointStep.COMPLETE,
                signal_manifest_id=signal_manifest_id,
                candidate_version_ids=[item.candidate_version_id for item in records],
                universe_snapshot_id=snapshot_id,
                needs_info_codes=needs_info,
                interrupted_attempt_ids=interrupted_ids,
            )
            report_object = self.objects.put_json(report.model_dump(mode="json"))
            self.repository.publish_report(report, report_object.sha256, attempt_id)
            return report
        except CandidateInterrupted:
            raise
        except Exception as exc:
            self.repository.fail_attempt(attempt_id, type(exc).__name__)
            raise

    def status(
        self,
        *,
        scan_id: str | None = None,
        company_id: str | None = None,
    ) -> dict[str, Any]:
        if (scan_id is None) == (company_id is None):
            raise ValueError("Specify exactly one of scan_id or company_id")
        if scan_id is not None:
            row = self.repository.get_scan(scan_id)
            if row is None:
                return {"status": "NOT_FOUND", "scan_id": scan_id}
            result: dict[str, Any] = {"status": "FOUND", "scan": row}
            if row.get("report_object_hash"):
                result["report"] = CandidateScanReport.model_validate_json(
                    self.objects.get_bytes(str(row["report_object_hash"]))
                )
            result["records"] = [
                CandidateRecord.model_validate_json(
                    self.objects.get_bytes(str(item["record_object_hash"]))
                )
                for item in self.repository.list_scan_records(scan_id)
            ]
            return result
        row = self.repository.status_by_company(str(company_id))
        if row is None:
            return {"status": "NOT_FOUND", "company_id": company_id}
        return {
            "status": "FOUND",
            "record": CandidateRecord.model_validate_json(
                self.objects.get_bytes(str(row["record_object_hash"]))
            ),
        }

    def audit(self, scan_id: str) -> CandidateAuditReport:
        failure_codes: list[str] = []
        checked_objects: list[str] = []
        checked_paths: list[str] = []
        scan = self.repository.get_scan(scan_id)
        input_row: dict[str, Any] | None = None
        release: CandidateInputRelease | None = None
        scan_report: CandidateScanReport | None = None
        records: list[CandidateRecord] = []
        snapshot: CandidateUniverseSnapshot | None = None
        if scan is None:
            failure_codes.append("SCAN_NOT_FOUND")
        else:
            input_row = self.repository.get_input_release(str(scan["input_release_id"]))
            if input_row is None:
                failure_codes.append("INPUT_RELEASE_METADATA_MISSING")
                release = None
            else:
                if not self.repository.artifact_matches(
                    str(input_row["manifest_artifact_id"]),
                    str(input_row["manifest_object_hash"]),
                ):
                    failure_codes.append("INPUT_RELEASE_REGISTRY_POINTER_INVALID")
                release = self._audit_object(
                    str(input_row["manifest_object_hash"]),
                    CandidateInputRelease,
                    checked_objects,
                    failure_codes,
                    "INPUT_RELEASE_OBJECT_INVALID",
                )
                if release is not None:
                    for artifact in release.artifacts:
                        if not self.objects.verify(artifact.object_hash):
                            failure_codes.append("UPSTREAM_OBJECT_INVALID")
                        if artifact.artifact_type == "SourceSnapshot":
                            upstream_snapshot = self.repository.state.get_snapshot(
                                artifact.artifact_id
                            )
                            if (
                                upstream_snapshot is None
                                or upstream_snapshot.object_sha256 != artifact.object_hash
                            ):
                                failure_codes.append("UPSTREAM_SNAPSHOT_POINTER_INVALID")
                        elif not self.repository.artifact_matches(
                            artifact.artifact_id, artifact.object_hash
                        ):
                            failure_codes.append("UPSTREAM_REGISTRY_POINTER_INVALID")
                        checked_objects.append(artifact.object_hash)
            if not scan.get("report_object_hash"):
                failure_codes.append("SCAN_REPORT_MISSING")
            else:
                if not self.repository.artifact_matches(
                    str(scan["report_artifact_id"]),
                    str(scan["report_object_hash"]),
                ):
                    failure_codes.append("SCAN_REPORT_REGISTRY_POINTER_INVALID")
                scan_report = self._audit_object(
                    str(scan["report_object_hash"]),
                    CandidateScanReport,
                    checked_objects,
                    failure_codes,
                    "SCAN_REPORT_OBJECT_INVALID",
                )
                if scan_report is not None and scan_report.scan_id != scan_id:
                    failure_codes.append("SCAN_REPORT_BINDING_INVALID")
            manifest_row = self.repository.get_signal_manifest(scan_id)
            if manifest_row is None:
                failure_codes.append("SIGNAL_MANIFEST_MISSING")
            else:
                if not self.repository.artifact_matches(
                    str(manifest_row["manifest_artifact_id"]),
                    str(manifest_row["manifest_object_hash"]),
                ):
                    failure_codes.append("SIGNAL_REGISTRY_POINTER_INVALID")
                manifest = self._audit_object(
                    str(manifest_row["manifest_object_hash"]),
                    CandidateSignalManifest,
                    checked_objects,
                    failure_codes,
                    "SIGNAL_MANIFEST_OBJECT_INVALID",
                )
                if manifest is not None:
                    checked_objects.append(manifest.signal_object_hash)
                    if not self.objects.verify(manifest.signal_object_hash):
                        failure_codes.append("SIGNAL_OBJECT_INVALID")
                    checked_paths.append(manifest.descriptor.path)
                    if not self.parquet.verify(
                        manifest.descriptor,
                        record_kind="candidate_signal",
                        scan_id=scan_id,
                        as_of=manifest.created_at,
                    ):
                        failure_codes.append("SIGNAL_PARQUET_INVALID")
            for row in self.repository.list_scan_records(scan_id):
                if not self.repository.artifact_matches(
                    str(row["record_artifact_id"]),
                    str(row["record_object_hash"]),
                ):
                    failure_codes.append("CANDIDATE_REGISTRY_POINTER_INVALID")
                record = self._audit_object(
                    str(row["record_object_hash"]),
                    CandidateRecord,
                    checked_objects,
                    failure_codes,
                    "CANDIDATE_RECORD_OBJECT_INVALID",
                )
                if record is not None:
                    records.append(record)
                    if (
                        row["previous_version_id"] != record.previous_version_id
                        or str(row["candidate_id"]) != record.candidate_id
                        or str(row["scan_id"]) != record.scan_id
                        or str(row["lifecycle_status"])
                        != record.lifecycle_status.value
                        or str(row["strength"]) != record.strength.value
                    ):
                        failure_codes.append("CANDIDATE_METADATA_BINDING_INVALID")
                    if self._contains_forbidden_output_key(record.model_dump(mode="json")):
                        failure_codes.append("TRADING_FIELD_PRESENT")
                    stored_previous_id = (
                        str(row["previous_version_id"])
                        if row["previous_version_id"] is not None
                        else None
                    )
                    if stored_previous_id is not None:
                        previous = self.repository.get_candidate_version(
                            stored_previous_id
                        )
                        if previous is None:
                            failure_codes.append("PREVIOUS_VERSION_MISSING")
                        else:
                            previous_as_of = datetime.fromisoformat(
                                str(previous["scan_as_of"])
                            )
                            if (
                                str(previous["candidate_id"]) != record.candidate_id
                                or previous_as_of >= record.as_of
                            ):
                                failure_codes.append("PREVIOUS_VERSION_ASOF_INVALID")
            universe_row = self.repository.get_universe(scan_id)
            if universe_row is None:
                failure_codes.append("UNIVERSE_SNAPSHOT_MISSING")
            else:
                if not self.repository.artifact_matches(
                    str(universe_row["snapshot_artifact_id"]),
                    str(universe_row["snapshot_object_hash"]),
                ):
                    failure_codes.append("UNIVERSE_REGISTRY_POINTER_INVALID")
                snapshot = self._audit_object(
                    str(universe_row["snapshot_object_hash"]),
                    CandidateUniverseSnapshot,
                    checked_objects,
                    failure_codes,
                    "UNIVERSE_OBJECT_INVALID",
                )
                try:
                    descriptor = json.loads(str(universe_row["member_descriptor_json"]))
                    parsed_descriptor = CandidateFileDescriptor.model_validate(descriptor)
                    checked_paths.append(parsed_descriptor.path)
                    if snapshot is None or not self.parquet.verify(
                        parsed_descriptor,
                        record_kind="candidate_member",
                        scan_id=scan_id,
                        as_of=snapshot.as_of,
                    ):
                        failure_codes.append("UNIVERSE_PARQUET_INVALID")
                except (TypeError, ValueError, json.JSONDecodeError):
                    failure_codes.append("UNIVERSE_DESCRIPTOR_INVALID")
            if snapshot is not None:
                by_version = {item.candidate_version_id: item for item in records}
                release_evidence_by_company = (
                    {
                        company.company_id: {
                            evidence_id
                            for item in [
                                *company.announcement_events,
                                *company.financial_flags,
                                *company.holding_observations,
                            ]
                            for evidence_id in item.evidence_ids
                        }
                        for company in release.companies
                    }
                    if release is not None
                    else {}
                )
                for member in snapshot.members:
                    record = by_version.get(member.candidate_version_id)
                    if (
                        record is None
                        or record.company_id != member.company_id
                        or record.instrument_id != member.instrument_id
                        or record.lifecycle_status
                        is not CandidateLifecycleStatus.RESEARCH_READY
                        or record.evaluation_status is not CandidateEvaluationStatus.EVALUATED
                        or record.evidence_ids != member.evidence_ids
                    ):
                        failure_codes.append("UNIVERSE_MEMBER_BINDING_INVALID")
                    company_evidence = release_evidence_by_company.get(member.company_id, set())
                    if release is not None and not set(member.evidence_ids).issubset(
                        company_evidence
                    ):
                        failure_codes.append("UNIVERSE_EVIDENCE_BINDING_INVALID")
                actual_hash = content_hash(
                    [
                        item.model_dump(mode="json", exclude={"created_at"})
                        for item in snapshot.members
                    ]
                )
                if actual_hash != snapshot.semantic_hash:
                    failure_codes.append("UNIVERSE_SEMANTIC_HASH_INVALID")
            if input_row is not None and release is not None and scan_report is not None:
                try:
                    replay_request = CandidateScanRequest.model_validate(
                        {
                            "request_id": scan["request_id"],
                            "input_release_id": release.input_release_id,
                            "input_release_object_hash": input_row["manifest_object_hash"],
                            "as_of": scan["as_of"],
                            "rules_version": scan["rules_version"],
                            "formal_historical": bool(scan["formal_historical"]),
                            "live": bool(scan["live"]),
                        }
                    )
                    self._validate_request_binding(replay_request, release)
                    semantic_issues = self._validate_inputs(replay_request, release)
                    (
                        committed_manifest,
                        committed_signals,
                        committed_records,
                        committed_snapshot,
                    ) = self._load_committed_state(scan_id, replay_request)
                    rebuilt_signals = self._build_signals(
                        scan_id,
                        replay_request,
                        release,
                    )
                    committed_signal_payload = [
                        item.model_dump(mode="json", exclude={"created_at"})
                        for item in committed_signals
                    ]
                    rebuilt_signal_payload = [
                        item.model_dump(mode="json", exclude={"created_at"})
                        for item in rebuilt_signals
                    ]
                    expected_status = (
                        CandidateScanStatus.NEEDS_INFO
                        if semantic_issues
                        else CandidateScanStatus.SUCCEEDED
                    )
                    if committed_signal_payload != rebuilt_signal_payload:
                        failure_codes.append("SIGNAL_SEMANTIC_REPLAY_MISMATCH")
                    if (
                        scan_report.request_id != str(scan["request_id"])
                        or scan_report.request_hash != str(scan["request_hash"])
                        or scan_report.input_release_id != release.input_release_id
                        or scan_report.rules_version != replay_request.rules_version
                        or scan_report.as_of != replay_request.as_of
                        or scan_report.status is not expected_status
                        or scan_report.checkpoint_step is not CandidateCheckpointStep.COMPLETE
                        or scan_report.signal_manifest_id
                        != committed_manifest.signal_manifest_id
                        or scan_report.candidate_version_ids
                        != [item.candidate_version_id for item in committed_records]
                        or scan_report.universe_snapshot_id != committed_snapshot.snapshot_id
                        or scan_report.needs_info_codes != semantic_issues
                        or str(scan["status"]) != scan_report.status.value
                    ):
                        failure_codes.append("SCAN_REPORT_SEMANTIC_BINDING_INVALID")
                    if semantic_issues and (
                        committed_records or committed_snapshot.members
                    ):
                        failure_codes.append("INCOMPLETE_INPUT_MUTATED_LIFECYCLE")
                except Exception:
                    failure_codes.append("COMMITTED_STATE_REPLAY_INVALID")
        failure_codes = sorted(set(failure_codes))
        now = datetime.now(UTC)
        audit_id = sha256_bytes(f"{scan_id}:{now.isoformat()}".encode())
        report = CandidateAuditReport(
            created_at=now,
            audit_id=audit_id,
            scan_id=scan_id,
            status=(
                CandidateAuditStatus.FAIL
                if failure_codes
                else CandidateAuditStatus.PASS
            ),
            checked_object_hashes=sorted(set(checked_objects)),
            checked_parquet_paths=sorted(set(checked_paths)),
            failure_codes=failure_codes,
            candidate_count=len(records),
            universe_member_count=len(snapshot.members) if snapshot is not None else 0,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        if scan is not None:
            self.repository.publish_audit(report, ref.sha256)
        return report

    def _validate_request_binding(
        self,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
    ) -> None:
        if request.input_release_id != release.input_release_id:
            raise ValueError("Candidate request input release id mismatch")
        if request.as_of != release.as_of:
            raise ValueError("Candidate request as_of must equal the frozen release as_of")
        if release.source_mode is CandidateSourceMode.LIVE and not request.live:
            raise ValueError("LIVE candidate releases require an explicit live request")
        if request.live and release.source_mode is not CandidateSourceMode.LIVE:
            raise ValueError("live requests must reference a LIVE input release")

    def _validate_inputs(
        self,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
    ) -> list[str]:
        issues = self._release_verification_issues(release)
        required_roles = {
            CandidateArtifactRole.INSTRUMENT_TRADABILITY,
            CandidateArtifactRole.TRADING_CALENDAR,
            CandidateArtifactRole.DAILY_LOCAL_VERSIONED,
            CandidateArtifactRole.CORPORATE_ACTION,
            CandidateArtifactRole.DATA_QUALITY,
            CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
            CandidateArtifactRole.FINANCIAL_INTEGRITY,
        }
        for artifact in release.artifacts:
            if not self.objects.verify(artifact.object_hash):
                issues.append(f"OBJECT_INVALID:{artifact.artifact_id}")
            if artifact.role in required_roles and (
                artifact.coverage_status is not CandidateCoverageStatus.COMPLETE
            ):
                issues.append(f"COVERAGE_{artifact.coverage_status.value}:{artifact.artifact_id}")
            if artifact.available_to_system_at > request.as_of:
                issues.append(f"FUTURE_INPUT:{artifact.artifact_id}")
            if (
                request.formal_historical
                and artifact.pit_status not in self.config.formal_historical_pit_statuses
            ):
                issues.append(f"NOT_PIT_SAFE:{artifact.artifact_id}")
        for company in release.companies:
            nested = [
                *company.daily_points,
                *company.announcement_events,
                *company.financial_flags,
                *company.watchlist_intents,
                *company.holding_observations,
            ]
            for item in nested:
                if item.available_to_system_at > request.as_of:
                    issues.append(
                        f"FUTURE_INPUT:{item.source_artifact_id}:{type(item).__name__}"
                    )
                if (
                    request.formal_historical
                    and item.pit_status not in self.config.formal_historical_pit_statuses
                ):
                    issues.append(
                        f"NOT_PIT_SAFE:{item.source_artifact_id}:{type(item).__name__}"
                    )
        return sorted(set(issues))

    def _release_verification_issues(self, release: CandidateInputRelease) -> list[str]:
        result = self.input_verifier.verify(release)
        issues = list(result.issue_codes)
        expected = set(release.expected_company_ids)
        actual = {item.company_id for item in release.companies}
        if release.expected_company_count != len(expected):
            issues.append("EXPECTED_COMPANY_COUNT_MISMATCH")
        if release.company_universe_semantic_hash != content_hash(sorted(expected)):
            issues.append("EXPECTED_COMPANY_SEMANTIC_HASH_MISMATCH")
        if actual != expected:
            issues.append("RELEASE_COMPANY_SET_MISMATCH")
        if result.proven_company_ids is None:
            issues.append("COMPANY_COVERAGE_UNPROVEN")
        elif set(result.proven_company_ids) != expected:
            issues.append("COMPANY_COVERAGE_PROOF_MISMATCH")
        return sorted(set(issues))

    def _verify_upstream_objects(self, release: CandidateInputRelease) -> None:
        invalid = [
            item.artifact_id
            for item in release.artifacts
            if not self.objects.verify(item.object_hash)
        ]
        if invalid:
            raise ValueError(f"Candidate upstream objects are missing or corrupt: {invalid}")

    def _build_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
    ) -> list[CandidateSignal]:
        artifacts = {item.artifact_id: item for item in release.artifacts}
        signals: list[CandidateSignal] = []
        for company in release.companies:
            signals.extend(self._gate_signals(scan_id, request, company, artifacts))
            signals.extend(self._event_signals(scan_id, request, company, artifacts))
            signals.extend(self._financial_signals(scan_id, request, company, artifacts))
            signals.extend(self._watchlist_signals(scan_id, request, company, artifacts))
            signals.extend(self._holding_signals(scan_id, request, company, artifacts))
            price_signal = self._price_volume_signal(scan_id, request, company, artifacts)
            if price_signal is not None:
                signals.append(price_signal)
        unique: dict[tuple[CandidateSignalType, str, str], CandidateSignal] = {}
        for signal in signals:
            key = (signal.signal_type, signal.source_artifact_id, signal.source_unit_id)
            unique.setdefault(key, signal)
        return sorted(unique.values(), key=lambda item: item.signal_id)

    def _gate_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateSignal]:
        quality_disposition = {
            CandidateQualityStatus.PASS: CandidateSignalDisposition.GATE_PASS,
            CandidateQualityStatus.PARTIAL: CandidateSignalDisposition.GATE_DEGRADED,
            CandidateQualityStatus.FAIL: CandidateSignalDisposition.GATE_FAIL,
        }[company.quality_status]
        quality_reason = f"QUALITY_{company.quality_status.value}"
        safe_daily = self._safe_daily_points(request, company.daily_points, artifacts)
        liquidity_pass, liquidity_reasons = self._liquidity_gate(company, safe_daily)
        tradability_pass = company.tradability is CandidateTradability.TRADABLE
        return [
            self._make_signal(
                scan_id,
                request,
                company,
                CandidateSignalType.QUALITY_GATE,
                artifacts[company.quality_artifact_id],
                f"quality:{company.company_id}",
                artifacts[company.quality_artifact_id].available_to_system_at,
                artifacts[company.quality_artifact_id].available_to_system_at,
                quality_disposition,
                [quality_reason],
                artifacts[company.quality_artifact_id].evidence_ids,
            ),
            self._make_signal(
                scan_id,
                request,
                company,
                CandidateSignalType.LIQUIDITY_GATE,
                artifacts[company.daily_artifact_id],
                f"liquidity:{company.company_id}",
                safe_daily[-1].observed_at if safe_daily else request.as_of,
                artifacts[company.daily_artifact_id].available_to_system_at,
                (
                    CandidateSignalDisposition.GATE_PASS
                    if liquidity_pass
                    else CandidateSignalDisposition.GATE_FAIL
                ),
                liquidity_reasons,
                artifacts[company.daily_artifact_id].evidence_ids,
            ),
            self._make_signal(
                scan_id,
                request,
                company,
                CandidateSignalType.TRADABILITY_GATE,
                artifacts[company.instrument_artifact_id],
                f"tradability:{company.instrument_id}",
                artifacts[company.instrument_artifact_id].available_to_system_at,
                artifacts[company.instrument_artifact_id].available_to_system_at,
                (
                    CandidateSignalDisposition.GATE_PASS
                    if tradability_pass
                    else CandidateSignalDisposition.GATE_FAIL
                ),
                [f"TRADABILITY_{company.tradability.value}"],
                artifacts[company.instrument_artifact_id].evidence_ids,
            ),
        ]

    def _event_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateSignal]:
        result = []
        for event in company.announcement_events:
            if event.event_type not in self.config.canonical_announcement_events:
                continue
            result.append(
                self._evidence_signal(
                    scan_id,
                    request,
                    company,
                    artifacts[event.source_artifact_id],
                    CandidateSignalType.ANNOUNCEMENT_EVENT,
                    event.event_id,
                    event.observed_at,
                    event.available_to_system_at,
                    event.pit_status,
                    event.severity,
                    event.evidence_ids,
                    [f"CANONICAL_EVENT:{event.event_type}"],
                )
            )
        return result

    def _financial_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateSignal]:
        result = []
        for finding in company.financial_flags:
            if not finding.evidence_closed:
                continue
            result.append(
                self._evidence_signal(
                    scan_id,
                    request,
                    company,
                    artifacts[finding.source_artifact_id],
                    CandidateSignalType.FINANCIAL_ANOMALY,
                    finding.finding_id,
                    finding.observed_at,
                    finding.available_to_system_at,
                    finding.pit_status,
                    finding.severity,
                    finding.evidence_ids,
                    ["CLOSED_FINANCIAL_EVIDENCE"],
                )
            )
        return result

    def _watchlist_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateSignal]:
        return [
            self._weak_input_signal(
                scan_id,
                request,
                company,
                artifacts[item.source_artifact_id],
                CandidateSignalType.USER_WATCHLIST,
                item.intent_id,
                item,
                ["USER_INTENT_ONLY"],
            )
            for item in company.watchlist_intents
        ]

    def _holding_signals(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateSignal]:
        result = []
        for item in company.holding_observations:
            if item.change is CandidateHoldingChange.UNCHANGED:
                continue
            result.append(
                self._evidence_signal(
                    scan_id,
                    request,
                    company,
                    artifacts[item.source_artifact_id],
                    CandidateSignalType.HOLDING_REVIEW,
                    item.review_id,
                    item.observed_at,
                    item.available_to_system_at,
                    item.pit_status,
                    CandidateEvidenceSeverity.MEDIUM,
                    item.evidence_ids,
                    [f"HOLDING_{item.change.value}"],
                )
            )
        return result

    def _price_volume_signal(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifacts: dict[str, CandidateInputArtifact],
    ) -> CandidateSignal | None:
        if company.quality_status is CandidateQualityStatus.FAIL:
            return None
        points = self._safe_daily_points(request, company.daily_points, artifacts)
        if len(points) < self.config.minimum_trading_days + 1:
            return None
        window = points[-(self.config.minimum_trading_days + 1) :]
        absolute_change = abs(window[-1].close / window[0].close - Decimal(1))
        prior_volume_median = median(item.volume for item in window[:-1])
        if prior_volume_median <= 0:
            return None
        volume_ratio = window[-1].volume / prior_volume_median
        if (
            absolute_change < self.config.minimum_absolute_price_change
            or volume_ratio < self.config.minimum_volume_ratio
        ):
            return None
        artifact = artifacts[company.daily_artifact_id]
        reasons = [
            f"ABS_20D_CHANGE_GE_{self.config.minimum_absolute_price_change}",
            f"CURRENT_VOLUME_RATIO_GE_{self.config.minimum_volume_ratio}",
        ]
        if company.quality_status is CandidateQualityStatus.PARTIAL:
            reasons.append("QUALITY_PARTIAL_DEGRADED")
        return self._make_signal(
            scan_id,
            request,
            company,
            CandidateSignalType.PRICE_VOLUME_CLUE,
            artifact,
            f"price-volume:{window[-1].session_date.isoformat()}",
            window[-1].observed_at,
            window[-1].available_to_system_at,
            CandidateSignalDisposition.WEAK_CLUE,
            reasons,
            artifact.evidence_ids,
        )

    def _weak_input_signal(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifact: CandidateInputArtifact,
        signal_type: CandidateSignalType,
        unit_id: str,
        item: CandidateWatchlistIntent,
        reasons: list[str],
    ) -> CandidateSignal:
        disposition = self._evidence_disposition(
            request, item.available_to_system_at, item.pit_status
        )
        if disposition is CandidateSignalDisposition.SUPPORT:
            disposition = CandidateSignalDisposition.WEAK_CLUE
        return self._make_signal(
            scan_id,
            request,
            company,
            signal_type,
            artifact,
            unit_id,
            item.observed_at,
            item.available_to_system_at,
            disposition,
            reasons,
            item.evidence_ids,
            pit_status=item.pit_status,
        )

    def _evidence_signal(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        artifact: CandidateInputArtifact,
        signal_type: CandidateSignalType,
        unit_id: str,
        observed_at: datetime,
        available_at: datetime,
        pit_status: CandidatePitStatus,
        severity: CandidateEvidenceSeverity,
        evidence_ids: list[str],
        reasons: list[str],
    ) -> CandidateSignal:
        return self._make_signal(
            scan_id,
            request,
            company,
            signal_type,
            artifact,
            unit_id,
            observed_at,
            available_at,
            self._evidence_disposition(request, available_at, pit_status),
            reasons,
            evidence_ids,
            pit_status=pit_status,
            severity=severity,
        )

    def _make_signal(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        company: CandidateCompanyInput,
        signal_type: CandidateSignalType,
        artifact: CandidateInputArtifact,
        unit_id: str,
        observed_at: datetime,
        available_at: datetime,
        disposition: CandidateSignalDisposition,
        reasons: list[str],
        evidence_ids: list[str],
        *,
        pit_status: CandidatePitStatus | None = None,
        severity: CandidateEvidenceSeverity | None = None,
    ) -> CandidateSignal:
        resolved_pit = pit_status or artifact.pit_status
        if available_at > request.as_of:
            disposition = CandidateSignalDisposition.EXCLUDED_FUTURE
        elif (
            request.formal_historical
            and resolved_pit not in self.config.formal_historical_pit_statuses
        ):
            disposition = CandidateSignalDisposition.EXCLUDED_NOT_PIT_SAFE
        identity = {
            "scan_id": scan_id,
            "company_id": company.company_id,
            "signal_type": signal_type,
            "source_artifact_id": artifact.artifact_id,
            "source_unit_id": unit_id,
            "rule_version": self.config.rules_version,
            "disposition": disposition,
        }
        return CandidateSignal(
            created_at=request.as_of,
            signal_id=content_hash(identity),
            scan_id=scan_id,
            company_id=company.company_id,
            instrument_id=company.instrument_id,
            signal_type=signal_type,
            rule_version=self.config.rules_version,
            source_artifact_id=artifact.artifact_id,
            source_unit_id=unit_id,
            source_snapshot_ids=artifact.source_snapshot_ids,
            source_family=artifact.source_family,
            observed_at=observed_at,
            available_to_system_at=available_at,
            pit_status=resolved_pit,
            evidence_ids=sorted(set(evidence_ids)),
            disposition=disposition,
            severity=severity,
            reason_codes=reasons,
        )

    def _evidence_disposition(
        self,
        request: CandidateScanRequest,
        available_at: datetime,
        pit_status: CandidatePitStatus,
    ) -> CandidateSignalDisposition:
        if available_at > request.as_of:
            return CandidateSignalDisposition.EXCLUDED_FUTURE
        if (
            request.formal_historical
            and pit_status not in self.config.formal_historical_pit_statuses
        ):
            return CandidateSignalDisposition.EXCLUDED_NOT_PIT_SAFE
        return CandidateSignalDisposition.SUPPORT

    def _safe_daily_points(
        self,
        request: CandidateScanRequest,
        points: list[CandidateDailyPoint],
        artifacts: dict[str, CandidateInputArtifact],
    ) -> list[CandidateDailyPoint]:
        safe = [
            item
            for item in points
            if item.available_to_system_at <= request.as_of
            and artifacts[item.source_artifact_id].available_to_system_at <= request.as_of
            and (
                not request.formal_historical
                or (
                    item.pit_status in self.config.formal_historical_pit_statuses
                    and artifacts[item.source_artifact_id].pit_status
                    in self.config.formal_historical_pit_statuses
                )
            )
        ]
        by_date = {item.session_date: item for item in safe}
        return sorted(by_date.values(), key=lambda item: item.session_date)

    def _liquidity_gate(
        self,
        company: CandidateCompanyInput,
        points: list[CandidateDailyPoint],
    ) -> tuple[bool, list[str]]:
        if company.quality_status is CandidateQualityStatus.FAIL:
            return False, ["TECHNICAL_DISABLED_QUALITY_FAIL"]
        if len(points) < self.config.minimum_trading_days:
            return False, ["INSUFFICIENT_VALID_TRADING_DAYS"]
        window = points[-self.config.minimum_trading_days :]
        median_turnover = median(item.turnover_cny for item in window)
        nonzero_ratio = Decimal(sum(item.turnover_cny > 0 for item in window)) / Decimal(
            len(window)
        )
        reasons = [
            f"MEDIAN_TURNOVER:{median_turnover}",
            f"NONZERO_RATIO:{nonzero_ratio}",
        ]
        return (
            median_turnover >= self.config.minimum_median_turnover_cny
            and nonzero_ratio >= self.config.minimum_nonzero_turnover_ratio,
            reasons,
        )

    def _build_records(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
        signals: list[CandidateSignal],
        complete_release: bool,
    ) -> list[CandidateRecord]:
        if not complete_release:
            # An incomplete release may expose diagnostic signals, but it must not
            # mutate candidate lifecycle state or increment a miss.
            return []
        latest_rows = self.repository.latest_records(
            before_as_of=request.as_of,
            exclude_scan_id=scan_id,
        )
        previous_by_instrument = {
            str(row["instrument_id"]): CandidateRecord.model_validate_json(
                self.objects.get_bytes(str(row["record_object_hash"]))
            )
            for row in latest_rows
        }
        signals_by_company: dict[str, list[CandidateSignal]] = {}
        for signal in signals:
            signals_by_company.setdefault(signal.company_id, []).append(signal)
        records: list[CandidateRecord] = []
        seen_instruments: set[str] = set()
        for company in release.companies:
            company_signals = signals_by_company.get(company.company_id, [])
            strength = self._strength(company_signals)
            if strength is CandidateStrength.NONE:
                continue
            seen_instruments.add(company.instrument_id)
            previous = previous_by_instrument.get(company.instrument_id)
            liquidity_pass = any(
                item.signal_type is CandidateSignalType.LIQUIDITY_GATE
                and item.disposition is CandidateSignalDisposition.GATE_PASS
                for item in company_signals
            )
            evidence_ids = sorted(
                {
                    evidence_id
                    for item in company_signals
                    if item.disposition is CandidateSignalDisposition.SUPPORT
                    for evidence_id in item.evidence_ids
                }
            )
            eligible = (
                complete_release
                and strength in {CandidateStrength.MODERATE, CandidateStrength.STRONG}
                and bool(evidence_ids)
                and company.quality_status is not CandidateQualityStatus.FAIL
                and liquidity_pass
                and company.tradability is CandidateTradability.TRADABLE
            )
            lifecycle_status = (
                CandidateLifecycleStatus.RESEARCH_READY
                if eligible
                else CandidateLifecycleStatus.OBSERVATION
            )
            reactivation_count = previous.reactivation_count if previous else 0
            if previous is not None and previous.lifecycle_status in {
                CandidateLifecycleStatus.REVIEW_DUE,
                CandidateLifecycleStatus.CLOSED,
            }:
                reactivation_count += 1
            record = self._new_record(
                scan_id=scan_id,
                request=request,
                release=release,
                company=company,
                previous=previous,
                lifecycle_status=lifecycle_status,
                evaluation_status=(
                    CandidateEvaluationStatus.EVALUATED
                    if complete_release
                    else CandidateEvaluationStatus.NEEDS_INFO
                ),
                strength=strength,
                signals=company_signals,
                evidence_ids=evidence_ids,
                liquidity_pass=liquidity_pass,
                miss_count=0,
                reactivation_count=reactivation_count,
                reasons=[
                    "RESEARCH_EVIDENCE_AND_GATES_PASSED"
                    if eligible
                    else "OBSERVATION_ONLY"
                ],
            )
            records.append(record)
        if complete_release:
            for instrument_id, previous in previous_by_instrument.items():
                if (
                    instrument_id in seen_instruments
                    or previous.lifecycle_status is CandidateLifecycleStatus.CLOSED
                ):
                    continue
                miss_count = previous.miss_count + 1
                lifecycle_status = (
                    CandidateLifecycleStatus.REVIEW_DUE
                    if miss_count == 1
                    else CandidateLifecycleStatus.CLOSED
                )
                records.append(
                    self._new_record_from_previous(
                        scan_id,
                        request,
                        release,
                        previous,
                        lifecycle_status,
                        miss_count,
                    )
                )
        return sorted(records, key=lambda item: item.company_id)

    def _new_record(
        self,
        *,
        scan_id: str,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
        company: CandidateCompanyInput,
        previous: CandidateRecord | None,
        lifecycle_status: CandidateLifecycleStatus,
        evaluation_status: CandidateEvaluationStatus,
        strength: CandidateStrength,
        signals: list[CandidateSignal],
        evidence_ids: list[str],
        liquidity_pass: bool,
        miss_count: int,
        reactivation_count: int,
        reasons: list[str],
    ) -> CandidateRecord:
        candidate_id = sha256_bytes(company.instrument_id.encode())
        identity = {
            "candidate_id": candidate_id,
            "scan_id": scan_id,
            "previous_version_id": previous.candidate_version_id if previous else None,
            "lifecycle_status": lifecycle_status,
            "strength": strength,
            "signal_ids": sorted(item.signal_id for item in signals),
            "evidence_ids": evidence_ids,
            "miss_count": miss_count,
            "reactivation_count": reactivation_count,
        }
        return CandidateRecord(
            created_at=request.as_of,
            candidate_id=candidate_id,
            candidate_version_id=content_hash(identity),
            previous_version_id=previous.candidate_version_id if previous else None,
            scan_id=scan_id,
            input_release_id=release.input_release_id,
            company_id=company.company_id,
            instrument_id=company.instrument_id,
            as_of=request.as_of,
            lifecycle_status=lifecycle_status,
            evaluation_status=evaluation_status,
            strength=strength,
            signal_ids=sorted(item.signal_id for item in signals),
            evidence_ids=evidence_ids,
            quality_status=company.quality_status,
            tradability=company.tradability,
            liquidity_gate_passed=liquidity_pass,
            miss_count=miss_count,
            reactivation_count=reactivation_count,
            reason_codes=reasons,
        )

    def _new_record_from_previous(
        self,
        scan_id: str,
        request: CandidateScanRequest,
        release: CandidateInputRelease,
        previous: CandidateRecord,
        lifecycle_status: CandidateLifecycleStatus,
        miss_count: int,
    ) -> CandidateRecord:
        identity = {
            "candidate_id": previous.candidate_id,
            "scan_id": scan_id,
            "previous_version_id": previous.candidate_version_id,
            "lifecycle_status": lifecycle_status,
            "strength": CandidateStrength.NONE,
            "miss_count": miss_count,
            "reactivation_count": previous.reactivation_count,
        }
        return CandidateRecord(
            created_at=request.as_of,
            candidate_id=previous.candidate_id,
            candidate_version_id=content_hash(identity),
            previous_version_id=previous.candidate_version_id,
            scan_id=scan_id,
            input_release_id=release.input_release_id,
            company_id=previous.company_id,
            instrument_id=previous.instrument_id,
            as_of=request.as_of,
            lifecycle_status=lifecycle_status,
            evaluation_status=CandidateEvaluationStatus.EVALUATED,
            strength=CandidateStrength.NONE,
            signal_ids=[],
            evidence_ids=[],
            quality_status=previous.quality_status,
            tradability=previous.tradability,
            liquidity_gate_passed=previous.liquidity_gate_passed,
            miss_count=miss_count,
            reactivation_count=previous.reactivation_count,
            reason_codes=["CANDIDATE_SIGNAL_MISS"],
        )

    @staticmethod
    def _strength(signals: list[CandidateSignal]) -> CandidateStrength:
        supporting = [
            item
            for item in signals
            if item.disposition is CandidateSignalDisposition.SUPPORT
            and item.signal_type
            in {
                CandidateSignalType.ANNOUNCEMENT_EVENT,
                CandidateSignalType.FINANCIAL_ANOMALY,
                CandidateSignalType.HOLDING_REVIEW,
            }
        ]
        if any(item.severity is CandidateEvidenceSeverity.HIGH for item in supporting):
            return CandidateStrength.STRONG
        medium_sources = [
            item
            for item in supporting
            if item.severity is CandidateEvidenceSeverity.MEDIUM
        ]
        if any(
            CandidateScanService._independent_medium(left, right)
            for index, left in enumerate(medium_sources)
            for right in medium_sources[index + 1 :]
        ):
            return CandidateStrength.STRONG
        if medium_sources:
            return CandidateStrength.MODERATE
        if any(
            item.disposition is CandidateSignalDisposition.WEAK_CLUE
            and item.signal_type
            in {CandidateSignalType.PRICE_VOLUME_CLUE, CandidateSignalType.USER_WATCHLIST}
            for item in signals
        ):
            return CandidateStrength.WEAK
        return CandidateStrength.NONE

    @staticmethod
    def _independent_medium(left: CandidateSignal, right: CandidateSignal) -> bool:
        left_evidence = set(left.evidence_ids)
        right_evidence = set(right.evidence_ids)
        left_snapshots = set(left.source_snapshot_ids)
        right_snapshots = set(right.source_snapshot_ids)
        return (
            left.source_family != right.source_family
            and bool(left_evidence)
            and bool(right_evidence)
            and left_evidence.isdisjoint(right_evidence)
            and bool(left_snapshots)
            and bool(right_snapshots)
            and left_snapshots.isdisjoint(right_snapshots)
        )

    def _load_report(self, scan_id: str) -> CandidateScanReport:
        row = self.repository.get_scan(scan_id)
        if row is None or not row.get("report_object_hash"):
            raise ValueError("Terminal candidate scan report is missing")
        return CandidateScanReport.model_validate_json(
            self.objects.get_bytes(str(row["report_object_hash"]))
        )

    def _load_committed_state(
        self,
        scan_id: str,
        request: CandidateScanRequest,
    ) -> tuple[
        CandidateSignalManifest,
        list[CandidateSignal],
        list[CandidateRecord],
        CandidateUniverseSnapshot,
    ]:
        manifest_row = self.repository.get_signal_manifest(scan_id)
        universe_row = self.repository.get_universe(scan_id)
        if manifest_row is None or universe_row is None:
            raise ValueError("Committed candidate metadata is incomplete")
        if not self.repository.artifact_matches(
            str(manifest_row["manifest_artifact_id"]),
            str(manifest_row["manifest_object_hash"]),
        ):
            raise ValueError("Committed signal registry pointer is invalid")
        manifest = CandidateSignalManifest.model_validate_json(
            self.objects.get_bytes(str(manifest_row["manifest_object_hash"]))
        )
        if (
            manifest.scan_id != scan_id
            or manifest.input_release_id != request.input_release_id
            or manifest.rules_version != request.rules_version
            or manifest.created_at != request.as_of
            or not self.objects.verify(manifest.signal_object_hash)
            or not self.parquet.verify(
                manifest.descriptor,
                record_kind="candidate_signal",
                scan_id=scan_id,
                as_of=request.as_of,
            )
        ):
            raise ValueError("Committed signals failed verification")
        stored_signals = self.parquet.read_signals(manifest.descriptor)
        object_signals = [
            CandidateSignal.model_validate(item)
            for item in json.loads(self.objects.get_bytes(manifest.signal_object_hash))
        ]
        if (
            [item.signal_id for item in stored_signals] != manifest.signal_ids
            or [
                item.model_dump(mode="json", exclude={"created_at"})
                for item in object_signals
            ]
            != [
                item.model_dump(mode="json", exclude={"created_at"})
                for item in stored_signals
            ]
        ):
            raise ValueError("Committed signal manifest membership mismatch")
        records: list[CandidateRecord] = []
        for row in self.repository.list_scan_records(scan_id):
            if not self.repository.artifact_matches(
                str(row["record_artifact_id"]),
                str(row["record_object_hash"]),
            ):
                raise ValueError("Committed candidate registry pointer is invalid")
            record = CandidateRecord.model_validate_json(
                self.objects.get_bytes(str(row["record_object_hash"]))
            )
            if record.scan_id != scan_id:
                raise ValueError("Committed candidate record scan mismatch")
            if (
                row["previous_version_id"] != record.previous_version_id
                or str(row["candidate_id"]) != record.candidate_id
                or str(row["lifecycle_status"]) != record.lifecycle_status.value
                or str(row["strength"]) != record.strength.value
                or record.input_release_id != request.input_release_id
                or record.rules_version != request.rules_version
                or record.as_of != request.as_of
            ):
                raise ValueError("Committed candidate metadata mismatch")
            records.append(record)
        sqlite_members = self.repository.list_scan_members(scan_id)
        if {str(item["candidate_version_id"]) for item in sqlite_members} != {
            item.candidate_version_id for item in records
        }:
            raise ValueError("Committed SQLite member set mismatch")
        if not self.repository.artifact_matches(
            str(universe_row["snapshot_artifact_id"]),
            str(universe_row["snapshot_object_hash"]),
        ):
            raise ValueError("Committed universe registry pointer is invalid")
        snapshot = CandidateUniverseSnapshot.model_validate_json(
            self.objects.get_bytes(str(universe_row["snapshot_object_hash"]))
        )
        descriptor = CandidateFileDescriptor.model_validate_json(
            str(universe_row["member_descriptor_json"])
        )
        if (
            snapshot.scan_id != scan_id
            or snapshot.input_release_id != request.input_release_id
            or snapshot.rules_version != request.rules_version
            or snapshot.as_of != request.as_of
            or not self.parquet.verify(
                descriptor,
                record_kind="candidate_member",
                scan_id=scan_id,
                as_of=request.as_of,
            )
        ):
            raise ValueError("Committed universe failed verification")
        stored_members = self.parquet.read_members(descriptor)
        expected_versions = [item.candidate_version_id for item in snapshot.members]
        expected_ready = [
            item.candidate_version_id
            for item in records
            if item.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY
            and item.evaluation_status is CandidateEvaluationStatus.EVALUATED
        ]
        actual_semantic_hash = content_hash(
            [
                item.model_dump(mode="json", exclude={"created_at"})
                for item in snapshot.members
            ]
        )
        if (
            [item.candidate_version_id for item in stored_members] != expected_versions
            or expected_versions != expected_ready
            or snapshot.semantic_hash != actual_semantic_hash
        ):
            raise ValueError("Committed universe membership mismatch")
        return (
            manifest,
            object_signals,
            sorted(records, key=lambda item: item.company_id),
            snapshot,
        )

    @staticmethod
    def _interrupt_if_requested(
        requested: CandidateCheckpointStep | None,
        current: CandidateCheckpointStep,
    ) -> None:
        if requested is current:
            raise CandidateInterrupted(f"Interrupted after {current.value}")

    def _audit_object(
        self,
        object_hash: str,
        model: Any,
        checked: list[str],
        failures: list[str],
        failure_code: str,
    ) -> Any | None:
        checked.append(object_hash)
        try:
            return model.model_validate_json(self.objects.get_bytes(object_hash))
        except Exception:
            failures.append(failure_code)
            return None

    @staticmethod
    def _contains_forbidden_output_key(value: Any) -> bool:
        forbidden = {
            "buy",
            "sell",
            "side",
            "order",
            "position",
            "target",
            "target_price",
            "quantity",
            "qty",
            "weight",
        }
        if isinstance(value, dict):
            return any(str(key).lower() in forbidden for key in value) or any(
                CandidateScanService._contains_forbidden_output_key(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(CandidateScanService._contains_forbidden_output_key(item) for item in value)
        return False


__all__ = ["CandidateInterrupted", "CandidateScanService"]
