"""Validated configuration loader for the deterministic committee."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import CommitteeRuleConfig


def load_committee_rules(path: Path) -> CommitteeRuleConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read committee rules: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid committee rules YAML: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"committee rules must contain a mapping: {path.name}")
    return CommitteeRuleConfig.model_validate(payload)


__all__ = ["load_committee_rules"]
