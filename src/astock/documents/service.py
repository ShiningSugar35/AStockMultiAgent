"""Recoverable official disclosure synchronization service."""

from __future__ import annotations

from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.documents.repository import DocumentRepository
from astock.schemas import DisclosureSearchRequest, DisclosureSyncReport


class DisclosureSyncService:
    def __init__(
        self,
        provider: CninfoDisclosureProvider,
        repository: DocumentRepository,
        state: StateStore,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.state = state

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
            for announcement in batch.announcements[:maximum_documents]:
                item = self.provider.download(announcement)
                self.repository.register(item.document, item.snapshot)
                downloaded.append(item)
            self.state.finish_attempt(attempt_id)
            attempt_open = False
            self.state.finish_job(job_id, "SUCCEEDED")
            return DisclosureSyncReport(
                job_id=job_id,
                search_batch_id=batch.batch_id,
                discovered_count=len(batch.announcements),
                downloaded=downloaded,
                skipped_count=max(0, len(batch.announcements) - len(downloaded)),
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
