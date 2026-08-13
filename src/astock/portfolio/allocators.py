"""Plugin registry for deterministic portfolio allocation score builders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from astock.portfolio.analytics import (
    AlignedPortfolioData,
    hierarchical_risk_weights,
    minimum_variance_weights,
)
from astock.schemas.portfolio import PortfolioAllocationMethod

ScoreBuilder = Callable[[AlignedPortfolioData], np.ndarray]


@dataclass(frozen=True, slots=True)
class PortfolioAllocatorPlugin:
    method: PortfolioAllocationMethod
    build_scores: ScoreBuilder
    model_risk_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioAllocatorPolicy:
    policy_version: str
    default_method: PortfolioAllocationMethod
    enabled_methods: tuple[PortfolioAllocationMethod, ...]


class PortfolioAllocatorRegistry:
    def __init__(self) -> None:
        self._plugins: dict[PortfolioAllocationMethod, PortfolioAllocatorPlugin] = {}

    def register(self, plugin: PortfolioAllocatorPlugin) -> None:
        if plugin.method in self._plugins:
            raise ValueError(f"portfolio allocator already registered: {plugin.method}")
        self._plugins[plugin.method] = plugin

    def get(self, method: PortfolioAllocationMethod) -> PortfolioAllocatorPlugin:
        try:
            return self._plugins[method]
        except KeyError as exc:
            raise ValueError(f"portfolio allocator is not registered: {method}") from exc

    def methods(self) -> tuple[PortfolioAllocationMethod, ...]:
        return tuple(sorted(self._plugins, key=lambda item: item.value))


def default_portfolio_allocator_registry() -> PortfolioAllocatorRegistry:
    registry = PortfolioAllocatorRegistry()
    registry.register(
        PortfolioAllocatorPlugin(
            method=PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED,
            build_scores=lambda aligned: np.ones(len(aligned.company_ids), dtype=float),
            model_risk_codes=("NAIVE_DIVERSIFICATION_BENCHMARK", "NO_EXPECTED_RETURN_ESTIMATE"),
        )
    )
    registry.register(
        PortfolioAllocatorPlugin(
            method=PortfolioAllocationMethod.INVERSE_VOLATILITY,
            build_scores=lambda aligned: 1.0
            / np.sqrt(np.maximum(np.diag(aligned.covariance), 1e-12)),
            model_risk_codes=("NO_EXPECTED_RETURN_ESTIMATE", "VOLATILITY_ESTIMATION_RISK"),
        )
    )
    registry.register(
        PortfolioAllocatorPlugin(
            method=PortfolioAllocationMethod.HIERARCHICAL_RISK,
            build_scores=hierarchical_risk_weights,
            model_risk_codes=(
                "CORRELATION_CLUSTER_ESTIMATION_RISK",
                "NO_EXPECTED_RETURN_ESTIMATE",
            ),
        )
    )
    registry.register(
        PortfolioAllocatorPlugin(
            method=PortfolioAllocationMethod.SHRINKAGE_MIN_VARIANCE,
            build_scores=lambda aligned: minimum_variance_weights(aligned.covariance),
            model_risk_codes=(
                "COVARIANCE_ESTIMATION_RISK",
                "LEDOIT_WOLF_SHRINKAGE",
                "NO_EXPECTED_RETURN_ESTIMATE",
            ),
        )
    )
    return registry


def load_portfolio_allocator_policy(path: Path) -> PortfolioAllocatorPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid portfolio allocator policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "portfolio-allocators-v1":
        raise ValueError("Unsupported portfolio allocator policy")
    methods_raw = raw.get("enabled_methods")
    if not isinstance(methods_raw, list) or not methods_raw:
        raise ValueError("portfolio allocator enabled_methods must be non-empty")
    methods = tuple(PortfolioAllocationMethod(str(item)) for item in methods_raw)
    if len(methods) != len(set(methods)):
        raise ValueError("portfolio allocator enabled methods must be unique")
    default = PortfolioAllocationMethod(str(raw["default_method"]))
    if default not in methods:
        raise ValueError("default portfolio allocator must be enabled")
    return PortfolioAllocatorPolicy(
        policy_version=str(raw["schema_version"]),
        default_method=default,
        enabled_methods=methods,
    )


__all__ = [
    "PortfolioAllocatorPlugin",
    "PortfolioAllocatorPolicy",
    "PortfolioAllocatorRegistry",
    "default_portfolio_allocator_registry",
    "load_portfolio_allocator_policy",
]
