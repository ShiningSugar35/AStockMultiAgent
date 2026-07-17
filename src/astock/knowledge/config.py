"""Validated allowlist configuration for local and Zhihu knowledge sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from astock.schemas import KnowledgeSourceDefinition, KnowledgeSourceRegistry


def load_knowledge_sources(path: Path) -> KnowledgeSourceRegistry:
    payload = _load_yaml_mapping(path)
    return KnowledgeSourceRegistry.model_validate(payload)


def get_knowledge_source(
    registry: KnowledgeSourceRegistry,
    source_id: str,
) -> KnowledgeSourceDefinition:
    source = next((item for item in registry.sources if item.source_id == source_id), None)
    if source is None:
        raise ValueError(f"unknown knowledge source: {source_id}")
    return source


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"knowledge config must contain a mapping: {path}")
    return value
