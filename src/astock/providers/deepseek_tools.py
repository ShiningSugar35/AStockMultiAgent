"""Narrow DeepSeek Search/Fetch client. No model APIs, redirects, or hidden retry."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from astock.core.credentials import CredentialResolution, resolve_credential
from astock.core.source_resilience import parse_retry_after_seconds
from astock.schemas.web_tools import WebToolHit, WebToolPolicy, WebToolRequest


@dataclass(frozen=True)
class WebTransportResult:
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    hits: tuple[WebToolHit, ...] = ()
    billing: Literal["CHARGED", "NOT_CHARGED", "UNKNOWN"] = "NOT_CHARGED"
    reason_code: str = ""
    raw: bytes = b""
    retry_after_seconds: int | None = None


class DeepSeekSearchFetchClient:
    def __init__(
        self, project_root: Path, policy: WebToolPolicy,
        *, transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.root = project_root
        self.policy = policy
        self.transport = transport

    @property
    def configured(self) -> bool:
        return resolve_credential("DEEPSEEK_API_KEY", project_root=self.root).configured

    @staticmethod
    def validate_request(request: WebToolRequest) -> None:
        if request.operation == "SEARCH" and len(request.query or "") > 128:
            raise ValueError("DeepSeek query exceeds 128 Unicode characters; refine it explicitly")

    def execute(
        self, request: WebToolRequest, *, credential: CredentialResolution | None = None,
        policy: WebToolPolicy | None = None,
    ) -> WebTransportResult:
        self.validate_request(request)
        credential = credential or resolve_credential("DEEPSEEK_API_KEY", project_root=self.root)
        policy = policy or self.policy
        if not credential.configured:
            return WebTransportResult("FAILED", reason_code="CREDENTIAL_UNAVAILABLE")
        path = "/v1/tools/search" if request.operation == "SEARCH" else "/v1/tools/fetch"
        payload = (
            {"query": request.query, "count": policy.max_results, "need_content": True}
            if request.operation == "SEARCH" else {"url": request.url}
        )
        from astock.core.request_budget import RequestBudgetExhausted, current_request_budget

        timeout = float(policy.timeout_seconds)
        budget = current_request_budget()
        if budget is not None:
            try:
                budget.reserve_attempt()
                timeout = min(timeout, budget.remaining_seconds())
            except RequestBudgetExhausted:
                # The paid ledger claim can safely release: no physical request was sent.
                return WebTransportResult("FAILED", reason_code="REQUEST_BUDGET_EXHAUSTED")
        began = time.monotonic()
        try:
            with httpx.Client(
                transport=self.transport, follow_redirects=False, trust_env=False,
                timeout=timeout,
            ) as client:
                with client.stream(
                    "POST", "https://api.deepseek.com" + path,
                    headers={"Authorization": f"Bearer {credential.value}"},
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        retry_after = parse_retry_after_seconds(response.headers.get("Retry-After"))
                        return WebTransportResult(
                            "FAILED", reason_code=f"HTTP_{response.status_code}",
                            retry_after_seconds=(
                                min(retry_after, 3600) if retry_after is not None else None
                            ),
                        )
                    chunks: list[bytes] = []
                    size = 0
                    # Do not aggregate small network chunks before checking elapsed time.
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > policy.max_response_bytes:
                            return WebTransportResult(
                                "UNKNOWN", billing="UNKNOWN", reason_code="RESPONSE_TOO_LARGE"
                            )
                        if time.monotonic() - began > timeout:
                            return WebTransportResult(
                                "UNKNOWN", billing="UNKNOWN", reason_code="TOTAL_DEADLINE"
                            )
                        chunks.append(chunk)
        except httpx.HTTPError:
            # Client timeout does not prove whether the server completed a paid operation.
            return WebTransportResult("UNKNOWN", billing="UNKNOWN", reason_code="TRANSPORT_UNKNOWN")
        if time.monotonic() - began > timeout:
            return WebTransportResult(
                "UNKNOWN", billing="UNKNOWN", reason_code="TOTAL_DEADLINE"
            )
        raw = b"".join(chunks)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("response is not an object")
            if request.operation == "SEARCH":
                records = body["results"]
                if not isinstance(records, list):
                    raise ValueError("search result is not a list")
                hits = []
                for record in records[:policy.max_results]:
                    try:
                        hits.append(WebToolHit(
                            url=record["url"], title=record.get("title", ""),
                            snippet=record.get("snippet", "")[:4000],
                            content=record.get("content", "")[:262144],
                        ))
                    except (KeyError, TypeError, ValueError, ValidationError):
                        # Invalid/private candidates remain raw evidence, never admitted leads.
                        continue
                billable = bool(records)
            else:
                content = body["content"]
                if not isinstance(content, str):
                    raise ValueError("fetch content is not text")
                billable = bool(content.strip())
                hits = [WebToolHit(
                    url=request.url or "", title=body.get("title", ""), content=content[:262144]
                )] if billable else []
        except (KeyError, TypeError, ValueError, ValidationError):
            return WebTransportResult(
                "UNKNOWN", billing="UNKNOWN", reason_code="RESPONSE_SCHEMA_UNKNOWN", raw=raw
            )
        return WebTransportResult(
            "SUCCEEDED", tuple(hits), "CHARGED" if billable else "NOT_CHARGED",
            "RESULT" if hits else "EMPTY_OR_REJECTED_RESULTS", raw,
        )
