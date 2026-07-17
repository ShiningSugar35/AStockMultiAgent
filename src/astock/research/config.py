"""Validated configuration for the common research kernel."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import ResearchCoreConfig


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


__all__ = ["load_research_core_config"]
