"""Deterministic, text-free manual recovery tasks for incomplete Zhihu coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import NamedTuple

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completeness import child_reply_count_mismatches
from astock.schemas import (
    CollectionTerminalCondition,
    KnowledgeSourceRegistry,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContainerType,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
    ZhihuManualCollectionTask,
    ZhihuManualTaskStatus,
    ZhihuResponseKind,
)

_SUCCESS_TERMINALS = {
    CollectionTerminalCondition.PAGINATION_COMPLETE,
    CollectionTerminalCondition.CONFIRMED_EMPTY,
}


class _GapContext(NamedTuple):
    failure_class: str
    last_cursor: str | None
    source_snapshot_id: str | None


class ZhihuManualTaskService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.state = state
        self.object_store = object_store

    def refresh(self, registry: KnowledgeSourceRegistry) -> list[ZhihuManualCollectionTask]:
        now = datetime.now(UTC)
        expected: list[ZhihuManualCollectionTask] = []
        for source in registry.sources:
            if not source.enabled or not source.online_collection_required:
                continue
            assert source.profile_url is not None
            if ZhihuContainerType.COLUMNS in source.collection_scope.container_types:
                expected.append(
                    self._task(
                        now,
                        source_id=source.source_id,
                        content_type=ZhihuContainerType.COLUMNS.value,
                        response_kind="COLUMN_LISTING",
                        public_url=source.profile_url,
                        failure_class="COLUMN_ENUMERATION_NOT_VERIFIED",
                        required_action=(
                            "Observe and freeze the author's complete column enumeration and "
                            "one column-to-article enumeration before implementing an endpoint."
                        ),
                    )
                )
            for raw_type in source.collection_scope.content_types:
                content_type = ZhihuContentType(raw_type)
                expected.extend(
                    self._scope_tasks(
                        now,
                        source.source_id,
                        source.profile_url,
                        content_type,
                    )
                )
        active = {task.task_id: task for task in expected}
        self._persist(active, now)
        return self.list_open()

    def list_open(self) -> list[ZhihuManualCollectionTask]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT task_json FROM zhihu_manual_collection_task "
                "WHERE status='OPEN' ORDER BY source_id,content_type,response_kind,"
                "content_id,parent_comment_id,task_id"
            ).fetchall()
        return [ZhihuManualCollectionTask.model_validate_json(row["task_json"]) for row in rows]

    def _scope_tasks(
        self,
        now: datetime,
        source_id: str,
        profile_url: str,
        content_type: ZhihuContentType,
    ) -> list[ZhihuManualCollectionTask]:
        """Build active listing/detail recovery tasks for one configured content type."""

        tasks: list[ZhihuManualCollectionTask] = []
        checkpoint = self.state.get_collection_checkpoint(source_id, content_type.value)
        if checkpoint is None or checkpoint.terminal_condition not in _SUCCESS_TERMINALS:
            gap = self._gap_context(
                source_id,
                content_type.value,
                default_failure="LISTING_NOT_COMPLETE",
                default_cursor=checkpoint.listing_cursor if checkpoint else None,
            )
            tasks.append(
                self._task(
                    now,
                    source_id=source_id,
                    content_type=content_type.value,
                    response_kind=ZhihuResponseKind.LISTING.value,
                    public_url=profile_url,
                    last_cursor=gap.last_cursor,
                    failure_class=gap.failure_class,
                    source_snapshot_id=gap.source_snapshot_id,
                    required_action=(
                        "Export every remaining listing page until Zhihu explicitly reports "
                        "the terminal page."
                    ),
                )
            )
        else:
            total_task = self._listing_total_task(
                now,
                source_id,
                profile_url,
                content_type,
            )
            if total_task is not None:
                tasks.append(total_task)

        content_records = self._content_records(source_id, content_type)
        by_content: dict[str, list[ZhihuContentRecord]] = {}
        for record in content_records:
            by_content.setdefault(record.content_id, []).append(record)
        for content_id, records in sorted(by_content.items()):
            listing = [
                item
                for item in records
                if item.content_completeness is ZhihuContentCompleteness.LISTING_UNVERIFIED
            ]
            details = [
                item
                for item in records
                if item.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
            ]
            latest_listing = max(listing, key=_freshness) if listing else None
            latest_detail = max(details, key=_freshness) if details else None
            canonical_record = latest_listing or latest_detail
            assert canonical_record is not None
            if latest_detail is not None and (
                latest_listing is None
                or _freshness(latest_detail) >= _freshness(latest_listing)
            ):
                continue
            detail_gap = self._gap_context(
                source_id,
                f"detail:{content_type.value}:{content_id}",
                default_failure=("DETAIL_STALE" if latest_detail else "DETAIL_MISSING"),
            )
            tasks.append(
                self._task(
                    now,
                    source_id=source_id,
                    content_type=content_type.value,
                    response_kind=ZhihuResponseKind.CONTENT_DETAIL.value,
                    content_id=content_id,
                    public_url=canonical_record.canonical_url,
                    last_cursor=detail_gap.last_cursor,
                    failure_class=detail_gap.failure_class,
                    source_snapshot_id=detail_gap.source_snapshot_id,
                    required_action=(
                        "Export the full detail response; a listing excerpt or collapsed "
                        "visible text is not sufficient."
                    ),
                )
            )
        return tasks

    def _legacy_interaction_scope_tasks(
        self,
        now: datetime,
        source_id: str,
        profile_url: str,
        content_type: ZhihuContentType,
    ) -> list[ZhihuManualCollectionTask]:
        tasks: list[ZhihuManualCollectionTask] = []
        checkpoint = self.state.get_collection_checkpoint(source_id, content_type.value)
        if checkpoint is None or checkpoint.terminal_condition not in _SUCCESS_TERMINALS:
            gap = self._gap_context(
                source_id,
                content_type.value,
                default_failure="LISTING_NOT_COMPLETE",
                default_cursor=checkpoint.listing_cursor if checkpoint else None,
            )
            tasks.append(
                self._task(
                    now,
                    source_id=source_id,
                    content_type=content_type.value,
                    response_kind=ZhihuResponseKind.LISTING.value,
                    public_url=profile_url,
                    last_cursor=gap.last_cursor,
                    failure_class=gap.failure_class,
                    source_snapshot_id=gap.source_snapshot_id,
                    required_action=(
                        "Export every remaining listing page until Zhihu explicitly reports "
                        "the terminal page."
                    ),
                )
            )
        else:
            total_task = self._listing_total_task(
                now,
                source_id,
                profile_url,
                content_type,
            )
            if total_task is not None:
                tasks.append(total_task)
        content_records, comment_records = self._records(source_id, content_type)
        by_content: dict[str, list[ZhihuContentRecord]] = {}
        for record in content_records:
            by_content.setdefault(record.content_id, []).append(record)
        latest_comments: dict[tuple[str, str], ZhihuCommentNode] = {}
        for comment in comment_records:
            latest_comments.setdefault((comment.content_id, comment.comment_id), comment)
        count_mismatches = child_reply_count_mismatches(latest_comments.values())
        for content_id, records in sorted(by_content.items()):
            listing = [
                item
                for item in records
                if item.content_completeness is ZhihuContentCompleteness.LISTING_UNVERIFIED
            ]
            details = [
                item
                for item in records
                if item.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
            ]
            latest_listing = max(listing, key=_freshness) if listing else None
            latest_detail = max(details, key=_freshness) if details else None
            canonical_record = latest_listing or latest_detail
            assert canonical_record is not None
            canonical = canonical_record.canonical_url
            if latest_detail is None or (
                latest_listing is not None
                and _freshness(latest_detail) < _freshness(latest_listing)
            ):
                detail_gap = self._gap_context(
                    source_id,
                    f"detail:{content_type.value}:{content_id}",
                    default_failure=(
                        "DETAIL_STALE" if latest_detail is not None else "DETAIL_MISSING"
                    ),
                )
                tasks.append(
                    self._task(
                        now,
                        source_id=source_id,
                        content_type=content_type.value,
                        response_kind=ZhihuResponseKind.CONTENT_DETAIL.value,
                        content_id=content_id,
                        public_url=canonical,
                        last_cursor=detail_gap.last_cursor,
                        failure_class=detail_gap.failure_class,
                        source_snapshot_id=detail_gap.source_snapshot_id,
                        required_action=(
                            "Export the full detail response; a listing excerpt or collapsed "
                            "visible text is not sufficient."
                        ),
                    )
                )
            root = self.state.get_collection_checkpoint(
                source_id,
                content_type.value,
                content_id,
            )
            if root is None or root.terminal_condition not in _SUCCESS_TERMINALS:
                root_gap = self._gap_context(
                    source_id,
                    f"comments:{content_type.value}:{content_id}:__root__",
                    default_failure="ROOT_COMMENTS_NOT_COMPLETE",
                    default_cursor=root.comment_cursor if root else None,
                )
                tasks.append(
                    self._task(
                        now,
                        source_id=source_id,
                        content_type=content_type.value,
                        response_kind=ZhihuResponseKind.ROOT_COMMENTS.value,
                        content_id=content_id,
                        public_url=canonical,
                        last_cursor=root_gap.last_cursor,
                        failure_class=root_gap.failure_class,
                        source_snapshot_id=root_gap.source_snapshot_id,
                        required_action=(
                            "Export all root-comment pages until the comment API explicitly "
                            "reports its terminal page."
                        ),
                    )
                )
            child_roots = [
                comment
                for comment in latest_comments.values()
                if comment.content_id == content_id
                and comment.parent_comment_id is None
                and comment.child_comment_count > 0
            ]
            children_complete = True
            for comment in sorted(child_roots, key=lambda item: item.comment_id):
                child = self.state.get_collection_checkpoint(
                    source_id,
                    content_type.value,
                    content_id,
                    comment.comment_id,
                )
                mismatch = count_mismatches.get((content_id, comment.comment_id))
                child_is_terminal = (
                    child is not None
                    and child.terminal_condition in _SUCCESS_TERMINALS
                )
                if child_is_terminal and mismatch is None:
                    continue
                children_complete = False
                child_gap = self._gap_context(
                    source_id,
                    (f"comments:{content_type.value}:{content_id}:{comment.comment_id}"),
                    default_failure=(
                        "CHILD_REPLY_COUNT_MISMATCH"
                        if child_is_terminal and mismatch is not None
                        else "CHILD_REPLIES_NOT_COMPLETE"
                    ),
                    default_cursor=child.nested_reply_cursor if child else None,
                )
                tasks.append(
                    self._task(
                        now,
                        source_id=source_id,
                        content_type=content_type.value,
                        response_kind=ZhihuResponseKind.CHILD_COMMENTS.value,
                        content_id=content_id,
                        parent_comment_id=comment.comment_id,
                        public_url=canonical,
                        last_cursor=child_gap.last_cursor,
                        failure_class=child_gap.failure_class,
                        source_snapshot_id=child_gap.source_snapshot_id,
                        expected_count=mismatch[0] if mismatch is not None else None,
                        collected_count=mismatch[1] if mismatch is not None else None,
                        required_action=(
                            "Expand and export every child-reply page for this root comment "
                            "until the reply API explicitly ends."
                        ),
                        )
                    )
            if root is not None and root.terminal_condition in _SUCCESS_TERMINALS:
                total_context = self._platform_comment_total_context(
                    source_id,
                    content_type,
                    content_id,
                    latest_comments,
                    children_complete=children_complete,
                )
                if total_context is not None:
                    (
                        expected_count,
                        collected_count,
                        request_url,
                        snapshot_id,
                        total_changed,
                    ) = total_context
                    tasks.append(
                        self._task(
                            now,
                            source_id=source_id,
                            content_type=content_type.value,
                            response_kind=ZhihuResponseKind.ROOT_COMMENTS.value,
                            content_id=content_id,
                            public_url=canonical,
                            last_cursor=request_url,
                            failure_class=(
                                "PLATFORM_COMMENT_TOTAL_CHANGED"
                                if total_changed
                                else "PLATFORM_COMMENT_TOTAL_MISMATCH"
                            ),
                            source_snapshot_id=snapshot_id,
                            expected_count=expected_count,
                            collected_count=collected_count,
                            required_action=(
                                "Zhihu's paging total covers root comments plus child replies. "
                                "All root and child scopes are terminal, but the unique comment "
                                "count still does not reconcile, or the total changed during "
                                "capture. Review this exact content manually; do not refetch only "
                                "the root pages or treat the terminal flag as zero omission."
                            ),
                        )
                    )
        return tasks

    def _platform_comment_total_context(
        self,
        source_id: str,
        content_type: ZhihuContentType,
        content_id: str,
        latest_comments: dict[tuple[str, str], ZhihuCommentNode],
        *,
        children_complete: bool,
    ) -> tuple[int, int, str, str, bool] | None:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT page_json FROM zhihu_comment_page_manifest "
                "WHERE source_id=? AND content_type=? AND content_id=? "
                "AND parent_comment_id IS NULL ORDER BY fetched_at,page_id",
                (source_id, content_type.value, content_id),
            ).fetchall()
        pages = [ZhihuCommentPage.model_validate_json(row["page_json"]) for row in rows]
        totals = [page.reported_total for page in pages if page.reported_total is not None]
        if not totals:
            return None
        expected = totals[-1]
        total_changed = len(set(totals)) > 1
        if not children_complete and not total_changed:
            return None
        collected = len(
            {
                comment.comment_id
                for comment in latest_comments.values()
                if comment.content_id == content_id
            }
        )
        if collected == expected and not total_changed:
            return None
        latest = pages[-1]
        return (
            expected,
            collected,
            latest.request_url,
            latest.source_snapshot_id,
            total_changed,
        )

    def _listing_total_task(
        self,
        now: datetime,
        source_id: str,
        profile_url: str,
        content_type: ZhihuContentType,
    ) -> ZhihuManualCollectionTask | None:
        """Expose platform-total mismatches instead of hiding unknown missing ids."""

        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT page_json,request_url,source_snapshot_id,raw_object_hash "
                "FROM zhihu_listing_page_manifest WHERE source_id=? AND content_type=? "
                "ORDER BY fetched_at,page_id",
                (source_id, content_type.value),
            ).fetchall()
        content_ids: set[str] = set()
        reported_totals: list[int] = []
        last_url: str | None = None
        last_snapshot: str | None = None
        for row in rows:
            page = ZhihuListingPage.model_validate_json(row["page_json"])
            content_ids.update(page.content_ids)
            reported_total = self._reported_total(page, str(row["raw_object_hash"]))
            if reported_total is not None:
                reported_totals.append(reported_total)
            last_url = str(row["request_url"])
            last_snapshot = str(row["source_snapshot_id"])
        if not reported_totals:
            return None
        reported_total = reported_totals[-1]
        total_changed = len(set(reported_totals)) > 1
        if len(content_ids) == reported_total and not total_changed:
            return None
        failure_class = (
            "LISTING_TOTAL_MISMATCH"
            if len(content_ids) != reported_total
            else "LISTING_REPORTED_TOTAL_CHANGED"
        )
        return self._task(
            now,
            source_id=source_id,
            content_type=content_type.value,
            response_kind=ZhihuResponseKind.LISTING.value,
            public_url=profile_url,
            last_cursor=last_url,
            failure_class=failure_class,
            source_snapshot_id=last_snapshot,
            expected_count=reported_total,
            collected_count=len(content_ids),
            required_action=(
                "Zhihu's own total does not reconcile with the unique ids returned by "
                "every terminal listing page. Re-export this complete tab and preserve "
                "the displayed total; ids that Zhihu never exposes must remain an explicit "
                "counted gap."
            ),
        )

    def _reported_total(
        self,
        page: ZhihuListingPage,
        raw_object_hash: str,
    ) -> int | None:
        if page.reported_total is not None:
            return page.reported_total
        if self.object_store is None:
            return None
        try:
            payload = json.loads(self.object_store.get_bytes(raw_object_hash))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("paging"), dict):
            return None
        reported_total = payload["paging"].get("totals")
        if (
            isinstance(reported_total, bool)
            or not isinstance(reported_total, int)
            or reported_total < 0
        ):
            return None
        return reported_total

    def _gap_context(
        self,
        source_id: str,
        scope_name: str,
        *,
        default_failure: str,
        default_cursor: str | None = None,
    ) -> _GapContext:
        """Return the newest raw failure evidence for one exact collection boundary."""

        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT s.last_cursor,g.failure_class,g.cursor_json "
                "FROM collection_scope s LEFT JOIN collection_gap g "
                "ON g.scope_id=s.scope_id AND g.status='OPEN' "
                "WHERE s.author_id=? AND s.content_type=? "
                "ORDER BY g.rowid DESC LIMIT 1",
                (source_id, scope_name),
            ).fetchone()
        if row is None:
            return _GapContext(default_failure, default_cursor, None)
        cursor_payload: dict[str, object] = {}
        if row["cursor_json"]:
            parsed = json.loads(str(row["cursor_json"]))
            if isinstance(parsed, dict):
                cursor_payload = parsed
        cursor = next(
            (
                str(cursor_payload[key])
                for key in (
                    "listing_cursor",
                    "comment_cursor",
                    "detail_url",
                )
                if cursor_payload.get(key) is not None
            ),
            str(row["last_cursor"]) if row["last_cursor"] is not None else default_cursor,
        )
        snapshot = cursor_payload.get("source_snapshot_id")
        return _GapContext(
            str(row["failure_class"] or default_failure),
            cursor,
            str(snapshot) if snapshot is not None else None,
        )

    def _records(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> tuple[list[ZhihuContentRecord], list[ZhihuCommentNode]]:
        with self.state.connect() as connection:
            content_rows = connection.execute(
                "SELECT record_json FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=?",
                (source_id, content_type.value),
            ).fetchall()
            comment_rows = connection.execute(
                "SELECT record_json FROM zhihu_comment_version "
                "WHERE source_id=? AND content_type=? "
                "ORDER BY collected_at DESC,version_id DESC",
                (source_id, content_type.value),
            ).fetchall()
        return (
            [ZhihuContentRecord.model_validate_json(row["record_json"]) for row in content_rows],
            [ZhihuCommentNode.model_validate_json(row["record_json"]) for row in comment_rows],
        )

    def _content_records(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> list[ZhihuContentRecord]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=?",
                (source_id, content_type.value),
            ).fetchall()
        return [ZhihuContentRecord.model_validate_json(row["record_json"]) for row in rows]

    @staticmethod
    def _task(
        now: datetime,
        *,
        source_id: str,
        content_type: str,
        response_kind: str,
        public_url: str,
        failure_class: str,
        required_action: str,
        content_id: str | None = None,
        parent_comment_id: str | None = None,
        last_cursor: str | None = None,
        source_snapshot_id: str | None = None,
        expected_count: int | None = None,
        collected_count: int | None = None,
    ) -> ZhihuManualCollectionTask:
        identity = {
            "source_id": source_id,
            "content_type": content_type,
            "response_kind": response_kind,
            "content_id": content_id,
            "parent_comment_id": parent_comment_id,
        }
        return ZhihuManualCollectionTask(
            task_id=f"zhihu-manual:{content_hash(identity)}",
            author_source_id=source_id,
            content_type=content_type,
            response_kind=response_kind,
            content_id=content_id,
            parent_comment_id=parent_comment_id,
            public_url=public_url,
            last_cursor=last_cursor,
            failure_class=failure_class,
            source_snapshot_id=source_snapshot_id,
            expected_count=expected_count,
            collected_count=collected_count,
            required_action=required_action,
            status=ZhihuManualTaskStatus.OPEN,
            created_at=now,
            updated_at=now,
        )

    def _persist(
        self,
        active: dict[str, ZhihuManualCollectionTask],
        now: datetime,
    ) -> None:
        with self.state.transaction() as connection:
            existing = {
                str(row["task_id"]): ZhihuManualCollectionTask.model_validate_json(row["task_json"])
                for row in connection.execute(
                    "SELECT task_id,task_json FROM zhihu_manual_collection_task"
                ).fetchall()
            }
            for task_id, task in active.items():
                previous = existing.get(task_id)
                if previous is not None and previous.status is ZhihuManualTaskStatus.OPEN:
                    comparable = task.model_copy(
                        update={
                            "created_at": previous.created_at,
                            "updated_at": previous.updated_at,
                        }
                    )
                    if comparable == previous:
                        continue
                stored = task.model_copy(
                    update={
                        "created_at": previous.created_at if previous else task.created_at,
                        "updated_at": now,
                        "status": ZhihuManualTaskStatus.OPEN,
                    }
                )
                payload = canonical_json_bytes(stored.model_dump(mode="json")).decode("utf-8")
                connection.execute(
                    "INSERT INTO zhihu_manual_collection_task("
                    "task_id,source_id,content_type,response_kind,content_id,"
                    "parent_comment_id,public_url,last_cursor,failure_class,"
                    "source_snapshot_id,required_action,status,task_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                    "public_url=excluded.public_url,last_cursor=excluded.last_cursor,"
                    "failure_class=excluded.failure_class,source_snapshot_id=excluded.source_snapshot_id,"
                    "required_action=excluded.required_action,status='OPEN',"
                    "task_json=excluded.task_json,updated_at=excluded.updated_at",
                    (
                        stored.task_id,
                        stored.author_source_id,
                        stored.content_type,
                        stored.response_kind,
                        stored.content_id,
                        stored.parent_comment_id,
                        stored.public_url,
                        stored.last_cursor,
                        stored.failure_class,
                        stored.source_snapshot_id,
                        stored.required_action,
                        stored.status.value,
                        payload,
                        stored.created_at.isoformat(),
                        stored.updated_at.isoformat(),
                    ),
                )
            inactive = sorted(set(existing) - set(active))
            for task_id in inactive:
                if existing[task_id].status is ZhihuManualTaskStatus.RESOLVED:
                    continue
                previous = existing[task_id].model_copy(
                    update={
                        "status": ZhihuManualTaskStatus.RESOLVED,
                        "updated_at": now,
                    }
                )
                payload = canonical_json_bytes(previous.model_dump(mode="json")).decode("utf-8")
                connection.execute(
                    "UPDATE zhihu_manual_collection_task SET status='RESOLVED',"
                    "task_json=?,updated_at=? WHERE task_id=?",
                    (payload, now.isoformat(), task_id),
                )


def _freshness(record: ZhihuContentRecord) -> datetime:
    return record.updated_at or record.published_at or record.collected_at


__all__ = ["ZhihuManualTaskService"]
