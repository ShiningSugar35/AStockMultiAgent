"""Host-first execution on the existing source router, checkpoint and evidence stores."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import HttpUrl

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.providers.deepseek_tools import DeepSeekSearchFetchClient, WebTransportResult
from astock.research.web_discovery import (
    EXTERNAL_DATA_NOTICE,
    admit_proposals,
    build_lead,
    load_official_domains,
    load_web_discovery_config,
    normalize_result_url,
)
from astock.research.web_tool_repository import WebToolOperationRepository, operation_id
from astock.schemas.market import (
    AccessTransport,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)
from astock.schemas.web_discovery import WebDiscoveryInput, WebDiscoveryRawResult
from astock.schemas.web_tools import (
    HostWebCapabilities,
    HostWebResult,
    WebToolPolicy,
    WebToolRequest,
)


def load_web_tool_policy(root: Path) -> WebToolPolicy:
    return WebToolPolicy.model_validate(yaml.safe_load(
        (root / "configs" / "web_tools.yaml").read_text(encoding="utf-8")
    ))


class WebToolExecutionService:
    def __init__(
        self, root: Path, state: StateStore, objects: ObjectStore, *,
        policy: WebToolPolicy | None = None,
        client: DeepSeekSearchFetchClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root, self.state, self.objects = root, state, objects
        self.policy = policy or load_web_tool_policy(root)
        self.client = client or DeepSeekSearchFetchClient(root, self.policy)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.repository = WebToolOperationRepository(state)
        self.router = SourceAccessRouter(state)

    def run(
        self, request: WebToolRequest, host: HostWebCapabilities | None = None,
    ) -> dict[str, Any]:
        """Return an exact host handoff or execute one authorized paid fallback."""
        now = self.clock()
        if request.fresh_after > now:
            raise ValueError("Web freshness boundary cannot be in the future")
        self._validate_continuation(request)
        key = operation_id(request)
        previous = self.repository.get(key)
        if previous is not None and previous["status"] != "HOST_FAILED":
            return self._view(self._recover(previous))
        cached = self._cached_content(request)
        if cached is not None:
            return cached
        if host is None:
            return {
                "operation_id": key, "status": "HOST_CAPABILITY_REQUIRED",
                "reason_code": "DECLARE_CURRENT_INVOCATION_TOOLS",
            }
        if (
            host.observed_at > now or host.expires_at <= now
            or now - host.observed_at > timedelta(
                seconds=self.policy.host_capability_max_age_seconds
            )
        ):
            return {"operation_id": key, "status": "HOST_CAPABILITY_REQUIRED",
                    "reason_code": "HOST_CAPABILITIES_STALE"}
        tool = host.tool_for(request.operation)
        if tool.availability == "UNKNOWN" or (
            tool.availability == "AVAILABLE" and tool.billing == "UNKNOWN"
        ):
            return {"operation_id": key, "status": "HOST_CAPABILITY_REQUIRED",
                    "reason_code": "HOST_TOOL_OR_BILLING_UNKNOWN"}
        included = (
            tool.availability == "AVAILABLE" and tool.billing == "INCLUDED"
            and previous is None
        )
        capability = "web.search" if request.operation == "SEARCH" else "web.fetch"
        candidates = [
            TransportCapability(
                source_id="host-included-web", transport=AccessTransport.SEARCH,
                requested_capabilities=[capability], available=included,
                reason="current invocation included tool" if included else "host capability gap",
                source_class=SourceClass.REPUTABLE_WEB_SEARCH, formal_eligible=False,
                cost_efficiency_score=Decimal("1"),
            ),
            TransportCapability(
                source_id="deepseek-tools", transport=AccessTransport.API,
                requested_capabilities=[capability],
                available=self.policy.deepseek_enabled and self.client.configured,
                reason="bounded standalone Search/Fetch fallback",
                source_class=SourceClass.REPUTABLE_WEB_SEARCH, formal_eligible=False,
                production_backup=True,
            ),
        ]
        decision = self.router.decide(
            SourceAccessRequest(requested_capability=capability), candidates
        )
        if decision.selected_source_id is None:
            return {"operation_id": key, "status": "NO_AVAILABLE_TOOL",
                    "reason_code": "HOST_GAP_AND_FALLBACK_UNAVAILABLE"}
        if decision.selected_source_id == "deepseek-tools":
            self.client.validate_request(request)
        cursor, acquired = self.repository.claim(
            request, source=decision.selected_source_id,
            host=host.model_dump(mode="json") if included else None,
            policy=self.policy, now=now,
        )
        if not acquired or included:
            return self._view(cursor)
        # A crash anywhere after reservation retains a possibly chargeable slot.
        result = self.client.execute(request)
        return self._publish(cursor, result, observed_at=self.clock())

    def complete_host(self, response: HostWebResult) -> dict[str, Any]:
        cursor = self.repository.get(response.operation_id)
        if cursor is None or cursor.get("host") is None:
            raise ValueError("host result has no pending host dispatch")
        host = HostWebCapabilities.model_validate(cursor["host"])
        request = WebToolRequest.model_validate(cursor["request"])
        response_hash = content_hash(response.model_dump(mode="json"))
        if cursor.get("host_result_hash") == response_hash:
            return self._view(self._recover(cursor))
        if (
            cursor["status"] != "WAITING_HOST"
            or cursor["dispatch_id"] != response.dispatch_id
            or host.invocation_id != response.invocation_id
            or host.tool_for(request.operation).tool_name != response.tool_name
        ):
            raise ValueError("host result does not match the owning invocation and dispatch")
        now = self.clock()
        if (
            response.observed_at < datetime.fromisoformat(cursor["started_at"])
            or response.observed_at < request.fresh_after
            or response.observed_at > now + timedelta(seconds=5)
        ):
            raise ValueError("host result observation time is outside the request")
        raw = canonical_json_bytes(response.model_dump(mode="json"))
        if len(raw) > self.policy.max_response_bytes:
            raise ValueError("host result exceeds the response byte budget")
        self._validate_continuation(request)
        if request.operation == "FETCH" and (
            len(response.hits) > 1
            or any(hit.url != request.url for hit in response.hits)
        ):
            raise ValueError("FETCH result must match the requested URL")
        result = WebTransportResult(
            status="SUCCEEDED" if response.status == "SUCCEEDED" else "FAILED",
            hits=tuple(response.hits[:self.policy.max_results]), raw=raw,
            reason_code="HOST_" + response.status,
        )
        return self._publish(
            cursor, result, observed_at=response.observed_at, host_result_hash=response_hash,
            cancelled=response.status == "CANCELLED",
        )

    def _publish(
        self, cursor: dict[str, Any], result: WebTransportResult, *, observed_at: datetime,
        host_result_hash: str | None = None, cancelled: bool = False,
    ) -> dict[str, Any]:
        request = WebToolRequest.model_validate(cursor["request"])
        engine = "host-included" if cursor["host"] else "deepseek-tools"
        config = load_web_discovery_config(self.root)
        lead = build_lead(
            WebDiscoveryInput(
                query=request.query or request.evidence_scope,
                requested_capability=request.requested_capability,
                expected_fact=request.expected_fact,
                results=tuple(WebDiscoveryRawResult(
                    url=HttpUrl(hit.url), title=hit.title, snippet=hit.snippet, engine=engine
                ) for hit in result.hits),
            ),
            config=config,
            official_domains=load_official_domains(
                request.requested_capability, root=self.root, config=config
            ),
        )
        admission = admit_proposals(lead)
        raw_ref = self.objects.put_bytes(result.raw or b"{}")
        status = result.status
        if cursor["host"] and result.status == "FAILED":
            status = "CANCELLED" if cancelled else "HOST_FAILED"
        billing = "INCLUDED" if cursor["host"] else result.billing
        payload = {
            "schema_version": "web-tool-result-v1", "operation_id": cursor["operation_id"],
            "dispatch_id": cursor["dispatch_id"], "source": cursor["source"],
            "status": status, "billing": billing, "observed_at": observed_at.isoformat(),
            "reason_code": result.reason_code, "raw_object_hash": raw_ref.sha256,
            "host_result_hash": host_result_hash,
            "request": request.model_dump(mode="json"),
            "hits": [hit.model_dump(mode="json") for hit in result.hits],
            "lead": lead.model_dump(mode="json"), "admission": admission.model_dump(mode="json"),
            "formal_use_allowed": False, "exhaustive_proof_allowed": False,
            "retry_after_seconds": result.retry_after_seconds,
        }
        ref = self.objects.put_json(payload)
        artifact_id = self._artifact_id(cursor)
        self.state.register_artifact(
            artifact_id=artifact_id, artifact_type="WebToolResult",
            schema_version="web-tool-result-v1", object_hash=ref.sha256,
            input_hashes=[raw_ref.sha256] + (
                [cursor["prior_result_hash"]] if cursor.get("prior_result_hash") else []
            ),
        )
        updated = self.repository.finish(
            cursor["operation_id"], cursor["dispatch_id"], status=status, billing=billing,
            result_hash=ref.sha256, artifact_id=artifact_id, reason_code=result.reason_code,
            host_result_hash=host_result_hash, now=self.clock(),
            content_entries=self._content_entries(payload, artifact_id),
            retry_after_seconds=result.retry_after_seconds,
        )
        return self._view(updated)

    def _content_entries(
        self, payload: dict[str, Any], artifact_id: str,
    ) -> dict[str, dict[str, Any]]:
        if payload["status"] != "SUCCEEDED":
            return {}
        request = WebToolRequest.model_validate(payload["request"])
        config = load_web_discovery_config(self.root)
        allowed = set(payload["admission"]["discovery_allowed_urls"])
        return {
            self._content_key(request, hit["url"]): {
                "status": "AVAILABLE", "observed_at": payload["observed_at"],
                "artifact_id": artifact_id, "url": hit["url"],
            }
            for hit in payload["hits"]
            if hit["content"] and normalize_result_url(hit["url"], config=config) in allowed
        }

    def _recover(self, cursor: dict[str, Any]) -> dict[str, Any]:
        if cursor["status"] not in {"WAITING_HOST", "PAID_RUNNING", "SUCCEEDED"}:
            return cursor
        artifact_id = self._artifact_id(cursor)
        record = self.state.artifact_record(artifact_id)
        if record is None or not self.objects.verify(record["object_hash"]):
            return self.repository.reconcile_owner(cursor["operation_id"], self.clock())
        payload = json.loads(self.objects.get_bytes(record["object_hash"]))
        return self.repository.finish(
            cursor["operation_id"], cursor["dispatch_id"], status=payload["status"],
            billing=payload["billing"], result_hash=record["object_hash"],
            artifact_id=artifact_id, reason_code=payload["reason_code"],
            host_result_hash=payload.get("host_result_hash"), now=self.clock(),
            content_entries=self._content_entries(payload, artifact_id),
            retry_after_seconds=payload.get("retry_after_seconds"),
        )

    def _cached_content(self, request: WebToolRequest) -> dict[str, Any] | None:
        if request.operation != "FETCH" or request.url is None:
            return None
        record = self.state.get_checkpoint(
            "web-tool-content", self._content_key(request, request.url)
        )
        if record is None or not record.get("object_hash"):
            return None
        cursor = record["cursor"]
        if (
            datetime.fromisoformat(cursor["observed_at"]) < request.fresh_after
            or not self.objects.verify(record["object_hash"])
        ):
            return None
        return {
            "operation_id": operation_id(request), "status": "CONTENT_REUSED",
            "source": "existing-evidence", "artifact_id": cursor["artifact_id"],
            "result_hash": record["object_hash"], "url": request.url,
            "additional_paid_calls": 0, "formal_use_allowed": False,
        }

    def _validate_continuation(self, request: WebToolRequest) -> None:
        if request.continuation_id is None:
            return
        row = self.state.get_checkpoint("current-research-continuation", request.continuation_id)
        if row is None or not row.get("object_hash"):
            raise ValueError("unknown current-research continuation")
        record = json.loads(self.objects.get_bytes(row["object_hash"]))
        if record["request_id"] != request.request_id:
            raise ValueError("Web request belongs to another investor request")
        if record["status"] != "AUTO_RESOLUTION_REQUIRED" or not any(
            task["task_id"] == request.task_id and task["status"] == "PENDING"
            for task in record["external_tasks"]
        ):
            raise ValueError("Web request requires an active matching evidence gap")

    def _view(self, cursor: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in cursor.items() if key not in {
            "request", "host", "budget_keys", "host_result_hash",
        }}
        if cursor.get("status") == "WAITING_HOST":
            host = HostWebCapabilities.model_validate(cursor["host"])
            request = WebToolRequest.model_validate(cursor["request"])
            tool = host.tool_for(request.operation)
            view["host_call"] = {
                "invocation_id": host.invocation_id, "tool_name": tool.tool_name,
                "operation": request.operation, "query": request.query, "url": request.url,
                "max_results": self.policy.max_results,
            }
            view["notice"] = (
                "Use the current host tool, then submit HostWebResult; do not pay in parallel."
            )
        if cursor.get("result_hash") and self.objects.verify(cursor["result_hash"]):
            payload = json.loads(self.objects.get_bytes(cursor["result_hash"]))
            allowed = set(payload["admission"]["discovery_allowed_urls"])
            config = load_web_discovery_config(self.root)
            view["results"] = [
                {"url": hit["url"], "title": hit["title"],
                 "snippet": hit["snippet"][:500], "content_available": bool(hit["content"])}
                for hit in payload["hits"]
                if normalize_result_url(hit["url"], config=config) in allowed
            ]
            view["formal_use_allowed"] = False
            view["exhaustive_proof_allowed"] = False
        view["evidence_notice"] = EXTERNAL_DATA_NOTICE
        return view

    @staticmethod
    def _artifact_id(cursor: dict[str, Any]) -> str:
        return f"WebToolResult:{cursor['operation_id']}:{cursor['dispatch_id']}"

    def _content_key(self, request: WebToolRequest, url: str) -> str:
        return content_hash({
            "request_id": request.request_id, "evidence_scope": request.evidence_scope,
            "capability": request.requested_capability,
            "url": normalize_result_url(url, config=load_web_discovery_config(self.root)),
        })
