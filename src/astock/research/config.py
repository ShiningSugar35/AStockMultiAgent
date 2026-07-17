"""Validated configuration for the common research kernel."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import (
    PositionLifecycleConfig,
    ResearchCoreConfig,
    ResearchDiagnosticConfig,
    ResearchSkillRegistry,
)


def load_research_core_config(path: Path) -> ResearchCoreConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read research configuration: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid research YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"research configuration must contain a mapping: {path.name}")
    return ResearchCoreConfig.model_validate(payload)


def load_research_skill_registry(path: Path) -> ResearchSkillRegistry:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read research Skill configuration: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid research Skill YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"research Skill configuration must contain a mapping: {path.name}"
        )
    return ResearchSkillRegistry.model_validate(payload)


def load_research_diagnostic_config(path: Path) -> ResearchDiagnosticConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read research diagnostic configuration: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid research diagnostic YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"research diagnostic configuration must contain a mapping: {path.name}"
        )
    return ResearchDiagnosticConfig.model_validate(payload)


def load_position_lifecycle_config(path: Path) -> PositionLifecycleConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read position lifecycle configuration: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid position lifecycle YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"position lifecycle configuration must contain a mapping: {path.name}"
        )
    return PositionLifecycleConfig.model_validate(payload)


__all__ = [
    "load_research_core_config",
    "load_research_diagnostic_config",
    "load_research_skill_registry",
    "load_position_lifecycle_config",
]
