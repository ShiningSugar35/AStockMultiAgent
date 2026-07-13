from __future__ import annotations

from astock.core.source_router import SourceAccessRouter
from astock.schemas import (
    AccessTransport,
    SourceAccessRequest,
    TransportCapability,
)


def capability(transport: AccessTransport, available: bool) -> TransportCapability:
    return TransportCapability(
        source_id="cninfo",
        transport=transport,
        requested_capabilities=["filing-search"],
        available=available,
        reason=f"{transport.value} {'ready' if available else 'disabled'}",
    )


def test_router_prefers_api_and_does_not_duplicate_lower_transports(state) -> None:
    router = SourceAccessRouter(state)
    decision = router.decide(
        SourceAccessRequest(source_id="cninfo", requested_capability="filing-search"),
        [
            capability(AccessTransport.BROWSER, True),
            capability(AccessTransport.API, True),
            capability(AccessTransport.MCP, True),
        ],
    )
    assert decision.selected_transport is AccessTransport.API
    assert decision.fallback_chain == [AccessTransport.API]


def test_router_falls_back_to_manual_when_automation_is_unavailable() -> None:
    decision = SourceAccessRouter().decide(
        SourceAccessRequest(source_id="cninfo", requested_capability="filing-search"),
        [capability(AccessTransport.API, False)],
    )
    assert decision.selected_transport is AccessTransport.MANUAL
    assert decision.fallback_chain[-1] is AccessTransport.MANUAL


def test_router_selects_mcp_before_browser_and_persists_one_decision(state) -> None:
    decision = SourceAccessRouter(state).decide(
        SourceAccessRequest(source_id="cninfo", requested_capability="filing-search"),
        [
            capability(AccessTransport.API, False),
            capability(AccessTransport.MCP, True),
            capability(AccessTransport.BROWSER, True),
        ],
    )
    assert decision.selected_transport is AccessTransport.MCP
    assert decision.fallback_chain == [AccessTransport.API, AccessTransport.MCP]
    with state.connect() as connection:
        stored = connection.execute(
            "SELECT selected_transport,fallback_chain_json FROM source_access_decision "
            "WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
    assert stored["selected_transport"] == "MCP"
    assert stored["fallback_chain_json"] == '["API", "MCP"]'
