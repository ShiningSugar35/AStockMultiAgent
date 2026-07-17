"""Validated configuration loader for frozen-weight shadow evaluation."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import ShadowEvaluationPolicy


def load_shadow_evaluation_policy(path: Path) -> ShadowEvaluationPolicy:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read shadow evaluation policy: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid shadow evaluation YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"shadow evaluation policy must contain a mapping: {path.name}")
    return ShadowEvaluationPolicy.model_validate(payload)


__all__ = ["load_shadow_evaluation_policy"]
