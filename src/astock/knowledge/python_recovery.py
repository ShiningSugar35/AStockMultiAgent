"""Low-frequency Python recovery for already verified Zhihu API boundaries."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.capture_coordinator import ZhihuFullCaptureSession
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.transport import ZhihuHttpTransport, ZhihuResponseTransport
from astock.schemas import (
    KnowledgeSourceRegistry,
    ZhihuBrowserResponseEnvelope,
    ZhihuEndpointTemplateRegistry,
    ZhihuResponseKind,
    ZhihuTransport,
)


class ZhihuPythonRecoveryExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    request_count: int
    accepted_envelope_count: int
    completed_listing_scope_count: int
    blocked_task_count: int
    terminal_condition: str | None
    response_failure: str | None
    limit_reached: bool
    response_kinds: list[str]


class ZhihuPythonRecoveryService:
    """Run the same durable coordinator using credential-free Python HTTP."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        runtime_root: Path,
        parquet_store: ParquetKnowledgeStore,
        source_registry: KnowledgeSourceRegistry,
        endpoint_registry: ZhihuEndpointTemplateRegistry,
        *,
        transport: ZhihuResponseTransport | None = None,
        request_interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_interval_seconds < 2:
            raise ValueError("request_interval_seconds must be at least 2")
        self.state = state
        self.object_store = object_store
        self.runtime_root = runtime_root
        self.parquet_store = parquet_store
        self.source_registry = source_registry
        self.endpoint_registry = endpoint_registry
        self.transport = transport or ZhihuHttpTransport(object_store, state)
        self.request_interval_seconds = request_interval_seconds
        self.sleeper = sleeper

    def run(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        response_kinds: Sequence[ZhihuResponseKind] | None = None,
        max_requests: int | None = None,
    ) -> ZhihuPythonRecoveryExecution:
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be positive")
        session = ZhihuFullCaptureSession(
            self.state,
            self.object_store,
            self.runtime_root,
            self.parquet_store,
            self.source_registry,
            self.endpoint_registry,
            source_ids=source_ids,
            task_response_kinds=response_kinds,
            request_interval_seconds=self.request_interval_seconds,
            expected_transport=ZhihuTransport.PYTHON_HTTP,
        )
        request_count = 0
        last_failure: str | None = None
        while session.initial_request is not None and (
            max_requests is None or request_count < max_requests
        ):
            request = session.initial_request
            response = self.transport.fetch(
                author_source_id=request.author_source_id,
                content_type=request.content_type,
                url=request.requested_url,
            )
            envelope = ZhihuBrowserResponseEnvelope.model_validate(
                {
                    **request.payload(),
                    "status_code": response.status_code,
                    "response_mime": response.content_type,
                    "body_base64": base64.b64encode(response.body).decode("ascii"),
                    "transport": ZhihuTransport.PYTHON_HTTP,
                    "captured_at": response.snapshot.fetched_at,
                }
            )
            ack = session.process_payload(
                envelope.model_dump_json().encode("utf-8"),
                origin=(
                    f"{urlsplit(request.requested_url).scheme}://"
                    f"{urlsplit(request.requested_url).netloc}"
                ),
                session_token=session.session_token,
            )
            request_count += 1
            last_failure = ack.response_failure
            if ack.done:
                break
            self.sleeper(self.request_interval_seconds)
        safe = session.safe_status()
        limit_reached = bool(
            max_requests is not None
            and request_count >= max_requests
            and session.initial_request is not None
        )
        return ZhihuPythonRecoveryExecution(
            status="PARTIAL_LIMIT_REACHED" if limit_reached else str(safe["status"]),
            request_count=request_count,
            accepted_envelope_count=cast(int, safe["accepted_envelope_count"]),
            completed_listing_scope_count=cast(
                int, safe["completed_listing_scope_count"]
            ),
            blocked_task_count=cast(int, safe["blocked_task_count"]),
            terminal_condition=(
                str(safe["terminal_condition"])
                if safe["terminal_condition"] is not None
                else None
            ),
            response_failure=last_failure,
            limit_reached=limit_reached,
            response_kinds=(
                sorted(kind.value for kind in response_kinds)
                if response_kinds is not None
                else []
            ),
        )


__all__ = ["ZhihuPythonRecoveryExecution", "ZhihuPythonRecoveryService"]
