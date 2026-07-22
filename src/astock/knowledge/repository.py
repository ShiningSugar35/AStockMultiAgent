"""SQLite metadata repository for immutable allowlisted knowledge collection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.state import StateStore
from astock.schemas import (
    AuthorCollectionCoverageReport,
    CollectionTerminalCondition,
    KnowledgeCoverageAuditReport,
    KnowledgeLocalCoverageReport,
    ZhihuAuthorIdentity,
    ZhihuAuthorParticipationChain,
    ZhihuCollectionGap,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuImportedResponse,
    ZhihuImportStatus,
    ZhihuListingPage,
    ZhihuResponseKind,
)


@dataclass(frozen=True, slots=True)
class ContentRegistration:
    status: str
    record: ZhihuContentRecord


@dataclass(frozen=True, slots=True)
class CommentRegistration:
    status: str
    record: ZhihuCommentNode


class KnowledgeRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register_identity(self, identity: ZhihuAuthorIdentity) -> ZhihuAuthorIdentity:
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT platform_user_id,url_token,verified_at "
                "FROM knowledge_source_identity "
                "WHERE source_id=?",
                (identity.author_source_id,),
            ).fetchone()
            if row is not None and (
                str(row["platform_user_id"]) != identity.platform_user_id
                or str(row["url_token"]) != identity.url_token
            ):
                raise ValueError(
                    f"Zhihu identity changed for {identity.author_source_id}; "
                    "manual review required"
                )
            stored_identity = (
                identity.model_copy(
                    update={"verified_at": _parse_utc_text(str(row["verified_at"]))}
                )
                if row is not None
                else identity
            )
            identity_json = canonical_json_bytes(
                stored_identity.model_dump(mode="json")
            ).decode("utf-8")
            connection.execute(
                "INSERT INTO knowledge_source_identity("
                "source_id,platform_user_id,url_token,display_name,profile_url,identity_status,"
                "profile_snapshot_id,profile_object_hash,identity_json,verified_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "display_name=excluded.display_name,profile_url=excluded.profile_url,"
                "identity_status=excluded.identity_status,"
                "profile_snapshot_id=excluded.profile_snapshot_id,"
                "profile_object_hash=excluded.profile_object_hash,"
                "identity_json=excluded.identity_json,"
                "updated_at=excluded.updated_at",
                (
                    identity.author_source_id,
                    identity.platform_user_id,
                    identity.url_token,
                    identity.display_name,
                    identity.profile_url,
                    identity.identity_status.value,
                    identity.profile_snapshot_id,
                    identity.profile_object_sha256,
                    identity_json,
                    _utc_text(stored_identity.verified_at),
                    now,
                ),
            )
        return stored_identity

    def get_identity(self, source_id: str) -> ZhihuAuthorIdentity | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT identity_json FROM knowledge_source_identity WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return ZhihuAuthorIdentity.model_validate_json(row["identity_json"]) if row else None

    def register_imported_response(
        self, record: ZhihuImportedResponse
    ) -> ZhihuImportedResponse:
        record_json = canonical_json_bytes(record.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM zhihu_imported_response WHERE envelope_id=?",
                (record.envelope_id,),
            ).fetchone()
            if row is not None:
                existing = ZhihuImportedResponse.model_validate_json(row["record_json"])
                if content_hash(_import_semantic(existing)) != content_hash(
                    _import_semantic(record)
                ):
                    raise ValueError(f"Zhihu response envelope collision: {record.envelope_id}")
                return existing
            connection.execute(
                "INSERT INTO zhihu_imported_response("
                "envelope_id,source_id,response_kind,content_type,content_id,"
                "parent_comment_id,listing_page,comment_page,request_cursor,requested_url,"
                "http_status,response_mime,transport,"
                "source_snapshot_id,raw_object_hash,body_byte_size,import_status,"
                "captured_at,imported_at,consumed_at,record_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.envelope_id,
                    record.author_source_id,
                    record.response_kind.value,
                    record.content_type.value if record.content_type else None,
                    record.content_id,
                    record.parent_comment_id,
                    record.listing_page,
                    record.comment_page,
                    record.request_cursor,
                    record.requested_url,
                    record.status_code,
                    record.response_mime,
                    record.transport.value,
                    record.source_snapshot_id,
                    record.raw_object_sha256,
                    record.body_byte_size,
                    record.import_status.value,
                    _utc_text(record.captured_at),
                    _utc_text(record.imported_at),
                    _utc_text(record.consumed_at) if record.consumed_at else None,
                    record_json,
                ),
            )
        return record

    def get_imported_response(self, envelope_id: str) -> ZhihuImportedResponse | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM zhihu_imported_response WHERE envelope_id=?",
                (envelope_id,),
            ).fetchone()
        return ZhihuImportedResponse.model_validate_json(row["record_json"]) if row else None

    def pending_import_count(
        self,
        source_id: str | None = None,
        *,
        response_kinds: tuple[ZhihuResponseKind, ...] | None = None,
        data_cutoff_at: datetime | None = None,
    ) -> int:
        query = (
            "SELECT imported_at,consumed_at,import_status FROM zhihu_imported_response "
            "WHERE 1=1"
        )
        parameters: list[str] = []
        if source_id is not None:
            query += " AND source_id=?"
            parameters.append(source_id)
        if response_kinds is not None:
            if not response_kinds:
                raise ValueError("response_kinds must not be empty")
            placeholders = ",".join("?" for _ in response_kinds)
            query += f" AND response_kind IN ({placeholders})"
            parameters.extend(kind.value for kind in response_kinds)
        with self.state.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        if data_cutoff_at is None:
            return sum(str(row["import_status"]) == "PENDING" for row in rows)
        cutoff = data_cutoff_at.astimezone(UTC)
        return sum(
            _parse_utc_text(str(row["imported_at"])) <= cutoff
            and (
                str(row["import_status"]) == "PENDING"
                or (
                    str(row["import_status"]) == "CONSUMED"
                    and row["consumed_at"] is not None
                    and _parse_utc_text(str(row["consumed_at"])) > cutoff
                )
            )
            for row in rows
        )

    def rejected_import_temporal_count(
        self,
        source_id: str,
        *,
        response_kinds: tuple[ZhihuResponseKind, ...],
        data_cutoff_at: datetime,
    ) -> int:
        """Count rejected rows whose transition time cannot be reconstructed."""

        if not response_kinds:
            raise ValueError("response_kinds must not be empty")
        placeholders = ",".join("?" for _ in response_kinds)
        parameters = [source_id, *(kind.value for kind in response_kinds)]
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT imported_at FROM zhihu_imported_response "
                "WHERE source_id=? AND import_status='REJECTED' "
                f"AND response_kind IN ({placeholders})",
                parameters,
            ).fetchall()
        cutoff = data_cutoff_at.astimezone(UTC)
        return sum(_parse_utc_text(str(row["imported_at"])) <= cutoff for row in rows)

    def list_pending_imports(
        self,
        source_id: str,
        *,
        limit: int = 100,
    ) -> list[ZhihuImportedResponse]:
        if not 1 <= limit <= 10_000:
            raise ValueError("pending import limit must be between 1 and 10000")
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_imported_response "
                "WHERE source_id=? AND import_status='PENDING' "
                "ORDER BY captured_at,envelope_id LIMIT ?",
                (source_id, limit),
            ).fetchall()
        return [
            ZhihuImportedResponse.model_validate_json(row["record_json"])
            for row in rows
        ]

    def mark_import_consumed(
        self, envelope_id: str, consumed_at: datetime
    ) -> ZhihuImportedResponse:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM zhihu_imported_response WHERE envelope_id=?",
                (envelope_id,),
            ).fetchone()
            if row is None:
                raise KeyError(envelope_id)
            existing = ZhihuImportedResponse.model_validate_json(row["record_json"])
            if existing.import_status is ZhihuImportStatus.CONSUMED:
                return existing
            consumed = existing.model_copy(
                update={
                    "import_status": ZhihuImportStatus.CONSUMED,
                    "consumed_at": consumed_at,
                }
            )
            record_json = canonical_json_bytes(consumed.model_dump(mode="json")).decode(
                "utf-8"
            )
            connection.execute(
                "UPDATE zhihu_imported_response SET import_status=?,consumed_at=?,"
                "record_json=? WHERE envelope_id=?",
                (
                    consumed.import_status.value,
                    _utc_text(consumed_at),
                    record_json,
                    envelope_id,
                ),
            )
        return consumed

    def register_listing_page(self, page: ZhihuListingPage) -> ZhihuListingPage:
        page_json = canonical_json_bytes(page.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT page_json FROM zhihu_listing_page_manifest WHERE page_id=?",
                (page.page_id,),
            ).fetchone()
            if row is not None:
                existing = ZhihuListingPage.model_validate_json(row["page_json"])
                if content_hash(_listing_semantic(existing)) != content_hash(
                    _listing_semantic(page)
                ):
                    raise ValueError(f"Zhihu listing page collision: {page.page_id}")
                return existing
            connection.execute(
                "INSERT INTO zhihu_listing_page_manifest("
                "page_id,source_id,content_type,listing_page,request_url,request_cursor,"
                "next_cursor,is_end,content_count,source_snapshot_id,raw_object_hash,transport,"
                "http_status,structure_version,page_json,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    page.page_id,
                    page.author_source_id,
                    page.content_type.value,
                    page.listing_page,
                    page.request_url,
                    page.request_cursor,
                    page.next_cursor,
                    int(page.is_end),
                    len(page.content_ids),
                    page.source_snapshot_id,
                    page.raw_object_sha256,
                    page.transport.value,
                    page.http_status,
                    page.response_structure_version,
                    page_json,
                    _utc_text(page.fetched_at),
                ),
            )
        return page

    def register_comment_page(self, page: ZhihuCommentPage) -> ZhihuCommentPage:
        page_json = canonical_json_bytes(page.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT page_json FROM zhihu_comment_page_manifest WHERE page_id=?",
                (page.page_id,),
            ).fetchone()
            if row is not None:
                existing = ZhihuCommentPage.model_validate_json(row["page_json"])
                if content_hash(_comment_page_semantic(existing)) != content_hash(
                    _comment_page_semantic(page)
                ):
                    raise ValueError(f"Zhihu comment page collision: {page.page_id}")
                return existing
            connection.execute(
                "INSERT INTO zhihu_comment_page_manifest("
                "page_id,source_id,content_type,content_id,parent_comment_id,comment_page,"
                "request_url,request_cursor,next_cursor,is_end,comment_count,"
                "source_snapshot_id,raw_object_hash,transport,http_status,structure_version,"
                "page_json,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    page.page_id,
                    page.author_source_id,
                    page.content_type.value,
                    page.content_id,
                    page.parent_comment_id,
                    page.comment_page,
                    page.request_url,
                    page.request_cursor,
                    page.next_cursor,
                    int(page.is_end),
                    len(page.comment_ids),
                    page.source_snapshot_id,
                    page.raw_object_sha256,
                    page.transport.value,
                    page.http_status,
                    page.response_structure_version,
                    page_json,
                    _utc_text(page.fetched_at),
                ),
            )
        return page

    def get_comment_page(self, page_id: str) -> ZhihuCommentPage | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT page_json FROM zhihu_comment_page_manifest WHERE page_id=?",
                (page_id,),
            ).fetchone()
        return (
            ZhihuCommentPage.model_validate_json(row["page_json"])
            if row is not None
            else None
        )

    def register_comment(self, record: ZhihuCommentNode) -> CommentRegistration:
        with self.state.transaction() as connection:
            same = connection.execute(
                "SELECT record_json FROM zhihu_comment_version WHERE version_id=?",
                (record.version_id,),
            ).fetchone()
            if same is not None:
                existing = ZhihuCommentNode.model_validate_json(same["record_json"])
                if content_hash(_comment_semantic(existing)) != content_hash(
                    _comment_semantic(record)
                ):
                    raise ValueError(f"Zhihu comment version collision: {record.version_id}")
                return CommentRegistration("DUPLICATE", existing)
            latest_row = connection.execute(
                "SELECT record_json FROM zhihu_comment_version "
                "WHERE source_id=? AND content_type=? AND content_id=? AND comment_id=? "
                "ORDER BY collected_at DESC,version_id DESC LIMIT 1",
                (
                    record.author_source_id,
                    record.content_type.value,
                    record.content_id,
                    record.comment_id,
                ),
            ).fetchone()
            latest = (
                ZhihuCommentNode.model_validate_json(latest_row["record_json"])
                if latest_row
                else None
            )
            stored = record.model_copy(
                update={"previous_version_id": latest.version_id if latest else None}
            )
            record_json = canonical_json_bytes(stored.model_dump(mode="json")).decode(
                "utf-8"
            )
            connection.execute(
                "INSERT INTO zhihu_comment_version("
                "version_id,source_id,content_type,content_id,comment_id,root_comment_id,"
                "parent_comment_id,reply_to_comment_id,platform_author_id,published_at,"
                "updated_at,collected_at,body_object_hash,metadata_hash,"
                "raw_source_snapshot_id,previous_version_id,record_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stored.version_id,
                    stored.author_source_id,
                    stored.content_type.value,
                    stored.content_id,
                    stored.comment_id,
                    stored.root_comment_id,
                    stored.parent_comment_id,
                    stored.reply_to_comment_id,
                    stored.platform_author_id,
                    _utc_text(stored.published_at) if stored.published_at else None,
                    _utc_text(stored.updated_at) if stored.updated_at else None,
                    _utc_text(stored.collected_at),
                    stored.body_object_sha256,
                    stored.metadata_sha256,
                    stored.raw_source_snapshot_id,
                    stored.previous_version_id,
                    record_json,
                ),
            )
        return CommentRegistration("UPDATED" if latest else "NEW", stored)

    def latest_comments(
        self,
        source_id: str,
        content_type: ZhihuContentType,
        content_id: str,
    ) -> list[ZhihuCommentNode]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_comment_version "
                "WHERE source_id=? AND content_type=? AND content_id=? "
                "ORDER BY collected_at DESC,version_id DESC",
                (source_id, content_type.value, content_id),
            ).fetchall()
        latest: dict[str, ZhihuCommentNode] = {}
        for row in rows:
            record = ZhihuCommentNode.model_validate_json(row["record_json"])
            latest.setdefault(record.comment_id, record)
        return sorted(
            latest.values(),
            key=lambda item: (
                item.published_at or item.collected_at,
                item.comment_id,
            ),
        )

    def register_participation_chain(
        self,
        chain: ZhihuAuthorParticipationChain,
        *,
        object_hash: str,
    ) -> ZhihuAuthorParticipationChain:
        chain_json = canonical_json_bytes(chain.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT chain_json,chain_object_hash FROM zhihu_author_participation_chain "
                "WHERE chain_id=?",
                (chain.chain_id,),
            ).fetchone()
            if row is not None:
                if str(row["chain_json"]) != chain_json or str(
                    row["chain_object_hash"]
                ) != object_hash:
                    raise ValueError(f"Zhihu participation chain collision: {chain.chain_id}")
                return chain
            connection.execute(
                "INSERT INTO zhihu_author_participation_chain("
                "chain_id,source_id,content_type,content_id,root_comment_id,"
                "selection_rule_version,chain_object_hash,chain_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    chain.chain_id,
                    chain.author_source_id,
                    chain.content_type.value,
                    chain.content_id,
                    chain.root_comment_id,
                    chain.selection_rule_version,
                    object_hash,
                    chain_json,
                    _utc_text(chain.created_at),
                ),
            )
        return chain

    def register_content(self, record: ZhihuContentRecord) -> ContentRegistration:
        with self.state.transaction() as connection:
            same = connection.execute(
                "SELECT record_json FROM zhihu_content_version WHERE version_id=?",
                (record.version_id,),
            ).fetchone()
            if same is not None:
                existing = ZhihuContentRecord.model_validate_json(same["record_json"])
                if content_hash(_content_semantic(existing)) != content_hash(
                    _content_semantic(record)
                ):
                    raise ValueError(f"Zhihu content version collision: {record.version_id}")
                return ContentRegistration("DUPLICATE", existing)
            latest_row = connection.execute(
                "SELECT record_json FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=? AND content_id=? "
                "ORDER BY collected_at DESC,version_id DESC LIMIT 1",
                (record.author_source_id, record.content_type.value, record.content_id),
            ).fetchone()
            latest = (
                ZhihuContentRecord.model_validate_json(latest_row["record_json"])
                if latest_row
                else None
            )
            stored = record.model_copy(
                update={"previous_version_id": latest.version_id if latest is not None else None}
            )
            record_json = canonical_json_bytes(stored.model_dump(mode="json")).decode("utf-8")
            connection.execute(
                "INSERT INTO zhihu_content_version("
                "version_id,source_id,content_id,content_type,canonical_url,published_at,"
                "updated_at,collected_at,body_object_hash,metadata_hash,raw_source_snapshot_id,"
                "content_completeness,previous_version_id,record_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stored.version_id,
                    stored.author_source_id,
                    stored.content_id,
                    stored.content_type.value,
                    stored.canonical_url,
                    _utc_text(stored.published_at) if stored.published_at else None,
                    _utc_text(stored.updated_at) if stored.updated_at else None,
                    _utc_text(stored.collected_at),
                    stored.body_object_sha256,
                    stored.metadata_sha256,
                    stored.raw_source_snapshot_id,
                    stored.content_completeness.value,
                    stored.previous_version_id,
                    record_json,
                ),
            )
        return ContentRegistration("UPDATED" if latest is not None else "NEW", stored)

    def upsert_collection_scope(
        self,
        *,
        source_id: str,
        content_type: ZhihuContentType,
        status: str,
        last_cursor: str | None,
        terminal_condition: CollectionTerminalCondition | None,
    ) -> str:
        scope_id = content_hash(
            {"author_id": source_id, "content_type": content_type.value}
        )
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO collection_scope("
                "scope_id,author_id,content_type,status,last_cursor,terminal_condition) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(author_id,content_type) DO UPDATE SET "
                "status=excluded.status,last_cursor=excluded.last_cursor,"
                "terminal_condition=excluded.terminal_condition",
                (
                    scope_id,
                    source_id,
                    content_type.value,
                    status,
                    last_cursor,
                    terminal_condition.value if terminal_condition else None,
                ),
            )
        return scope_id

    def record_gap(self, scope_id: str, gap: ZhihuCollectionGap) -> None:
        cursor_json = canonical_json_bytes(
            {
                "listing_page": gap.listing_page,
                "listing_cursor": gap.listing_cursor,
                "source_snapshot_id": gap.source_snapshot_id,
            }
        ).decode("utf-8")
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO collection_gap("
                "gap_id,scope_id,cursor_json,failure_class,retryable,status) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(gap_id) DO UPDATE SET "
                "failure_class=excluded.failure_class,retryable=excluded.retryable,"
                "status=excluded.status",
                (
                    gap.gap_id,
                    scope_id,
                    cursor_json,
                    gap.failure_class,
                    int(gap.retryable),
                    gap.status,
                ),
            )

    def resolve_open_listing_gaps(
        self,
        *,
        source_id: str,
        content_type: ZhihuContentType,
        listing_page: int,
        listing_cursor: str | None,
    ) -> int:
        scope_id = content_hash(
            {"author_id": source_id, "content_type": content_type.value}
        )
        resolved: list[str] = []
        with self.state.transaction() as connection:
            rows = connection.execute(
                "SELECT gap_id,cursor_json FROM collection_gap "
                "WHERE scope_id=? AND status='OPEN'",
                (scope_id,),
            ).fetchall()
            for row in rows:
                cursor = json.loads(str(row["cursor_json"]))
                if (
                    cursor.get("listing_page") == listing_page
                    and cursor.get("listing_cursor") == listing_cursor
                ):
                    resolved.append(str(row["gap_id"]))
            if resolved:
                connection.executemany(
                    "UPDATE collection_gap SET status='RESOLVED' WHERE gap_id=?",
                    [(gap_id,) for gap_id in resolved],
                )
        return len(resolved)

    def register_coverage_report(
        self,
        report: AuthorCollectionCoverageReport,
        *,
        object_hash: str,
    ) -> None:
        if report.report_id is None:
            raise ValueError("coverage reports require a stable report_id")
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT report_json,report_object_hash FROM knowledge_coverage_report "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or str(row["report_object_hash"]) != object_hash
                ):
                    raise ValueError(f"coverage report collision: {report.report_id}")
                return
            connection.execute(
                "INSERT INTO knowledge_coverage_report("
                "report_id,source_id,content_type,terminal_condition,coverage_status,"
                "report_object_hash,report_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.author_id,
                    report.content_type,
                    report.terminal_condition.value,
                    report.coverage_status.value,
                    object_hash,
                    report_json,
                    _utc_text(report.created_at),
                ),
            )

    def latest_coverage_report(
        self,
        source_id: str,
        content_type: ZhihuContentType,
        *,
        data_cutoff_at: datetime | None = None,
    ) -> AuthorCollectionCoverageReport | None:
        query = (
            "SELECT report_id,report_json,created_at FROM knowledge_coverage_report "
            "WHERE source_id=? AND content_type=?"
        )
        parameters = [source_id, content_type.value]
        with self.state.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        if data_cutoff_at is not None:
            cutoff = data_cutoff_at.astimezone(UTC)
            rows = [
                row
                for row in rows
                if _parse_utc_text(str(row["created_at"])) <= cutoff
            ]
        row = max(
            rows,
            key=lambda item: (
                _parse_utc_text(str(item["created_at"])),
                str(item["report_id"]),
            ),
            default=None,
        )
        return (
            AuthorCollectionCoverageReport.model_validate_json(row["report_json"])
            if row
            else None
        )

    def content_version_count(self, source_id: str, content_type: ZhihuContentType) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM zhihu_content_version WHERE source_id=? AND content_type=?",
                (source_id, content_type.value),
            ).fetchone()
        return int(row[0]) if row else 0
    def latest_content_records(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> list[ZhihuContentRecord]:
        """Return exactly one deterministic latest version for every discovered content id."""

        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM ("
                "SELECT record_json,content_id,ROW_NUMBER() OVER ("
                "PARTITION BY content_id ORDER BY collected_at DESC,version_id DESC"
                ") AS latest_rank FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=?"
                ") WHERE latest_rank=1 ORDER BY content_id",
                (source_id, content_type.value),
            ).fetchall()
        return [ZhihuContentRecord.model_validate_json(row["record_json"]) for row in rows]

    def latest_detail_content_records(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> list[ZhihuContentRecord]:
        """Return the latest verified full-body version without listing regressions."""

        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM ("
                "SELECT record_json,content_id,ROW_NUMBER() OVER ("
                "PARTITION BY content_id ORDER BY collected_at DESC,version_id DESC"
                ") AS latest_rank FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=? "
                "AND content_completeness='DETAIL_VERIFIED'"
                ") WHERE latest_rank=1 ORDER BY content_id",
                (source_id, content_type.value),
            ).fetchall()
        return [ZhihuContentRecord.model_validate_json(row["record_json"]) for row in rows]

    def register_local_coverage_report(
        self,
        report: KnowledgeLocalCoverageReport,
        *,
        object_hash: str,
    ) -> None:
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT report_json,report_object_hash FROM knowledge_local_coverage_report "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or str(row["report_object_hash"]) != object_hash
                ):
                    raise ValueError(f"local coverage report collision: {report.report_id}")
                return
            connection.execute(
                "INSERT INTO knowledge_local_coverage_report("
                "report_id,source_id,seed_source_id,status,report_object_hash,report_json,"
                "audited_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.author_source_id,
                    report.seed_source_id,
                    report.status.value,
                    object_hash,
                    report_json,
                    report.audited_at.isoformat(),
                    report.created_at.isoformat(),
                ),
            )

    def register_coverage_audit_report(
        self,
        report: KnowledgeCoverageAuditReport,
        *,
        object_hash: str,
    ) -> None:
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT report_json,report_object_hash FROM knowledge_coverage_audit_report "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or str(row["report_object_hash"]) != object_hash
                ):
                    raise ValueError(f"coverage audit report collision: {report.report_id}")
                return
            connection.execute(
                "INSERT INTO knowledge_coverage_audit_report("
                "report_id,status,report_object_hash,report_json,audited_at,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.status.value,
                    object_hash,
                    report_json,
                    report.audited_at.isoformat(),
                    report.created_at.isoformat(),
                ),
            )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_utc_text(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _listing_semantic(page: ZhihuListingPage) -> dict[str, object]:
    return page.model_dump(mode="json", exclude={"created_at", "fetched_at"})


def _content_semantic(record: ZhihuContentRecord) -> dict[str, object]:
    return record.model_dump(
        mode="json",
        exclude={
            "created_at",
            "collected_at",
            "previous_version_id",
            "raw_source_snapshot_id",
        },
    )


def _comment_page_semantic(page: ZhihuCommentPage) -> dict[str, object]:
    return page.model_dump(mode="json", exclude={"created_at", "fetched_at"})


def _comment_semantic(record: ZhihuCommentNode) -> dict[str, object]:
    return record.model_dump(
        mode="json",
        exclude={
            "created_at",
            "collected_at",
            "previous_version_id",
            "raw_source_snapshot_id",
        },
    )


def _import_semantic(record: ZhihuImportedResponse) -> dict[str, object]:
    return {
        "envelope_id": record.envelope_id,
        "author_source_id": record.author_source_id,
        "response_kind": record.response_kind.value,
        "content_type": record.content_type.value if record.content_type else None,
        "content_id": record.content_id,
        "parent_comment_id": record.parent_comment_id,
        "listing_page": record.listing_page,
        "comment_page": record.comment_page,
        "request_cursor": record.request_cursor,
        "requested_url": record.requested_url,
        "status_code": record.status_code,
        "response_mime": record.response_mime,
        "transport": record.transport.value,
        "source_snapshot_id": record.source_snapshot_id,
        "raw_object_sha256": record.raw_object_sha256,
        "body_byte_size": record.body_byte_size,
    }


__all__ = [
    "CommentRegistration",
    "ContentRegistration",
    "KnowledgeRepository",
]
