from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import yaml

from astock.core.source_router import SourceAccessRouter
from astock.portfolio.allocators import (
    PortfolioAllocatorPlugin,
    PortfolioAllocatorRegistry,
    load_portfolio_allocator_policy,
)
from astock.providers.config import load_provider_registry
from astock.research.policy import CapabilityGraph, load_current_research_policy
from astock.research.resource_policy import load_specialist_resource_policy
from astock.schemas import AccessTransport, SourceAccessRequest, TransportCapability
from astock.schemas.portfolio import PortfolioAllocationMethod
from astock.schemas.reference_data import Market
from astock.schemas.research import SpecialistRouteRequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 13, 8, tzinfo=UTC)


class _HealthState:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    def get_provider_probe_health_snapshot(self, provider_id: str):
        status = self.statuses.get(provider_id)
        return ({"status": status}, None) if status else (None, None)


def test_current_research_policy_changes_schedule_without_service_constants(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "current_research_policy.yaml").read_text(encoding="utf-8")
    )
    payload["default_lookback_days"] = 150
    payload["max_workers"] = 6
    path = tmp_path / "current-policy.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    policy = load_current_research_policy(path)
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    graph = CapabilityGraph(
        policy,
        registry,
        _HealthState(
            {
                "eastmoney-reference": "UNAVAILABLE",
                "sina-reference": "HEALTHY",
            }
        ),  # type: ignore[arg-type]
    )

    schedule = graph.build("600989", Market.XSHG, lookback_days=150, planned_at=NOW)
    daily = next(item for item in schedule.steps if item.capability.value == "DAILY_MARKET")

    assert schedule.lookback_days == 150
    assert schedule.max_workers == 6
    assert schedule.automatic_resolution_budget_seconds == 1800
    assert schedule.manual_last is True
    assert daily.provider_candidates[0] == "sina-reference"
    assert "eastmoney-reference" in daily.degraded_provider_candidates


def test_source_router_gives_available_primary_official_hard_priority() -> None:
    router = SourceAccessRouter()
    request = SourceAccessRequest(source_id="issuer", requested_capability="financial.official")
    decision = router.decide(
        request,
        [
            TransportCapability(
                source_id="issuer",
                transport=AccessTransport.API,
                requested_capabilities=["financial.official"],
                available=True,
                reason="fast secondary API",
                officiality="SECONDARY_STRUCTURED",
                health_status="HEALTHY",
                freshness_score=Decimal("1"),
                latency_ms=1,
                cost_efficiency_score=Decimal("1"),
                auth_ease_score=Decimal("1"),
            ),
            TransportCapability(
                source_id="issuer",
                transport=AccessTransport.BROWSER,
                requested_capabilities=["financial.official"],
                available=True,
                reason="issuer official disclosure",
                officiality="PRIMARY_OFFICIAL",
                health_status="DEGRADED",
                freshness_score=Decimal("0.8"),
                latency_ms=5000,
            ),
        ],
    )

    assert decision.selected_transport is AccessTransport.BROWSER
    assert decision.fallback_chain == [AccessTransport.BROWSER]


def test_portfolio_allocator_registry_is_extensible_without_portfolio_service_switch() -> None:
    registry = PortfolioAllocatorRegistry()
    plugin = PortfolioAllocatorPlugin(
        method=PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED,
        build_scores=lambda aligned: np.ones(len(aligned.company_ids), dtype=float),
        model_risk_codes=("CUSTOM_PLUGIN_TEST",),
    )
    registry.register(plugin)

    assert registry.get(PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED) is plugin
    assert registry.methods() == (PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED,)
    policy = load_portfolio_allocator_policy(
        PROJECT_ROOT / "configs" / "portfolio_allocators.yaml"
    )
    assert policy.default_method is PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED


def test_specialist_budget_is_resource_policy_not_schema_literal() -> None:
    policy = load_specialist_resource_policy(
        PROJECT_ROOT / "configs" / "specialist_resource_policy.yaml"
    )
    request = SpecialistRouteRequest(
        base_case_id="base:test",
        thesis_tags=[],
        industry_tags=[],
        event_tags=[],
        horizon="medium",
        available_inputs=[],
        available_frequencies=[],
        specialist_budget=4,
    )

    assert policy.default_budget == 3
    assert policy.resolve(request.specialist_budget) == 4
    assert policy.maximum_budget == 8
