"""Auditable API -> MCP -> Browser -> Manual source selection."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from astock.core.state import StateStore
from astock.schemas import (
    AccessTransport,
    RateLimitState,
    SourceAccessDecision,
    SourceAccessRequest,
    TransportCapability,
)

_PRIORITY = (
    AccessTransport.API,
    AccessTransport.MCP,
    AccessTransport.BROWSER,
    AccessTransport.MANUAL,
)


class SourceAccessRouter:
    def __init__(self, state: StateStore | None = None) -> None:
        self.state = state

    def decide(
        self,
        request: SourceAccessRequest,
        capabilities: list[TransportCapability],
    ) -> SourceAccessDecision:
        started = datetime.now(UTC)
        attempted: list[AccessTransport] = []
        selected: TransportCapability | None = None
        for transport in _PRIORITY:
            attempted.append(transport)
            matching = [
                item
                for item in capabilities
                if item.source_id == request.source_id
                and item.transport == transport
                and request.requested_capability in item.requested_capabilities
            ]
            if matching and matching[0].available:
                selected = matching[0]
                break
        if selected is None:
            selected_transport = AccessTransport.MANUAL
            reason = "No automated capability is available; create a manual investigation task."
            if AccessTransport.MANUAL not in attempted:
                attempted.append(AccessTransport.MANUAL)
        else:
            selected_transport = selected.transport
            reason = selected.reason
        decision = SourceAccessDecision(
            decision_id=uuid4().hex,
            source_id=request.source_id,
            requested_capability=request.requested_capability,
            selected_transport=selected_transport,
            selection_reason=reason,
            fallback_chain=attempted,
            request_started_at=started,
            request_finished_at=datetime.now(UTC),
            rate_limit_state=RateLimitState.UNKNOWN,
        )
        if self.state is not None:
            self.state.record_source_decision(decision)
        return decision
