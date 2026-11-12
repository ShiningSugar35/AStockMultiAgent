"""Loopback task bridge between AStock and the installed Chrome extension."""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from astock.social_sources.config import SocialSearchConfig
from astock.social_sources.models import SocialCaptureResult, SocialSearchJob


class SocialCaptureSession:
    def __init__(
        self,
        jobs: tuple[SocialSearchJob, ...],
        *,
        lease_seconds: int,
    ) -> None:
        self.session_id = jobs[0].session_id if jobs else ""
        self.lease_seconds = lease_seconds
        self._condition = Condition()
        self._jobs: dict[str, dict[str, Any]] = {
            job.job_id: {
                "job": job,
                "status": "PENDING",
                "attempts": 0,
                "lease_until": None,
                "error_code": "",
            }
            for job in jobs
        }
        self._results: dict[str, SocialCaptureResult] = {}
        self._last_heartbeat: datetime | None = None
        self._extension_version = ""

    def next_job(self, *, wait_seconds: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0, min(wait_seconds, 20))
        with self._condition:
            while True:
                now = datetime.now(UTC)
                self._requeue_expired(now)
                for _job_id, state in self._jobs.items():
                    if state["status"] != "PENDING":
                        continue
                    state["status"] = "LEASED"
                    state["attempts"] += 1
                    state["lease_until"] = now + timedelta(seconds=self.lease_seconds)
                    job: SocialSearchJob = state["job"]
                    return job.model_dump(mode="json")
                if self._all_terminal():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=min(remaining, 1.0))

    def submit_result(self, payload: Mapping[str, Any]) -> None:
        result = SocialCaptureResult.model_validate(payload)
        with self._condition:
            state = self._jobs.get(result.job_id)
            if state is None or result.session_id != self.session_id:
                raise ValueError("social capture result does not belong to this session")
            job: SocialSearchJob = state["job"]
            if result.platform is not job.platform:
                raise ValueError("social capture platform differs from its assigned job")
            if state["status"] == "COMPLETE":
                return
            if state["status"] != "LEASED":
                raise ValueError("social capture job is not leased")
            expected = {query.query_id: query for query in job.queries}
            received = [item.query_id for item in result.query_results]
            if len(received) != len(set(received)) or not set(received).issubset(expected):
                raise ValueError("social capture query identities are invalid")
            for capture in result.query_results:
                query = expected[capture.query_id]
                if capture.query != query.text or capture.query_kind is not query.kind:
                    raise ValueError("social capture query scope differs from assigned job")
                source_url = urlparse(query.search_url)
                capture_url = urlparse(capture.page_url)
                if (
                    capture_url.scheme != "https"
                    or (capture_url.hostname or "").removeprefix("www.")
                    != (source_url.hostname or "").removeprefix("www.")
                    or (
                        capture.parse_status.value != "LOGIN_REQUIRED"
                        and (
                            capture_url.path != source_url.path
                            or parse_qs(capture_url.query).get("q")
                            != parse_qs(source_url.query).get("q")
                        )
                    )
                ):
                    raise ValueError("social capture page does not match assigned search")
            state["status"] = "COMPLETE"
            state["lease_until"] = None
            self._results[result.job_id] = result
            self._extension_version = result.extension_version
            self._condition.notify_all()

    def heartbeat(self, version: str = "") -> None:
        with self._condition:
            self._last_heartbeat = datetime.now(UTC)
            if version:
                self._extension_version = version[:40]
            self._condition.notify_all()

    def wait(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while not self._all_terminal():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 1.0))
                self._requeue_expired(datetime.now(UTC))
            return all(state["status"] == "COMPLETE" for state in self._jobs.values())

    def results(self) -> tuple[SocialCaptureResult, ...]:
        with self._condition:
            return tuple(self._results[key] for key in sorted(self._results))

    def status(self) -> dict[str, Any]:
        with self._condition:
            counts: dict[str, int] = {}
            for state in self._jobs.values():
                status = str(state["status"])
                counts[status] = counts.get(status, 0) + 1
            return {
                "session_id": self.session_id,
                "jobs": len(self._jobs),
                "counts": counts,
                "extension_last_seen_at": (
                    self._last_heartbeat.isoformat() if self._last_heartbeat else None
                ),
                "extension_version": self._extension_version,
            }

    def _all_terminal(self) -> bool:
        return all(state["status"] in {"COMPLETE", "FAILED"} for state in self._jobs.values())

    def _requeue_expired(self, now: datetime) -> None:
        changed = False
        for state in self._jobs.values():
            if state["status"] != "LEASED":
                continue
            expires = state["lease_until"]
            if expires is None or now < expires:
                continue
            if int(state["attempts"]) < 2:
                state["status"] = "PENDING"
                state["lease_until"] = None
            else:
                state["status"] = "FAILED"
                state["error_code"] = "LEASE_EXPIRED"
            changed = True
        if changed:
            self._condition.notify_all()


class SocialCaptureHTTPServer:
    def __init__(self, config: SocialSearchConfig, session: SocialCaptureSession) -> None:
        self.config = config
        self.session = session
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AStockSocialBridge/1"

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _cors(self) -> bool:
                origin = self.headers.get("Origin", "")
                if origin != outer.config.extension_origin:
                    return False
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-AStock-Bridge-Key",
                )
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")
                return True

            def _send(self, status: int, payload: object | None = None) -> None:
                body = b"" if payload is None else json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                origin = self.headers.get("Origin", "")
                if origin == outer.config.extension_origin:
                    self._cors()
                if body:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _authorized(self) -> bool:
                supplied = self.headers.get("X-AStock-Bridge-Key", "")
                origin = self.headers.get("Origin", "")
                if origin and origin != outer.config.extension_origin:
                    return False
                return hmac.compare_digest(supplied, outer.config.bridge_key)

            def _read_payload(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > outer.config.max_request_bytes:
                    raise ValueError("social capture request size is invalid")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("social capture payload must be an object")
                return payload

            def do_OPTIONS(self) -> None:
                if self.headers.get("Origin", "") != outer.config.extension_origin:
                    self._send(403, {"error": "origin_not_allowed"})
                    return
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Max-Age", "300")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                if not self._authorized():
                    self._send(403, {"error": "bridge_auth_failed"})
                    return
                path = urlparse(self.path).path
                if path == "/v1/next":
                    params = parse_qs(urlparse(self.path).query)
                    wait_seconds = int(params.get("wait", ["0"])[0])
                    job = outer.session.next_job(wait_seconds=wait_seconds)
                    self._send(200, job) if job else self._send(204)
                    return
                if path == "/v1/status":
                    self._send(200, outer.session.status())
                    return
                self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._send(403, {"error": "bridge_auth_failed"})
                    return
                path = urlparse(self.path).path
                try:
                    payload = self._read_payload()
                    if path == "/v1/heartbeat":
                        outer.session.heartbeat(str(payload.get("version", "")))
                        self._send(200, {"ok": True})
                        return
                    if path == "/v1/result":
                        outer.session.submit_result(payload)
                        self._send(200, {"ok": True})
                        return
                except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._send(400, {"error": type(exc).__name__})
                    return
                self._send(404, {"error": "not_found"})

        self._handler = Handler

    def __enter__(self) -> SocialCaptureHTTPServer:
        self._server = ThreadingHTTPServer(
            (self.config.bridge_host, self.config.bridge_port),
            self._handler,
        )
        self._server.daemon_threads = True
        self._thread = Thread(
            target=self._server.serve_forever,
            name="astock-social-capture-bridge",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._server = None
        self._thread = None
