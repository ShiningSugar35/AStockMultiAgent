"""Durable full-history Zhihu capture coordinator over the credential-free bridge."""

from __future__ import annotations

import html
import json
import secrets
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from pydantic import ValidationError

from astock.core.atomic import atomic_write_text
from astock.core.errors import FailureClass, PolicyError, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.config import get_knowledge_source
from astock.knowledge.imports import ZhihuResponseImportService
from astock.knowledge.manual_tasks import ZhihuManualTaskService
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import normalize_zhihu_api_url, validate_zhihu_article_url
from astock.schemas import (
    CollectionTerminalCondition,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    ZhihuBrowserResponseEnvelope,
    ZhihuCommentEndpointTemplate,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuEndpointTemplateStatus,
    ZhihuImportedResponse,
    ZhihuManualCollectionTask,
    ZhihuResponseKind,
    ZhihuTransport,
)

_CAPTURE_ORIGINS = frozenset(
    {
        "https://www.zhihu.com",
        "https://zhuanlan.zhihu.com",
    }
)
_MAX_ENVELOPE_BYTES = 90_100_000
_SUCCESS_TERMINALS = {
    CollectionTerminalCondition.PAGINATION_COMPLETE,
    CollectionTerminalCondition.CONFIRMED_EMPTY,
}
_FAILURE_TERMINALS = {
    CollectionTerminalCondition.ACCESS_RESTRICTED,
    CollectionTerminalCondition.FETCH_FAILED,
}
_CONTENT_ORDER = (
    ZhihuContentType.ANSWERS,
    ZhihuContentType.ARTICLES,
    ZhihuContentType.THOUGHTS,
)
_TASK_ORDER = {
    ZhihuResponseKind.CONTENT_DETAIL.value: 0,
    "COLUMN_LISTING": 1,
    ZhihuResponseKind.LISTING.value: 2,
}
_TRANSIENT_TASK_FAILURES = {
    FailureClass.NETWORK.value,
    FailureClass.TIMEOUT.value,
}
_ACTIONABLE_TASK_FAILURES = {
    ZhihuResponseKind.CONTENT_DETAIL: {"DETAIL_MISSING"} | _TRANSIENT_TASK_FAILURES,
}
_MAX_EMPTY_NONTERMINAL_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ZhihuCaptureRequest:
    author_source_id: str
    response_kind: ZhihuResponseKind
    content_type: ZhihuContentType
    requested_url: str
    content_id: str | None = None
    parent_comment_id: str | None = None
    listing_page: int | None = None
    comment_page: int | None = None
    request_cursor: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "author_source_id": self.author_source_id,
            "response_kind": self.response_kind.value,
            "content_type": self.content_type.value,
            "content_id": self.content_id,
            "parent_comment_id": self.parent_comment_id,
            "listing_page": self.listing_page,
            "comment_page": self.comment_page,
            "request_cursor": self.request_cursor,
            "requested_url": self.requested_url,
        }


@dataclass(frozen=True, slots=True)
class ZhihuCoordinatorAck:
    status: str
    envelope_id: str
    source_snapshot_id: str
    response_failure: str | None
    next_request: dict[str, object] | None
    done: bool
    terminal_condition: str | None
    content_record_count: int
    comment_record_count: int
    accepted_envelope_count: int
    blocked_task_count: int
    retry_count: int


class ZhihuFullCaptureSession:
    """Drive all allowlisted listings and every currently verified recovery boundary."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        runtime_root: Path,
        parquet_store: ParquetKnowledgeStore,
        source_registry: KnowledgeSourceRegistry,
        endpoint_registry: ZhihuEndpointTemplateRegistry,
        *,
        source_ids: Sequence[str] | None = None,
        task_response_kinds: Sequence[ZhihuResponseKind] | None = None,
        page_size: int = 20,
        request_interval_seconds: float = 2.0,
        ttl_seconds: int = 21_600,
        session_token: str | None = None,
        expected_transport: ZhihuTransport = ZhihuTransport.CHROME,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if request_interval_seconds < 2:
            raise ValueError("request_interval_seconds must be at least 2")
        if not 60 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be between 60 and 86400")
        selected_ids = (
            list(source_ids)
            if source_ids is not None
            else [
                source.source_id
                for source in source_registry.sources
                if source.enabled and source.online_collection_required
            ]
        )
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise ValueError("source_ids must be non-empty and unique")
        if task_response_kinds is not None and (
            not task_response_kinds
            or len(task_response_kinds) != len(set(task_response_kinds))
            or any(kind is not ZhihuResponseKind.CONTENT_DETAIL for kind in task_response_kinds)
        ):
            raise ValueError("task_response_kinds only accepts unique CONTENT_DETAIL values")
        sources = [get_knowledge_source(source_registry, source_id) for source_id in selected_ids]
        if any(
            not source.enabled or not source.online_collection_required or source.url_token is None
            for source in sources
        ):
            raise PolicyError(
                "Full capture only accepts enabled online allowlisted sources",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        token = session_token or secrets.token_urlsafe(32)
        if len(token) < 32:
            raise ValueError("session_token must contain at least 32 characters")
        self.state = state
        self.runtime_root = runtime_root.resolve()
        self.source_registry = source_registry
        self.endpoint_registry = endpoint_registry
        self.sources = sources
        self.source_ids = set(selected_ids)
        self.task_response_kinds = (
            frozenset(kind.value for kind in task_response_kinds)
            if task_response_kinds is not None
            else None
        )
        self.page_size = page_size
        self.request_interval_seconds = request_interval_seconds
        self.session_token = token
        self.expected_transport = expected_transport
        self._now = now or (lambda: datetime.now(UTC))
        self.started_at = self._now()
        self.expires_at = self.started_at + timedelta(seconds=ttl_seconds)
        self.import_service = ZhihuResponseImportService(
            state,
            object_store,
            runtime_root,
        )
        self.parquet_store = parquet_store
        self.manual_service = ZhihuManualTaskService(state, object_store)
        self._listing_finished: set[tuple[str, ZhihuContentType]] = {
            (source.source_id, content_type)
            for source in sources
            for content_type in _CONTENT_ORDER
            if (
                checkpoint := state.get_collection_checkpoint(
                    source.source_id,
                    content_type.value,
                )
            )
            is not None
            and checkpoint.terminal_condition in _SUCCESS_TERMINALS
        }
        self._accepted_envelope_ids: set[str] = set()
        self._bridge_event_lock = threading.Lock()
        self._bridge_current_read_count = 0
        self._bridge_preflight_count = 0
        self._boundary_attempts: dict[tuple[object, ...], int] = {}
        self._lock = threading.Lock()
        self._terminal_condition: str | None = None
        self._blocked_task_count = 0
        self._task_queue: deque[ZhihuManualCollectionTask] = deque()
        self._active_task_id: str | None = None
        self._last_request: ZhihuCaptureRequest | None = None
        self.last_ack: ZhihuCoordinatorAck | None = None
        self._current = self._select_next_request()

    @property
    def initial_request(self) -> ZhihuCaptureRequest | None:
        return self._current

    @property
    def is_terminal(self) -> bool:
        return self._current is None

    def safe_status(self) -> dict[str, object]:
        if self._now() > self.expires_at and not self.is_terminal:
            status = "EXPIRED"
        elif self.is_terminal:
            status = (
                "COMPLETE" if self._terminal_condition == "COMPLETE" else "MANUAL_ACTION_REQUIRED"
            )
        elif self.last_ack is None:
            status = "READY"
        else:
            status = "RUNNING"
        return {
            "status": status,
            "source_ids": [source.source_id for source in self.sources],
            "task_response_kinds": (
                sorted(self.task_response_kinds)
                if self.task_response_kinds is not None
                else None
            ),
            "accepted_envelope_count": len(self._accepted_envelope_ids),
            "bridge_current_read_count": self._bridge_current_read_count,
            "bridge_preflight_count": self._bridge_preflight_count,
            "completed_listing_scope_count": len(self._listing_finished),
            "required_listing_scope_count": len(self.sources) * len(_CONTENT_ORDER),
            "blocked_task_count": self._blocked_task_count,
            "expires_at": self.expires_at,
            "current_request": self._current.payload() if self._current else None,
            "terminal_condition": self._terminal_condition,
            "last_ack": asdict(self.last_ack) if self.last_ack else None,
        }

    def record_bridge_event(self, event: str) -> None:
        """Count credential-free loopback handshakes for local diagnostics."""

        with self._bridge_event_lock:
            if event == "CURRENT_READ":
                self._bridge_current_read_count += 1
            elif event == "PREFLIGHT":
                self._bridge_preflight_count += 1
            else:
                raise ValueError(f"unsupported bridge event: {event}")

    def process_payload(
        self,
        payload: bytes,
        *,
        origin: str | None,
        session_token: str | None,
    ) -> ZhihuCoordinatorAck:
        self._validate_call(origin, session_token, payload)
        if not self._lock.acquire(blocking=False):
            raise ProviderError(
                "Another capture response is still being committed",
                failure_class=FailureClass.CONFLICT,
                retryable=True,
            )
        try:
            try:
                envelope = ZhihuBrowserResponseEnvelope.model_validate_json(payload)
            except ValidationError as exc:
                raise ProviderError(
                    "Capture envelope is invalid",
                    failure_class=FailureClass.INVALID_RESPONSE,
                    details={"validation_error_count": exc.error_count()},
                ) from exc
            current = self._current
            if (
                self.last_ack is not None
                and self._last_request is not None
                and self._envelope_matches(self._last_request, envelope)
                and self.last_ack.status != "RETRYING"
            ):
                repeated = self.import_service.import_envelope(
                    envelope,
                    self.source_registry,
                    self.endpoint_registry,
                )
                if repeated.record.envelope_id != self.last_ack.envelope_id:
                    raise ProviderError(
                        "Repeated capture boundary returned different response bytes",
                        failure_class=FailureClass.CONFLICT,
                    )
                return self.last_ack
            if current is None:
                raise PolicyError(
                    "Full capture session has no pending browser request",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            self._validate_envelope(current, envelope)
            imported = self.import_service.import_envelope(
                envelope,
                self.source_registry,
                self.endpoint_registry,
            )
            if (
                self.last_ack is not None
                and self.last_ack.envelope_id == imported.record.envelope_id
                and self.last_ack.status != "RETRYING"
            ):
                return self.last_ack
            content_count = 0
            comment_count = 0
            safe_to_skip = False
            failure = imported.response_failure
            if current.response_kind is ZhihuResponseKind.LISTING:
                replay = self.import_service.replay_listing(
                    imported.record.envelope_id,
                    self.source_registry,
                    self.parquet_store,
                    recover_consumed=True,
                )
                report = replay.sync_execution.report if replay.sync_execution else None
                if replay.sync_execution is not None:
                    content_count = len(replay.sync_execution.content_records)
                if report is not None and report.terminal_condition in _SUCCESS_TERMINALS:
                    self._listing_finished.add((current.author_source_id, current.content_type))
                if report is not None and report.terminal_condition in _FAILURE_TERMINALS:
                    failure = _report_failure(report.gaps) or failure
            elif current.response_kind is ZhihuResponseKind.CONTENT_DETAIL:
                replay_detail = self.import_service.replay_detail(
                    imported.record.envelope_id,
                    self.source_registry,
                    self.parquet_store,
                    recover_consumed=True,
                )
                failure = replay_detail.response_failure or failure
                content_count = int(replay_detail.content_record is not None)
            else:
                replay_comment = self.import_service.replay_comment(
                    imported.record.envelope_id,
                    self.source_registry,
                    self.parquet_store,
                    recover_consumed=True,
                )
                failure = replay_comment.response_failure or failure
                safe_to_skip = replay_comment.safe_to_skip
                if replay_comment.comment_execution is not None:
                    comment_count = len(replay_comment.comment_execution.comment_records)
            self._accepted_envelope_ids.add(imported.record.envelope_id)
            boundary_key = self._request_key(current)
            retry_count = self._boundary_attempts.get(boundary_key, 0)
            retrying = False
            if (
                failure is FailureClass.INVALID_RESPONSE
                and current.response_kind is ZhihuResponseKind.LISTING
                and self._is_empty_nonterminal_listing(current, imported.record)
            ):
                retry_count += 1
                self._boundary_attempts[boundary_key] = retry_count
                retrying = retry_count < _MAX_EMPTY_NONTERMINAL_ATTEMPTS
            elif failure is None:
                self._boundary_attempts.pop(boundary_key, None)
                retry_count = 0
            if retrying:
                self._terminal_condition = None
                self._current = current
            elif failure is not None and safe_to_skip:
                self._skip_active_task(current)
                self._blocked_task_count += 1
                self._terminal_condition = None
                self._current = self._select_next_request()
            elif failure is not None:
                self._terminal_condition = failure.value
                self._current = None
            else:
                self._advance_active_task(current)
                self._current = self._select_next_request()
            done = self._current is None
            ack = ZhihuCoordinatorAck(
                status=(
                    "RETRYING"
                    if retrying
                    else "COMPLETE"
                    if done and self._terminal_condition == "COMPLETE"
                    else ("STOPPED" if done else "COMMITTED")
                ),
                envelope_id=imported.record.envelope_id,
                source_snapshot_id=imported.record.source_snapshot_id,
                response_failure=(
                    failure.value if failure is not None and not safe_to_skip else None
                ),
                next_request=(self._current.payload() if self._current else None),
                done=done,
                terminal_condition=self._terminal_condition if done else None,
                content_record_count=content_count,
                comment_record_count=comment_count,
                accepted_envelope_count=len(self._accepted_envelope_ids),
                blocked_task_count=self._blocked_task_count,
                retry_count=retry_count,
            )
            self.last_ack = ack
            self._last_request = current
            return ack
        finally:
            self._lock.release()

    def installer_html(self, bridge_origin: str) -> bytes:
        if self._current is None:
            bookmark = "#"
        else:
            bookmark = _coordinator_bookmarklet(
                bridge_origin=bridge_origin,
                session_token=self.session_token,
                interval_ms=round(self.request_interval_seconds * 1000),
            )
        extension_directory = build_coordinator_capture_extension(
            runtime_root=self.runtime_root,
            bridge_origin=bridge_origin,
            session_token=self.session_token,
            interval_ms=round(self.request_interval_seconds * 1000),
        )
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AStock 知乎全量采集</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:48px auto;
padding:0 20px;line-height:1.7}}
a{{display:inline-block;padding:12px 18px;background:#1769aa;color:white;
border-radius:8px;text-decoration:none}}
code{{background:#f3f4f6;padding:2px 5px}}
</style></head><body>
<h1>知乎全量采集</h1>
<p>范围：{html.escape("、".join(source.display_name for source in self.sources))}</p>
<ol><li>按 <code>Ctrl+Shift+B</code> 显示 Chrome 书签栏。</li>
<li>把下面按钮拖到书签栏，不要在本页直接点击。</li>
<li>切换到任意已登录的知乎页面，点击该书签一次并保持页面打开。</li></ol>
<p><a href="{html.escape(bookmark, quote=True)}">AStock 知乎全量采集</a></p>
<h2>书签没有启动时</h2>
<p>可在 Chrome 扩展管理页开启开发者模式，选择“加载已解压的扩展程序”，
加载下面的临时目录，然后刷新已登录知乎页：</p>
<p><code>{html.escape(str(extension_directory))}</code></p>
<p>扩展只匹配 <code>https://www.zhihu.com/*</code> 和
<code>https://zhuanlan.zhihu.com/*</code>，会话结束后请移除。</p>
<p>程序将串行处理列表和已验证正文详情；遇到访问限制立即停止。
尚未观测确认的接口会进入本地手工任务清单，不会猜测或伪装完成。</p>
<p>Cookie 始终留在 Chrome；会话到期后令牌自动失效。</p>
</body></html>"""
        return body.encode("utf-8")

    def _select_next_request(self) -> ZhihuCaptureRequest | None:
        for source in self.sources:
            for content_type in _CONTENT_ORDER:
                scope = (source.source_id, content_type)
                if scope not in self._listing_finished:
                    self._terminal_condition = None
                    return self._listing_request(source, content_type)
        while self._task_queue:
            task = self._task_queue[0]
            request = self._request_for_task(task)
            if request is not None:
                self._active_task_id = task.task_id
                self._terminal_condition = None
                return request
            self._task_queue.popleft()
        self._active_task_id = None
        return self._refresh_task_queue()

    def _refresh_task_queue(self) -> ZhihuCaptureRequest | None:
        """Rebuild the expensive manual-task view only at queue phase boundaries."""

        tasks = self.manual_service.refresh(self.source_registry)
        selected_content_types = {item.value for item in _CONTENT_ORDER}
        selected = [
            task
            for task in tasks
            if task.author_source_id in self.source_ids
            and (
                (task.content_type == "columns" and task.response_kind == "COLUMN_LISTING")
                or (
                    (
                        self.task_response_kinds is None
                        or task.response_kind in self.task_response_kinds
                    )
                    and task.content_type in selected_content_types
                    and task.response_kind
                    in {
                        ZhihuResponseKind.LISTING.value,
                        ZhihuResponseKind.CONTENT_DETAIL.value,
                    }
                )
            )
        ]
        selected.sort(
            key=lambda task: (
                _TASK_ORDER.get(task.response_kind, 99),
                task.author_source_id,
                task.content_type,
                task.content_id or "",
                task.parent_comment_id or "",
            )
        )
        actionable = [task for task in selected if self._task_is_actionable(task)]
        self._blocked_task_count = len(selected) - len(actionable)
        self._task_queue.extend(actionable)
        if not self._task_queue:
            self._terminal_condition = "COMPLETE" if not selected else "NEEDS_MANUAL_ACTION"
            return None
        task = self._task_queue[0]
        request = self._request_for_task(task)
        if request is None:
            raise ProviderError(
                "Actionable Zhihu task could not build its verified request",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        self._active_task_id = task.task_id
        self._terminal_condition = None
        return request

    def _advance_active_task(self, request: ZhihuCaptureRequest) -> None:
        if request.response_kind is ZhihuResponseKind.LISTING:
            return
        if not self._task_queue or self._active_task_id != self._task_queue[0].task_id:
            raise ProviderError(
                "Zhihu task queue lost its active boundary",
                failure_class=FailureClass.CONFLICT,
            )
        if request.response_kind is ZhihuResponseKind.CONTENT_DETAIL:
            self._task_queue.popleft()
            self._active_task_id = None
            return
        checkpoint = self.state.get_collection_checkpoint(
            request.author_source_id,
            request.content_type.value,
            request.content_id,
            request.parent_comment_id,
        )
        if checkpoint is not None and checkpoint.terminal_condition in _SUCCESS_TERMINALS:
            self._task_queue.popleft()
            self._active_task_id = None

    def _skip_active_task(self, request: ZhihuCaptureRequest) -> None:
        if request.response_kind is ZhihuResponseKind.LISTING:
            raise ProviderError(
                "Zhihu listing boundary cannot be skipped",
                failure_class=FailureClass.CONFLICT,
            )
        if not self._task_queue or self._active_task_id != self._task_queue[0].task_id:
            raise ProviderError(
                "Zhihu task queue lost its blocked boundary",
                failure_class=FailureClass.CONFLICT,
            )
        self._task_queue.popleft()
        self._active_task_id = None

    def _task_is_actionable(self, task: ZhihuManualCollectionTask) -> bool:
        try:
            response_kind = ZhihuResponseKind(task.response_kind)
        except ValueError:
            return False
        if response_kind is not ZhihuResponseKind.CONTENT_DETAIL:
            return False
        if task.failure_class not in _ACTIONABLE_TASK_FAILURES.get(response_kind, set()):
            return False
        return self._verified_template_for_task(task) is not None

    def _verified_template_for_task(
        self,
        task: ZhihuManualCollectionTask,
    ) -> ZhihuCommentEndpointTemplate | None:
        response_kind = ZhihuResponseKind(task.response_kind)
        content_type = ZhihuContentType(task.content_type)
        return next(
            (
                item
                for item in self.endpoint_registry.templates
                if item.response_kind is response_kind
                and content_type in item.content_types
                and item.status is ZhihuEndpointTemplateStatus.VERIFIED
                and item.path_template is not None
            ),
            None,
        )

    def _listing_request(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
    ) -> ZhihuCaptureRequest:
        checkpoint = self.state.get_collection_checkpoint(
            source.source_id,
            content_type.value,
        )
        if (
            checkpoint is not None
            and checkpoint.terminal_condition is None
            and checkpoint.listing_cursor
        ):
            page = checkpoint.listing_page
            cursor = checkpoint.listing_cursor
            url = normalize_zhihu_api_url(checkpoint.listing_cursor)
        else:
            page = 0
            cursor = None
            assert source.url_token is not None
            segment = "pins" if content_type is ZhihuContentType.THOUGHTS else content_type.value
            url = (
                f"https://www.zhihu.com/api/v4/members/{source.url_token}/{segment}"
                f"?limit={self.page_size}&offset=0&sort_by=created"
            )
        return ZhihuCaptureRequest(
            author_source_id=source.source_id,
            response_kind=ZhihuResponseKind.LISTING,
            content_type=content_type,
            listing_page=page,
            request_cursor=cursor,
            requested_url=url,
        )

    def _request_for_task(
        self,
        task: ZhihuManualCollectionTask,
    ) -> ZhihuCaptureRequest | None:
        if not self._task_is_actionable(task):
            return None
        response_kind = ZhihuResponseKind(task.response_kind)
        content_type = ZhihuContentType(task.content_type)
        template = self._verified_template_for_task(task)
        assert template is not None and template.path_template is not None
        assert task.content_id is not None
        checkpoint = None
        if response_kind in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            checkpoint = self.state.get_collection_checkpoint(
                task.author_source_id,
                content_type.value,
                task.content_id,
                task.parent_comment_id,
            )
        if (
            checkpoint is not None
            and checkpoint.terminal_condition is None
            and (
                checkpoint.nested_reply_cursor
                if task.parent_comment_id
                else checkpoint.comment_cursor
            )
        ):
            cursor = (
                checkpoint.nested_reply_cursor
                if task.parent_comment_id
                else checkpoint.comment_cursor
            )
            assert cursor is not None
            url = normalize_zhihu_api_url(cursor)
            expected_path = template.path_template.replace("{content_id}", task.content_id)
            expected_path = expected_path.replace(
                "{parent_comment_id}", task.parent_comment_id or ""
            )
            parsed_cursor = urlsplit(url)
            cursor_origin = f"{parsed_cursor.scheme}://{parsed_cursor.netloc}"
            if (
                parsed_cursor.path.rstrip("/") != expected_path.rstrip("/")
                or cursor_origin != template.request_origin
            ):
                raise ProviderError(
                    "Zhihu comment cursor changed its verified task boundary",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            comment_page = checkpoint.comment_page
        else:
            path = template.path_template.replace("{content_id}", task.content_id)
            path = path.replace("{parent_comment_id}", task.parent_comment_id or "")
            if "{" in path or "}" in path:
                raise ProviderError(
                    "Verified Zhihu endpoint template has an unresolved placeholder",
                    failure_class=FailureClass.INVALID_RESPONSE,
                )
            query = urlencode(template.default_query)
            url = f"{template.request_origin}{path}" + (f"?{query}" if query else "")
            if (
                content_type is ZhihuContentType.ARTICLES
                and response_kind is ZhihuResponseKind.CONTENT_DETAIL
            ):
                validate_zhihu_article_url(url)
            else:
                url = normalize_zhihu_api_url(url)
            cursor = None
            comment_page = (
                0
                if response_kind
                in {ZhihuResponseKind.ROOT_COMMENTS, ZhihuResponseKind.CHILD_COMMENTS}
                else None
            )
        return ZhihuCaptureRequest(
            author_source_id=task.author_source_id,
            response_kind=response_kind,
            content_type=content_type,
            content_id=task.content_id,
            parent_comment_id=task.parent_comment_id,
            comment_page=comment_page,
            request_cursor=cursor,
            requested_url=url,
        )

    def _validate_call(
        self,
        origin: str | None,
        session_token: str | None,
        payload: bytes,
    ) -> None:
        if origin not in _CAPTURE_ORIGINS:
            raise PolicyError(
                "Capture payload origin is not the allowlisted Zhihu origin",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if not session_token or not secrets.compare_digest(
            session_token,
            self.session_token,
        ):
            raise PolicyError(
                "Capture session token is invalid",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if self._now() > self.expires_at:
            raise PolicyError(
                "Capture session has expired",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if len(payload) > _MAX_ENVELOPE_BYTES:
            raise ProviderError(
                "Capture envelope exceeds the local size limit",
                failure_class=FailureClass.INVALID_RESPONSE,
            )

    @staticmethod
    def _request_key(request: ZhihuCaptureRequest) -> tuple[object, ...]:
        payload = request.payload()
        return tuple(payload[key] for key in sorted(payload))

    def _is_empty_nonterminal_listing(
        self,
        request: ZhihuCaptureRequest,
        record: ZhihuImportedResponse,
    ) -> bool:
        """Recognize only the observed empty-hole shape; never advance past it here."""

        try:
            payload = json.loads(
                self.import_service.object_store.get_bytes(record.raw_object_sha256)
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or payload.get("data") != []:
            return False
        paging = payload.get("paging")
        if not isinstance(paging, dict) or paging.get("is_end") is not False:
            return False
        next_cursor = paging.get("next")
        if not isinstance(next_cursor, str):
            return False
        try:
            normalized_next = normalize_zhihu_api_url(next_cursor)
        except (PolicyError, ProviderError, ValueError):
            return False
        requested = urlsplit(request.requested_url)
        following = urlsplit(normalized_next)
        return following.path == requested.path and normalized_next != request.requested_url

    def _validate_envelope(
        self,
        request: ZhihuCaptureRequest,
        envelope: ZhihuBrowserResponseEnvelope,
    ) -> None:
        if not self._envelope_matches(request, envelope):
            raise PolicyError(
                "Capture envelope does not match the exact pending request boundary",
                failure_class=FailureClass.POLICY_REJECTED,
            )

    def _envelope_matches(
        self,
        request: ZhihuCaptureRequest,
        envelope: ZhihuBrowserResponseEnvelope,
    ) -> bool:
        expected = request.payload()
        received = {
            "author_source_id": envelope.author_source_id,
            "response_kind": envelope.response_kind.value,
            "content_type": envelope.content_type.value if envelope.content_type else None,
            "content_id": envelope.content_id,
            "parent_comment_id": envelope.parent_comment_id,
            "listing_page": envelope.listing_page,
            "comment_page": envelope.comment_page,
            "request_cursor": envelope.request_cursor,
            "requested_url": envelope.requested_url,
        }
        return received == expected and envelope.transport is self.expected_transport


def _coordinator_bookmarklet(
    *,
    bridge_origin: str,
    session_token: str,
    interval_ms: int,
) -> str:
    config = json.dumps(
        {
            "bridge": bridge_origin,
            "token": session_token,
            "interval": interval_ms,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        "javascript:(async()=>{"
        f"const c={config};"
        "if(!['https://www.zhihu.com','https://zhuanlan.zhihu.com'].includes(location.origin)){"
        "alert('Open a logged-in Zhihu or Zhihu article page first.');return;}"
        "const start=async()=>{"
        "const k='__astockCaptureLoop';const prior=window[k];"
        "if(prior&&prior.running){alert('AStock capture is already running.');return;}"
        "const run={running:true,accepted:0,error:null};window[k]=run;try{"
        "const started=await fetch(c.bridge+'/v1/current',{method:'GET',mode:'cors',"
        "credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',"
        "headers:{'X-AStock-Capture-Token':c.token}});"
        "const state=await started.json();if(!started.ok)"
        "throw new Error(state.message||state.status||'Local checkpoint read failed');"
        "let r=state.current_request;if(!r){"
        "alert('AStock capture stopped: '+(state.terminal_condition||state.status));return;}"
        "for(let guard=0;guard<100000;guard++){"
        "const target=new URL(r.requested_url);"
        "if(!['https://www.zhihu.com','https://zhuanlan.zhihu.com'].includes(target.origin))"
        "throw new Error('Pending request origin is not allowlisted');"
        "if(target.origin!==location.origin){"
        "location.replace(target.origin==='https://zhuanlan.zhihu.com'"
        "?r.requested_url:'https://www.zhihu.com/');return;}"
        "const response=await fetch(r.requested_url,{credentials:'include',"
        "cache:'no-store',headers:{accept:'application/json,text/plain,*/*'}});"
        "const bytes=new Uint8Array(await response.arrayBuffer());"
        "if(bytes.length===0||bytes.length>67108864)"
        "throw new Error('Unexpected response size: '+bytes.length);"
        "let binary='';for(let i=0;i<bytes.length;i+=32768)"
        "binary+=String.fromCharCode(...bytes.subarray(i,i+32768));"
        "const envelope={...r,status_code:response.status,"
        "response_mime:response.headers.get('content-type')||'application/octet-stream',"
        "body_base64:btoa(binary),transport:'CHROME',"
        "captured_at:new Date().toISOString()};"
        "const committed=await fetch(c.bridge+'/v1/envelopes',{method:'POST',"
        "mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',"
        "headers:{'Content-Type':'application/json',"
        "'X-AStock-Capture-Token':c.token},body:JSON.stringify(envelope)});"
        "const ack=await committed.json();if(!committed.ok)"
        "throw new Error(ack.message||ack.status||'Local commit failed');"
        "if(ack.done){alert('AStock capture stopped: '+ack.terminal_condition);return;}"
        "if(!ack.next_request)throw new Error('Local coordinator has no next request');"
        "run.accepted=ack.accepted_envelope_count;r=ack.next_request;"
        "await new Promise(resolve=>setTimeout(resolve,c.interval));"
        "}throw new Error('Request safety limit reached');"
        "}catch(error){run.error=error&&error.message?error.message:String(error);"
        "alert('AStock capture error: '+run.error);"
        "}finally{run.running=false;}};"
        "if(navigator.locks&&navigator.locks.request){"
        "await navigator.locks.request('astock-zhihu-capture-v1',"
        "{ifAvailable:true},async lock=>{if(!lock)return;await start();});"
        "}else{await start();}})()"
    )


def build_coordinator_capture_extension(
    *,
    runtime_root: Path,
    bridge_origin: str,
    session_token: str,
    interval_ms: int,
) -> Path:
    """Build one session-scoped, minimum-permission unpacked Chrome extension."""

    parsed = urlsplit(bridge_origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("bridge_origin must be an exact 127.0.0.1 HTTP origin")
    if len(session_token) < 32:
        raise ValueError("session_token must contain at least 32 characters")
    if interval_ms < 2_000:
        raise ValueError("interval_ms must be at least 2000")
    identity = content_hash(
        {
            "bridge_origin": bridge_origin,
            "session_token": session_token,
            "interval_ms": interval_ms,
        }
    )[:16]
    extension_directory = runtime_root.resolve() / "zhihu_capture_extension" / identity
    manifest = {
        "manifest_version": 3,
        "name": "AStock 知乎本地全量采集",
        "version": "1.0.0",
        "minimum_chrome_version": "111",
        "description": "Session-scoped local capture without browser credential access.",
        "content_scripts": [
            {
                "matches": [
                    "https://www.zhihu.com/*",
                    "https://zhuanlan.zhihu.com/*",
                ],
                "js": ["capture.js"],
                "run_at": "document_idle",
                "all_frames": False,
                "world": "MAIN",
            }
        ],
    }
    bookmarklet = _coordinator_bookmarklet(
        bridge_origin=bridge_origin,
        session_token=session_token,
        interval_ms=interval_ms,
    )
    script = bookmarklet.removeprefix("javascript:") + ";\n"
    atomic_write_text(
        extension_directory / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(extension_directory / "capture.js", script)
    return extension_directory


def _report_failure(gaps: list[dict[str, object]]) -> FailureClass | None:
    if not gaps:
        return None
    raw = gaps[0].get("failure_class")
    try:
        return FailureClass(str(raw)) if raw else None
    except ValueError:
        return FailureClass.INVALID_RESPONSE


__all__ = [
    "ZhihuCaptureRequest",
    "ZhihuCoordinatorAck",
    "ZhihuFullCaptureSession",
    "build_coordinator_capture_extension",
]
