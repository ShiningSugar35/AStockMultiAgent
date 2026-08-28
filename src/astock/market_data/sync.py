"""Resumable dual-source 5m synchronization without canonical overwrite on failure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from astock.core.errors import AStockError, FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.source_resilience import (
    SourceCircuitBreaker,
    SourceFailureClass,
    classify_source_error,
)
from astock.core.state import StateStore
from astock.market_data.quality import cross_validate_batches, validate_batch
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.providers.base import MarketDataProvider
from astock.schemas import BarRequest, DataQualityReport, MarketDataBatch, QualityStatus


@dataclass(frozen=True, slots=True)
class SyncResult:
    job_id: str
    batches: tuple[MarketDataBatch, ...]
    provider_reports: tuple[DataQualityReport, ...]
    canonical_report: DataQualityReport
    canonical_manifest: dict[str, object]
    observation_files: tuple[Path, ...]
    failures: dict[str, str]
    canonical_updated: bool
    canonical_publish_reason: str


class MarketSyncService:
    def __init__(
        self,
        providers: list[MarketDataProvider],
        state: StateStore,
        observation_store: ParquetMarketStore,
        canonical_store: CanonicalMarketStore,
    ) -> None:
        self.providers = providers
        self.state = state
        self.source_breaker = SourceCircuitBreaker(state)
        self.observation_store = observation_store
        self.canonical_store = canonical_store

    def sync_5m(self, request: BarRequest) -> SyncResult:
        return self.sync_intraday(request)

    def sync_intraday(self, request: BarRequest) -> SyncResult:
        job_id = self.state.create_job(f"sync-{request.frequency.value}", content_hash(request))
        attempt_id = self.state.start_attempt(job_id)
        attempt_open = True
        previous_manifest = self.canonical_store.load_manifest(request)
        batches: list[MarketDataBatch] = []
        reports: list[DataQualityReport] = []
        files: list[Path] = []
        failures: dict[str, str] = {}
        capability = f"market.raw_{request.frequency.value}"
        try:
            for provider in self.providers:
                if not self.source_breaker.claim_attempt(provider.provider_id, capability):
                    failures[provider.provider_id] = f"CIRCUIT_OPEN:{capability}"
                    continue
                try:
                    provider_request = self._incremental_request(
                        provider.provider_id,
                        request,
                        has_canonical=previous_manifest is not None,
                    )
                    batch = provider.fetch_bars(provider_request)
                    report = validate_batch(batch)
                    written = self.observation_store.write_batch(batch)
                    batches.append(batch)
                    reports.append(report)
                    files.extend(written)
                    if report.quality_status == QualityStatus.FAIL:
                        failures[provider.provider_id] = (
                            "DATA_QUALITY: provider batch failed deterministic quality gates"
                        )
                        self.source_breaker.record_failure(
                            provider.provider_id,
                            capability,
                            SourceFailureClass.COVERAGE_INCOMPLETE,
                        )
                    else:
                        self.source_breaker.record_success(provider.provider_id, capability)
                except AStockError as exc:
                    failures[provider.provider_id] = f"{exc.failure_class.value}: {exc}"
                    self.source_breaker.record_failure(
                        provider.provider_id,
                        capability,
                        classify_source_error(exc),
                    )
            usable = [
                (batch, report)
                for batch, report in zip(batches, reports, strict=True)
                if report.quality_status != QualityStatus.FAIL
            ]
            if not usable:
                raise ProviderError(
                    (
                        "No intraday provider passed the quality gate; "
                        "previous canonical remains unchanged"
                    ),
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"failures": failures},
                )
            primary_pair = next(
                (pair for pair in usable if pair[0].provider_id == "eastmoney-5m"), usable[0]
            )
            selected, selected_report = primary_pair
            secondary_pairs = [pair for pair in usable if pair[0] is not selected]
            canonical_report = (
                cross_validate_batches(selected, secondary_pairs[0][0])
                if secondary_pairs
                else selected_report
            )
            preserve_previous = previous_manifest is not None and bool(failures)
            if preserve_previous:
                assert previous_manifest is not None
                manifest = previous_manifest
                canonical_updated = False
                canonical_publish_reason = "PRESERVED_PREVIOUS_CANONICAL_DUE_TO_PROVIDER_FAILURE"
            else:
                manifest = self.canonical_store.publish(
                    selected,
                    canonical_report,
                    source_batch_ids=[batch.batch_id for batch, _ in usable],
                    source_snapshot_ids=[batch.raw_snapshot_id for batch, _ in usable],
                )
                canonical_updated = True
                canonical_publish_reason = "PUBLISHED_QUALITY_PASSED_INCREMENT"
                for batch, report in usable:
                    self._advance_checkpoint(batch, report, job_id)
            self.state.finish_attempt(attempt_id)
            attempt_open = False
            self.state.finish_job(job_id, "SUCCEEDED")
            return SyncResult(
                job_id=job_id,
                batches=tuple(batches),
                provider_reports=tuple(reports),
                canonical_report=canonical_report,
                canonical_manifest=manifest,
                observation_files=tuple(files),
                failures=failures,
                canonical_updated=canonical_updated,
                canonical_publish_reason=canonical_publish_reason,
            )
        except Exception as exc:
            error_class = (
                exc.failure_class.value if isinstance(exc, AStockError) else type(exc).__name__
            )
            retryable = isinstance(exc, AStockError) and exc.retryable
            if attempt_open:
                self.state.finish_attempt(attempt_id, error_class=error_class, retryable=retryable)
            self.state.finish_job(job_id, "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED")
            raise

    def _incremental_request(
        self, provider_id: str, request: BarRequest, *, has_canonical: bool
    ) -> BarRequest:
        if not has_canonical:
            return request
        checkpoint = self.state.get_checkpoint(
            "market-provider", self._checkpoint_scope(provider_id, request)
        )
        if checkpoint is None:
            return request
        raw_cursor = checkpoint.get("cursor", {}).get("actual_end")
        if not raw_cursor:
            return request
        try:
            actual_end = datetime.fromisoformat(str(raw_cursor))
        except ValueError:
            return request
        if not (request.requested_start <= actual_end <= request.requested_end):
            return request
        overlap_start = actual_end - timedelta(days=7)
        return request.model_copy(
            update={"requested_start": max(request.requested_start, overlap_start)}
        )

    def _advance_checkpoint(
        self,
        batch: MarketDataBatch,
        report: DataQualityReport,
        job_id: str,
    ) -> None:
        if batch.actual_end is None:
            return
        scope_key = self._checkpoint_scope(batch.provider_id, batch.request)
        existing = self.state.get_checkpoint("market-provider", scope_key)
        raw_existing_end = existing.get("cursor", {}).get("actual_end") if existing else None
        if raw_existing_end:
            try:
                if batch.actual_end < datetime.fromisoformat(str(raw_existing_end)):
                    return
            except ValueError:
                pass
        self.state.set_checkpoint(
            scope_type="market-provider",
            scope_key=scope_key,
            cursor={
                "actual_end": batch.actual_end.isoformat(),
                "batch_id": batch.batch_id,
                "raw_snapshot_id": batch.raw_snapshot_id,
            },
            status=("SUCCEEDED" if report.quality_status != QualityStatus.FAIL else "FAILED"),
            object_hash=batch.raw_snapshot_id.rsplit(":", 1)[-1],
            job_id=job_id,
        )

    @staticmethod
    def _checkpoint_scope(provider_id: str, request: BarRequest) -> str:
        return f"{provider_id}:{request.market.value}:{request.symbol}:{request.frequency.value}"
