"""Credential-free import of browser-observed Zhihu API responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.knowledge.adapter import ZhihuResponseAdapter
from astock.knowledge.comments import ZhihuCommentIngestExecution, ZhihuCommentService
from astock.knowledge.config import get_knowledge_source
from astock.knowledge.repository import KnowledgeRepository
from astock.knowledge.service import ZhihuCollectionService, ZhihuSyncExecution
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import (
    PersistedZhihuResponse,
    ZhihuHttpTransport,
    classify_article_html_failure,
    classify_response_failure,
    normalize_zhihu_api_url,
    validate_zhihu_article_url,
)
from astock.schemas import (
    AccessTransport,
    KnowledgeIdentityStatus,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    SourceAccessRequest,
    TransportCapability,
    ZhihuBrowserResponseEnvelope,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuEndpointTemplateStatus,
    ZhihuImportedResponse,
    ZhihuImportStatus,
    ZhihuResponseKind,
    ZhihuTransport,
)

_MAX_ENVELOPE_FILE_BYTES = 91_000_000


@dataclass(frozen=True, slots=True)
class ZhihuImportExecution:
    record: ZhihuImportedResponse
    response_failure: FailureClass | None


@dataclass(frozen=True, slots=True)
class ZhihuReplayExecution:
    record: ZhihuImportedResponse
    sync_execution: ZhihuSyncExecution | None


@dataclass(frozen=True, slots=True)
class ZhihuCommentReplayExecution:
    record: ZhihuImportedResponse
    comment_execution: ZhihuCommentIngestExecution | None
    response_failure: FailureClass | None = None
    safe_to_skip: bool = False


@dataclass(frozen=True, slots=True)
class ZhihuDetailReplayExecution:
    record: ZhihuImportedResponse
    content_record: ZhihuContentRecord | None
    parquet_file: Path | None
    response_failure: FailureClass | None = None


class _SingleImportedListingTransport:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        record: ZhihuImportedResponse,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.record = record
        self.used = False
        self.access_transport = (
            AccessTransport.API
            if record.transport is ZhihuTransport.PYTHON_HTTP
            else (
                AccessTransport.BROWSER
                if record.transport is ZhihuTransport.CHROME
                else AccessTransport.MANUAL
            )
        )

    def fetch(
        self,
        *,
        author_source_id: str,
        content_type: ZhihuContentType | None,
        url: str,
    ) -> PersistedZhihuResponse:
        if self.used:
            raise ProviderError(
                "Imported listing replay attempted more than one response",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if (
            author_source_id != self.record.author_source_id
            or content_type != self.record.content_type
        ):
            raise ProviderError(
                "Imported listing replay scope changed",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if self.record.request_cursor is not None and url != self.record.request_cursor:
            raise ProviderError(
                "Imported listing cursor does not match the durable checkpoint",
                failure_class=FailureClass.CONFLICT,
            )
        snapshot = self.state.get_snapshot(self.record.source_snapshot_id)
        if snapshot is None:
            raise ProviderError(
                "Imported response lost its SourceSnapshot",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        self.used = True
        return PersistedZhihuResponse(
            requested_url=self.record.requested_url,
            status_code=self.record.status_code,
            content_type=self.record.response_mime,
            body=self.object_store.get_bytes(self.record.raw_object_sha256),
            snapshot=snapshot,
            transport=self.record.transport,
            latency_ms=0,
        )


class ZhihuResponseImportService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        runtime_root: Path,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.runtime_root = runtime_root
        self.repository = KnowledgeRepository(state)
        self.persistence = ZhihuHttpTransport(object_store, state)
        self.router = SourceAccessRouter(state)

    def import_file(
        self,
        envelope_path: Path,
        registry: KnowledgeSourceRegistry,
        endpoint_registry: ZhihuEndpointTemplateRegistry | None = None,
    ) -> ZhihuImportExecution:
        resolved = self._validated_runtime_file(envelope_path)
        try:
            envelope = ZhihuBrowserResponseEnvelope.model_validate_json(resolved.read_bytes())
        except ValidationError as exc:
            raise ProviderError(
                "Zhihu response envelope is invalid",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"validation_error_count": exc.error_count()},
            ) from exc
        return self.import_envelope(envelope, registry, endpoint_registry)

    def import_envelope(
        self,
        envelope: ZhihuBrowserResponseEnvelope,
        registry: KnowledgeSourceRegistry,
        endpoint_registry: ZhihuEndpointTemplateRegistry | None = None,
    ) -> ZhihuImportExecution:
        """Persist one already-validated credential-free response envelope."""

        source = get_knowledge_source(registry, envelope.author_source_id)
        self._validate_source_and_envelope(source, envelope, endpoint_registry)
        self._record_import_access(source, envelope.transport)
        body = envelope.decoded_body()
        persisted = self.persistence.persist_imported_response(
            author_source_id=source.source_id,
            content_type=envelope.content_type,
            requested_url=envelope.requested_url,
            status_code=envelope.status_code,
            content_type_header=envelope.response_mime,
            body=body,
            transport=envelope.transport,
            fetched_at=envelope.captured_at,
        )
        imported_at = datetime.now(UTC)
        identity = {
            "author_source_id": source.source_id,
            "response_kind": envelope.response_kind.value,
            "content_type": envelope.content_type.value if envelope.content_type else None,
            "content_id": envelope.content_id,
            "parent_comment_id": envelope.parent_comment_id,
            "listing_page": envelope.listing_page,
            "comment_page": envelope.comment_page,
            "request_cursor": envelope.request_cursor,
            "requested_url": envelope.requested_url,
            "status_code": envelope.status_code,
            "raw_object_sha256": persisted.snapshot.object_sha256,
            "transport": envelope.transport.value,
        }
        record = ZhihuImportedResponse(
            envelope_id=f"zhihu-import:{content_hash(identity)}",
            author_source_id=source.source_id,
            response_kind=envelope.response_kind,
            content_type=envelope.content_type,
            content_id=envelope.content_id,
            parent_comment_id=envelope.parent_comment_id,
            listing_page=envelope.listing_page,
            comment_page=envelope.comment_page,
            request_cursor=envelope.request_cursor,
            requested_url=envelope.requested_url,
            status_code=envelope.status_code,
            response_mime=envelope.response_mime,
            transport=envelope.transport,
            source_snapshot_id=persisted.snapshot.snapshot_id,
            raw_object_sha256=persisted.snapshot.object_sha256,
            body_byte_size=len(body),
            import_status=ZhihuImportStatus.PENDING,
            captured_at=envelope.captured_at,
            imported_at=imported_at,
        )
        stored = self.repository.register_imported_response(record)
        immutable_artifact_record = stored.model_copy(
            update={
                "import_status": ZhihuImportStatus.PENDING,
                "consumed_at": None,
            }
        )
        artifact = self.object_store.put_json(immutable_artifact_record.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"ZhihuImportedResponse:{stored.envelope_id}",
            artifact_type="ZhihuImportedResponse",
            schema_version=stored.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[stored.source_snapshot_id],
        )
        return ZhihuImportExecution(
            record=stored,
            response_failure=(
                classify_article_html_failure(persisted)
                if envelope.response_kind is ZhihuResponseKind.CONTENT_DETAIL
                and envelope.content_type is ZhihuContentType.ARTICLES
                else classify_response_failure(persisted)
            ),
        )

    def replay_listing(
        self,
        envelope_id: str,
        registry: KnowledgeSourceRegistry,
        parquet_store: ParquetKnowledgeStore,
        *,
        recover_consumed: bool = False,
    ) -> ZhihuReplayExecution:
        record = self.repository.get_imported_response(envelope_id)
        if record is None:
            raise ProviderError(
                "Zhihu response envelope is not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if record.response_kind is not ZhihuResponseKind.LISTING:
            raise ProviderError(
                "Only listing response replay is implemented in K5.3a",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        assert record.content_type is not None
        assert record.listing_page is not None
        with self.state.connect() as connection:
            already_committed = connection.execute(
                "SELECT 1 FROM zhihu_listing_page_manifest WHERE source_snapshot_id=?",
                (record.source_snapshot_id,),
            ).fetchone()
        if already_committed is not None:
            consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
            return ZhihuReplayExecution(record=consumed, sync_execution=None)
        if record.import_status is ZhihuImportStatus.CONSUMED and not recover_consumed:
            return ZhihuReplayExecution(record=record, sync_execution=None)
        source = get_knowledge_source(registry, record.author_source_id)
        self._validate_replay_checkpoint(record)
        transport = _SingleImportedListingTransport(self.state, self.object_store, record)
        collector = ZhihuCollectionService(
            self.state,
            self.object_store,
            parquet_store,
            transport=transport,
            minimum_request_interval_seconds=0,
        )
        execution = collector.sync_listing(
            source,
            record.content_type,
            max_pages=1,
            page_size=_page_size(record.requested_url),
        )
        consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
        return ZhihuReplayExecution(record=consumed, sync_execution=execution)

    def replay_comment(
        self,
        envelope_id: str,
        registry: KnowledgeSourceRegistry,
        parquet_store: ParquetKnowledgeStore,
        *,
        recover_consumed: bool = False,
    ) -> ZhihuCommentReplayExecution:
        record = self.repository.get_imported_response(envelope_id)
        if record is None:
            raise ProviderError(
                "Zhihu response envelope is not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if (
            record.import_status is ZhihuImportStatus.CONSUMED
            and not recover_consumed
        ):
            return ZhihuCommentReplayExecution(record=record, comment_execution=None)
        if record.response_kind not in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            raise ProviderError(
                "Response envelope is not a comment page",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        assert record.content_type is not None
        assert record.content_id is not None
        assert record.comment_page is not None
        source = get_knowledge_source(registry, record.author_source_id)
        self._validate_comment_replay_checkpoint(record)
        with self.state.connect() as connection:
            already_committed = connection.execute(
                "SELECT 1 FROM zhihu_comment_page_manifest WHERE source_snapshot_id=?",
                (record.source_snapshot_id,),
            ).fetchone()
        if already_committed is not None:
            consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
            return ZhihuCommentReplayExecution(record=consumed, comment_execution=None)
        persisted = self._persisted_response(record)
        failure = classify_response_failure(persisted)
        if failure is not None:
            self._record_comment_gap(record, failure)
            consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
            return ZhihuCommentReplayExecution(
                record=consumed,
                comment_execution=None,
                response_failure=failure,
            )
        try:
            execution = ZhihuCommentService(
                self.state,
                self.object_store,
                parquet_store,
            ).ingest_page(
                source,
                record.content_type,
                record.content_id,
                parent_comment_id=record.parent_comment_id,
                comment_page=record.comment_page,
                request_cursor=record.request_cursor,
                response=persisted,
            )
            self._resolve_comment_gap(
                record,
                terminal=execution.page.is_end,
                next_cursor=execution.page.next_cursor,
            )
        except ProviderError as exc:
            failure = exc.failure_class
            self._record_comment_gap(record, failure)
            consumed = self.repository.mark_import_consumed(
                envelope_id,
                datetime.now(UTC),
            )
            return ZhihuCommentReplayExecution(
                record=consumed,
                comment_execution=None,
                response_failure=failure,
                safe_to_skip=bool(exc.details.get("safe_to_skip")),
            )
        consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
        return ZhihuCommentReplayExecution(
            record=consumed,
            comment_execution=execution,
        )

    def replay_detail(
        self,
        envelope_id: str,
        registry: KnowledgeSourceRegistry,
        parquet_store: ParquetKnowledgeStore,
        *,
        recover_consumed: bool = False,
    ) -> ZhihuDetailReplayExecution:
        record = self.repository.get_imported_response(envelope_id)
        if record is None:
            raise ProviderError(
                "Zhihu response envelope is not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if record.response_kind is not ZhihuResponseKind.CONTENT_DETAIL:
            raise ProviderError(
                "Response envelope is not a content detail",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if (
            record.import_status is ZhihuImportStatus.CONSUMED
            and not recover_consumed
        ):
            return ZhihuDetailReplayExecution(
                record=record,
                content_record=None,
                parquet_file=None,
            )
        assert record.content_type is not None
        assert record.content_id is not None
        source = get_knowledge_source(registry, record.author_source_id)
        persisted = self._persisted_response(record)
        failure = (
            classify_article_html_failure(persisted)
            if record.content_type is ZhihuContentType.ARTICLES
            else classify_response_failure(persisted)
        )
        parsed: ZhihuContentRecord | None = None
        parquet_file: Path | None = None
        if failure is None:
            try:
                adapter = ZhihuResponseAdapter(self.object_store)
                parsed = (
                    adapter.parse_article_html(
                        source,
                        record.content_id,
                        persisted,
                    )
                    if record.content_type is ZhihuContentType.ARTICLES
                    else adapter.parse_content_detail(
                        source,
                        record.content_type,
                        record.content_id,
                        persisted,
                    )
                )
            except ProviderError as exc:
                failure = exc.failure_class
        if failure is None and parsed is not None:
            registration = self.repository.register_content(parsed)
            parsed = registration.record
            parquet_file = parquet_store.write(parsed)
            self._resolve_detail_gap(record)
        else:
            assert failure is not None
            self._record_detail_gap(record, failure)
        consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
        return ZhihuDetailReplayExecution(
            record=consumed,
            content_record=parsed,
            parquet_file=parquet_file,
            response_failure=failure,
        )

    def _validate_replay_checkpoint(self, record: ZhihuImportedResponse) -> None:
        assert record.content_type is not None
        assert record.listing_page is not None
        checkpoint = self.state.get_collection_checkpoint(
            record.author_source_id,
            record.content_type.value,
        )
        if checkpoint is None or checkpoint.terminal_condition is not None:
            expected_page = 0
            expected_cursor = None
        else:
            expected_page = checkpoint.listing_page
            expected_cursor = checkpoint.listing_cursor
        if record.listing_page != expected_page or record.request_cursor != expected_cursor:
            raise ProviderError(
                "Imported listing response is not the next durable checkpoint boundary",
                failure_class=FailureClass.CONFLICT,
                details={
                    "expected_listing_page": expected_page,
                    "received_listing_page": record.listing_page,
                },
            )

    def _validate_comment_replay_checkpoint(self, record: ZhihuImportedResponse) -> None:
        assert record.content_type is not None
        assert record.content_id is not None
        assert record.comment_page is not None
        checkpoint = self.state.get_collection_checkpoint(
            record.author_source_id,
            record.content_type.value,
            record.content_id,
            record.parent_comment_id,
        )
        if checkpoint is None or checkpoint.terminal_condition is not None:
            expected_page = 0
            expected_cursor = None
        else:
            expected_page = checkpoint.comment_page
            expected_cursor = (
                checkpoint.nested_reply_cursor
                if record.parent_comment_id
                else checkpoint.comment_cursor
            )
        if record.comment_page != expected_page or record.request_cursor != expected_cursor:
            raise ProviderError(
                "Imported comment response is not the next durable checkpoint boundary",
                failure_class=FailureClass.CONFLICT,
                details={
                    "expected_comment_page": expected_page,
                    "received_comment_page": record.comment_page,
                },
            )

    def _persisted_response(self, record: ZhihuImportedResponse) -> PersistedZhihuResponse:
        snapshot = self.state.get_snapshot(record.source_snapshot_id)
        if snapshot is None:
            raise ProviderError(
                "Imported response lost its SourceSnapshot",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        return PersistedZhihuResponse(
            requested_url=record.requested_url,
            status_code=record.status_code,
            content_type=record.response_mime,
            body=self.object_store.get_bytes(record.raw_object_sha256),
            snapshot=snapshot,
            transport=record.transport,
            latency_ms=0,
        )

    def _record_comment_gap(
        self,
        record: ZhihuImportedResponse,
        failure: FailureClass,
    ) -> None:
        assert record.content_type is not None
        assert record.content_id is not None
        comment_scope = (
            f"comments:{record.content_type.value}:{record.content_id}:"
            f"{record.parent_comment_id or '__root__'}"
        )
        access_failure = failure in {
            FailureClass.AUTH_REQUIRED,
            FailureClass.ACCESS_RESTRICTED,
            FailureClass.RATE_LIMITED,
        }
        scope_id = self.state.upsert_collection_scope(
            author_id=record.author_source_id,
            content_type=comment_scope,
            status="ACCESS_RESTRICTED" if access_failure else "PARTIAL",
            last_cursor=record.request_cursor,
            terminal_condition=("ACCESS_RESTRICTED" if access_failure else "FETCH_FAILED"),
        )
        cursor = {
            "comment_page": record.comment_page,
            "comment_cursor": record.request_cursor,
            "source_snapshot_id": record.source_snapshot_id,
        }
        with self.state.transaction() as connection:
            rows = connection.execute(
                "SELECT gap_id,cursor_json,failure_class FROM collection_gap "
                "WHERE scope_id=? AND status='OPEN'",
                (scope_id,),
            ).fetchall()
            superseded = [
                str(row["gap_id"])
                for row in rows
                if json.loads(str(row["cursor_json"])) == cursor
                and str(row["failure_class"]) != failure.value
            ]
            if superseded:
                connection.executemany(
                    "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?",
                    [(gap_id,) for gap_id in superseded],
                )
        self.state.record_collection_gap(
            scope_id=scope_id,
            cursor=cursor,
            failure_class=failure.value,
            retryable=failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
            status="OPEN",
        )

    def _resolve_comment_gap(
        self,
        record: ZhihuImportedResponse,
        *,
        terminal: bool,
        next_cursor: str | None,
    ) -> None:
        assert record.content_type is not None
        assert record.content_id is not None
        comment_scope = (
            f"comments:{record.content_type.value}:{record.content_id}:"
            f"{record.parent_comment_id or '__root__'}"
        )
        with self.state.transaction() as connection:
            scope = connection.execute(
                "SELECT scope_id FROM collection_scope "
                "WHERE author_id=? AND content_type=?",
                (record.author_source_id, comment_scope),
            ).fetchone()
            if scope is None:
                return
            scope_id = str(scope["scope_id"])
            rows = connection.execute(
                "SELECT gap_id,cursor_json FROM collection_gap "
                "WHERE scope_id=? AND status='OPEN'",
                (scope_id,),
            ).fetchall()
            resolved = []
            for row in rows:
                cursor = json.loads(str(row["cursor_json"]))
                if (
                    cursor.get("comment_page") == record.comment_page
                    and cursor.get("comment_cursor") == record.request_cursor
                ):
                    resolved.append(str(row["gap_id"]))
            if resolved:
                connection.executemany(
                    "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?",
                    [(gap_id,) for gap_id in resolved],
                )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM collection_gap "
                "WHERE scope_id=? AND status='OPEN'",
                (scope_id,),
            ).fetchone()[0]
            if int(remaining) == 0:
                connection.execute(
                    "UPDATE collection_scope SET status=?,last_cursor=?,"
                    "terminal_condition=? WHERE scope_id=?",
                    (
                        "COMPLETE" if terminal else "RUNNING",
                        next_cursor,
                        "PAGINATION_COMPLETE" if terminal else None,
                        scope_id,
                    ),
                )

    def _record_detail_gap(
        self,
        record: ZhihuImportedResponse,
        failure: FailureClass,
    ) -> None:
        assert record.content_type is not None
        assert record.content_id is not None
        detail_scope = f"detail:{record.content_type.value}:{record.content_id}"
        access_failure = failure in {
            FailureClass.AUTH_REQUIRED,
            FailureClass.ACCESS_RESTRICTED,
            FailureClass.RATE_LIMITED,
        }
        scope_id = self.state.upsert_collection_scope(
            author_id=record.author_source_id,
            content_type=detail_scope,
            status="ACCESS_RESTRICTED" if access_failure else "PARTIAL",
            last_cursor=record.requested_url,
            terminal_condition=("ACCESS_RESTRICTED" if access_failure else "FETCH_FAILED"),
        )
        self.state.record_collection_gap(
            scope_id=scope_id,
            cursor={
                "content_id": record.content_id,
                "detail_url": record.requested_url,
                "source_snapshot_id": record.source_snapshot_id,
            },
            failure_class=failure.value,
            retryable=failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
            status="OPEN",
        )

    def _resolve_detail_gap(self, record: ZhihuImportedResponse) -> None:
        assert record.content_type is not None
        assert record.content_id is not None
        detail_scope = f"detail:{record.content_type.value}:{record.content_id}"
        with self.state.transaction() as connection:
            scope = connection.execute(
                "SELECT scope_id FROM collection_scope WHERE author_id=? AND content_type=?",
                (record.author_source_id, detail_scope),
            ).fetchone()
            if scope is None:
                return
            connection.execute(
                "UPDATE collection_scope SET status='COMPLETE',last_cursor=?,"
                "terminal_condition='PAGINATION_COMPLETE' WHERE scope_id=?",
                (record.requested_url, scope["scope_id"]),
            )
            connection.execute(
                "UPDATE collection_gap SET status='RESOLVED' WHERE scope_id=? AND status='OPEN'",
                (scope["scope_id"],),
            )

    def _validated_runtime_file(self, path: Path) -> Path:
        runtime = self.runtime_root.resolve()
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProviderError(
                "Zhihu response envelope does not exist",
                failure_class=FailureClass.INVALID_RESPONSE,
            ) from exc
        try:
            resolved.relative_to(runtime)
        except ValueError as exc:
            raise ProviderError(
                "Zhihu response envelope must remain inside runtime",
                failure_class=FailureClass.POLICY_REJECTED,
            ) from exc
        if not resolved.is_file() or resolved.stat().st_size > _MAX_ENVELOPE_FILE_BYTES:
            raise ProviderError(
                "Zhihu response envelope is not a permitted file",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        return resolved

    @staticmethod
    def _validate_source_and_envelope(
        source: KnowledgeSourceDefinition,
        envelope: ZhihuBrowserResponseEnvelope,
        endpoint_registry: ZhihuEndpointTemplateRegistry | None,
    ) -> None:
        if (
            not source.online_collection_required
            or not source.enabled
            or source.identity_status is not KnowledgeIdentityStatus.CONFIRMED
        ):
            raise ProviderError(
                "Zhihu response import source is not approved for online collection",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if envelope.content_type and (
            envelope.content_type.value not in source.collection_scope.content_types
        ):
            raise ProviderError(
                "Zhihu response content type is outside the author allowlist",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if envelope.captured_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ProviderError(
                "Zhihu response captured_at is implausibly in the future",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        is_article_html = (
            envelope.response_kind is ZhihuResponseKind.CONTENT_DETAIL
            and envelope.content_type is ZhihuContentType.ARTICLES
        )
        if is_article_html:
            validate_zhihu_article_url(envelope.requested_url)
            normalized = envelope.requested_url
        else:
            normalized = normalize_zhihu_api_url(envelope.requested_url)
        if normalized != envelope.requested_url:
            raise ProviderError(
                "Imported Zhihu responses must already use HTTPS",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        assert source.url_token is not None
        path = urlsplit(envelope.requested_url).path.rstrip("/")
        if envelope.response_kind is ZhihuResponseKind.PROFILE:
            expected = f"/api/v4/members/{source.url_token}"
            if path != expected:
                raise ProviderError(
                    "Zhihu profile response does not match the allowlisted author",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
        elif envelope.response_kind is ZhihuResponseKind.LISTING:
            assert envelope.content_type is not None
            segment = (
                "pins" if envelope.content_type.value == "thoughts" else envelope.content_type.value
            )
            expected = f"/api/v4/members/{source.url_token}/{segment}"
            if path != expected:
                raise ProviderError(
                    "Zhihu listing response does not match the allowlisted author scope",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
        elif envelope.response_kind in {
            ZhihuResponseKind.CONTENT_DETAIL,
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            ZhihuResponseImportService._validate_endpoint(
                envelope,
                path,
                endpoint_registry,
            )
        elif envelope.content_id not in path.split("/"):
            raise ProviderError(
                "Zhihu detail/comment URL does not contain its declared content id",
                failure_class=FailureClass.POLICY_REJECTED,
            )

    @staticmethod
    def _validate_endpoint(
        envelope: ZhihuBrowserResponseEnvelope,
        path: str,
        endpoint_registry: ZhihuEndpointTemplateRegistry | None,
    ) -> None:
        if endpoint_registry is None:
            raise ProviderError(
                "Zhihu comment import requires an approved endpoint registry",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        assert envelope.content_type is not None
        template = next(
            (
                item
                for item in endpoint_registry.templates
                if item.response_kind is envelope.response_kind
                and envelope.content_type in item.content_types
            ),
            None,
        )
        if (
            template is None
            or template.status is not ZhihuEndpointTemplateStatus.VERIFIED
            or template.path_template is None
        ):
            raise ProviderError(
                "Zhihu comment endpoint has not been observed and approved",
                failure_class=FailureClass.CAPABILITY_UNAVAILABLE,
            )
        expected = template.path_template
        expected = expected.replace("{content_id}", envelope.content_id or "")
        expected = expected.replace("{parent_comment_id}", envelope.parent_comment_id or "")
        if "{" in expected or "}" in expected:
            raise ProviderError(
                "Zhihu endpoint template contains an unsupported placeholder",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if path != expected.rstrip("/"):
            raise ProviderError(
                "Zhihu comment response does not match an approved endpoint template",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        requested = urlsplit(envelope.requested_url)
        origin = f"{requested.scheme}://{requested.netloc}"
        if origin != template.request_origin:
            raise ProviderError(
                "Zhihu response does not match its approved endpoint origin",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        query = parse_qs(requested.query, keep_blank_values=True)
        if set(query) != set(template.default_query):
            raise ProviderError(
                "Zhihu response query keys do not match its approved endpoint template",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        is_continuation = envelope.request_cursor is not None
        if is_continuation:
            assert envelope.request_cursor is not None
            if normalize_zhihu_api_url(envelope.request_cursor) != envelope.requested_url:
                raise ProviderError(
                    "Zhihu continuation response does not match its durable request cursor",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
        for key, expected_value in template.default_query.items():
            values = query.get(key, [])
            if len(values) != 1:
                raise ProviderError(
                    "Zhihu response query values do not match its approved endpoint template",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            value = values[0]
            if key == "offset" and is_continuation:
                valid = bool(value)
            elif key == "limit" and is_continuation:
                valid = (
                    value.isdecimal()
                    and expected_value.isdecimal()
                    and 1 <= int(value) <= int(expected_value)
                )
            else:
                valid = value == expected_value
            if not valid:
                raise ProviderError(
                    "Zhihu response query values do not match its approved endpoint template",
                    failure_class=FailureClass.POLICY_REJECTED,
                )

    def _record_import_access(
        self,
        source: KnowledgeSourceDefinition,
        transport: ZhihuTransport,
    ) -> None:
        capability = "zhihu-response-import"
        selected = (
            AccessTransport.API
            if transport is ZhihuTransport.PYTHON_HTTP
            else (
                AccessTransport.BROWSER
                if transport is ZhihuTransport.CHROME
                else AccessTransport.MANUAL
            )
        )
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
                        "A verified low-frequency Python structured response is available."
                        if selected is AccessTransport.API
                        else "The response was already captured outside Python HTTP."
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
                    transport=selected,
                    requested_capabilities=[capability],
                    available=True,
                    reason="A credential-free runtime response envelope is available.",
                ),
            ],
        )


def _page_size(requested_url: str) -> int:
    raw = parse_qs(urlsplit(requested_url).query).get("limit", ["20"])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProviderError(
            "Imported listing limit is not an integer",
            failure_class=FailureClass.INVALID_RESPONSE,
        ) from exc
    if not 1 <= value <= 100:
        raise ProviderError(
            "Imported listing limit is outside the supported range",
            failure_class=FailureClass.INVALID_RESPONSE,
        )
    return value


__all__ = [
    "ZhihuImportExecution",
    "ZhihuCommentReplayExecution",
    "ZhihuDetailReplayExecution",
    "ZhihuReplayExecution",
    "ZhihuResponseImportService",
]
