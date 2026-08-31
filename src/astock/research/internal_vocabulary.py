"""Dynamically derive internal implementation vocabulary for investor-answer audits."""

from __future__ import annotations

import inspect
from pathlib import Path

from astock.core.project_root import resolve_project_root
from astock.providers.config import load_provider_registry


def internal_vocabulary_terms(project_root: Path | None = None) -> set[str]:
    root = project_root or resolve_project_root(module_file=Path(__file__))
    terms: set[str] = set()
    try:
        registry = load_provider_registry(root / "configs" / "provider_registry.yaml")
        for definition in registry.providers:
            terms.add(definition.provider_id.lower())
            terms.add(definition.adapter_class.rpartition(":")[-1].lower())
    except (OSError, ValueError):
        pass
    terms.update(_schema_terms())
    terms.update(_cli_terms())
    return {item for item in terms if len(item) >= 4}


def _schema_terms() -> set[str]:
    from astock.schemas import adaptation, institutional_research, research_runtime
    from astock.schemas import research_acquisition as acquisition

    terms: set[str] = set()
    for module in (research_runtime, acquisition, institutional_research, adaptation):
        for name, value in vars(module).items():
            if not inspect.isclass(value) or not getattr(value, "__module__", "").startswith(
                "astock.schemas"
            ):
                continue
            if any(
                token in name
                for token in (
                    "Artifact",
                    "Protocol",
                    "Pack",
                    "Release",
                    "Schedule",
                    "Classification",
                    "BaseCase",
                    "DecisionContext",
                )
            ):
                terms.add(name.lower())
    return terms


def _cli_terms() -> set[str]:
    try:
        from astock.cli import app
    except (ImportError, RuntimeError):
        return set()
    result: set[str] = set()
    for command in getattr(app, "registered_commands", []):
        name = getattr(command, "name", None)
        if isinstance(name, str) and name:
            result.add(name.lower())
    return result


__all__ = ["internal_vocabulary_terms"]
