"""Host-first execution on the existing source router, checkpoint and evidence stores."""
from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml
from pydantic import HttpUrl

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.providers.deepseek_tools import DeepSeekSearchFetchClient, WebTransportResult
from astock.research.policy import load_default_current_research_policy
from astock.research.web_discovery import (
    EXTERNAL_DATA_NOTICE,
    admit_proposals,
    build_lead,
    load_official_domains,
    load_web_discovery_config,
    normalize_result_url,
)
from astock.research.web_tool_repository import WebToolOperationRepository, operation_id
from astock.schemas import AgentSourceProposal, DocumentType
from astock.schemas.market import (
    AccessTransport,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)
from astock.schemas.research_acquisition import AcquisitionCapability
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuation,
    CurrentResearchContinuationStatus,
    CurrentResearchExternalTask,
    ExternalResearchTaskStatus,
)
from astock.schemas.web_discovery import WebDiscoveryInput, WebDiscoveryRawResult
from astock.schemas.web_tools import (
    CurrentResearchWebBatch,
    HostWebCapabilities,
    HostWebResult,
    WebOfficialDocumentSelection,
    WebToolHit,
    WebToolPolicy,
    WebToolRequest,
)
from astock.settings import ProjectPaths

if TYPE_CHECKING:
    from astock.core.credentials import CredentialResolution
    from astock.core.request_budget import RequestExecutionBudget
    from astock.research.continuation import CurrentResearchContinuationService
    from astock.research.process_worker import KillableResearchWorker
    from astock.research.sla_runtime import CancellableTaskHandler
    from astock.schemas.research_sla import ResearchSchedulerTask


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
        request = self._validate_continuation(request, bind_dispatch=True)
        key = operation_id(request)
        previous = self.repository.get(key)
        if previous is not None and previous["status"] != "HOST_FAILED":
            return self._view(self._recover(previous))
        cached = self._cached_content(request)
        if cached is not None:
            return cached
        shared = self._existing_shared_web(request)
        if shared is not None:
            return shared
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
            return self._shared_paid_dispatch(request)
        cursor, _ = self.repository.claim(
            request, source=decision.selected_source_id,
            host=host.model_dump(mode="json") if included else None,
            policy=self.policy, now=now,
        )
        return self._view(cursor)


    def _shared_identity(
        self, request: WebToolRequest, *, provider: str, target: str,
        selection: WebOfficialDocumentSelection | None = None,
        authorization_scope: str | None = None, wire_max_results: int | None = None,
    ) -> dict[str, Any]:
        from astock.core.credentials import resolve_credential
        from astock.core.source_policy_gate import SourcePolicyGate

        context = self._continuation_context(request)
        subject: dict[str, Any] = {
            "declared_scope": _canonical_query(request.evidence_scope),
            "fact": _canonical_query(request.expected_fact),
        }
        if context is not None:
            record, task = context
            subject = {
                "company_id": record.company_id, "market": record.market.value,
                "capability": task.capability.value, "lookback_days": record.lookback_days,
                "fact": _canonical_query(request.expected_fact),
            }
        subject["as_of_date"] = request.fresh_after.date().isoformat()
        if selection is not None:
            subject.update(
                period_end=selection.period_end.isoformat() if selection.period_end else None,
                document_type=selection.document_type,
                published_at=selection.published_at.isoformat(),
            )
        registry = SourcePolicyGate().registry
        authorization = "PUBLIC_UNAUTHENTICATED"
        if provider == "deepseek-tools":
            if authorization_scope is None:
                credential = resolve_credential("DEEPSEEK_API_KEY", project_root=self.root)
                authorization_scope = content_hash({"credential": credential.value})
            authorization = authorization_scope
        return {
            "schema_version": "owned-web-acquisition-v1", "provider": provider,
            "operation": "OFFICIAL_PDF" if selection is not None else request.operation,
            "target": target, "subject": subject, "capability": request.requested_capability,
            "authorization_scope": authorization,
            "source_policy": {"registry_version": registry.schema_version,
                              "scope": "PUBLIC_EXACT_ITEM" if selection else "PUBLIC_DISCOVERY"},
            "wire": {
                "max_results": wire_max_results or self.client.policy.max_results,
            } if selection is None else {},
            "freshness": {
                "kind": "OFFICIAL_RAW_MAX_AGE" if selection else "NOT_BEFORE_REQUEST",
                "max_age_seconds": self.policy.shared_pdf_max_age_seconds if selection
                else self.policy.shared_search_max_age_seconds,
            },
        }

    def _shared_deadline(self, request: WebToolRequest) -> datetime:
        deadline = self.clock() + timedelta(seconds=self.policy.timeout_seconds)
        context = self._continuation_context(request)
        return min(deadline, context[0].deadline_at) if context is not None else deadline

    @staticmethod
    def _shared_result_id(acquisition: dict[str, Any]) -> str:
        return (
            f"WebSharedAcquisition:{acquisition['acquisition_id']}:"
            f"{acquisition['generation']}"
        )

    def _shared_result(self, acquisition: dict[str, Any]) -> dict[str, Any] | None:
        identifier = self._shared_result_id(acquisition)
        artifact = self.state.artifact_record(identifier)
        if artifact is None:
            return self._freeze_undispatched_acquisition(acquisition)
        if artifact["type"] != "WebSharedAcquisition" or not self.objects.verify(
            artifact["object_hash"]
        ):
            raise ValueError("Shared acquisition receipt is corrupt")
        payload = json.loads(self.objects.get_bytes(artifact["object_hash"]))
        if (
            payload["acquisition_id"] != acquisition["acquisition_id"]
            or payload["generation"] != acquisition["generation"]
            or payload["identity_hash"] != acquisition["acquisition_id"]
        ):
            raise ValueError("Shared acquisition receipt has another identity")
        head = self.repository.acquisition(acquisition["acquisition_id"], self.clock())
        if head is not None and head["generation"] == acquisition["generation"]:
            self.repository.finish_acquisition(
                head["acquisition_id"], head["generation"], status=payload["status"],
                now=self.clock(), observed_at=datetime.fromisoformat(payload["observed_at"]),
                result_hash=artifact["object_hash"],
            )
        return payload

    def _freeze_undispatched_acquisition(
        self, acquisition: dict[str, Any],
    ) -> dict[str, Any] | None:
        head = self.repository.acquisition(acquisition["acquisition_id"], self.clock())
        if (
            head is None or head["generation"] != acquisition["generation"]
            or head["status"] != "NOT_DISPATCHED"
        ):
            return None
        is_pdf = head["identity"]["operation"] == "OFFICIAL_PDF"
        result: dict[str, Any] = {"reason_code": "SHARED_NOT_DISPATCHED"}
        hashes: list[str] = []
        if not is_pdf:
            raw_ref = self.objects.put_bytes(b"{}")
            hashes.append(raw_ref.sha256)
            result.update(
                hits=[], billing="NOT_CHARGED", raw_object_hash=raw_ref.sha256,
                retry_after_seconds=None,
            )
        return self._freeze_shared_result(
            head, kind="OFFICIAL_PDF" if is_pdf else "WEB_TRANSPORT", status="FAILED",
            observed_at=datetime.fromisoformat(head["started_at"]),
            result=result, input_hashes=hashes,
        )

    def _freeze_shared_result(
        self, acquisition: dict[str, Any], *, kind: str, status: str,
        result: dict[str, Any], observed_at: datetime, input_hashes: list[str],
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "web-shared-result-v1",
            "acquisition_id": acquisition["acquisition_id"],
            "generation": acquisition["generation"], "identity_hash": acquisition["acquisition_id"],
            "kind": kind, "status": status,
            "observed_at": observed_at.isoformat(), "result": result,
        }
        ref = self.objects.put_json(payload)
        self.state.register_artifact(
            artifact_id=self._shared_result_id(acquisition), artifact_type="WebSharedAcquisition",
            schema_version="web-shared-result-v1", object_hash=ref.sha256,
            input_hashes=input_hashes,
        )
        self.repository.finish_acquisition(
            acquisition["acquisition_id"], acquisition["generation"], status=status,
            result_hash=ref.sha256, observed_at=observed_at, now=self.clock(),
        )
        return payload

    def _existing_shared_web(self, request: WebToolRequest) -> dict[str, Any] | None:
        target = _canonical_query(request.query or "") if request.operation == "SEARCH" else (
            normalize_result_url(request.url or "", config=load_web_discovery_config(self.root))
        )
        if not target:
            return None
        identity = self._shared_identity(request, provider="deepseek-tools", target=target)
        now = self.clock()
        acquisition, _ = self.repository.claim_acquisition(
            identity, provider="deepseek-tools", domain="api.deepseek.com", policy=self.policy,
            now=now, deadline_at=self._shared_deadline(request),
            fresh_after=max(request.fresh_after, now - timedelta(
                seconds=self.policy.shared_search_max_age_seconds,
            )), allow_start=False,
        )
        if "generation" not in acquisition:
            return None
        payload = self._shared_result(acquisition)
        if payload is None or payload["status"] != "SUCCEEDED":
            return None
        if datetime.fromisoformat(payload["observed_at"]) < request.fresh_after:
            return None
        cursor = self.repository.claim_shared_consumer(request, acquisition, now=now)
        return self._view(self._recover(cursor))

    def _shared_paid_dispatch(self, request: WebToolRequest) -> dict[str, Any]:
        from astock.core.credentials import resolve_credential

        target = _canonical_query(request.query or "") if request.operation == "SEARCH" else (
            normalize_result_url(request.url or "", config=load_web_discovery_config(self.root))
        )
        if not target:
            raise ValueError("Shared Web dispatch requires a canonical target")
        credential = resolve_credential("DEEPSEEK_API_KEY", project_root=self.root)
        wire_policy = self.client.policy.model_copy(deep=True)
        identity = self._shared_identity(
            request, provider="deepseek-tools", target=target,
            authorization_scope=content_hash({"credential": credential.value}),
            wire_max_results=wire_policy.max_results,
        )
        now = self.clock()
        cursor, acquisition, acquired = self.repository.claim_paid_acquisition(
            request, identity, policy=self.policy, now=now,
            deadline_at=self._shared_deadline(request),
            fresh_after=max(request.fresh_after, now - timedelta(
                seconds=self.policy.shared_search_max_age_seconds,
            )),
        )
        if acquired:
            assert acquisition is not None
            return self._dispatch_shared_owner(
                request, cursor, acquisition, credential=credential, wire_policy=wire_policy,
            )
        if acquisition is not None:
            self._shared_result(acquisition)  # Freeze a proven non-dispatch before reuse.
        return self._view(self._recover(cursor)) if "dispatch_id" in cursor else cursor

    def _dispatch_shared_owner(
        self, request: WebToolRequest, cursor: dict[str, Any], acquisition: dict[str, Any], *,
        credential: CredentialResolution, wire_policy: WebToolPolicy,
    ) -> dict[str, Any]:
        started = False
        frozen = False
        try:
            self.repository.start_acquisition(
                acquisition["acquisition_id"], acquisition["generation"], self.clock(),
            )
            started = True
            result = self.client.execute(request, credential=credential, policy=wire_policy)
            raw_ref = self.objects.put_bytes(result.raw or b"{}")
            observed = self.clock()
            self._freeze_shared_result(
                acquisition, kind="WEB_TRANSPORT", status=result.status, observed_at=observed,
                result={
                    "hits": [hit.model_dump(mode="json") for hit in result.hits],
                    "billing": result.billing, "reason_code": result.reason_code,
                    "raw_object_hash": raw_ref.sha256,
                    "retry_after_seconds": result.retry_after_seconds,
                }, input_hashes=[raw_ref.sha256],
            )
            frozen = True
            return self._publish(cursor, result, observed_at=observed)
        finally:
            try:
                if not frozen:
                    self._close_shared_dispatch(acquisition, started=started)
            finally:
                self.repository.shared_consumer_unknown(
                    cursor["operation_id"], cursor["dispatch_id"], self.clock(),
                )

    def _close_shared_dispatch(self, acquisition: dict[str, Any], *, started: bool) -> None:
        if self.state.artifact_record(self._shared_result_id(acquisition)) is not None:
            # Immutable bytes may commit before the mutable head; repair only that head.
            self._shared_result(acquisition)
            return
        self.repository.finish_acquisition(
            acquisition["acquisition_id"], acquisition["generation"],
            status="UNKNOWN" if started else "NOT_DISPATCHED", now=self.clock(),
            reason_code="OWNED_DISPATCH_INTERRUPTED" if started else "NO_PHYSICAL_DISPATCH",
        )
        if not started:
            self._shared_result(acquisition)

    def _recover_shared_web(self, cursor: dict[str, Any]) -> dict[str, Any]:
        acquisition = {
            "acquisition_id": cursor["shared_acquisition_id"],
            "generation": cursor["shared_generation"],
        }
        payload = self._shared_result(acquisition)
        if payload is None:
            head = self.repository.acquisition(acquisition["acquisition_id"], self.clock())
            if head is None or head["generation"] != acquisition["generation"]:
                raise ValueError("Shared Web operation lost its immutable acquisition")
            if head["status"] == "NOT_DISPATCHED":
                return self._published_cursor(
                    cursor, WebTransportResult("FAILED", reason_code="SHARED_NOT_DISPATCHED"),
                    self.clock(),
                )
            if head["status"] == "UNKNOWN":
                return self.repository.shared_consumer_unknown(
                    cursor["operation_id"], cursor["dispatch_id"], self.clock(),
                )
            return cursor
        if payload["kind"] != "WEB_TRANSPORT":
            raise ValueError("Shared Web result has an incompatible kind")
        request = WebToolRequest.model_validate(cursor["request"])
        observed = datetime.fromisoformat(payload["observed_at"])
        if payload["status"] == "SUCCEEDED" and observed < request.fresh_after:
            return self._published_cursor(
                cursor, WebTransportResult("FAILED", reason_code="SHARED_RESULT_STALE"),
                self.clock(),
            )
        result_payload = payload["result"]
        if not self.objects.verify(result_payload["raw_object_hash"]):
            raise ValueError("Shared Web raw bytes are corrupt")
        result = WebTransportResult(
            status=payload["status"],
            hits=tuple(WebToolHit.model_validate(item) for item in result_payload["hits"]),
            billing=result_payload["billing"] if cursor.get("shared_owner") else "NOT_CHARGED",
            reason_code=result_payload["reason_code"],
            raw=self.objects.get_bytes(result_payload["raw_object_hash"]),
            retry_after_seconds=result_payload.get("retry_after_seconds"),
        )
        return self._published_cursor(
            cursor, result, observed,
        )

    def _published_cursor(
        self, cursor: dict[str, Any], result: WebTransportResult, observed: datetime,
    ) -> dict[str, Any]:
        self._publish(cursor, result, observed_at=observed)
        completed = self.repository.get(cursor["operation_id"])
        assert completed is not None
        return completed


    def _pdf_shared_scope(
        self, request: WebToolRequest, selection: WebOfficialDocumentSelection,
        proposal: AgentSourceProposal,
    ) -> tuple[dict[str, Any], datetime]:
        from astock.core.source_policy_gate import SourcePolicyGate

        decision = SourcePolicyGate().validate(proposal)
        if not decision.allowed or not decision.formal_eligible or decision.source_id is None:
            raise ValueError("Shared PDF scope no longer satisfies source policy")
        canonical = normalize_result_url(
            str(proposal.candidate_url), config=load_web_discovery_config(self.root),
        )
        if canonical is None:
            raise ValueError("Shared PDF has no canonical URL")
        identity = self._shared_identity(
            request, provider="official-pdf", target=canonical, selection=selection,
        )
        identity["source_policy"]["source_id"] = decision.source_id
        fresh_after = max(
            selection.published_at,
            self.clock() - timedelta(seconds=self.policy.shared_pdf_max_age_seconds),
        )
        return identity, fresh_after

    def _shared_official_original(
        self, request: WebToolRequest, selection: WebOfficialDocumentSelection,
        proposal: AgentSourceProposal, *, allow_network: bool,
    ) -> tuple[bytes, datetime, str, tuple[str, ...]] | None:
        from astock.core.request_budget import RequestBudgetExhausted, current_request_budget

        identity, fresh_after = self._pdf_shared_scope(request, selection, proposal)
        budget = current_request_budget()
        can_start = allow_network and (
            budget is None or (budget.remaining_seconds() > 0 and budget.remaining_attempts() > 0)
        )
        acquisition, acquired = self.repository.claim_acquisition(
            identity, provider="official-pdf", domain=urlsplit(identity["target"]).hostname or "",
            policy=self.policy, now=self.clock(), deadline_at=self._shared_deadline(request),
            fresh_after=fresh_after, allow_start=can_start,
        )
        if "generation" not in acquisition:
            if allow_network and not can_start:
                raise RequestBudgetExhausted("Official PDF request budget exhausted")
            if allow_network:
                raise ValueError(acquisition.get("reason_code") or acquisition["status"])
            return None
        if acquired:
            return self._download_shared_original(acquisition, proposal)
        payload = self._shared_result(acquisition)
        if payload is None and not allow_network:
            return None
        deadline = time.monotonic() + max(
            0.0, (self._shared_deadline(request) - self.clock()).total_seconds(),
        )
        while payload is None:
            if acquisition["status"] == "UNKNOWN":
                raise RuntimeError("Shared original completion is unknown; do not redispatch")
            if time.monotonic() >= deadline:
                raise TimeoutError("Shared original owner did not complete within this request")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            current = self.repository.acquisition(acquisition["acquisition_id"], self.clock())
            if current is not None and current["generation"] == acquisition["generation"]:
                acquisition = current
            payload = self._shared_result(acquisition)
        return self._validated_shared_original(payload, proposal, fresh_after)

    def _download_shared_original(
        self, acquisition: dict[str, Any], proposal: AgentSourceProposal,
    ) -> tuple[bytes, datetime, str, tuple[str, ...]]:
        from astock.core.request_budget import RequestBudgetExhausted
        from astock.core.source_policy_gate import SourcePolicyGate
        from astock.documents.public_pdf import fetch_official_pdf

        frozen = False
        started = False
        try:
            self.repository.start_acquisition(
                acquisition["acquisition_id"], acquisition["generation"], self.clock(),
            )
            started = True
            downloaded = fetch_official_pdf(
                proposal, gate=SourcePolicyGate(), timeout_seconds=self.policy.timeout_seconds,
                max_bytes=self.policy.max_document_bytes,
                max_redirects=self.policy.max_document_redirects,
                before_hop=lambda url: self.repository.move_acquisition_domain(
                    acquisition["acquisition_id"], acquisition["generation"],
                    urlsplit(url).hostname or "", policy=self.policy, now=self.clock(),
                ),
            )
            raw_ref = self.objects.put_bytes(downloaded.content)
            observed = self.clock()
            self._freeze_shared_result(
                acquisition, kind="OFFICIAL_PDF", status="SUCCEEDED", observed_at=observed,
                result={"raw_object_hash": raw_ref.sha256,
                        "original_url": str(proposal.candidate_url),
                        "final_url": downloaded.final_url,
                        "redirect_chain": list(downloaded.redirect_chain)},
                input_hashes=[raw_ref.sha256],
            )
            frozen = True
            return downloaded.content, observed, downloaded.final_url, downloaded.redirect_chain
        except (ValueError, RequestBudgetExhausted) as exc:
            self._freeze_shared_result(
                acquisition, kind="OFFICIAL_PDF", status="FAILED", observed_at=self.clock(),
                result={"reason_code": type(exc).__name__.upper()}, input_hashes=[],
            )
            frozen = True
            raise
        finally:
            if not frozen:
                self._close_shared_dispatch(acquisition, started=started)

    def _validated_shared_original(
        self, payload: dict[str, Any], proposal: AgentSourceProposal, fresh_after: datetime,
    ) -> tuple[bytes, datetime, str, tuple[str, ...]]:
        from astock.core.source_policy_gate import SourcePolicyGate

        if payload["kind"] != "OFFICIAL_PDF" or payload["status"] != "SUCCEEDED":
            raise ValueError("Shared original receipt is not a completed PDF")
        observed = datetime.fromisoformat(payload["observed_at"])
        if observed < fresh_after:
            raise ValueError("Shared original does not satisfy the requested freshness")
        result = payload["result"]
        if not self.objects.verify(result["raw_object_hash"]):
            raise ValueError("Shared official original bytes are corrupt")
        config = load_web_discovery_config(self.root)
        if normalize_result_url(result["original_url"], config=config) != normalize_result_url(
            str(proposal.candidate_url), config=config,
        ):
            raise ValueError("Shared official original URL differs from this request")
        gate = SourcePolicyGate()
        for url in {*result["redirect_chain"], result["final_url"]}:
            hop = proposal.model_copy(update={"candidate_url": HttpUrl(url)})
            decision = gate.validate(hop)
            if not decision.allowed or not decision.formal_eligible:
                raise ValueError("Shared original no longer satisfies current source policy")
        raw = self.objects.get_bytes(result["raw_object_hash"])
        if len(raw) > self.policy.max_document_bytes or not raw.lstrip().startswith(b"%PDF-"):
            raise ValueError("Shared original violates the current byte/PDF contract")
        return raw, observed, result["final_url"], tuple(result["redirect_chain"])

    def _has_shared_original(
        self, cursor: dict[str, Any], selection: WebOfficialDocumentSelection,
    ) -> bool:
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_id is None:
            return False
        task = self.selected_original_task(request.continuation_id, selection)
        capability = official_document_capability(task.capability)
        if capability is None:
            return False
        proposal = AgentSourceProposal(
            requested_capability=capability, query=request.query or request.evidence_scope,
            candidate_url=HttpUrl(selection.url), expected_fact=request.expected_fact,
            preferred_source_class=SourceClass.PRIMARY_OFFICIAL_WEB, formal_use=True,
            require_complete=False, reason="Verify reusable public original",
        )
        return self._shared_official_original(
            request, selection, proposal, allow_network=False,
        ) is not None

    def complete_host(self, response: HostWebResult) -> dict[str, Any]:
        cursor = self.repository.get(response.operation_id)
        if cursor is None or cursor.get("host") is None:
            raise ValueError("host result has no pending host dispatch")
        host = HostWebCapabilities.model_validate(cursor["host"])
        request = WebToolRequest.model_validate(cursor["request"])
        context = self._continuation_context(request)
        if context is not None and request.continuation_dispatch_id != context[1].dispatch_id:
            raise ValueError("host result belongs to another continuation dispatch generation")
        response_hash = content_hash(response.model_dump(mode="json"))
        if cursor.get("host_result_hash") == response_hash:
            return self._view(self._recover(cursor))
        raw = self._validate_new_host_response(cursor, response, host, request)
        result = WebTransportResult(
            status="SUCCEEDED" if response.status == "SUCCEEDED" else "FAILED",
            hits=tuple(response.hits[:self.policy.max_results]), raw=raw,
            reason_code="HOST_" + response.status,
        )
        return self._publish(
            cursor, result, observed_at=response.observed_at, host_result_hash=response_hash,
            cancelled=response.status == "CANCELLED",
        )

    def resolve_official_result(
        self, selection: WebOfficialDocumentSelection, *, allow_network: bool = True,
    ) -> CurrentResearchAutomaticResolution:
        from astock.investor_orchestration.run_ownership import schedule_run_ownership

        with schedule_run_ownership(
            self.state.path, "web-official-resolution", selection.operation_id
        ):
            return self._resolve_official_result(selection, allow_network=allow_network)

    def _resolve_official_result(
        self, selection: WebOfficialDocumentSelection, *, allow_network: bool,
    ) -> CurrentResearchAutomaticResolution:
        """Bind one admitted search URL to raw PDF capture for the same pending task.

        Returned resolution goes through the existing coordinator's apply/resume path.
        Search/Fetch text itself never becomes an official PDF or financial release.
        """
        from astock.core.request_budget import RequestExecutionBudget
        from astock.documents.official_web import OfficialWebDocumentCaptureService
        cursor = self.repository.get(selection.operation_id)
        if cursor is None:
            raise ValueError("Unknown Web operation")
        cursor = self._recover(cursor)
        if cursor["status"] != "SUCCEEDED" or not cursor.get("result_hash"):
            raise ValueError("Official capture requires a successful Web result")
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_id is None or request.task_id is None:
            raise ValueError("Official capture requires a bound continuation/task")
        resolution_key = "WebOfficialResolution:" + content_hash({
            "operation": cursor["operation_id"], "dispatch": cursor["dispatch_id"],
            "selection": selection.model_dump(mode="json"),
        })
        payload = json.loads(self.objects.get_bytes(cursor["result_hash"]))
        config = load_web_discovery_config(self.root)
        canonical = normalize_result_url(selection.url, config=config)
        if canonical is None or canonical not in payload["admission"]["discovery_allowed_urls"]:
            raise ValueError("Original URL is not bound to the admitted Web result")
        checkpoint = self.state.get_checkpoint(
            "current-research-continuation", request.continuation_id
        )
        if checkpoint is None or not checkpoint.get("object_hash"):
            raise ValueError("Continuation disappeared before original capture")
        continuation = CurrentResearchContinuation.model_validate_json(
            self.objects.get_bytes(checkpoint["object_hash"])
        )
        record = continuation.model_dump(mode="json")
        if record["request_id"] != request.request_id:
            raise ValueError("Official original belongs to another investor request")
        task = next(
            (item for item in record["external_tasks"] if item["task_id"] == request.task_id), None
        )
        if task is None:
            raise ValueError("Official original references an unknown continuation task")
        if (
            task.get("dispatch_id") != selection.dispatch_id
            or request.continuation_dispatch_id != selection.dispatch_id
        ):
            raise ValueError("Official original belongs to another continuation dispatch")
        previous = self.state.artifact_record(resolution_key)
        if previous is not None:
            if (
                previous["type"] != "CurrentResearchAutomaticResolution"
                or not self.objects.verify(previous["object_hash"])
            ):
                raise ValueError("Stored official resolution is corrupt")
            return CurrentResearchAutomaticResolution.model_validate_json(
                self.objects.get_bytes(previous["object_hash"])
            )
        capability = official_document_capability(AcquisitionCapability(task["capability"]))
        if capability is None:
            raise ValueError("This evidence gap cannot consume an official PDF capture")
        proposal = AgentSourceProposal(
            requested_capability=capability, query=request.query or request.evidence_scope,
            candidate_url=HttpUrl(canonical), expected_fact=request.expected_fact,
            preferred_source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
            formal_use=True, require_complete=False,
            reason="Same-request Web result to original document",
        )
        fetch_key = self._official_raw_id(cursor, canonical)
        fetched = self.state.artifact_record(fetch_key)
        if fetched is not None:
            # A frozen original may finish local admission after the transport deadline.
            # It remains bound to the same pending operation, request and task.
            if task["status"] != "PENDING" or record["status"] not in {
                "AUTO_RESOLUTION_REQUIRED", "EXTERNAL_COMPLETION_UNKNOWN",
                "BUDGET_EXHAUSTED", "PUBLIC_DATA_UNAVAILABLE", "NEEDS_USER_INPUT",
            }:
                raise ValueError("Frozen official original requires the same unresolved task")
            if fetched["type"] != "WebOfficialRaw" or not self.objects.verify(
                fetched["object_hash"]
            ):
                raise ValueError("Stored official original receipt is corrupt")
            fetch_payload = json.loads(self.objects.get_bytes(fetched["object_hash"]))
            if fetch_payload["original_url"] != canonical:
                raise ValueError("Stored official original URL differs from its Web operation")
            if not self.objects.verify(fetch_payload["raw_object_hash"]):
                raise ValueError("Stored official original bytes are corrupt")
            raw = self.objects.get_bytes(fetch_payload["raw_object_hash"])
            observed_at = datetime.fromisoformat(fetch_payload["observed_at"])
            proposal = proposal.model_copy(update={
                "candidate_url": HttpUrl(fetch_payload["final_url"]),
            })
        else:
            self._validate_continuation(request, recovery=True)
            budget = RequestExecutionBudget(
                self.state, continuation.request_id, continuation.deadline_at,
                continuation.max_transport_attempts, clock=self.clock,
            )
            # A canonical raw claim is shared, while this operation keeps its own receipt.
            with budget.bind():
                shared = self._shared_official_original(
                    request, selection, proposal, allow_network=allow_network,
                )
            if shared is None:
                raise ValueError("Local-only original recovery requires frozen raw bytes")
            raw, observed_at, final_url, redirect_chain = shared
            raw_ref = self.objects.put_bytes(raw)
            fetch_payload = {
                "raw_object_hash": raw_ref.sha256, "observed_at": observed_at.isoformat(),
                "original_url": canonical, "final_url": final_url,
                "redirect_chain": list(redirect_chain),
            }
            fetch_ref = self.objects.put_json(fetch_payload)
            self.state.register_artifact(
                artifact_id=fetch_key, artifact_type="WebOfficialRaw",
                schema_version="web-official-raw-v1", object_hash=fetch_ref.sha256,
                input_hashes=[cursor["result_hash"], raw_ref.sha256],
            )
            proposal = proposal.model_copy(update={"candidate_url": HttpUrl(final_url)})
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            if pdf.page_count > self.policy.max_document_pages:
                raise ValueError("Official original exceeds the page budget")
        capture = OfficialWebDocumentCaptureService(self.state, self.objects).capture(
            proposal, raw, title=selection.title, company_ids=[record["company_id"]],
            published_at=selection.published_at,
            document_type=DocumentType(selection.document_type),
            disclosure_id=selection.disclosure_id, period_end=selection.period_end,
            observed_at=observed_at,
        )
        resolution = CurrentResearchAutomaticResolution(
            continuation_id=request.continuation_id, task_id=request.task_id,
            external_operation_id=cursor["operation_id"], dispatch_id=selection.dispatch_id,
            capture_artifact_ids=[f"OfficialWebDocumentCapture:{capture.capture_id}"],
            created_at=observed_at,
        )
        ref = self.objects.put_json(resolution.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=resolution_key, artifact_type="CurrentResearchAutomaticResolution",
            schema_version=resolution.schema_version, object_hash=ref.sha256,
            input_hashes=[cursor["result_hash"], capture.object_sha256],
        )
        return resolution

    def _validate_new_host_response(
        self, cursor: dict[str, Any], response: HostWebResult,
        host: HostWebCapabilities, request: WebToolRequest,
    ) -> bytes:
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
        self._validate_continuation(request, recovery=True)
        if request.operation == "FETCH" and (
            len(response.hits) > 1
            or any(hit.url != request.url for hit in response.hits)
        ):
            raise ValueError("FETCH result must match the requested URL")
        return raw

    def complete_continuation_results(
        self, continuation_id: str, responses: Sequence[HostWebResult],
    ) -> list[dict[str, Any]]:
        # Validate every binding before publishing even the first callback.
        record = self._continuation_record(continuation_id)
        repeated: dict[str, dict[str, Any]] = {}
        for response in responses:
            cursor = self.repository.get(response.operation_id)
            if cursor is None:
                raise ValueError("Host callback has no registered operation")
            request = WebToolRequest.model_validate(cursor["request"])
            if request.continuation_id != continuation_id:
                raise ValueError("Host callback belongs to another continuation")
            if (
                cursor.get("host_result_hash") == content_hash(response.model_dump(mode="json"))
                and self._already_applied(
                    record, response.operation_id, request.continuation_dispatch_id,
                )
            ):
                repeated[response.operation_id] = cursor
                continue
            context = self._continuation_context(request)
            if context is None or request.continuation_dispatch_id != context[1].dispatch_id:
                raise ValueError("Host callback belongs to another dispatch generation")
            if cursor.get("host_result_hash") != content_hash(response.model_dump(mode="json")):
                self._validate_new_host_response(
                    cursor, response, HostWebCapabilities.model_validate(cursor["host"]), request,
                )
        return [
            self._view(self._recover(repeated[response.operation_id]))
            if response.operation_id in repeated else self.complete_host(response)
            for response in responses
        ]

    def _already_applied(
        self, record: CurrentResearchContinuation, operation: str, dispatch_id: str | None,
    ) -> bool:
        cursor = self.repository.get(operation)
        if cursor is None:
            return False
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_id != record.continuation_id:
            raise ValueError("Web operation belongs to another continuation")
        task = next(
            (item for item in record.external_tasks if item.task_id == request.task_id), None,
        )
        if request.continuation_dispatch_id != dispatch_id or (
            task is not None and task.dispatch_id != dispatch_id
        ):
            raise ValueError("Web operation belongs to another dispatch generation")
        for identifier in reversed(record.automatic_resolution_artifact_ids):
            stored = self.state.artifact_record(identifier)
            if stored is None or stored["type"] != "CurrentResearchAutomaticResolution":
                raise ValueError("Applied Web resolution history is unavailable")
            if not self.objects.verify(stored["object_hash"]):
                raise ValueError("Applied Web resolution history is corrupt")
            result = CurrentResearchAutomaticResolution.model_validate_json(
                self.objects.get_bytes(stored["object_hash"])
            )
            if (
                result.continuation_id == record.continuation_id
                and result.task_id == request.task_id and result.external_operation_id == operation
                and result.dispatch_id == dispatch_id and result.capture_artifact_ids
            ):
                return True
        return False

    def selected_original_task(
        self, continuation_id: str, selection: WebOfficialDocumentSelection,
    ) -> CurrentResearchExternalTask:
        cursor = self.repository.get(selection.operation_id)
        if cursor is None:
            raise ValueError("Original selection has no registered Web operation")
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_id != continuation_id:
            raise ValueError("Original selection belongs to another continuation")
        context = self._continuation_context(request)
        if context is None:
            raise ValueError("Original selection requires a bound continuation")
        _record, task = context
        if selection.dispatch_id != task.dispatch_id or (
            request.continuation_dispatch_id != selection.dispatch_id
        ):
            raise ValueError("Original selection belongs to another dispatch generation")
        if official_document_capability(task.capability) is None:
            raise ValueError("This evidence gap cannot consume an official PDF capture")
        required_types = {
            AcquisitionCapability.FINANCIAL_ANNUAL: {"ANNUAL_REPORT"},
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM: {
                "QUARTERLY_REPORT", "SEMIANNUAL_REPORT",
            },
        }.get(task.capability, {"ANNOUNCEMENT"})
        if selection.document_type not in required_types:
            raise ValueError("Original document type does not match its evidence gap")
        return task

    @staticmethod
    def _official_raw_id(cursor: dict[str, Any], url: str) -> str:
        return "WebOfficialRaw:" + content_hash({
            "operation": cursor["operation_id"], "dispatch": cursor["dispatch_id"], "url": url,
        })

    def has_frozen_original(self, selection: WebOfficialDocumentSelection) -> bool:
        cursor = self.repository.get(selection.operation_id)
        if cursor is None:
            return False
        canonical = normalize_result_url(
            selection.url, config=load_web_discovery_config(self.root)
        )
        if canonical is None:
            return False
        stored = self.state.artifact_record(self._official_raw_id(cursor, canonical))
        if stored is None:
            return self._has_shared_original(cursor, selection)
        if stored["type"] != "WebOfficialRaw" or not self.objects.verify(stored["object_hash"]):
            raise ValueError("Frozen original receipt is corrupt")
        receipt = json.loads(self.objects.get_bytes(stored["object_hash"]))
        if receipt["original_url"] != canonical or not self.objects.verify(
            receipt["raw_object_hash"]
        ):
            raise ValueError("Frozen original bytes or URL are corrupt")
        return True

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

    def verified_empty_operation(self, cursor: dict[str, Any]) -> bool:
        from astock.research.recovery_evidence import verified_empty_web_response

        if cursor.get("status") != "SUCCEEDED" or not cursor.get("artifact_id"):
            return False
        record = self.state.artifact_record(cursor["artifact_id"])
        if record is None or record["type"] != "WebToolResult" or not self.objects.verify(
            record["object_hash"]
        ):
            return False
        payload = json.loads(self.objects.get_bytes(record["object_hash"]))
        if (
            record["object_hash"] != cursor.get("result_hash")
            or payload["operation_id"] != cursor["operation_id"]
            or payload["dispatch_id"] != cursor["dispatch_id"]
            or payload["request"] != cursor["request"]
            or not self.objects.verify(payload["raw_object_hash"])
        ):
            return False
        try:
            raw = json.loads(self.objects.get_bytes(payload["raw_object_hash"]))
        except (ValueError, UnicodeError):
            return False
        return verified_empty_web_response(payload, raw)

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
        if cursor.get("result_hash"):
            return cursor
        shared = bool(cursor.get("shared_acquisition_id"))
        if not shared and cursor["status"] not in {"WAITING_HOST", "PAID_RUNNING", "SUCCEEDED"}:
            return cursor
        artifact_id = self._artifact_id(cursor)
        record = self.state.artifact_record(artifact_id)
        if record is None:
            return self._recover_shared_web(cursor) if shared else (
                self.repository.reconcile_owner(cursor["operation_id"], self.clock())
            )
        if record["type"] != "WebToolResult" or not self.objects.verify(record["object_hash"]):
            raise ValueError("Frozen Web operation result is corrupt")
        payload = json.loads(self.objects.get_bytes(record["object_hash"]))
        if (
            payload["operation_id"] != cursor["operation_id"]
            or payload["dispatch_id"] != cursor["dispatch_id"]
            or payload["request"] != cursor["request"]
        ):
            raise ValueError("Frozen Web operation result has another identity")
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

    def _continuation_record(self, continuation_id: str) -> CurrentResearchContinuation:
        row = self.state.get_checkpoint("current-research-continuation", continuation_id)
        if row is None or not row.get("object_hash") or not self.objects.verify(row["object_hash"]):
            raise ValueError("unknown or corrupt current-research continuation")
        return CurrentResearchContinuation.model_validate_json(
            self.objects.get_bytes(row["object_hash"])
        )

    def _continuation_context(
        self, request: WebToolRequest,
    ) -> tuple[CurrentResearchContinuation, CurrentResearchExternalTask] | None:
        if request.continuation_id is None:
            return None
        record = self._continuation_record(request.continuation_id)
        if record.request_id != request.request_id:
            raise ValueError("Web request belongs to another investor request")
        task = next(
            (item for item in record.external_tasks if item.task_id == request.task_id), None
        )
        if task is None:
            raise ValueError("Web request references an unknown continuation task")
        return record, task

    def _validate_continuation(
        self, request: WebToolRequest, *, recovery: bool = False, bind_dispatch: bool = False,
    ) -> WebToolRequest:
        context = self._continuation_context(request)
        if context is None:
            return request
        record, task = context
        if bind_dispatch and request.continuation_dispatch_id is None:
            request = request.model_copy(update={"continuation_dispatch_id": task.dispatch_id})
        if request.continuation_dispatch_id != task.dispatch_id:
            raise ValueError("Web request belongs to another continuation dispatch generation")
        allowed = {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }
        if recovery:
            # Only callers holding an existing exact Web operation use this local recovery path.
            allowed.update({
                CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
                CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
                CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
            })
        if record.status not in allowed or task.status is not ExternalResearchTaskStatus.PENDING:
            raise ValueError("Web request requires an active matching evidence gap")
        if not recovery:
            if task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN" and not task.resolution_hash:
                raise ValueError("unknown external completion cannot be redispatched")
            unresolved = {
                item.capability for item in record.external_tasks
                if item.status is not ExternalResearchTaskStatus.RESOLVED
            }
            policy = load_default_current_research_policy(self.root)
            if task.private_material_required or unresolved.intersection(
                policy.capabilities[task.capability].dependencies
            ):
                raise ValueError("Web dispatch requires an independent public evidence gap")
        return request

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


def _canonical_query(value: str) -> str:
    return unicodedata.normalize("NFC", " ".join(value.split()))


def official_document_capability(capability: AcquisitionCapability) -> str | None:
    if capability in {
        AcquisitionCapability.FINANCIAL_ANNUAL, AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
    }:
        return "financial.official_document"
    if capability in {
        AcquisitionCapability.INSTRUMENT_IDENTITY, AcquisitionCapability.CORPORATE_ACTIONS,
    }:
        return "disclosure.document"
    return None


def _continuation_web_request(
    record: CurrentResearchContinuation, task: CurrentResearchExternalTask, capability: str,
) -> WebToolRequest:
    """Keep the persisted operation identity identical for preparation and local recovery."""
    return WebToolRequest(
        request_id=record.request_id, operation="SEARCH", query=task.research_question,
        requested_capability=capability,
        evidence_scope=f"{record.company_id}:{task.capability.value}:{task.task_id}",
        expected_fact=task.research_question, fresh_after=record.started_at,
        continuation_id=record.continuation_id, task_id=task.task_id,
        continuation_dispatch_id=task.dispatch_id,
    )


def prepare_continuation_web_requests(
    record: CurrentResearchContinuation, *, root: Path,
) -> tuple[WebToolRequest, ...]:
    """Project prepared, independent gaps without inventing a host capability."""
    policy = load_default_current_research_policy(root)
    unresolved = {
        task.capability for task in record.external_tasks
        if task.status is not ExternalResearchTaskStatus.RESOLVED
    }
    requests = []
    for task in record.external_tasks:
        capability = official_document_capability(task.capability)
        if (
            capability is None or not task.dispatch_id or not task.scheduler_run_id
            or task.status is not ExternalResearchTaskStatus.PENDING
            or task.private_material_required or task.resolution_hash is not None
            or task.external_operation_id is not None
            or task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
            or unresolved.intersection(policy.capabilities[task.capability].dependencies)
        ):
            continue
        requests.append(_continuation_web_request(record, task, capability))
    return tuple(requests)


def _transport_budget(
    state: StateStore, root: Path, record: CurrentResearchContinuation,
    *, clock: Callable[[], datetime] | None = None,
) -> RequestExecutionBudget:
    from astock.core.request_budget import RequestExecutionBudget

    policy = load_default_current_research_policy(root)
    return RequestExecutionBudget(
        state, record.request_id, record.deadline_at,
        min(record.max_transport_attempts, policy.max_transport_attempts), clock=clock,
    )


def _web_worker_stores(
    project_root: str, database: str, object_root: str,
) -> tuple[Path, StateStore, ObjectStore]:
    root = Path(project_root).resolve()
    if any(not Path(value).resolve().is_relative_to(root) for value in (database, object_root)):
        raise ValueError("Web worker stores must remain inside the owning project")
    return root, StateStore(Path(database), root / "migrations"), ObjectStore(Path(object_root))


def _run_official_original(
    project_root: str, database: str, object_root: str, selection_json: str,
    continuation_id: str, allow_network: bool,
) -> str:
    """Fixed child target: freeze a result; only the parent may advance research."""
    root, state, objects = _web_worker_stores(project_root, database, object_root)
    web = WebToolExecutionService(root, state, objects)
    selection = WebOfficialDocumentSelection.model_validate_json(selection_json)
    task = web.selected_original_task(continuation_id, selection)
    cursor = web.repository.get(selection.operation_id)
    assert cursor is not None
    context = web._continuation_context(WebToolRequest.model_validate(cursor["request"]))
    assert context is not None
    record, _task = context
    with _transport_budget(state, root, record).bind():
        try:
            resolution = web.resolve_official_result(selection, allow_network=allow_network)
        except (OSError, ValueError) as exc:
            if not allow_network:
                raise
            resolution = CurrentResearchAutomaticResolution(
                continuation_id=continuation_id, task_id=task.task_id,
                external_operation_id=selection.operation_id, dispatch_id=selection.dispatch_id,
                failure_code=f"OFFICIAL_ORIGINAL_{type(exc).__name__.upper()}",
                created_at=datetime.now(UTC),
            )
    ref = objects.put_json(resolution.model_dump(mode="json"))
    return "CurrentResearchAutomaticResolution:" + ref.sha256


def _original_worker(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore,
    record: CurrentResearchContinuation, selection: WebOfficialDocumentSelection,
    *, allow_network: bool,
) -> KillableResearchWorker:
    from astock.research.process_worker import KillableResearchWorker

    # A local recovery grace bounds CPU only. The child keeps the original expired
    # transport budget, and allow_network=False is enforced at the downloader boundary.
    deadline = record.deadline_at if allow_network else (
        datetime.now(UTC) + timedelta(seconds=load_web_tool_policy(paths.root).timeout_seconds)
    )
    return KillableResearchWorker(
        _run_official_original,
        (str(paths.root), str(state.path), str(objects.root), selection.model_dump_json(),
         record.continuation_id, allow_network),
        deadline_at=deadline, project_root=paths.root,
        scratch_root=paths.root / "runtime" / "workers",
    )


class _OfficialOriginalHandler:
    def __init__(
        self, worker: KillableResearchWorker, task: CurrentResearchExternalTask,
    ) -> None:
        self.worker, self.task = worker, task

    def __call__(self, task: ResearchSchedulerTask) -> str:
        expected_dispatch = f"{task.task_id}:gen:{task.generation}"
        if (
            task.node_id != self.task.task_id
            or task.run_id != self.task.scheduler_run_id
            or expected_dispatch != self.task.dispatch_id
        ):
            raise ValueError("Original worker belongs to another scheduler generation")
        return self.worker(task)

    def cancel(self) -> None:
        self.worker.cancel()


def build_official_resolution_handlers(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore,
    record: CurrentResearchContinuation, originals: Sequence[WebOfficialDocumentSelection],
) -> dict[str, CancellableTaskHandler]:
    """Production factory over existing owned workers; no caller-selected code."""
    web = WebToolExecutionService(paths.root, state, objects)
    selected = [
        (web.selected_original_task(record.continuation_id, item), item) for item in originals
    ]
    if len({task.task_id for task, _ in selected}) != len(selected):
        raise ValueError("Only one original can resolve a task in one wave")
    handlers: dict[str, CancellableTaskHandler] = {}
    for task, selection in selected:
        if not task.dispatch_id or not task.scheduler_run_id:
            raise ValueError("Original wave requires a prepared scheduler generation")
        if (
            task.status is not ExternalResearchTaskStatus.PENDING
            or task.resolution_hash is not None
        ):
            raise ValueError("Original wave requires an unresolved task")
        if task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN":
            raise ValueError("Unknown original completion cannot be redispatched")
        handlers[task.task_id] = _OfficialOriginalHandler(
            _original_worker(
                paths, state, objects, record, selection,
                allow_network=not web.has_frozen_original(selection),
            ), task,
        )
    return handlers


def _read_frozen_resolution(
    objects: ObjectStore, identifier: str,
) -> CurrentResearchAutomaticResolution:
    prefix = "CurrentResearchAutomaticResolution:"
    if not identifier.startswith(prefix) or not objects.verify(identifier.removeprefix(prefix)):
        raise ValueError("Original worker returned an invalid immutable result")
    return CurrentResearchAutomaticResolution.model_validate_json(
        objects.get_bytes(identifier.removeprefix(prefix))
    )


def _run_web_dispatch(
    project_root: str, database: str, object_root: str, request_json: str, host_json: str,
) -> str:
    root, state, objects = _web_worker_stores(project_root, database, object_root)
    web = WebToolExecutionService(root, state, objects)
    request = WebToolRequest.model_validate_json(request_json)
    host = HostWebCapabilities.model_validate_json(host_json)
    context = web._continuation_context(request)
    if context is None:
        raise ValueError("Owned Web dispatch requires a continuation")
    with _transport_budget(state, root, context[0]).bind():
        view = web.run(request, host)
    return "WebToolDispatch:" + objects.put_json(view).sha256


def _needs_owned_dispatch(
    web: WebToolExecutionService, request: WebToolRequest, host: HostWebCapabilities | None,
) -> bool:
    if host is None or host.expires_at <= web.clock() or host.observed_at > web.clock():
        return False
    age = (web.clock() - host.observed_at).total_seconds()
    if age > web.policy.host_capability_max_age_seconds:
        return False
    previous = web.repository.get(operation_id(request))
    if previous is not None and previous["status"] != "HOST_FAILED":
        return False
    tool = host.tool_for(request.operation)
    if tool.availability == "UNKNOWN" or (
        tool.availability == "AVAILABLE" and tool.billing == "UNKNOWN"
    ):
        return False
    return not (
        previous is None and tool.availability == "AVAILABLE" and tool.billing == "INCLUDED"
    )


def execute_continuation_web_request(
    paths: ProjectPaths, web: WebToolExecutionService, record: CurrentResearchContinuation,
    request: WebToolRequest, host: HostWebCapabilities | None,
) -> dict[str, Any]:
    """Included tools only create a handoff; a paid physical call is process-owned."""
    if not _needs_owned_dispatch(web, request, host):
        return web.run(request, host)
    from astock.research.process_worker import KillableResearchWorker

    assert host is not None
    deadline = min(
        record.deadline_at, datetime.now(UTC) + timedelta(seconds=web.policy.timeout_seconds),
    )
    worker = KillableResearchWorker(
        _run_web_dispatch,
        (str(paths.root), str(web.state.path), str(web.objects.root),
         request.model_dump_json(), host.model_dump_json()),
        deadline_at=deadline, project_root=paths.root,
        scratch_root=paths.root / "runtime" / "workers",
    )
    try:
        identifier = worker()
    except (RuntimeError, TimeoutError):
        cursor = web.repository.get(operation_id(request))
        return (
            web._view(web._recover(cursor)) if cursor is not None else
            {"operation_id": operation_id(request), "status": "NOT_DISPATCHED",
             "reason_code": "OWNED_WEB_WORKER_STOPPED"}
        )
    prefix = "WebToolDispatch:"
    if not identifier.startswith(prefix) or not web.objects.verify(identifier.removeprefix(prefix)):
        raise ValueError("Web worker returned an invalid frozen dispatch")
    return json.loads(web.objects.get_bytes(identifier.removeprefix(prefix)))


def _resolve_web_original_batch(
    paths: ProjectPaths, web: WebToolExecutionService,
    continuation: CurrentResearchContinuationService, record: CurrentResearchContinuation,
    originals: Sequence[WebOfficialDocumentSelection],
) -> tuple[CurrentResearchContinuation, list[dict[str, Any]]]:
    selected = []
    views: list[dict[str, Any]] = []
    for item in originals:
        if web._already_applied(record, item.operation_id, item.dispatch_id):
            views.append({"operation_id": item.operation_id, "status": "ALREADY_APPLIED"})
        else:
            selected.append((web.selected_original_task(record.continuation_id, item), item))
    if len({task.task_id for task, _ in selected}) != len(selected):
        raise ValueError("A batch cannot resolve one task with multiple originals")
    network_originals = []
    budget = _transport_budget(web.state, paths.root, record, clock=continuation.clock)
    for task, selection in selected:
        if task.resolution_hash is not None:
            if task.external_operation_id != selection.operation_id:
                raise ValueError("Original batch cannot replace an applied operation")
            continue
        try:
            frozen = web.has_frozen_original(selection)
        except (OSError, ValueError, KeyError) as exc:
            views.append({
                "operation_id": selection.operation_id, "task_id": task.task_id,
                "status": "ORIGINAL_FAILED",
                "reason_code": f"FROZEN_RAW_{type(exc).__name__.upper()}",
            })
            continue
        available = budget.remaining_seconds() > 0 and budget.remaining_attempts() > 0
        local_recovery = frozen and (
            not available or task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
        )
        if local_recovery or (task.dispatch_id is None and available):
            try:
                identifier = _original_worker(
                    paths, web.state, web.objects, record, selection, allow_network=not frozen,
                )()
                resolution = _read_frozen_resolution(web.objects, identifier)
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                views.append({
                    "operation_id": selection.operation_id, "task_id": task.task_id,
                    "status": "ORIGINAL_FAILED",
                    "reason_code": f"ORIGINAL_RECOVERY_{type(exc).__name__.upper()}",
                })
                continue
            record = continuation.apply_automatic_resolution(resolution)
        elif available and task.last_failure_code != "EXTERNAL_COMPLETION_UNKNOWN":
            network_originals.append(selection)
        else:
            views.append({
                "operation_id": selection.operation_id, "task_id": task.task_id,
                "status": "ORIGINAL_PENDING", "reason_code": (
                    "EXTERNAL_COMPLETION_UNKNOWN"
                    if task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
                    else "REQUEST_BUDGET_EXHAUSTED"
                ),
            })
    if network_originals:
        handlers = build_official_resolution_handlers(
            paths, web.state, web.objects, record, network_originals,
        )
        record = continuation.resolve_external_wave(
            record.continuation_id, handlers, selected_task_ids=set(handlers),
        )
    return record, views


def _bind_web_operation(
    web: WebToolExecutionService, continuation: CurrentResearchContinuationService,
    record: CurrentResearchContinuation, request: WebToolRequest, view: dict[str, Any],
) -> CurrentResearchContinuation:
    identifier = view.get("operation_id")
    cursor = web.repository.get(identifier) if isinstance(identifier, str) else None
    if cursor is None:
        return record
    assert isinstance(identifier, str)
    stored = WebToolRequest.model_validate(cursor["request"])
    if (
        stored.continuation_id != record.continuation_id or stored.task_id != request.task_id
        or stored.continuation_dispatch_id != request.continuation_dispatch_id
        or operation_id(stored) != identifier
    ):
        raise ValueError("Prepared Web operation has a conflicting continuation binding")
    assert stored.task_id is not None and stored.continuation_dispatch_id is not None
    if web.verified_empty_operation(cursor):
        return continuation.release_external_operation(
            record.continuation_id, stored.task_id, stored.continuation_dispatch_id, identifier,
        )
    if cursor["status"] in {"WAITING_HOST", "SUCCEEDED", "UNKNOWN", "PAID_RUNNING",
                            "SHARED_WAITING", "SHARED_UNKNOWN"}:
        return continuation.record_external_operation(
            record.continuation_id, stored.task_id, stored.continuation_dispatch_id, identifier,
            completion_unknown=cursor["status"] in {"UNKNOWN", "PAID_RUNNING", "SHARED_UNKNOWN"},
        )
    if cursor["status"] in {"HOST_FAILED", "FAILED", "CANCELLED"}:
        return continuation.release_external_operation(
            record.continuation_id, stored.task_id, stored.continuation_dispatch_id, identifier,
        )
    return record


def _bind_web_callbacks(
    web: WebToolExecutionService, continuation: CurrentResearchContinuationService,
    record: CurrentResearchContinuation, callbacks: Sequence[dict[str, Any]],
) -> CurrentResearchContinuation:
    if record.status not in {
        CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
        CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
    }:
        return record
    for view in callbacks:
        cursor = web.repository.get(view["operation_id"])
        assert cursor is not None
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_dispatch_id is None or web._already_applied(
            record, view["operation_id"], request.continuation_dispatch_id,
        ):
            continue
        record = _bind_web_operation(web, continuation, record, request, view)
    return record


def _recover_continuation_web_operations(
    web: WebToolExecutionService, continuation: CurrentResearchContinuationService,
    record: CurrentResearchContinuation, *, handled_ids: frozenset[str],
) -> tuple[CurrentResearchContinuation, list[dict[str, Any]]]:
    """Reconcile an existing claim before ordinary resume can advance its generation."""
    if record.status not in {
        CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
        CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
    }:
        return record, []
    operations = []
    for task in record.external_tasks:
        capability = official_document_capability(task.capability)
        if (
            capability is None or not task.dispatch_id or not task.scheduler_run_id
            or task.status is not ExternalResearchTaskStatus.PENDING
            or task.private_material_required or task.resolution_hash is not None
        ):
            continue
        expected = _continuation_web_request(record, task, capability)
        identifier = task.external_operation_id or operation_id(expected)
        if identifier in handled_ids:
            continue
        cursor = web.repository.get(identifier)
        if cursor is None:
            continue  # Another adapter may own the generic external-operation identity.
        request = WebToolRequest.model_validate(cursor["request"])
        if (
            cursor.get("operation_id") != identifier or operation_id(request) != identifier
            or request.request_id != record.request_id
            or request.continuation_id != record.continuation_id or request.task_id != task.task_id
            or request.continuation_dispatch_id != task.dispatch_id
            or (task.external_operation_id is None and request != expected)
        ):
            raise ValueError("Existing Web operation has a conflicting request or generation")
        # This is read-only validation of the actual SLA binding, before ledger recovery.
        continuation._external_operation_task(
            record.continuation_id, task.task_id, task.dispatch_id, identifier,
        )
        cursor = web._recover(cursor)
        view = web._view(cursor)
        record = _bind_web_operation(web, continuation, record, request, view)
        operations.append({**view, "task_id": task.task_id,
                           "request": request.model_dump(mode="json")})
    return record, operations


def _prepare_web_operations(
    paths: ProjectPaths, web: WebToolExecutionService,
    continuation: CurrentResearchContinuationService, record: CurrentResearchContinuation,
    host: HostWebCapabilities | None, *, recovered_ids: frozenset[str] = frozenset(),
) -> tuple[CurrentResearchContinuation, list[dict[str, Any]]]:
    if record.status not in {
        CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
        CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
    }:
        return record, []
    budget = _transport_budget(web.state, paths.root, record, clock=continuation.clock)
    if budget.remaining_seconds() <= 0 or budget.remaining_attempts() == 0:
        return record, []
    record = continuation.prepare_external_wave(record.continuation_id)
    operations = []
    for request in prepare_continuation_web_requests(record, root=paths.root):
        if operation_id(request) in recovered_ids:
            continue
        if len(request.query or "") > 128:
            view: dict[str, Any] = {
                "status": "QUERY_REFINEMENT_REQUIRED",
                "reason_code": "QUERY_EXCEEDS_128_UNICODE",
            }
        else:
            view = execute_continuation_web_request(paths, web, record, request, host)
        record = _bind_web_operation(web, continuation, record, request, view)
        operations.append({**view, "task_id": request.task_id,
                           "request": request.model_dump(mode="json")})
    return record, operations


def advance_current_web(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore, continuation_id: str,
    batch: CurrentResearchWebBatch | None = None, *,
    continuation: CurrentResearchContinuationService | None = None,
    resume_unbound: bool = False,
) -> dict[str, Any]:
    """Default start/resume composition over host receipts and the existing coordinator."""
    from astock.research.continuation import CurrentResearchContinuationService

    batch = batch or CurrentResearchWebBatch(continuation_id=continuation_id)
    if batch.continuation_id != continuation_id:
        raise ValueError("Web batch belongs to another continuation")
    coordinator = continuation or CurrentResearchContinuationService(paths, state, objects)
    # Reuse the existing OS ownership, not a long-lived SQLite transaction. Child
    # workers only freeze Web/raw artifacts; coordinator calls on this thread reenter.
    with coordinator._coordinator(continuation_id):
        record = coordinator.get(continuation_id)
        if record is None:
            raise ValueError("Unknown current-research continuation")
        web = WebToolExecutionService(paths.root, state, objects, clock=coordinator.clock)
        callbacks = web.complete_continuation_results(continuation_id, batch.host_results)
        record = _bind_web_callbacks(web, coordinator, record, callbacks)
        handled_ids = frozenset(item["operation_id"] for item in callbacks)
        record, recovered = _recover_continuation_web_operations(
            web, coordinator, record, handled_ids=handled_ids,
        )
        recovered_ids = handled_ids | frozenset(item["operation_id"] for item in recovered)
        previous_results = {task.task_id: task.resolution_hash for task in record.external_tasks}
        record, original_views = _resolve_web_original_batch(
            paths, web, coordinator, record, batch.originals,
        )
        progressed = any(
            task.resolution_hash and task.resolution_hash != previous_results.get(task.task_id)
            for task in record.external_tasks
        )
        budget = _transport_budget(state, paths.root, record, clock=coordinator.clock)
        if resume_unbound or progressed or any(
            task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
            for task in record.external_tasks
        ):
            record = coordinator.resume(continuation_id)
        elif budget.remaining_seconds() <= 0 or budget.remaining_attempts() == 0:
            record = coordinator.resume(continuation_id)
        record, prepared = _prepare_web_operations(
            paths, web, coordinator, record, batch.host_capabilities, recovered_ids=recovered_ids,
        )
        return {**record.model_dump(mode="json"),
                "web_operations": [*callbacks, *recovered, *original_views, *prepared]}
