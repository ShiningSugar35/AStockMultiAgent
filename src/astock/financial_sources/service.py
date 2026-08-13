"""Recorded-by-default financial source to official evidence to audit pipeline."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from astock.core.errors import AStockError, StorageError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.financial_integrity import FinancialIntegrityService
from astock.financial_sources.certification import FinancialPdfCertifier
from astock.financial_sources.config import (
    FinancialFieldMapping,
    FinancialSourceConfig,
    load_financial_field_mappings,
    load_financial_source_config,
)
from astock.financial_sources.instrument import (
    FinancialInstrumentBinding,
    FinancialInstrumentResolver,
)
from astock.financial_sources.official import OfficialFinancialReportService
from astock.financial_sources.repository import (
    FinancialSourceReleaseRepository,
    _release_identity,
)
from astock.financial_sources.storage import FinancialSourceParquetStore
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.providers import EastMoneyFinancialProvider, SinaFinancialProvider
from astock.providers.financial_base import (
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import (
    FinancialAuditRequest,
    FinancialDurationSemantics,
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialSourceCoverage,
    FinancialSourceObservation,
    FinancialSourceReleaseManifest,
    FinancialSourceReleaseStatus,
    FinancialSourceSyncReport,
    FinancialStatementScope,
    FinancialStatementType,
    FinancialUnit,
    InstrumentType,
    Market,
)


class FinancialSourceService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        parquet: FinancialSourceParquetStore,
        project_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.parquet = parquet
        self.root = project_root.resolve()
        self.config: FinancialSourceConfig = load_financial_source_config(
            self.root / "configs" / "financial_sources.yaml"
        )
        self.mappings = load_financial_field_mappings(
            self.root / "configs" / "financial_field_mappings.yaml"
        )
        self.eastmoney = EastMoneyFinancialProvider(
            objects,
            state,
            self.config.provider_fixtures["eastmoney-financial"].parent,
        )
        self.sina = SinaFinancialProvider(
            objects,
            state,
            self.config.provider_fixtures["sina-financial"].parent,
        )
        self.official = OfficialFinancialReportService(
            state, objects, self.config.official_reports_fixture
        )
        self.repository = FinancialSourceReleaseRepository(state)
        self.instruments = FinancialInstrumentResolver(
            state, objects, parquet.root.parent
        )

    def sync(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime | None = None,
        live: bool = False,
        cross_check: bool = False,
    ) -> FinancialSourceSyncReport:
        explicit_as_of = as_of is not None
        effective_as_of = as_of or datetime.now(UTC)
        if effective_as_of.tzinfo is None or effective_as_of.utcoffset() is None:
            raise ValueError("financial source as_of must include a timezone")
        job_id = self.state.create_job(
            "financial-source-sync",
            content_hash(
                {
                    "company_id": company_id,
                    "market": market,
                    "period_end": period_end,
                    "period_type": period_type,
                    "as_of": effective_as_of,
                    "live": live,
                    "cross_check": cross_check,
                }
            ),
        )
        attempt = self.state.start_attempt(job_id)
        try:
            report = self._sync(
                company_id,
                market,
                period_end,
                period_type,
                as_of=effective_as_of,
                live=live,
                cross_check=cross_check,
                explicit_as_of=explicit_as_of,
            )
            self.state.finish_attempt(attempt)
            self.state.finish_job(
                job_id,
                "SUCCEEDED"
                if report.status is FinancialSourceReleaseStatus.CERTIFIED
                else "PERMANENT_FAILED",
            )
            return report
        except Exception as exc:
            retryable = isinstance(exc, AStockError) and exc.retryable
            self.state.finish_attempt(
                attempt,
                error_class=type(exc).__name__,
                retryable=retryable,
            )
            self.state.finish_job(job_id, "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED")
            raise

    def _sync(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime,
        live: bool,
        cross_check: bool,
        explicit_as_of: bool,
    ) -> FinancialSourceSyncReport:
        binding = self.instruments.resolve(company_id, market, as_of=as_of)
        if live and market is Market.BJSE:
            return self._empty_report(
                company_id,
                period_end,
                period_type,
                [self.config.primary_provider, self.config.backup_provider],
                [],
                FinancialSourceReleaseStatus.NEEDS_INFO,
                ["BJSE_OFFICIAL_FINANCIAL_REPORT_BLOCKED"],
                [],
            )
        providers = {
            self.eastmoney.provider_id: self.eastmoney,
            self.sina.provider_id: self.sina,
        }
        primary_provider = providers[self.config.primary_provider]
        backup_provider = providers[self.config.backup_provider]
        reasons: list[str] = []
        payloads: list[FinancialProviderPayload] = []
        captured_snapshots = []
        primary_failed = False
        try:
            primary = primary_provider.fetch(company_id, market, period_end, live=live)
            captured_snapshots.extend(primary.snapshots)
            primary_observations, primary_reasons = _parse_provider(
                primary,
                self.mappings,
                company_id,
                period_end,
                period_type,
                binding,
                as_of=as_of if explicit_as_of else None,
            )
            reasons.extend(primary_reasons)
            primary_failed = _critical_missing(primary_observations)
            if primary_failed:
                reasons.append(
                    f"{_provider_reason_token(primary.provider_id)}_CRITICAL_PERIOD_OR_TABLE_MISSING"
                )
            else:
                payloads.append(primary)
        except (FinancialRawCaptureError, KeyError, TypeError, ValueError, ValidationError) as exc:
            primary_failed = True
            if isinstance(exc, FinancialRawCaptureError):
                captured_snapshots.extend(exc.snapshots)
            reasons.append(
                f"{_provider_reason_token(primary_provider.provider_id)}_FINANCIAL_FAILED"
            )

        if primary_failed or cross_check:
            try:
                backup = backup_provider.fetch(company_id, market, period_end, live=live)
                captured_snapshots.extend(backup.snapshots)
                backup_observations, backup_reasons = _parse_provider(
                    backup,
                    self.mappings,
                    company_id,
                    period_end,
                    period_type,
                    binding,
                    as_of=as_of if explicit_as_of else None,
                )
                reasons.extend(backup_reasons)
                if _critical_missing(backup_observations):
                    reasons.append(
                        f"{_provider_reason_token(backup.provider_id)}_CRITICAL_PERIOD_OR_TABLE_MISSING"
                    )
                else:
                    payloads.append(backup)
                    suffix = "FALLBACK_USED" if primary_failed else "CROSS_CHECK_USED"
                    reasons.append(
                        f"{_provider_reason_token(backup.provider_id)}_{suffix}"
                    )
            except (
                FinancialRawCaptureError,
                KeyError,
                TypeError,
                ValueError,
                ValidationError,
            ) as exc:
                if isinstance(exc, FinancialRawCaptureError):
                    captured_snapshots.extend(exc.snapshots)
                reasons.append(
                    f"{_provider_reason_token(backup_provider.provider_id)}_FINANCIAL_FAILED"
                )

        observations: list[FinancialSourceObservation] = []
        for payload in payloads:
            parsed, parsed_reasons = _parse_provider(
                payload,
                self.mappings,
                company_id,
                period_end,
                period_type,
                binding,
                as_of=as_of if explicit_as_of else None,
            )
            observations.extend(parsed)
            reasons.extend(parsed_reasons)
        if cross_check and len(payloads) > 1:
            reasons.extend(_cross_provider_conflicts(observations))
        if live and not explicit_as_of:
            as_of = max(
                as_of,
                datetime.now(UTC),
                *(snapshot.available_to_system_at for snapshot in captured_snapshots),
            )
        raw_snapshot_ids = list(
            dict.fromkeys(item.source_snapshot_id for item in observations)
        )
        provider_ids = list(dict.fromkeys(item.provider_id for item in observations))
        observed_statements = list(
            dict.fromkeys(item.statement_type for item in observations)
        )
        if not observations:
            return self._empty_report(
                company_id,
                period_end,
                period_type,
                provider_ids or [self.config.primary_provider, self.config.backup_provider],
                list(
                    dict.fromkeys(
                        snapshot.snapshot_id for snapshot in captured_snapshots
                    )
                ),
                FinancialSourceReleaseStatus.FAILED,
                reasons,
                observed_statements,
            )
        provider_available = max(item.available_to_system_at for item in observations)
        _, source_hash, source_descriptor = self.parquet.write_observations(
            company_id, period_end, provider_available, observations
        )
        official = self.official.get(
            company_id,
            market,
            period_end,
            period_type,
            as_of=as_of,
            live=live,
            allow_live_capture_after_cutoff=live and not explicit_as_of,
        )
        if official is None:
            if live and market is Market.BJSE:
                reasons.append("BJSE_OFFICIAL_FINANCIAL_REPORT_BLOCKED")
            else:
                reasons.append("OFFICIAL_REPORT_NOT_AVAILABLE_AT_AS_OF")
                if live:
                    reasons.append("OFFICIAL_EXCHANGE_FALLBACK_BLOCKED")
            return self._empty_report(
                company_id,
                period_end,
                period_type,
                provider_ids,
                raw_snapshot_ids,
                FinancialSourceReleaseStatus.NEEDS_INFO,
                reasons,
                observed_statements,
                source_count=len(observations),
            )
        if official.document.publisher not in self.config.allowed_official_publishers:
            reasons.append("OFFICIAL_PUBLISHER_NOT_ALLOWED")
            return self._empty_report(
                company_id,
                period_end,
                period_type,
                provider_ids,
                raw_snapshot_ids,
                FinancialSourceReleaseStatus.NEEDS_INFO,
                reasons,
                observed_statements,
                source_count=len(observations),
                official_snapshot_id=official.snapshot.snapshot_id,
            )
        facts, certification_reasons = FinancialPdfCertifier(
            self.state, self.objects
        ).certify(official, observations, self.mappings)
        reasons.extend(certification_reasons)
        if not facts:
            reasons.append("NO_EXACT_OFFICIAL_FINANCIAL_FACT")
            return self._empty_report(
                company_id,
                period_end,
                period_type,
                provider_ids,
                raw_snapshot_ids,
                FinancialSourceReleaseStatus.NEEDS_INFO,
                reasons,
                observed_statements,
                source_count=len(observations),
                official_snapshot_id=official.snapshot.snapshot_id,
            )
        available = max(
            binding.available_to_system_at,
            official.index_snapshot.available_to_system_at,
            official.snapshot.available_to_system_at,
            *(item.available_to_system_at for item in observations),
            *(fact.created_at for fact in facts),
        )
        if explicit_as_of and available > as_of:
            raise ValueError("Financial release input is late for explicit as_of")
        _, certified_hash, certified_descriptor = self.parquet.write_facts(
            company_id, period_end, available, facts
        )
        coverage = FinancialSourceCoverage(
            created_at=available,
            provider_ids=provider_ids,
            statements_requested=list(self.config.required_statements),
            statements_observed=observed_statements,
            source_observation_count=len(observations),
            certified_fact_count=len(facts),
            reason_codes=list(dict.fromkeys(reasons)),
        )
        historical_row = self.repository.get(
            company_id,
            period_end.isoformat(),
            period_type.value,
            as_of=available,
        )
        historical = (
            self._verified_manifest(historical_row)
            if historical_row is not None
            else None
        )
        if historical is not None and _release_matches(
            historical,
            binding,
            provider_ids,
            raw_snapshot_ids,
            official.index_snapshot.snapshot_id,
            official.snapshot.snapshot_id,
            source_hash,
            certified_hash,
            available,
            coverage,
        ):
            if historical_row is None:
                raise ValueError("Historical financial release disappeared")
            return _report_from_manifest(
                historical,
                str(historical_row["manifest_object_hash"]),
                [*coverage.reason_codes, "IDEMPOTENT_EXISTING_RELEASE"],
            )
        current_row = self.repository.get(
            company_id, period_end.isoformat(), period_type.value
        )
        current = self._verified_manifest(current_row) if current_row is not None else None
        if current is not None and _release_matches(
            current,
            binding,
            provider_ids,
            raw_snapshot_ids,
            official.index_snapshot.snapshot_id,
            official.snapshot.snapshot_id,
            source_hash,
            certified_hash,
            available,
            coverage,
        ):
            if current_row is None:
                raise ValueError("Financial source head disappeared during verification")
            return _report_from_manifest(
                current,
                str(current_row["manifest_object_hash"]),
                [*coverage.reason_codes, "IDEMPOTENT_EXISTING_RELEASE"],
            )
        previous = current.release_id if current is not None else None
        if current is not None and current.available_to_system_at > available:
            raise ValueError("Financial source head availability cannot move backwards")
        supersedes = (
            current.release_id
            if current is not None
            and official.supersedes_document_id == current.official_document_id
            else None
        )
        provisional = FinancialSourceReleaseManifest(
            created_at=available,
            release_id="0" * 64,
            company_id=company_id,
            instrument_id=binding.record.instrument_id,
            market=binding.record.market,
            instrument_type=binding.record.instrument_type,
            instrument_release_id=binding.release_id,
            instrument_manifest_artifact_id=binding.manifest_artifact_id,
            instrument_manifest_object_hash=binding.manifest_object_hash,
            instrument_content_hash=binding.content_hash,
            instrument_available_to_system_at=binding.available_to_system_at,
            period_end=period_end,
            period_type=period_type,
            previous_release_id=previous,
            supersedes_release_id=supersedes,
            provider_ids=provider_ids,
            raw_snapshot_ids=raw_snapshot_ids,
            official_document_id=official.document.document_id,
            official_index_snapshot_id=official.index_snapshot.snapshot_id,
            official_snapshot_id=official.snapshot.snapshot_id,
            official_pit_id=official.pit.pit_id,
            source_files=[source_descriptor],
            certified_files=[certified_descriptor],
            source_content_hash=source_hash,
            certified_content_hash=certified_hash,
            available_to_system_at=available,
            status=FinancialSourceReleaseStatus.CERTIFIED,
            coverage=coverage,
        )
        manifest = provisional.model_copy(
            update={"release_id": content_hash(_release_identity(provisional))}
        )
        object_ref = self.objects.put_bytes(canonical_json_bytes(manifest))
        if not self.objects.verify(object_ref.sha256):
            raise ValueError("Financial source manifest object verification failed")
        self.repository.publish(manifest, object_ref.sha256)
        return _report_from_manifest(manifest, object_ref.sha256, coverage.reason_codes)

    def status(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        row = self.repository.get(
            company_id, period_end.isoformat(), period_type.value, as_of=as_of
        )
        if row is None:
            return {
                "schema_version": "financial-source-status-v1",
                "status": "NOT_AVAILABLE",
                "company_id": company_id,
                "period_end": period_end.isoformat(),
                "period_type": period_type.value,
            }
        try:
            manifest = self._verified_manifest(row)
        except (OSError, StorageError, ValueError, ValidationError):
            return {
                "schema_version": "financial-source-status-v1",
                "status": "CORRUPT",
                "company_id": company_id,
                "period_end": period_end.isoformat(),
                "period_type": period_type.value,
                "release_id": str(row["release_id"]),
            }
        return {
            "schema_version": "financial-source-status-v1",
            "status": "AVAILABLE",
            "release": manifest.model_dump(mode="json"),
        }

    def build_audit_request(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime,
        industry_profile: FinancialIndustryProfile,
        requested_rule_ids: list[str] | None = None,
    ) -> FinancialAuditRequest:
        row = self.repository.get(
            company_id,
            period_end.isoformat(),
            period_type.value,
            as_of=as_of,
        )
        facts: list[FinancialFact] = []
        if row is not None:
            manifest = self._verified_manifest(row)
            loaded = self.parquet.read_facts(manifest.certified_files[0])
            pit_repository = PointInTimeRepository(self.state)
            usable = []
            for fact in loaded:
                if fact.pit_id is None:
                    raise ValueError("Financial fact PIT reference is missing")
                metadata = pit_repository.get(fact.pit_id)
                if metadata is None:
                    raise ValueError("Financial fact PIT metadata is missing")
                PointInTimeService.assert_usable(
                    metadata,
                    as_of,
                    formal_historical=True,
                    allow_approximated=False,
                )
                usable.append(fact)
            facts = usable
        return FinancialAuditRequest(
            company_id=company_id,
            as_of=as_of,
            industry_profile=industry_profile,
            facts=facts,
            requested_rule_ids=requested_rule_ids or [],
            formal_historical=True,
            allow_approximated_pit=False,
        )

    def run_audit(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime,
        industry_profile: FinancialIndustryProfile,
        requested_rule_ids: list[str] | None = None,
    ) -> Any:
        request = self.build_audit_request(
            company_id,
            period_end,
            period_type,
            as_of=as_of,
            industry_profile=industry_profile,
            requested_rule_ids=requested_rule_ids,
        )
        return FinancialIntegrityService(
            self.state,
            self.objects,
            rule_config_path=self.root / "configs" / "financial_rules.yaml",
            industry_profile_path=self.root / "configs" / "financial_industry_profiles.yaml",
        ).run(request).pack

    def audit(self) -> dict[str, object]:
        rows = self.repository.list()
        corrupt = []
        for row in rows:
            try:
                self._verified_manifest(row)
            except (OSError, StorageError, ValueError, ValidationError):
                corrupt.append(str(row["release_id"]))
        return {
            "schema_version": "financial-source-audit-v1",
            "release_count": len(rows),
            "corrupt_release_ids": corrupt,
            "status": "PASS" if not corrupt else "FAIL",
        }

    def _verified_manifest(self, row: dict[str, Any]) -> FinancialSourceReleaseManifest:
        raw = self.objects.get_bytes(str(row["manifest_object_hash"]))
        if sha256_bytes(raw) != str(row["manifest_object_hash"]):
            raise ValueError("Financial release manifest object hash mismatch")
        manifest = FinancialSourceReleaseManifest.model_validate_json(raw)
        if content_hash(_release_identity(manifest)) != manifest.release_id:
            raise ValueError("Financial release identity mismatch")
        _verify_release_row(row, manifest)
        expected_inputs = json.dumps(
            [
                manifest.instrument_release_id,
                manifest.instrument_manifest_object_hash,
                manifest.instrument_content_hash,
                *manifest.raw_snapshot_ids,
                manifest.official_index_snapshot_id,
                manifest.official_snapshot_id,
                manifest.source_content_hash,
                manifest.certified_content_hash,
            ],
            separators=(",", ":"),
        )
        if row["input_hashes_json"] != expected_inputs:
            raise ValueError("Financial release artifact inputs mismatch")
        for snapshot_id in [
            *manifest.raw_snapshot_ids,
            manifest.official_index_snapshot_id,
            manifest.official_snapshot_id,
        ]:
            snapshot = self.state.get_snapshot(snapshot_id)
            if (
                snapshot is None
                or snapshot.available_to_system_at > manifest.available_to_system_at
                or not self.objects.verify(snapshot.object_sha256)
            ):
                raise ValueError("Financial release snapshot chain is invalid")
        binding = self.instruments.resolve(
            manifest.company_id,
            manifest.market,
            as_of=manifest.available_to_system_at,
        )
        if (
            binding.record.instrument_id != manifest.instrument_id
            or binding.record.instrument_type is not manifest.instrument_type
            or binding.release_id != manifest.instrument_release_id
            or binding.manifest_artifact_id != manifest.instrument_manifest_artifact_id
            or binding.manifest_object_hash != manifest.instrument_manifest_object_hash
            or binding.content_hash != manifest.instrument_content_hash
            or binding.available_to_system_at
            != manifest.instrument_available_to_system_at
        ):
            raise ValueError("Financial release instrument chain is invalid")
        for descriptor in manifest.source_files:
            if not self.parquet.verify_descriptor(
                descriptor,
                record_kind="SOURCE_OBSERVATION",
                company_id=manifest.company_id,
                period_end=manifest.period_end,
                available_at=descriptor.created_at,
            ):
                raise ValueError("Financial source Parquet is invalid")
        source_observations = [
            observation
            for descriptor in manifest.source_files
            for observation in self.parquet.read_observations(descriptor)
        ]
        if len(source_observations) != manifest.coverage.source_observation_count:
            raise ValueError("Financial source observation count mismatch")
        for observation in source_observations:
            if (
                observation.company_id != manifest.company_id
                or observation.instrument_id != manifest.instrument_id
                or observation.market is not manifest.market
                or observation.instrument_type is not manifest.instrument_type
                or observation.instrument_release_id != manifest.instrument_release_id
                or observation.instrument_manifest_artifact_id
                != manifest.instrument_manifest_artifact_id
                or observation.instrument_manifest_object_hash
                != manifest.instrument_manifest_object_hash
                or observation.instrument_content_hash != manifest.instrument_content_hash
                or observation.source_snapshot_id not in manifest.raw_snapshot_ids
            ):
                raise ValueError("Financial source observation lineage mismatch")
            snapshot = self.state.get_snapshot(observation.source_snapshot_id)
            if (
                snapshot is None
                or snapshot.source_id
                != f"{observation.provider_id}:{observation.source_request_hash}"
            ):
                raise ValueError("Financial provider snapshot request binding mismatch")
        for descriptor in manifest.certified_files:
            if not self.parquet.verify_descriptor(
                descriptor,
                record_kind="CERTIFIED_FACT",
                company_id=manifest.company_id,
                period_end=manifest.period_end,
                available_at=manifest.available_to_system_at,
            ):
                raise ValueError("Financial fact Parquet is invalid")
        pit = PointInTimeRepository(self.state).get(manifest.official_pit_id)
        if pit is None or pit.source_snapshot_id != manifest.official_snapshot_id:
            raise ValueError("Financial release PIT chain is invalid")
        evidence_repository = EvidenceRepository(self.state)
        facts = [
            fact
            for descriptor in manifest.certified_files
            for fact in self.parquet.read_facts(descriptor)
        ]
        if len(facts) != manifest.coverage.certified_fact_count:
            raise ValueError("Financial fact count mismatch")
        for fact in facts:
            if (
                fact.source_snapshot_id != manifest.official_snapshot_id
                or fact.pit_id != manifest.official_pit_id
                or not fact.evidence_ids
            ):
                raise ValueError("Financial fact lineage mismatch")
            for evidence_id in fact.evidence_ids:
                evidence = evidence_repository.get_evidence(evidence_id)
                if evidence is None or evidence.snapshot_id != manifest.official_snapshot_id:
                    raise ValueError("Financial evidence lineage mismatch")
        return manifest

    def _empty_report(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
        provider_ids: list[str],
        raw_snapshot_ids: list[str],
        status: FinancialSourceReleaseStatus,
        reasons: list[str],
        observed_statements: list[FinancialStatementType],
        *,
        source_count: int = 0,
        official_snapshot_id: str | None = None,
    ) -> FinancialSourceSyncReport:
        unique_reasons = list(dict.fromkeys(reasons))
        coverage = FinancialSourceCoverage(
            provider_ids=list(dict.fromkeys(provider_ids)),
            statements_requested=list(self.config.required_statements),
            statements_observed=observed_statements,
            source_observation_count=source_count,
            certified_fact_count=0,
            reason_codes=unique_reasons,
        )
        return FinancialSourceSyncReport(
            company_id=company_id,
            period_end=period_end,
            period_type=period_type,
            status=status,
            provider_ids=coverage.provider_ids,
            raw_snapshot_ids=raw_snapshot_ids,
            official_snapshot_id=official_snapshot_id,
            coverage=coverage,
            reason_codes=unique_reasons,
        )


def _parse_provider(
    payload: FinancialProviderPayload,
    mappings: list[FinancialFieldMapping],
    company_id: str,
    period_end: date,
    period_type: FinancialPeriodType,
    binding: FinancialInstrumentBinding,
    *,
    as_of: datetime | None,
) -> tuple[list[FinancialSourceObservation], list[str]]:
    observations = []
    reasons = []
    period_start = date(period_end.year, 1, 1)
    if (
        payload.request_company_id != company_id
        or payload.request_market is not binding.record.market
        or payload.request_period_end != period_end.isoformat()
    ):
        raise ValueError("Financial provider response request binding mismatch")
    by_statement = {
        statement: [mapping for mapping in mappings if mapping.statement_type is statement]
        for statement in (
            FinancialStatementType.BALANCE_SHEET,
            FinancialStatementType.INCOME_STATEMENT,
            FinancialStatementType.CASH_FLOW_STATEMENT,
        )
    }
    for statement, statement_mappings in by_statement.items():
        rows = payload.tables.get(statement.value)
        snapshot = payload.snapshots_by_statement.get(statement.value)
        request_hash = payload.request_hashes_by_statement.get(statement.value)
        if rows is None or snapshot is None or request_hash is None:
            reasons.append(f"PROVIDER_TABLE_MISSING:{statement.value}")
            continue
        if as_of is not None and snapshot.available_to_system_at > as_of:
            reasons.append(f"PROVIDER_SNAPSHOT_LATE:{statement.value}")
            continue
        matching = [
            row
            for row in rows
            if str(row.get("company_id")) == company_id
            and str(row.get("period_end"))[:10] == period_end.isoformat()
            and row.get("scope") == FinancialStatementScope.CONSOLIDATED.value
        ]
        if len(matching) != 1:
            reasons.append(f"PROVIDER_PERIOD_OR_SCOPE_NOT_UNIQUE:{statement.value}")
            continue
        row = matching[0]
        if row.get("currency") != "CNY":
            reasons.append(f"PROVIDER_CURRENCY_UNSUPPORTED:{statement.value}")
            continue
        for mapping in statement_mappings:
            provider_field = mapping.provider_field(payload.provider_id)
            raw_value = row.get(provider_field)
            value = _strict_decimal(raw_value)
            if raw_value is not None and value is None:
                reasons.append(f"PROVIDER_VALUE_INVALID:{mapping.field_code.value}")
                continue
            duration = (
                FinancialDurationSemantics.INSTANT
                if statement is FinancialStatementType.BALANCE_SHEET
                else (
                    FinancialDurationSemantics.REPORTED_PERIOD
                    if period_type is FinancialPeriodType.ANNUAL
                    else FinancialDurationSemantics.YEAR_TO_DATE
                )
            )
            identity = {
                "provider_id": payload.provider_id,
                "snapshot_id": snapshot.snapshot_id,
                "source_request_hash": request_hash,
                "company_id": company_id,
                "instrument_id": binding.record.instrument_id,
                "market": binding.record.market,
                "instrument_type": InstrumentType.STOCK,
                "instrument_release_id": binding.release_id,
                "instrument_manifest_artifact_id": binding.manifest_artifact_id,
                "instrument_manifest_object_hash": binding.manifest_object_hash,
                "instrument_content_hash": binding.content_hash,
                "period_start": (
                    None
                    if statement is FinancialStatementType.BALANCE_SHEET
                    else period_start
                ),
                "period_end": period_end,
                "period_type": period_type,
                "duration_semantics": duration,
                "statement": statement,
                "statement_scope": FinancialStatementScope.CONSOLIDATED,
                "field_code": mapping.field_code,
                "provider_field": provider_field,
                "value": value,
                "unit": mapping.unit,
            }
            observations.append(
                FinancialSourceObservation(
                    created_at=snapshot.available_to_system_at,
                    observation_id=content_hash(identity),
                    company_id=company_id,
                    instrument_id=binding.record.instrument_id,
                    market=binding.record.market,
                    instrument_type=InstrumentType.STOCK,
                    instrument_release_id=binding.release_id,
                    instrument_manifest_artifact_id=binding.manifest_artifact_id,
                    instrument_manifest_object_hash=binding.manifest_object_hash,
                    instrument_content_hash=binding.content_hash,
                    period_start=(
                        None
                        if statement is FinancialStatementType.BALANCE_SHEET
                        else period_start
                    ),
                    period_end=period_end,
                    period_type=period_type,
                    duration_semantics=duration,
                    statement_type=statement,
                    statement_scope=FinancialStatementScope.CONSOLIDATED,
                    field_code=mapping.field_code,
                    provider_field=provider_field,
                    reported_value=value,
                    unit=mapping.unit,
                    provider_id=payload.provider_id,
                    source_snapshot_id=snapshot.snapshot_id,
                    source_request_hash=request_hash,
                    available_to_system_at=snapshot.available_to_system_at,
                )
            )
    return observations, list(dict.fromkeys(reasons))


def _critical_missing(observations: list[FinancialSourceObservation]) -> bool:
    required = {
        FinancialStatementType.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW_STATEMENT,
    }
    observed = {
        item.statement_type for item in observations if item.reported_value is not None
    }
    return observed != required


def _cross_provider_conflicts(
    observations: list[FinancialSourceObservation],
) -> list[str]:
    values: dict[
        tuple[FinancialStatementType, FinancialFieldCode],
        set[tuple[Decimal, FinancialUnit]],
    ] = {}
    providers: dict[tuple[FinancialStatementType, FinancialFieldCode], set[str]] = {}
    for item in observations:
        if item.reported_value is None:
            continue
        key = (item.statement_type, item.field_code)
        values.setdefault(key, set()).add((item.reported_value, item.unit))
        providers.setdefault(key, set()).add(item.provider_id)
    return [
        f"SECONDARY_PROVIDER_CONFLICT:{field.value}"
        for (statement, field), observed in values.items()
        if len(providers[(statement, field)]) > 1 and len(observed) > 1
    ]


def _strict_decimal(value: object) -> Decimal | None:
    if value is None or value in {"", "--", "-"}:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.replace(",", "").replace(" ", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _verify_release_row(
    row: dict[str, Any], manifest: FinancialSourceReleaseManifest
) -> None:
    expected = {
        "release_id": manifest.release_id,
        "company_id": manifest.company_id,
        "instrument_id": manifest.instrument_id,
        "market": manifest.market.value,
        "instrument_type": manifest.instrument_type.value,
        "instrument_release_id": manifest.instrument_release_id,
        "instrument_manifest_artifact_id": manifest.instrument_manifest_artifact_id,
        "instrument_manifest_object_hash": manifest.instrument_manifest_object_hash,
        "instrument_content_hash": manifest.instrument_content_hash,
        "instrument_available_to_system_at": (
            manifest.instrument_available_to_system_at.isoformat()
        ),
        "period_end": manifest.period_end.isoformat(),
        "period_type": manifest.period_type.value,
        "previous_release_id": manifest.previous_release_id,
        "supersedes_release_id": manifest.supersedes_release_id,
        "manifest_schema_version": manifest.schema_version,
        "provider_ids_json": canonical_json_bytes(manifest.provider_ids).decode(),
        "raw_snapshot_ids_json": canonical_json_bytes(manifest.raw_snapshot_ids).decode(),
        "official_document_id": manifest.official_document_id,
        "official_index_snapshot_id": manifest.official_index_snapshot_id,
        "official_snapshot_id": manifest.official_snapshot_id,
        "official_pit_id": manifest.official_pit_id,
        "source_files_json": canonical_json_bytes(manifest.source_files).decode(),
        "certified_files_json": canonical_json_bytes(manifest.certified_files).decode(),
        "source_content_hash": manifest.source_content_hash,
        "certified_content_hash": manifest.certified_content_hash,
        "available_to_system_at": manifest.available_to_system_at.isoformat(),
        "status": manifest.status.value,
        "source_observation_count": manifest.coverage.source_observation_count,
        "certified_fact_count": manifest.coverage.certified_fact_count,
        "coverage_json": canonical_json_bytes(manifest.coverage).decode(),
        "artifact_type": "FinancialSourceReleaseManifest",
        "artifact_schema_version": manifest.schema_version,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError("Financial source release row mismatch")
    if row["manifest_object_hash"] != row["artifact_object_hash"]:
        raise ValueError("Financial source manifest artifact mismatch")


def _report_from_manifest(
    manifest: FinancialSourceReleaseManifest,
    manifest_object_hash: str,
    reasons: list[str],
) -> FinancialSourceSyncReport:
    return FinancialSourceSyncReport(
        created_at=manifest.available_to_system_at,
        company_id=manifest.company_id,
        period_end=manifest.period_end,
        period_type=manifest.period_type,
        status=manifest.status,
        release_id=manifest.release_id,
        manifest_object_hash=manifest_object_hash,
        provider_ids=manifest.provider_ids,
        raw_snapshot_ids=manifest.raw_snapshot_ids,
        official_snapshot_id=manifest.official_snapshot_id,
        coverage=manifest.coverage,
        reason_codes=list(dict.fromkeys(reasons)),
    )


def _release_matches(
    manifest: FinancialSourceReleaseManifest,
    binding: FinancialInstrumentBinding,
    provider_ids: list[str],
    raw_snapshot_ids: list[str],
    official_index_snapshot_id: str,
    official_snapshot_id: str,
    source_hash: str,
    certified_hash: str,
    available: datetime,
    coverage: FinancialSourceCoverage,
) -> bool:
    return (
        manifest.instrument_id == binding.record.instrument_id
        and manifest.market is binding.record.market
        and manifest.instrument_type is binding.record.instrument_type
        and manifest.instrument_release_id == binding.release_id
        and manifest.instrument_manifest_artifact_id == binding.manifest_artifact_id
        and manifest.instrument_manifest_object_hash == binding.manifest_object_hash
        and manifest.instrument_content_hash == binding.content_hash
        and manifest.instrument_available_to_system_at
        == binding.available_to_system_at
        and manifest.provider_ids == provider_ids
        and manifest.raw_snapshot_ids == raw_snapshot_ids
        and manifest.official_index_snapshot_id == official_index_snapshot_id
        and manifest.official_snapshot_id == official_snapshot_id
        and manifest.source_content_hash == source_hash
        and manifest.certified_content_hash == certified_hash
        and manifest.available_to_system_at == available
        and manifest.coverage == coverage
    )


def _provider_reason_token(provider_id: str) -> str:
    return provider_id.upper().replace("-", "_")


__all__ = ["FinancialSourceService"]
