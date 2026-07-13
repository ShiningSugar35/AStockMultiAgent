"""Recoverable official disclosure synchronization service."""

from __future__ import annotations

from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.documents.repository import DocumentRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AvailabilityBasis,
    DisclosureSearchRequest,
    DisclosureSyncReport,
    PointInTimeStatus,
)


class DisclosureSyncService:
    def __init__(
        self,
        provider: CninfoDisclosureProvider,
        repository: DocumentRepository,
        state: StateStore,
        pit_service: PointInTimeService | None = None,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.state = state
        self.pit_service = pit_service or PointInTimeService(
            PointInTimeRepository(state), state, provider.object_store
        )

    def sync(
        self,
        request: DisclosureSearchRequest,
        *,
        maximum_documents: int = 1,
    ) -> DisclosureSyncReport:
        if maximum_documents < 0 or maximum_documents > 20:
            raise ValueError("maximum_documents must be between 0 and 20")
        job_id = self.state.create_job("disclosure-sync", content_hash(request))
        attempt_id = self.state.start_attempt(job_id)
        attempt_open = True
        try:
            batch = self.provider.search(request)
            downloaded = []
            pit_metadata_ids = []
            for announcement in batch.announcements[:maximum_documents]:
                item = self.provider.download(announcement)
                self.repository.register(item.document, item.snapshot)
                canonical_snapshot = self.repository.snapshot(item.snapshot.snapshot_id)
                if canonical_snapshot is None:  # pragma: no cover - register guarantees lineage
                    raise ValueError(f"Snapshot registration failed: {item.snapshot.snapshot_id}")
                pit = self.pit_service.create(
                    source_id=item.document.document_id,
                    source_document_id=item.document.document_id,
                    source_snapshot_id=canonical_snapshot.snapshot_id,
                    published_at=item.document.published_at,
                    effective_at=item.document.effective_at,
                    ingested_at=canonical_snapshot.fetched_at,
                    available_to_system_at=canonical_snapshot.available_to_system_at,
                    point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
                    availability_basis=AvailabilityBasis.FETCH_OBSERVED,
                )
                pit_metadata_ids.append(pit.pit_id)
                downloaded.append(
                    item.model_copy(
                        update={"snapshot": canonical_snapshot, "pit_metadata": pit}
                    )
                )
            self.state.finish_attempt(attempt_id)
            attempt_open = False
            self.state.finish_job(job_id, "SUCCEEDED")
            return DisclosureSyncReport(
                job_id=job_id,
                search_batch_id=batch.batch_id,
                discovered_count=len(batch.announcements),
                downloaded=downloaded,
                skipped_count=max(0, len(batch.announcements) - len(downloaded)),
                pit_metadata_ids=pit_metadata_ids,
            )
        except Exception as exc:
            retryable = isinstance(exc, AStockError) and exc.retryable
            error_class = (
                exc.failure_class.value if isinstance(exc, AStockError) else type(exc).__name__
            )
            if attempt_open:
                self.state.finish_attempt(
                    attempt_id,
                    error_class=error_class,
                    retryable=retryable,
                )
            self.state.finish_job(job_id, "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED")
            raise
