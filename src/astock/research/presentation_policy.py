"""Versioned presentation policy loader for public response rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from astock.core.project_root import resolve_project_root
from astock.schemas.presentation import ResponseMode, ResponseTaskType


@dataclass(frozen=True, slots=True)
class ResponseBudget:
    min_chars: int
    max_chars: int
    max_reasons: int


@dataclass(frozen=True, slots=True)
class PresentationPolicy:
    schema_version: str
    locale: str
    default_mode: ResponseMode
    safe_fallback_text: str
    max_bullets: int
    max_heading_level: int
    semantic_duplicate_threshold: float
    english_density_threshold: float
    diagnostic_intent_terms: tuple[str, ...]
    diagnostic_negation_terms: tuple[str, ...]
    forbidden_expressions: tuple[str, ...]
    protected_terms: tuple[str, ...]
    budgets: dict[ResponseTaskType, ResponseBudget]

    def budget(self, task_type: ResponseTaskType) -> ResponseBudget:
        try:
            return self.budgets[task_type]
        except KeyError as exc:
            raise ValueError(f"Presentation budget is missing for {task_type.value}") from exc


def load_presentation_policy(path: Path | None = None) -> PresentationPolicy:
    root = resolve_project_root(module_file=Path(__file__))
    policy_path = path or root / "configs" / "presentation_policy.yaml"
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid presentation policy: {policy_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "presentation-policy-v1":
        raise ValueError("Unsupported presentation policy")

    raw_budgets = raw.get("budgets")
    if not isinstance(raw_budgets, dict):
        raise ValueError("Presentation policy budgets must be an object")
    budgets: dict[ResponseTaskType, ResponseBudget] = {}
    for task_type in ResponseTaskType:
        item = raw_budgets.get(task_type.value)
        if not isinstance(item, dict):
            raise ValueError(f"Presentation budget is missing for {task_type.value}")
        min_chars = _positive_int(item.get("min_chars"), f"{task_type.value}.min_chars")
        max_chars = _positive_int(item.get("max_chars"), f"{task_type.value}.max_chars")
        max_reasons = _positive_int(item.get("max_reasons"), f"{task_type.value}.max_reasons")
        if min_chars > max_chars:
            raise ValueError(f"Presentation budget range is invalid for {task_type.value}")
        budgets[task_type] = ResponseBudget(
            min_chars=min_chars,
            max_chars=max_chars,
            max_reasons=max_reasons,
        )

    diagnostic_terms = _unique_non_empty_strings(
        raw.get("diagnostic_intent_terms"), "diagnostic_intent_terms"
    )
    diagnostic_negation_terms = _unique_non_empty_strings(
        raw.get("diagnostic_negation_terms"), "diagnostic_negation_terms"
    )
    forbidden = _unique_non_empty_strings(
        raw.get("forbidden_expressions"), "forbidden_expressions"
    )
    protected = _unique_non_empty_strings(raw.get("protected_terms"), "protected_terms")
    max_bullets = _positive_int(raw.get("max_bullets"), "max_bullets")
    max_heading_level = _positive_int(raw.get("max_heading_level"), "max_heading_level")
    semantic_duplicate_threshold = float(raw.get("semantic_duplicate_threshold", 0))
    english_density_threshold = float(raw.get("english_density_threshold", 0))
    if not 0.75 <= semantic_duplicate_threshold <= 1:
        raise ValueError("semantic_duplicate_threshold must be within [0.75, 1]")
    if not 0 < english_density_threshold < 1:
        raise ValueError("english_density_threshold must be within (0, 1)")
    safe_fallback_text = str(raw.get("safe_fallback_text") or "").strip()
    locale = str(raw.get("locale") or "").strip()
    if not safe_fallback_text or not locale:
        raise ValueError("Presentation policy locale and safe fallback text are required")

    return PresentationPolicy(
        schema_version="presentation-policy-v1",
        locale=locale,
        default_mode=ResponseMode(str(raw.get("default_mode"))),
        safe_fallback_text=safe_fallback_text,
        max_bullets=max_bullets,
        max_heading_level=max_heading_level,
        semantic_duplicate_threshold=semantic_duplicate_threshold,
        english_density_threshold=english_density_threshold,
        diagnostic_intent_terms=diagnostic_terms,
        diagnostic_negation_terms=diagnostic_negation_terms,
        forbidden_expressions=forbidden,
        protected_terms=protected,
        budgets=budgets,
    )


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Presentation policy {label} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"Presentation policy {label} must be positive")
    return parsed


def _unique_non_empty_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Presentation policy {label} must be a non-empty list")
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items) or len(items) != len(set(items)):
        raise ValueError(f"Presentation policy {label} must contain unique non-empty strings")
    return items


__all__ = ["PresentationPolicy", "ResponseBudget", "load_presentation_policy"]
