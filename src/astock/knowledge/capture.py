"""Credential-free loopback bridge for user-initiated Zhihu capture."""

from __future__ import annotations

import html
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from astock.core.errors import AStockError, FailureClass, PolicyError, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.config import get_knowledge_source
from astock.knowledge.imports import ZhihuResponseImportService
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.schemas import (
    AuthorCollectionCoverageReport,
    CollectionTerminalCondition,
    KnowledgeSourceRegistry,
    ZhihuBrowserResponseEnvelope,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuResponseKind,
    ZhihuTransport,
)

_CAPTURE_ORIGINS = frozenset(
    {
        "https://www.zhihu.com",
        "https://zhuanlan.zhihu.com",
    }
)
_LOOPBACK_HOST = "127.0.0.1"
_MAX_ENVELOPE_BYTES = 90_100_000
_SUPPORTED_TYPES = {
    ZhihuContentType.ANSWERS,
    ZhihuContentType.ARTICLES,
    ZhihuContentType.THOUGHTS,
}


@dataclass(frozen=True, slots=True)
class ZhihuCaptureAck:
    status: str
    envelope_id: str
    source_snapshot_id: str
    response_failure: str | None
    next_page: int
    next_url: str | None
    done: bool
    terminal_condition: str | None
    content_record_count: int


class ZhihuLoopbackCaptureSession:
    """Validate and replay browser responses without receiving browser credentials."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        runtime_root: Path,
        parquet_store: ParquetKnowledgeStore,
        source_registry: KnowledgeSourceRegistry,
        endpoint_registry: ZhihuEndpointTemplateRegistry,
        *,
        source_id: str,
        content_type: ZhihuContentType,
        page_size: int = 20,
        request_interval_seconds: float = 2.0,
        ttl_seconds: int = 900,
        session_token: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if content_type not in _SUPPORTED_TYPES:
            raise PolicyError(
                "Loopback capture supports answers, articles, and thoughts only",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if request_interval_seconds < 2:
            raise ValueError("request_interval_seconds must be at least 2")
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be between 60 and 3600")
        source = get_knowledge_source(source_registry, source_id)
        if (
            not source.online_collection_required
            or content_type.value not in source.collection_scope.content_types
            or not source.url_token
        ):
            raise PolicyError(
                "Source is not eligible for online loopback capture",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        token = session_token or secrets.token_urlsafe(32)
        if len(token) < 32:
            raise ValueError("session_token must contain at least 32 characters")
        self.state = state
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.source_registry = source_registry
        self.endpoint_registry = endpoint_registry
        self.source = source
        self.content_type = content_type
        self.page_size = page_size
        self.request_interval_seconds = request_interval_seconds
        self.session_token = token
        self._now = now or (lambda: datetime.now(UTC))
        self.started_at = self._now()
        self.expires_at = self.started_at + timedelta(seconds=ttl_seconds)
        self.import_service = ZhihuResponseImportService(
            state,
            object_store,
            runtime_root,
        )
        self._lock = threading.Lock()
        self._bridge_event_lock = threading.Lock()
        self._bridge_current_read_count = 0
        self._bridge_preflight_count = 0
        self._accepted_envelope_ids: set[str] = set()
        self.last_ack: ZhihuCaptureAck | None = None

    @property
    def initial_url(self) -> str:
        _, url = self.initial_boundary
        return url

    @property
    def initial_boundary(self) -> tuple[int, str]:
        checkpoint = self.state.get_collection_checkpoint(
            self.source.source_id,
            self.content_type.value,
        )
        if (
            checkpoint is not None
            and checkpoint.terminal_condition is None
            and checkpoint.listing_cursor
        ):
            return checkpoint.listing_page, checkpoint.listing_cursor
        assert self.source.url_token is not None
        segment = (
            "pins" if self.content_type is ZhihuContentType.THOUGHTS else self.content_type.value
        )
        return 0, (
            f"https://www.zhihu.com/api/v4/members/{self.source.url_token}/"
            f"{segment}?limit={self.page_size}&offset=0&sort_by=created"
        )

    @property
    def is_terminal(self) -> bool:
        return bool(self.last_ack and self.last_ack.done)

    def safe_status(self) -> dict[str, Any]:
        if self.last_ack is None:
            status = "READY" if self._now() <= self.expires_at else "EXPIRED"
        elif self.last_ack.done:
            status = (
                "COMPLETE"
                if self.last_ack.terminal_condition
                in {
                    CollectionTerminalCondition.PAGINATION_COMPLETE.value,
                    CollectionTerminalCondition.CONFIRMED_EMPTY.value,
                }
                else "STOPPED"
            )
        else:
            status = "RUNNING"
        return {
            "status": status,
            "source_id": self.source.source_id,
            "content_type": self.content_type.value,
            "accepted_envelope_count": len(self._accepted_envelope_ids),
            "bridge_current_read_count": self._bridge_current_read_count,
            "bridge_preflight_count": self._bridge_preflight_count,
            "expires_at": self.expires_at,
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
    ) -> ZhihuCaptureAck:
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
        if not self._lock.acquire(blocking=False):
            raise ProviderError(
                "Another capture page is still being committed",
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
            self._validate_envelope_scope(envelope)
            imported = self.import_service.import_envelope(
                envelope,
                self.source_registry,
                self.endpoint_registry,
            )
            if (
                self.last_ack is not None
                and imported.record.envelope_id == self.last_ack.envelope_id
            ):
                return self.last_ack
            replay = self.import_service.replay_listing(
                imported.record.envelope_id,
                self.source_registry,
                self.parquet_store,
            )
            report = (
                replay.sync_execution.report
                if replay.sync_execution is not None
                else self.import_service.repository.latest_coverage_report(
                    self.source.source_id,
                    self.content_type,
                )
            )
            checkpoint = self.state.get_collection_checkpoint(
                self.source.source_id,
                self.content_type.value,
            )
            report_terminal = report.terminal_condition if report is not None else None
            stopped = report_terminal in {
                CollectionTerminalCondition.ACCESS_RESTRICTED,
                CollectionTerminalCondition.FETCH_FAILED,
            }
            done = stopped or bool(
                checkpoint is not None and checkpoint.terminal_condition is not None
            )
            if not done and (checkpoint is None or not checkpoint.listing_cursor):
                raise ProviderError(
                    "Capture checkpoint is missing the next cursor",
                    failure_class=FailureClass.STORAGE,
                )
            terminal = (
                report_terminal
                if stopped
                else (checkpoint.terminal_condition if checkpoint is not None else None)
            )
            response_failure = (
                imported.response_failure.value
                if imported.response_failure is not None
                else _report_failure(report)
            )
            content_record_count = (
                len(replay.sync_execution.content_records)
                if replay.sync_execution is not None
                else 0
            )
            next_url = None
            if not done:
                assert checkpoint is not None
                next_url = checkpoint.listing_cursor
            ack = ZhihuCaptureAck(
                status="STOPPED" if stopped or response_failure else "COMMITTED",
                envelope_id=imported.record.envelope_id,
                source_snapshot_id=imported.record.source_snapshot_id,
                response_failure=response_failure,
                next_page=(
                    checkpoint.listing_page
                    if checkpoint is not None
                    else envelope.listing_page or 0
                ),
                next_url=next_url,
                done=done,
                terminal_condition=(terminal.value if terminal is not None else None),
                content_record_count=content_record_count,
            )
            self._accepted_envelope_ids.add(imported.record.envelope_id)
            self.last_ack = ack
            return ack
        finally:
            self._lock.release()

    def installer_html(self, bridge_origin: str) -> bytes:
        initial_page, initial_url = self.initial_boundary
        bookmarklet = _bookmarklet(
            bridge_origin=bridge_origin,
            session_token=self.session_token,
            source_id=self.source.source_id,
            initial_page=initial_page,
            initial_url=initial_url,
            content_type=self.content_type.value,
            interval_ms=round(self.request_interval_seconds * 1000),
        )
        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AStock 知乎安全采集</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:48px auto;
padding:0 20px;line-height:1.7}}
a{{display:inline-block;padding:12px 18px;background:#1769aa;color:white;
border-radius:8px;text-decoration:none}}
code{{background:#f3f4f6;padding:2px 5px}}
</style>
</head><body><h1>知乎安全采集</h1>
<p>采集范围：<code>{html.escape(self.source.display_name)}</code> /
<code>{self.content_type.value}</code>。</p>
<ol><li>按 <code>Ctrl+Shift+B</code> 显示 Chrome 书签栏。</li>
<li>把下面按钮拖到书签栏，不要在本页直接点击。</li>
<li>切换到任意已登录的知乎页面，点击该书签一次。</li></ol>
<p><a href="{html.escape(bookmarklet, quote=True)}">AStock 知乎采集：
{html.escape(self.source.display_name)} {self.content_type.value}</a></p>
<p>Cookie 始终留在 Chrome；本机只接收响应正文和安全元数据。遇到 401/403/429 会立即停止。</p>
<p>采集完成后可以删除这个临时书签；会话令牌到期后自动失效。</p>
</body></html>"""
        return body.encode("utf-8")

    def _validate_envelope_scope(self, envelope: ZhihuBrowserResponseEnvelope) -> None:
        if (
            envelope.author_source_id != self.source.source_id
            or envelope.content_type is not self.content_type
            or envelope.response_kind is not ZhihuResponseKind.LISTING
            or envelope.transport is not ZhihuTransport.CHROME
        ):
            raise PolicyError(
                "Capture envelope escaped the active author or content scope",
                failure_class=FailureClass.POLICY_REJECTED,
            )


class _CaptureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    capture_session: _CaptureSession


class _CaptureSession(Protocol):
    session_token: str
    expires_at: datetime

    def safe_status(self) -> dict[str, Any]: ...

    def record_bridge_event(self, event: str) -> None: ...

    def process_payload(
        self,
        payload: bytes,
        *,
        origin: str | None,
        session_token: str | None,
    ) -> Any: ...

    def installer_html(self, bridge_origin: str) -> bytes: ...


def create_loopback_capture_server(
    session: _CaptureSession,
    *,
    port: int = 8765,
) -> _CaptureHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    handler = _handler_for(session)
    server = _CaptureHTTPServer((_LOOPBACK_HOST, port), handler)
    server.capture_session = session
    return server


def serve_loopback_capture(
    server: _CaptureHTTPServer,
    session: _CaptureSession,
) -> dict[str, Any]:
    remaining = max(0.0, (session.expires_at - datetime.now(UTC)).total_seconds())
    watchdog = threading.Timer(remaining, server.shutdown)
    watchdog.daemon = True
    watchdog.start()
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        watchdog.cancel()
        server.server_close()
    return session.safe_status()


def loopback_installer_url(server: _CaptureHTTPServer) -> str:
    session = server.capture_session
    return f"{_loopback_origin(server)}/install/{session.session_token}"


def loopback_status_url(server: _CaptureHTTPServer) -> str:
    session = server.capture_session
    return f"{_loopback_origin(server)}/status/{session.session_token}"


def _loopback_origin(server: ThreadingHTTPServer) -> str:
    port = int(server.server_address[1])
    return f"http://{_LOOPBACK_HOST}:{port}"


def _handler_for(
    session: _CaptureSession,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            installer_path = f"/install/{session.session_token}"
            status_path = f"/status/{session.session_token}"
            current_path = "/v1/current"
            if secrets.compare_digest(self.path, installer_path):
                server = cast(ThreadingHTTPServer, self.server)
                body = session.installer_html(_loopback_origin(server))
                self._send(200, body, "text/html; charset=utf-8")
                return
            if secrets.compare_digest(self.path, status_path):
                self._send_json(200, session.safe_status())
                return
            if self.path == current_path:
                origin = self.headers.get("Origin")
                token = self.headers.get("X-AStock-Capture-Token")
                if (
                    origin not in _CAPTURE_ORIGINS
                    or not token
                    or not secrets.compare_digest(
                        token,
                        session.session_token,
                    )
                ):
                    self._send_json(
                        403,
                        {"status": "REJECTED"},
                        cors_origin=origin if origin in _CAPTURE_ORIGINS else None,
                    )
                    return
                session.record_bridge_event("CURRENT_READ")
                self._send_json(200, session.safe_status(), cors_origin=origin)
                return
            self._send_json(404, {"status": "NOT_FOUND"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            allowed_methods = {
                "/v1/current": "GET, OPTIONS",
                "/v1/envelopes": "POST, OPTIONS",
            }
            origin = self.headers.get("Origin")
            if self.path not in allowed_methods or origin not in _CAPTURE_ORIGINS:
                self._send_json(403, {"status": "REJECTED"})
                return
            session.record_bridge_event("PREFLIGHT")
            self.send_response(204)
            assert origin is not None
            self._send_cors_headers(origin)
            self.send_header("Access-Control-Allow-Methods", allowed_methods[self.path])
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-AStock-Capture-Token",
            )
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/envelopes":
                self._send_json(404, {"status": "NOT_FOUND"})
                return
            origin = self.headers.get("Origin")
            token = self.headers.get("X-AStock-Capture-Token")
            if (
                origin not in _CAPTURE_ORIGINS
                or not token
                or not secrets.compare_digest(
                    token,
                    session.session_token,
                )
            ):
                try:
                    rejected_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    rejected_length = 0
                if 0 < rejected_length <= 65_536:
                    self.rfile.read(rejected_length)
                self._send_json(403, {"status": "REJECTED"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_ENVELOPE_BYTES:
                self._send_json(413, {"status": "REJECTED", "message": "invalid size"})
                return
            payload = self.rfile.read(length)
            try:
                ack = session.process_payload(
                    payload,
                    origin=origin,
                    session_token=token,
                )
            except AStockError as exc:
                self._send_json(
                    _http_status(exc.failure_class),
                    {
                        "status": "REJECTED",
                        "failure_class": exc.failure_class.value,
                        "message": str(exc),
                    },
                    cors_origin=origin if origin in _CAPTURE_ORIGINS else None,
                )
                return
            self._send_json(200, asdict(ack), cors_origin=origin)
            if ack.done:
                shutdown = threading.Timer(0.25, self.server.shutdown)
                shutdown.daemon = True
                shutdown.start()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            cors_origin: str | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(
                status,
                body,
                "application/json; charset=utf-8",
                cors_origin=cors_origin,
            )

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cors_origin: str | None = None,
        ) -> None:
            self.send_response(status)
            if cors_origin is not None:
                self._send_cors_headers(cors_origin)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send_cors_headers(self, origin: str) -> None:
            if origin not in _CAPTURE_ORIGINS:
                raise ValueError("CORS origin is not allowlisted")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    return Handler


def _bookmarklet(
    *,
    bridge_origin: str,
    session_token: str,
    source_id: str,
    initial_page: int,
    initial_url: str,
    content_type: str,
    interval_ms: int,
) -> str:
    values = {
        "bridge": bridge_origin,
        "token": session_token,
        "source": source_id,
        "initialPage": initial_page,
        "initial": initial_url,
        "contentType": content_type,
        "interval": interval_ms,
    }
    config = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    script = (
        "javascript:(async()=>{try{"
        f"const c={config};"
        "if(location.origin!=='https://www.zhihu.com'){"
        "alert('Open a logged-in Zhihu page first.');"
        "return;}"
        "let page=c.initialPage,url=c.initial;"
        "for(let guard=0;guard<10000;guard++){"
        "const response=await fetch(url,{credentials:'include',cache:'no-store',"
        "headers:{accept:'application/json,text/plain,*/*'}});"
        "const bytes=new Uint8Array(await response.arrayBuffer());"
        "if(bytes.length===0||bytes.length>67108864)"
        "throw new Error('Unexpected response size: '+bytes.length);"
        "let binary='';"
        "for(let i=0;i<bytes.length;i+=32768)"
        "binary+=String.fromCharCode(...bytes.subarray(i,i+32768));"
        "const envelope={author_source_id:c.source,response_kind:'LISTING',"
        "content_type:c.contentType,listing_page:page,"
        "request_cursor:page===0?null:url,requested_url:url,status_code:response.status,"
        "response_mime:response.headers.get('content-type')||'application/octet-stream',"
        "body_base64:btoa(binary),transport:'CHROME',captured_at:new Date().toISOString()};"
        "const committed=await fetch(c.bridge+'/v1/envelopes',{method:'POST',"
        "mode:'cors',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',"
        "headers:{'Content-Type':'application/json',"
        "'X-AStock-Capture-Token':c.token},body:JSON.stringify(envelope)});"
        "const ack=await committed.json();"
        "if(!committed.ok)"
        "throw new Error(ack.message||ack.status||'Local commit failed');"
        "if(ack.done){alert('AStock capture stopped: '+ack.terminal_condition);return;}"
        "if(!ack.next_url)throw new Error('Local checkpoint has no next URL');"
        "page=ack.next_page;url=ack.next_url;"
        "await new Promise(resolve=>setTimeout(resolve,c.interval));"
        "}throw new Error('Page safety limit reached');"
        "}catch(error){alert('AStock capture error: '+"
        "(error&&error.message?error.message:String(error)));}})()"
    )
    return script


def _http_status(failure: FailureClass) -> int:
    if failure is FailureClass.POLICY_REJECTED:
        return 403
    if failure is FailureClass.CONFLICT:
        return 409
    if failure in {FailureClass.INVALID_RESPONSE, FailureClass.DATA_QUALITY}:
        return 422
    return 500


def _report_failure(report: AuthorCollectionCoverageReport | None) -> str | None:
    if report is None or not report.gaps:
        return None
    failure = report.gaps[0].get("failure_class")
    return str(failure) if failure else None


__all__ = [
    "ZhihuCaptureAck",
    "ZhihuLoopbackCaptureSession",
    "create_loopback_capture_server",
    "loopback_installer_url",
    "loopback_status_url",
    "serve_loopback_capture",
]
