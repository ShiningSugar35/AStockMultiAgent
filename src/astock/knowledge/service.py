"""Resumable allowlisted Zhihu collection with immutable raw snapshots."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astock.core.errors import AStockError, FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.knowledge.adapter import ZhihuResponseAdapter
from astock.knowledge.repository import KnowledgeRepository
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import (
    PersistedZhihuResponse,
    ZhihuHttpTransport,
    ZhihuResponseTransport,
    classify_response_failure,
)
from astock.schemas import (
    AccessTransport,
    AuthorCollectionCoverageReport,
    CollectionCheckpoint,
    CollectionTerminalCondition,
    CoverageStatus,
    KnowledgeIdentityStatus,
    KnowledgeSourceDefinition,
    SourceAccessRequest,
    TransportCapability,
    ZhihuAuthorIdentity,
    ZhihuCollectionGap,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
)


@dataclass(frozen=True, slots=True)
class ZhihuSyncExecution:
    job_id: str
    report: AuthorCollectionCoverageReport
    listing_pages: tuple[ZhihuListingPage, ...]
    content_records: tuple[ZhihuContentRecord, ...]
    parquet_files: tuple[Path, ...]


class ZhihuCollectionService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        parquet_store: ParquetKnowledgeStore,
        *,
        transport: ZhihuResponseTransport | None = None,
        minimum_request_interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.transport = transport or ZhihuHttpTransport(object_store, state)
        self.adapter = ZhihuResponseAdapter(object_store)
        self.repository = KnowledgeRepository(state)
        self.router = SourceAccessRouter(state)
        self.minimum_request_interval_seconds = max(
            0.0, minimum_request_interval_seconds
        )
        self.sleeper = sleeper

    def probe_identity(self, source: KnowledgeSourceDefinition) -> ZhihuAuthorIdentity:
        self._assert_online_source(source)
        request_identity = {
            "source_id": source.source_id,
            "url_token": source.url_token,
            "operation": "profile-probe",
        }
        job_id = self.state.create_job("zhihu-author-probe", content_hash(request_identity))
        attempt_id = self.state.start_attempt(job_id)
        try:
            self._record_access(source, "profile")
            assert source.url_token is not None
            response = self.transport.fetch(
                author_source_id=source.source_id,
                content_type=None,
                url=f"https://www.zhihu.com/api/v4/members/{source.url_token}",
            )
            identity = self.adapter.parse_identity(source, response)
            self.repository.register_identity(identity)
            artifact = self.object_store.put_json(identity.model_dump(mode="json"))
            self.state.register_artifact(
                artifact_id=f"ZhihuAuthorIdentity:{source.source_id}:{artifact.sha256}",
                artifact_type="ZhihuAuthorIdentity",
                schema_version=identity.schema_version,
                object_hash=artifact.sha256,
                input_hashes=[response.snapshot.snapshot_id],
            )
            self.state.finish_attempt(attempt_id)
            self.state.finish_job(job_id, "SUCCEEDED")
            return identity
        except Exception as exc:
            failure, retryable = _exception_failure(exc)
            self.state.finish_attempt(
                attempt_id,
                error_class=failure.value,
                retryable=retryable,
            )
            self.state.finish_job(
                job_id,
                "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED",
            )
            raise

    def sync_listing(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        *,
        max_pages: int | None = None,
        page_size: int = 20,
    ) -> ZhihuSyncExecution:
        self._assert_online_source(source)
        if content_type.value not in source.collection_scope.content_types:
            raise ProviderError(
                f"{content_type.value} is not approved for {source.source_id}",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        request_identity = {
            "source_id": source.source_id,
            "content_type": content_type.value,
            "max_pages": max_pages,
            "page_size": page_size,
        }
        job_id = self.state.create_job("zhihu-author-sync", content_hash(request_identity))
        attempt_id = self.state.start_attempt(job_id)
        checkpoint = self.state.get_collection_checkpoint(
            source.source_id,
            content_type.value,
        )
        if checkpoint is not None and checkpoint.terminal_condition is None:
            listing_page = checkpoint.listing_page
            request_cursor = checkpoint.listing_cursor
            request_url = request_cursor or self._initial_listing_url(
                source, content_type, page_size
            )
        else:
            listing_page = 0
            request_cursor = None
            request_url = self._initial_listing_url(source, content_type, page_size)

        pages: list[ZhihuListingPage] = []
        records: list[ZhihuContentRecord] = []
        files: list[Path] = []
        snapshot_ids: list[str] = []
        discovered = success = duplicates = updated = 0
        fetched_pages = 0
        try:
            while True:
                self._record_access(source, f"listing:{content_type.value}")
                try:
                    response = self.transport.fetch(
                        author_source_id=source.source_id,
                        content_type=content_type,
                        url=request_url,
                    )
                except AStockError as exc:
                    return self._finish_failure(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        source=source,
                        content_type=content_type,
                        listing_page=listing_page,
                        request_cursor=request_cursor,
                        failure=exc.failure_class,
                        retryable=exc.retryable,
                        response=None,
                        pages=pages,
                        records=records,
                        files=files,
                        snapshot_ids=snapshot_ids,
                        discovered=discovered,
                        success=success,
                        duplicates=duplicates,
                        updated=updated,
                    )
                response_failure = classify_response_failure(response)
                if response_failure is not None:
                    return self._finish_failure(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        source=source,
                        content_type=content_type,
                        listing_page=listing_page,
                        request_cursor=request_cursor,
                        failure=response_failure,
                        retryable=response_failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
                        response=response,
                        pages=pages,
                        records=records,
                        files=files,
                        snapshot_ids=snapshot_ids,
                        discovered=discovered,
                        success=success,
                        duplicates=duplicates,
                        updated=updated,
                    )
                try:
                    page, parsed_records = self.adapter.parse_listing(
                        source,
                        content_type,
                        listing_page=listing_page,
                        request_cursor=request_cursor,
                        response=response,
                    )
                except ProviderError as exc:
                    return self._finish_failure(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        source=source,
                        content_type=content_type,
                        listing_page=listing_page,
                        request_cursor=request_cursor,
                        failure=exc.failure_class,
                        retryable=exc.retryable,
                        response=response,
                        pages=pages,
                        records=records,
                        files=files,
                        snapshot_ids=snapshot_ids,
                        discovered=discovered,
                        success=success,
                        duplicates=duplicates,
                        updated=updated,
                    )
                discovered += len(parsed_records)
                for parsed in parsed_records:
                    registration = self.repository.register_content(parsed)
                    stored = registration.record
                    files.append(self.parquet_store.write(stored))
                    records.append(stored)
                    success += 1
                    if registration.status == "DUPLICATE":
                        duplicates += 1
                    elif registration.status == "UPDATED":
                        updated += 1
                self.repository.register_listing_page(page)
                self.repository.resolve_open_listing_gaps(
                    source_id=source.source_id,
                    content_type=content_type,
                    listing_page=listing_page,
                    listing_cursor=request_cursor,
                )
                pages.append(page)
                if page.source_snapshot_id not in snapshot_ids:
                    snapshot_ids.append(page.source_snapshot_id)
                fetched_pages += 1

                if page.is_end:
                    terminal = (
                        CollectionTerminalCondition.CONFIRMED_EMPTY
                        if discovered == 0
                        else CollectionTerminalCondition.PAGINATION_COMPLETE
                    )
                    checkpoint_value = CollectionCheckpoint(
                        author=source.source_id,
                        content_type=content_type.value,
                        listing_page=listing_page,
                        listing_cursor=request_url,
                        terminal_condition=terminal,
                    )
                    self.state.set_collection_checkpoint(
                        checkpoint_value,
                        status="SUCCEEDED",
                        object_hash=page.raw_object_sha256,
                        job_id=job_id,
                    )
                    return self._finish_success(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        source=source,
                        content_type=content_type,
                        terminal=terminal,
                        coverage_status=CoverageStatus.COMPLETE,
                        last_cursor=request_url,
                        pages=pages,
                        records=records,
                        files=files,
                        snapshot_ids=snapshot_ids,
                        discovered=discovered,
                        success=success,
                        duplicates=duplicates,
                        updated=updated,
                    )

                assert page.next_cursor is not None
                next_checkpoint = CollectionCheckpoint(
                    author=source.source_id,
                    content_type=content_type.value,
                    listing_page=listing_page + 1,
                    listing_cursor=page.next_cursor,
                )
                self.state.set_collection_checkpoint(
                    next_checkpoint,
                    status="RUNNING",
                    object_hash=page.raw_object_sha256,
                    job_id=job_id,
                )
                if max_pages is not None and fetched_pages >= max_pages:
                    return self._finish_success(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        source=source,
                        content_type=content_type,
                        terminal=CollectionTerminalCondition.PARTIAL,
                        coverage_status=CoverageStatus.PARTIAL,
                        last_cursor=page.next_cursor,
                        pages=pages,
                        records=records,
                        files=files,
                        snapshot_ids=snapshot_ids,
                        discovered=discovered,
                        success=success,
                        duplicates=duplicates,
                        updated=updated,
                    )
                request_cursor = page.next_cursor
                request_url = page.next_cursor
                listing_page += 1
                if self.minimum_request_interval_seconds:
                    self.sleeper(self.minimum_request_interval_seconds)
        except Exception as exc:
            failure, retryable = _exception_failure(exc)
            self.state.finish_attempt(
                attempt_id,
                error_class=failure.value,
                retryable=retryable,
            )
            self.state.finish_job(
                job_id,
                "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED",
            )
            raise

    def _finish_success(
        self,
        *,
        job_id: str,
        attempt_id: str,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        terminal: CollectionTerminalCondition,
        coverage_status: CoverageStatus,
        last_cursor: str | None,
        pages: list[ZhihuListingPage],
        records: list[ZhihuContentRecord],
        files: list[Path],
        snapshot_ids: list[str],
        discovered: int,
        success: int,
        duplicates: int,
        updated: int,
    ) -> ZhihuSyncExecution:
        report = self._build_report(
            job_id=job_id,
            source=source,
            content_type=content_type,
            terminal=terminal,
            coverage_status=coverage_status,
            last_cursor=last_cursor,
            snapshot_ids=snapshot_ids,
            discovered=discovered,
            scheduled=discovered,
            success=success,
            failed=0,
            restricted=0,
            duplicates=duplicates,
            updated=updated,
            gaps=[],
        )
        self._persist_report(report)
        self.repository.upsert_collection_scope(
            source_id=source.source_id,
            content_type=content_type,
            status=coverage_status.value,
            last_cursor=last_cursor,
            terminal_condition=terminal,
        )
        self.state.finish_attempt(attempt_id)
        self.state.finish_job(job_id, "SUCCEEDED")
        return ZhihuSyncExecution(
            job_id=job_id,
            report=report,
            listing_pages=tuple(pages),
            content_records=tuple(records),
            parquet_files=tuple(files),
        )

    def _finish_failure(
        self,
        *,
        job_id: str,
        attempt_id: str,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        listing_page: int,
        request_cursor: str | None,
        failure: FailureClass,
        retryable: bool,
        response: PersistedZhihuResponse | None,
        pages: list[ZhihuListingPage],
        records: list[ZhihuContentRecord],
        files: list[Path],
        snapshot_ids: list[str],
        discovered: int,
        success: int,
        duplicates: int,
        updated: int,
    ) -> ZhihuSyncExecution:
        restricted = failure in {
            FailureClass.AUTH_REQUIRED,
            FailureClass.ACCESS_RESTRICTED,
            FailureClass.RATE_LIMITED,
        }
        terminal = (
            CollectionTerminalCondition.ACCESS_RESTRICTED
            if restricted
            else CollectionTerminalCondition.FETCH_FAILED
        )
        coverage_status = (
            CoverageStatus.ACCESS_RESTRICTED if restricted else CoverageStatus.PARTIAL
        )
        snapshot_id = response.snapshot.snapshot_id if response else None
        if snapshot_id and snapshot_id not in snapshot_ids:
            snapshot_ids.append(snapshot_id)
        gap = ZhihuCollectionGap(
            gap_id=f"zhihu-gap:{content_hash({
                'job_id': job_id,
                'source_id': source.source_id,
                'content_type': content_type.value,
                'listing_page': listing_page,
                'listing_cursor': request_cursor,
                'failure': failure.value,
            })}",
            author_source_id=source.source_id,
            content_type=content_type,
            listing_page=listing_page,
            listing_cursor=request_cursor,
            failure_class=failure.value,
            retryable=retryable,
            source_snapshot_id=snapshot_id,
            status="OPEN",
        )
        scope_id = self.repository.upsert_collection_scope(
            source_id=source.source_id,
            content_type=content_type,
            status=coverage_status.value,
            last_cursor=request_cursor,
            terminal_condition=terminal,
        )
        self.repository.record_gap(scope_id, gap)
        report = self._build_report(
            job_id=job_id,
            source=source,
            content_type=content_type,
            terminal=terminal,
            coverage_status=coverage_status,
            last_cursor=request_cursor,
            snapshot_ids=snapshot_ids,
            discovered=discovered,
            scheduled=discovered + 1,
            success=success,
            failed=0 if restricted else 1,
            restricted=1 if restricted else 0,
            duplicates=duplicates,
            updated=updated,
            gaps=[gap.model_dump(mode="json")],
        )
        self._persist_report(report)
        self.state.finish_attempt(
            attempt_id,
            error_class=failure.value,
            retryable=retryable,
        )
        self.state.finish_job(
            job_id,
            (
                "BLOCKED_MANUAL"
                if restricted
                else ("RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED")
            ),
        )
        return ZhihuSyncExecution(
            job_id=job_id,
            report=report,
            listing_pages=tuple(pages),
            content_records=tuple(records),
            parquet_files=tuple(files),
        )

    def _build_report(
        self,
        *,
        job_id: str,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        terminal: CollectionTerminalCondition,
        coverage_status: CoverageStatus,
        last_cursor: str | None,
        snapshot_ids: list[str],
        discovered: int,
        scheduled: int,
        success: int,
        failed: int,
        restricted: int,
        duplicates: int,
        updated: int,
        gaps: list[dict[str, object]],
    ) -> AuthorCollectionCoverageReport:
        identity = {
            "job_id": job_id,
            "source_id": source.source_id,
            "content_type": content_type.value,
            "terminal": terminal.value,
            "snapshot_ids": sorted(snapshot_ids),
        }
        return AuthorCollectionCoverageReport(
            report_id=f"author-coverage:{content_hash(identity)}",
            author_id=source.source_id,
            content_type=content_type.value,
            discovered_count=discovered,
            scheduled_count=scheduled,
            success_count=success,
            failed_count=failed,
            restricted_count=restricted,
            skipped_duplicate_count=duplicates,
            updated_count=updated,
            missing_count=0,
            last_page_or_cursor=last_cursor,
            terminal_condition=terminal,
            coverage_status=coverage_status,
            source_snapshot_ids=sorted(snapshot_ids),
            gaps=gaps,
        )

    def _persist_report(self, report: AuthorCollectionCoverageReport) -> None:
        report_object = self.object_store.put_json(report.model_dump(mode="json"))
        self.repository.register_coverage_report(report, object_hash=report_object.sha256)
        assert report.report_id is not None
        self.state.register_artifact(
            artifact_id=f"AuthorCollectionCoverageReport:{report.report_id}",
            artifact_type="AuthorCollectionCoverageReport",
            schema_version=report.schema_version,
            object_hash=report_object.sha256,
            input_hashes=report.source_snapshot_ids,
        )

    def _record_access(
        self,
        source: KnowledgeSourceDefinition,
        capability: str,
    ) -> None:
        selected = getattr(self.transport, "access_transport", AccessTransport.API)
        self.router.decide(
            SourceAccessRequest(
                source_id=source.source_id,
                requested_capability=capability,
            ),
            [
                TransportCapability(
                    source_id=source.source_id,
                    transport=AccessTransport.API,
                    requested_capabilities=[capability],
                    available=selected is AccessTransport.API,
                    reason=(
                        "Verified low-frequency Python structured request is available."
                        if selected is AccessTransport.API
                        else "Python structured access did not supply this response."
                    ),
                ),
                TransportCapability(
                    source_id=source.source_id,
                    transport=AccessTransport.MCP,
                    requested_capabilities=[capability],
                    available=False,
                    reason="No Zhihu MCP connector is configured.",
                ),
                TransportCapability(
                    source_id=source.source_id,
                    transport=AccessTransport.BROWSER,
                    requested_capabilities=[capability],
                    available=selected is AccessTransport.BROWSER,
                    reason="A credential-free logged-in Chrome response was imported.",
                ),
                TransportCapability(
                    source_id=source.source_id,
                    transport=AccessTransport.MANUAL,
                    requested_capabilities=[capability],
                    available=selected is AccessTransport.MANUAL,
                    reason="A credential-free manually saved response was imported.",
                ),
            ],
        )

    @staticmethod
    def _assert_online_source(source: KnowledgeSourceDefinition) -> None:
        if not source.online_collection_required:
            raise ProviderError(
                f"{source.source_id} is satisfied by a user-confirmed local export",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if not source.enabled or source.identity_status is not KnowledgeIdentityStatus.CONFIRMED:
            raise ProviderError(
                f"{source.source_id} is not identity-confirmed for online collection",
                failure_class=FailureClass.POLICY_REJECTED,
            )

    @staticmethod
    def _initial_listing_url(
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        page_size: int,
    ) -> str:
        assert source.url_token is not None
        endpoint_segment = (
            "pins" if content_type is ZhihuContentType.THOUGHTS else content_type.value
        )
        base = (
            f"https://www.zhihu.com/api/v4/members/{source.url_token}/"
            f"{endpoint_segment}?limit={page_size}&offset=0"
        )
        if content_type in {ZhihuContentType.ANSWERS, ZhihuContentType.ARTICLES}:
            return f"{base}&sort_by=created"
        return base


def _exception_failure(exc: Exception) -> tuple[FailureClass, bool]:
    if isinstance(exc, AStockError):
        return exc.failure_class, exc.retryable
    return FailureClass.INTERNAL, False


__all__ = ["ZhihuCollectionService", "ZhihuSyncExecution"]
