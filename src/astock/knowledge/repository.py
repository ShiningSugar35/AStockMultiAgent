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
    ZhihuAuthorIdentity,
    ZhihuCollectionGap,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
)


@dataclass(frozen=True, slots=True)
class ContentRegistration:
    status: str
    record: ZhihuContentRecord


class KnowledgeRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register_identity(self, identity: ZhihuAuthorIdentity) -> ZhihuAuthorIdentity:
        identity_json = canonical_json_bytes(identity.model_dump(mode="json")).decode("utf-8")
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT platform_user_id,url_token FROM knowledge_source_identity "
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
            connection.execute(
                "INSERT INTO knowledge_source_identity("
                "source_id,platform_user_id,url_token,display_name,profile_url,identity_status,"
                "profile_snapshot_id,profile_object_hash,identity_json,verified_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "display_name=excluded.display_name,profile_url=excluded.profile_url,"
                "identity_status=excluded.identity_status,"
                "profile_snapshot_id=excluded.profile_snapshot_id,"
                "profile_object_hash=excluded.profile_object_hash,"
                "identity_json=excluded.identity_json,verified_at=excluded.verified_at,"
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
                    identity.verified_at.isoformat(),
                    now,
                ),
            )
        return identity

    def get_identity(self, source_id: str) -> ZhihuAuthorIdentity | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT identity_json FROM knowledge_source_identity WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return ZhihuAuthorIdentity.model_validate_json(row["identity_json"]) if row else None

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
                    page.fetched_at.isoformat(),
                ),
            )
        return page

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
                "previous_version_id,record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stored.version_id,
                    stored.author_source_id,
                    stored.content_id,
                    stored.content_type.value,
                    stored.canonical_url,
                    stored.published_at.isoformat() if stored.published_at else None,
                    stored.updated_at.isoformat() if stored.updated_at else None,
                    stored.collected_at.isoformat(),
                    stored.body_object_sha256,
                    stored.metadata_sha256,
                    stored.raw_source_snapshot_id,
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
                    report.created_at.isoformat(),
                ),
            )

    def latest_coverage_report(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> AuthorCollectionCoverageReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM knowledge_coverage_report "
                "WHERE source_id=? AND content_type=? "
                "ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (source_id, content_type.value),
            ).fetchone()
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


__all__ = [
    "ContentRegistration",
    "KnowledgeRepository",
]
