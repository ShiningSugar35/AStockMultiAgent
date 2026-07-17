"""Credential-free import of browser-observed Zhihu API responses."""

from __future__ import annotations

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
from astock.knowledge.comments import ZhihuCommentIngestExecution, ZhihuCommentService
from astock.knowledge.config import get_knowledge_source
from astock.knowledge.repository import KnowledgeRepository
from astock.knowledge.service import ZhihuCollectionService, ZhihuSyncExecution
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import (
    PersistedZhihuResponse,
    ZhihuHttpTransport,
    classify_response_failure,
    normalize_zhihu_api_url,
)
from astock.schemas import (
    AccessTransport,
    KnowledgeIdentityStatus,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    SourceAccessRequest,
    TransportCapability,
    ZhihuBrowserResponseEnvelope,
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
            AccessTransport.BROWSER
            if record.transport is ZhihuTransport.CHROME
            else AccessTransport.MANUAL
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
            envelope = ZhihuBrowserResponseEnvelope.model_validate_json(
                resolved.read_bytes()
            )
        except ValidationError as exc:
            raise ProviderError(
                "Zhihu response envelope is invalid",
                failure_class=FailureClass.INVALID_RESPONSE,
                details={"validation_error_count": exc.error_count()},
            ) from exc
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
        artifact = self.object_store.put_json(stored.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"ZhihuImportedResponse:{stored.envelope_id}",
            artifact_type="ZhihuImportedResponse",
            schema_version=stored.schema_version,
            object_hash=artifact.sha256,
            input_hashes=[stored.source_snapshot_id],
        )
        return ZhihuImportExecution(
            record=stored,
            response_failure=classify_response_failure(persisted),
        )

    def replay_listing(
        self,
        envelope_id: str,
        registry: KnowledgeSourceRegistry,
        parquet_store: ParquetKnowledgeStore,
    ) -> ZhihuReplayExecution:
        record = self.repository.get_imported_response(envelope_id)
        if record is None:
            raise ProviderError(
                "Zhihu response envelope is not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if record.import_status is ZhihuImportStatus.CONSUMED:
            return ZhihuReplayExecution(record=record, sync_execution=None)
        if record.response_kind is not ZhihuResponseKind.LISTING:
            raise ProviderError(
                "Only listing response replay is implemented in K5.3a",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        assert record.content_type is not None
        assert record.listing_page is not None
        source = get_knowledge_source(registry, record.author_source_id)
        self._validate_replay_checkpoint(record)
        with self.state.connect() as connection:
            already_committed = connection.execute(
                "SELECT 1 FROM zhihu_listing_page_manifest WHERE source_snapshot_id=?",
                (record.source_snapshot_id,),
            ).fetchone()
        if already_committed is not None:
            consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
            return ZhihuReplayExecution(record=consumed, sync_execution=None)
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
    ) -> ZhihuCommentReplayExecution:
        record = self.repository.get_imported_response(envelope_id)
        if record is None:
            raise ProviderError(
                "Zhihu response envelope is not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        if record.import_status is ZhihuImportStatus.CONSUMED:
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
        consumed = self.repository.mark_import_consumed(envelope_id, datetime.now(UTC))
        return ZhihuCommentReplayExecution(
            record=consumed,
            comment_execution=execution,
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
        if (
            record.listing_page != expected_page
            or record.request_cursor != expected_cursor
        ):
            raise ProviderError(
                "Imported listing response is not the next durable checkpoint boundary",
                failure_class=FailureClass.CONFLICT,
                details={
                    "expected_listing_page": expected_page,
                    "received_listing_page": record.listing_page,
                },
            )

    def _validate_comment_replay_checkpoint(
        self, record: ZhihuImportedResponse
    ) -> None:
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

    def _persisted_response(
        self, record: ZhihuImportedResponse
    ) -> PersistedZhihuResponse:
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
        self.state.record_collection_gap(
            scope_id=scope_id,
            cursor={
                "comment_page": record.comment_page,
                "comment_cursor": record.request_cursor,
                "source_snapshot_id": record.source_snapshot_id,
            },
            failure_class=failure.value,
            retryable=failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
            status="OPEN",
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
                "pins"
                if envelope.content_type.value == "thoughts"
                else envelope.content_type.value
            )
            expected = f"/api/v4/members/{source.url_token}/{segment}"
            if path != expected:
                raise ProviderError(
                    "Zhihu listing response does not match the allowlisted author scope",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
        elif envelope.response_kind in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            ZhihuResponseImportService._validate_comment_endpoint(
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
    def _validate_comment_endpoint(
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
        expected = expected.replace(
            "{parent_comment_id}", envelope.parent_comment_id or ""
        )
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

    def _record_import_access(
        self,
        source: KnowledgeSourceDefinition,
        transport: ZhihuTransport,
    ) -> None:
        capability = "zhihu-response-import"
        selected = (
            AccessTransport.BROWSER
            if transport is ZhihuTransport.CHROME
            else AccessTransport.MANUAL
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
                    available=False,
                    reason="The response was already captured outside Python HTTP.",
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
    "ZhihuReplayExecution",
    "ZhihuResponseImportService",
]
