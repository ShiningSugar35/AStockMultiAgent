from __future__ import annotations

from astock.core.source_router import SourceAccessRouter
from astock.schemas import (
    AccessTransport,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)


def capability(
    transport: AccessTransport,
    available: bool,
    requested_capability: str = "filing-search",
) -> TransportCapability:
    return TransportCapability(
        source_id="cninfo",
        transport=transport,
        requested_capabilities=[requested_capability],
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


def test_enterprise_intelligence_prefers_mcp_before_api() -> None:
    requested = "enterprise.personnel"
    decision = SourceAccessRouter().decide(
        SourceAccessRequest(requested_capability=requested),
        [
            capability(AccessTransport.API, True, requested),
            capability(AccessTransport.MCP, True, requested),
            capability(AccessTransport.BROWSER, True, requested),
        ],
    )

    assert decision.selected_transport is AccessTransport.MCP
    assert decision.fallback_chain == [AccessTransport.MCP]


def test_enterprise_official_source_outranks_commercial_mcp() -> None:
    requested = "enterprise.personnel"
    official = TransportCapability(
        source_id="government-official-web",
        transport=AccessTransport.BROWSER,
        requested_capabilities=[requested],
        available=True,
        reason="official government appointment notice",
        officiality="PRIMARY_OFFICIAL",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        formal_eligible=True,
    )
    commercial = TransportCapability(
        source_id="qcc-enterprise-intelligence-mcp",
        transport=AccessTransport.MCP,
        requested_capabilities=[requested],
        available=True,
        reason="qualified commercial enterprise intelligence",
        officiality="SECONDARY_STRUCTURED",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        formal_eligible=True,
    )

    decision = SourceAccessRouter().decide(
        SourceAccessRequest(requested_capability=requested, formal_use=True),
        [commercial, official],
    )

    assert decision.selected_source_id == "government-official-web"
    assert decision.selected_transport is AccessTransport.BROWSER
