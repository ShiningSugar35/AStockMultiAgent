"""Versioned resource bounds for specialist routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class SpecialistResourcePolicy:
    policy_version: str
    minimum_budget: int
    default_budget: int
    maximum_budget: int

    def resolve(self, requested: int | None) -> int:
        budget = self.default_budget if requested is None else requested
        if not self.minimum_budget <= budget <= self.maximum_budget:
            raise ValueError("specialist budget is outside active resource policy")
        return budget


def load_specialist_resource_policy(path: Path) -> SpecialistResourcePolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid specialist resource policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "specialist-resource-policy-v1":
        raise ValueError("Unsupported specialist resource policy")
    minimum = int(raw["minimum_budget"])
    default = int(raw["default_budget"])
    maximum = int(raw["maximum_budget"])
    if not 1 <= minimum <= default <= maximum <= 32:
        raise ValueError("specialist resource budget bounds are invalid")
    return SpecialistResourcePolicy(
        policy_version=str(raw["schema_version"]),
        minimum_budget=minimum,
        default_budget=default,
        maximum_budget=maximum,
    )


__all__ = ["SpecialistResourcePolicy", "load_specialist_resource_policy"]
