"""Versioned financial rule and industry-profile configuration loaders."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas import (
    FinancialAnomalyModelRegistry,
    FinancialIndustryProfileRegistry,
    FinancialRuleOutputType,
    FinancialRuleRegistry,
    FinancialSeverity,
)


def load_financial_anomaly_models(path: Path) -> FinancialAnomalyModelRegistry:
    return FinancialAnomalyModelRegistry.model_validate(_load_yaml_mapping(path))


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
    # Core equation coverage is determined from rule metadata, not stock codes,
    # report titles, or a hard-coded list of industry names.
    core_identities = {
        rule.rule_id for rule in rules.rules
        if rule.default_enabled
        and rule.output_type is FinancialRuleOutputType.IDENTITY
        and rule.severity is FinancialSeverity.HIGH
    }
    for rule in rules.rules:
        unknown_profiles = set(rule.applicable_industries) - known_profiles
        unknown_profiles.update(set(rule.excluded_industries) - known_profiles)
        if unknown_profiles:
            values = ", ".join(sorted(profile.value for profile in unknown_profiles))
            raise ValueError(f"rule {rule.rule_id} references unknown profiles: {values}")
        if rule.rule_id in core_identities and (
            set(rule.applicable_industries) != known_profiles or rule.excluded_industries
        ):
            raise ValueError(
                f"core accounting identity {rule.rule_id} must cover all registered industries"
            )
    for profile in profiles.profiles:
        forbidden = sorted(set(profile.excluded_rule_ids) & core_identities)
        if forbidden:
            names = ", ".join(forbidden)
            raise ValueError(
                f"industry profile {profile.profile_id.value} cannot exclude "
                f"core accounting identity: {names}"
            )
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
