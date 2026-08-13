"""Versioned current-research policy and deterministic capability scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from astock.core.hashing import content_hash
from astock.core.state import StateStore
from astock.providers.config import load_provider_registry
from astock.schemas.provider import ProviderHealthStatus, ProviderOfficiality, ProviderRegistry
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CapabilityScheduleStep,
    CurrentResearchSchedule,
    ExternalAuthority,
)


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    capability: AcquisitionCapability
    core: bool
    stage: int
    dependencies: tuple[AcquisitionCapability, ...]
    provider_capabilities: tuple[str, ...]
    research_question: str
    preferred_authorities: tuple[ExternalAuthority, ...]
    external_on: tuple[AcquisitionAttemptStatus, ...]


@dataclass(frozen=True, slots=True)
class CurrentResearchPolicy:
    policy_version: str
    policy_hash: str
    default_lookback_days: int
    minimum_lookback_days: int
    maximum_lookback_days: int
    max_workers: int
    automatic_resolution_budget_seconds: int
    manual_last: bool
    capabilities: dict[AcquisitionCapability, CapabilityPolicy]

    @property
    def core_capabilities(self) -> frozenset[AcquisitionCapability]:
        return frozenset(item.capability for item in self.capabilities.values() if item.core)


class CapabilityGraph:
    """Build a frozen schedule from policy + provider capability/health, not service constants."""

    def __init__(
        self,
        policy: CurrentResearchPolicy,
        registry: ProviderRegistry,
        state: StateStore,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.state = state

    def build(
        self,
        company_id: str,
        market: Market,
        *,
        lookback_days: int,
        planned_at: datetime,
        capability_filter: set[AcquisitionCapability] | None = None,
        planner_plan_artifact_id: str | None = None,
    ) -> CurrentResearchSchedule:
        selected = (
            set(self.policy.capabilities)
            if capability_filter is None
            else set(self.policy.core_capabilities) | set(capability_filter)
        )
        changed = True
        while changed:
            changed = False
            for capability in tuple(selected):
                for dependency in self.policy.capabilities[capability].dependencies:
                    if dependency not in selected:
                        selected.add(dependency)
                        changed = True
        steps: list[CapabilityScheduleStep] = []
        for capability_policy in sorted(
            (
                item
                for item in self.policy.capabilities.values()
                if item.capability in selected
            ),
            key=lambda item: (item.stage, item.capability.value),
        ):
            ranked = self._rank_providers(capability_policy.provider_capabilities)
            candidates = [provider_id for provider_id, _status in ranked]
            degraded = [
                provider_id
                for provider_id, status in ranked
                if status not in {ProviderHealthStatus.HEALTHY, ProviderHealthStatus.NOT_PROBED}
            ]
            steps.append(
                CapabilityScheduleStep(
                    capability=capability_policy.capability,
                    stage=capability_policy.stage,
                    core=capability_policy.core,
                    dependencies=list(capability_policy.dependencies),
                    provider_candidates=candidates,
                    degraded_provider_candidates=degraded,
                    preferred_authorities=list(capability_policy.preferred_authorities),
                )
            )
        identity = {
            "policy_version": self.policy.policy_version,
            "policy_hash": self.policy.policy_hash,
            "company_id": company_id,
            "market": market.value,
            "lookback_days": lookback_days,
            "planned_at": planned_at.isoformat(),
            "planner_plan_artifact_id": planner_plan_artifact_id,
            "steps": [item.model_dump(mode="json") for item in steps],
        }
        return CurrentResearchSchedule(
            created_at=planned_at,
            schedule_id=f"current-research-schedule:{content_hash(identity)}",
            policy_version=self.policy.policy_version,
            policy_hash=self.policy.policy_hash,
            company_id=company_id,
            market=market,
            lookback_days=lookback_days,
            max_workers=self.policy.max_workers,
            automatic_resolution_budget_seconds=self.policy.automatic_resolution_budget_seconds,
            planner_plan_artifact_id=planner_plan_artifact_id,
            steps=steps,
        )

    def _rank_providers(
        self, required_capabilities: tuple[str, ...]
    ) -> list[tuple[str, ProviderHealthStatus]]:
        matched = [
            item
            for item in self.registry.providers
            if any(capability in item.capabilities for capability in required_capabilities)
        ]
        ranked: list[tuple[tuple[int, int, int, str], str, ProviderHealthStatus]] = []
        health_weight = {
            ProviderHealthStatus.HEALTHY: 4,
            ProviderHealthStatus.NOT_PROBED: 3,
            ProviderHealthStatus.DEGRADED: 2,
            ProviderHealthStatus.UNAVAILABLE: 0,
            ProviderHealthStatus.CORRUPT: 0,
        }
        officiality_weight = {
            ProviderOfficiality.PRIMARY_OFFICIAL: 2,
            ProviderOfficiality.SECONDARY_STRUCTURED: 1,
        }
        for definition in matched:
            row, _head = self.state.get_provider_probe_health_snapshot(definition.provider_id)
            try:
                status = (
                    ProviderHealthStatus(str(row["status"]))
                    if row is not None and row.get("status")
                    else ProviderHealthStatus.NOT_PROBED
                )
            except ValueError:
                status = ProviderHealthStatus.CORRUPT
            key = (
                -health_weight[status],
                -officiality_weight[definition.officiality],
                -definition.priority,
                definition.provider_id,
            )
            ranked.append((key, definition.provider_id, status))
        ranked.sort(key=lambda item: item[0])
        return [(provider_id, status) for _key, provider_id, status in ranked]


def load_current_research_policy(path: Path) -> CurrentResearchPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid current research policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "current-research-policy-v1":
        raise ValueError("Unsupported current research policy")
    raw_capabilities = raw.get("capabilities")
    if not isinstance(raw_capabilities, dict) or not raw_capabilities:
        raise ValueError("Current research capability policy is empty")
    capabilities: dict[AcquisitionCapability, CapabilityPolicy] = {}
    for raw_capability, value in raw_capabilities.items():
        capability = AcquisitionCapability(str(raw_capability))
        if not isinstance(value, dict):
            raise ValueError("Current research capability policy must be an object")
        dependencies_raw = value.get("dependencies", [])
        providers_raw = value.get("provider_capabilities", [])
        authorities_raw = value.get("preferred_authorities", [])
        external_on_raw = value.get("external_on", [])
        if not isinstance(dependencies_raw, list) or not isinstance(providers_raw, list):
            raise ValueError("Current research dependencies/provider capabilities must be lists")
        if not isinstance(authorities_raw, list) or not authorities_raw:
            raise ValueError("Current research authority preference must be non-empty")
        if not isinstance(external_on_raw, list):
            raise ValueError("Current research external_on must be a list")
        capabilities[capability] = CapabilityPolicy(
            capability=capability,
            core=bool(value.get("core", False)),
            stage=int(value["stage"]),
            dependencies=tuple(AcquisitionCapability(str(item)) for item in dependencies_raw),
            provider_capabilities=tuple(str(item) for item in providers_raw),
            research_question=str(value["research_question"]),
            preferred_authorities=tuple(ExternalAuthority(str(item)) for item in authorities_raw),
            external_on=tuple(AcquisitionAttemptStatus(str(item)) for item in external_on_raw),
        )
    if set(capabilities) != set(AcquisitionCapability):
        raise ValueError("Current research policy must cover every acquisition capability")
    for item in capabilities.values():
        for dependency in item.dependencies:
            if capabilities[dependency].stage >= item.stage:
                raise ValueError("Capability dependency must be in an earlier stage")
    minimum = int(raw["minimum_lookback_days"])
    default = int(raw["default_lookback_days"])
    maximum = int(raw["maximum_lookback_days"])
    workers = int(raw["max_workers"])
    budget = int(raw["automatic_resolution_budget_seconds"])
    if not (1 <= minimum <= default <= maximum <= 3650):
        raise ValueError("Current research lookback bounds are invalid")
    if not 1 <= workers <= 16:
        raise ValueError("Current research max_workers must be in 1..16")
    if not 60 <= budget <= 7200:
        raise ValueError("Current research automatic budget must be in 60..7200")
    if raw.get("manual_last") is not True:
        raise ValueError("Current research policy must keep manual escalation last")
    return CurrentResearchPolicy(
        policy_version=str(raw["schema_version"]),
        policy_hash=content_hash(raw),
        default_lookback_days=default,
        minimum_lookback_days=minimum,
        maximum_lookback_days=maximum,
        max_workers=workers,
        automatic_resolution_budget_seconds=budget,
        manual_last=True,
        capabilities=capabilities,
    )


def load_default_current_research_policy(project_root: Path) -> CurrentResearchPolicy:
    return load_current_research_policy(project_root / "configs" / "current_research_policy.yaml")


def load_default_provider_registry(project_root: Path) -> ProviderRegistry:
    return load_provider_registry(project_root / "configs" / "provider_registry.yaml")


__all__ = [
    "CapabilityGraph",
    "CapabilityPolicy",
    "CurrentResearchPolicy",
    "load_current_research_policy",
    "load_default_current_research_policy",
    "load_default_provider_registry",
]
