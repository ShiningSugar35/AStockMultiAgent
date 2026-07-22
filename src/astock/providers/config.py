"""Strict project-level provider registry loading."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from astock.schemas import ProviderDefinition, ProviderRegistry


def load_provider_registry(path: Path) -> ProviderRegistry:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        registry = ProviderRegistry.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"Invalid provider registry: {path}") from exc
    for provider in registry.providers:
        fixture = (path.parent.parent / provider.recorded_fixture).resolve()
        project_root = path.parent.parent.resolve()
        if not fixture.is_relative_to(project_root):
            raise ValueError(f"Provider fixture escapes project root: {provider.provider_id}")
    return registry


def get_provider(registry: ProviderRegistry, provider_id: str) -> ProviderDefinition:
    matches = [item for item in registry.providers if item.provider_id == provider_id]
    if len(matches) != 1:
        raise ValueError(f"Unknown provider: {provider_id}")
    return matches[0]


__all__ = ["get_provider", "load_provider_registry"]
