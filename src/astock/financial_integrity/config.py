"""Versioned financial rule and industry-profile configuration loaders."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import (
    FinancialIndustryProfileRegistry,
    FinancialRuleRegistry,
)


def load_financial_rule_registry(path: Path) -> FinancialRuleRegistry:
    return FinancialRuleRegistry.model_validate(_load_yaml_mapping(path))


def load_financial_industry_profiles(path: Path) -> FinancialIndustryProfileRegistry:
    return FinancialIndustryProfileRegistry.model_validate(_load_yaml_mapping(path))


def validate_financial_config(
    rules: FinancialRuleRegistry,
    profiles: FinancialIndustryProfileRegistry,
) -> None:
    rule_ids = {rule.rule_id for rule in rules.rules}
    known_profiles = {profile.profile_id for profile in profiles.profiles}
    for rule in rules.rules:
        unknown_profiles = set(rule.applicable_industries) - known_profiles
        unknown_profiles.update(set(rule.excluded_industries) - known_profiles)
        if unknown_profiles:
            values = ", ".join(sorted(profile.value for profile in unknown_profiles))
            raise ValueError(f"rule {rule.rule_id} references unknown profiles: {values}")
    for profile in profiles.profiles:
        unknown_rules = sorted(set(profile.excluded_rule_ids) - rule_ids)
        if unknown_rules:
            raise ValueError(
                f"profile {profile.profile_id.value} excludes unknown rules: "
                f"{', '.join(unknown_rules)}"
            )


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read configuration: {path.name}") from exc
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML configuration: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"configuration must contain a mapping: {path.name}")
    return payload
