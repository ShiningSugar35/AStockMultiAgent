"""Versioned capability routes for market-reference acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from astock.schemas import CompletenessSemantics, Market, ProviderRegistry

_FORMAL_ROUTE_COMPLETENESS = {
    "instrument.master": CompletenessSemantics.FULL_UNIVERSE,
    "instrument.identity": CompletenessSemantics.EXACT_ITEM,
    "market.calendar": CompletenessSemantics.WINDOW_EXHAUSTIVE,
    "market.daily_unadjusted": CompletenessSemantics.CONTINUOUS_SERIES,
    "corporate_actions.official_evidence": CompletenessSemantics.WINDOW_EXHAUSTIVE,
}


@dataclass(frozen=True, slots=True)
class ReferenceRouteStep:
    provider_id: str
    operation: str
    markets: frozenset[Market] | None = None

    def supports(self, market: Market | None) -> bool:
        return self.markets is None or (market is not None and market in self.markets)


@dataclass(frozen=True, slots=True)
class MarketReferenceConfig:
    config_version: str
    routes: dict[str, tuple[ReferenceRouteStep, ...]]
    retry_max_attempts: int
    retry_backoff_seconds: float
    identity_search_max_pages: int
    minimum_instrument_records: dict[Market, int]
    official_market_coverage: dict[str, dict[Market, str]]

    def route(self, capability: str) -> tuple[ReferenceRouteStep, ...]:
        try:
            return self.routes[capability]
        except KeyError as exc:
            raise ValueError(f"Market reference route is not configured: {capability}") from exc

    def official_coverage(self, capability: str, market: Market) -> str:
        return self.official_market_coverage.get(capability, {}).get(market, "UNAVAILABLE")


def load_market_reference_config(
    path: Path,
    registry: ProviderRegistry,
) -> MarketReferenceConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid market reference configuration: {path}") from exc
    if not isinstance(raw, dict) or raw.get("config_version") != "market-reference-v2":
        raise ValueError("Unsupported market reference configuration")
    if "circuit_breakers" in raw:
        raise ValueError(
            "Provider-wide market reference circuit breakers are unsupported; "
            "use source_resilience provider+capability policy"
        )
    provider_by_id = {item.provider_id: item for item in registry.providers}
    raw_routes = raw.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ValueError("Market reference routes are empty")
    routes: dict[str, tuple[ReferenceRouteStep, ...]] = {}
    for capability, values in raw_routes.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"Market reference route is empty: {capability}")
        steps: list[ReferenceRouteStep] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Market reference route step must be an object")
            provider_id = str(value["provider_id"])
            operation = str(value["operation"])
            raw_markets = value.get("markets")
            markets: frozenset[Market] | None = None
            if raw_markets is not None:
                if not isinstance(raw_markets, list) or not raw_markets:
                    raise ValueError("Market-scoped route markets must be a non-empty list")
                parsed_markets = frozenset(Market(str(item)) for item in raw_markets)
                if Market.INDEX in parsed_markets:
                    raise ValueError("Market-scoped provider routes support equity markets only")
                if len(parsed_markets) != len(raw_markets):
                    raise ValueError("Market-scoped provider route contains duplicate markets")
                markets = parsed_markets
            definition = provider_by_id.get(provider_id)
            if definition is None:
                raise ValueError(f"Unknown market reference provider: {provider_id}")
            declared = str(capability)
            base_capability = {
                "instrument.identity": "instrument.identity",
                "instrument.master": "instrument.master",
                "market.calendar": "market.calendar",
                "market.daily_unadjusted": "market.daily_unadjusted",
                "corporate_actions.structured_hint": "corporate_actions.structured_hint",
                "corporate_actions.official_evidence": "corporate_actions.official_evidence",
            }.get(declared, declared)
            if base_capability not in definition.capabilities:
                raise ValueError(
                    f"Provider {provider_id} does not declare route capability {base_capability}"
                )
            required_semantics = _FORMAL_ROUTE_COMPLETENESS.get(base_capability)
            if required_semantics is not None:
                if base_capability not in definition.formal_capabilities:
                    raise ValueError(
                        f"Provider {provider_id} is not formally eligible for {base_capability}"
                    )
                if definition.completeness_semantics.get(base_capability) is not required_semantics:
                    raise ValueError(
                        f"Provider {provider_id} has invalid completeness semantics for "
                        f"{base_capability}"
                    )
            steps.append(
                ReferenceRouteStep(provider_id=provider_id, operation=operation, markets=markets)
            )
        if len({(item.provider_id, item.operation, item.markets) for item in steps}) != len(steps):
            raise ValueError(f"Market reference route contains duplicate steps: {capability}")
        routes[str(capability)] = tuple(steps)
    retry = raw.get("retry", {})
    if not isinstance(retry, dict):
        raise ValueError("Market reference retry policy must be an object")
    max_attempts = int(retry.get("max_attempts", 2))
    backoff = float(retry.get("backoff_seconds", 0.25))
    identity_search_max_pages = int(raw.get("identity_search_max_pages", 50))
    if max_attempts != 1 or not 0 <= backoff <= 10:
        raise ValueError(
            "Market reference retry must stay at one attempt; HTTP retry belongs to transport"
        )
    if not 1 <= identity_search_max_pages <= 200:
        raise ValueError("Identity search page bound must be in 1..200")
    raw_minimum_records = raw.get("minimum_instrument_records")
    if not isinstance(raw_minimum_records, dict):
        raise ValueError("minimum_instrument_records must be an object")
    minimum_records: dict[Market, int] = {}
    required_markets = {Market.XSHG, Market.XSHE, Market.BJSE}
    for raw_market, raw_count in raw_minimum_records.items():
        parsed_market = Market(str(raw_market))
        if parsed_market not in required_markets:
            raise ValueError("minimum_instrument_records supports equity markets only")
        count = int(raw_count)
        if not 1 <= count <= 10000:
            raise ValueError("minimum instrument record floor must be in 1..10000")
        minimum_records[parsed_market] = count
    if set(minimum_records) != required_markets:
        raise ValueError("minimum_instrument_records must cover XSHG/XSHE/BJSE")
    raw_coverage = raw.get("official_market_coverage", {})
    if not isinstance(raw_coverage, dict):
        raise ValueError("Official market coverage must be an object")
    coverage: dict[str, dict[Market, str]] = {}
    for capability, values in raw_coverage.items():
        if not isinstance(values, dict):
            raise ValueError("Official market coverage entry must be an object")
        per_market: dict[Market, str] = {}
        for raw_market, status in values.items():
            parsed_market = Market(str(raw_market))
            parsed_status = str(status)
            if parsed_status not in {"AVAILABLE", "PARTIAL", "UNAVAILABLE"}:
                raise ValueError("Official market coverage status is invalid")
            per_market[parsed_market] = parsed_status
        coverage[str(capability)] = per_market
    return MarketReferenceConfig(
        config_version=str(raw["config_version"]),
        routes=routes,
        retry_max_attempts=max_attempts,
        retry_backoff_seconds=backoff,
        identity_search_max_pages=identity_search_max_pages,
        minimum_instrument_records=minimum_records,
        official_market_coverage=coverage,
    )


__all__ = [
    "MarketReferenceConfig",
    "ReferenceRouteStep",
    "load_market_reference_config",
]
