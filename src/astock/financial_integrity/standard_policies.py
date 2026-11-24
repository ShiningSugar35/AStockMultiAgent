"""Versioned, evidence-gated financial presentation standards.

Standards select only *existing* audited calculators; they never author new
formulas or relax cash/balance identities. Production is opt-in per issuer and
official release through a separately approved immutable adoption artifact.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas.financial import (
    FinancialRuleOutputType,
    FinancialRuleRegistry,
    FinancialSeverity,
)
from astock.schemas.financial_presentation import FinancialStandardPolicyRegistry


def load_financial_standard_policies(
    path: Path, financial_rules: FinancialRuleRegistry,
) -> FinancialStandardPolicyRegistry:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("standard policy registry must be a mapping")
    registry = FinancialStandardPolicyRegistry.model_validate(data)
    existing = {rule.rule_id: rule for rule in financial_rules.rules}
    core = {
        rule.rule_id
        for rule in financial_rules.rules
        if rule.default_enabled
        and rule.output_type is FinancialRuleOutputType.IDENTITY
        and rule.severity is FinancialSeverity.HIGH
    }
    if not core:
        raise ValueError("missing core accounting identities")
    for policy in registry.profiles:
        selected_core = set(policy.accounting_core_rule_ids)
        if selected_core != core:
            raise ValueError(
                f"financial standard {policy.standard_id} cannot remove core identities"
            )
        if set(policy.noncore_rule_ids) - set(existing):
            raise ValueError(f"financial standard {policy.standard_id} references unknown rules")
        if policy.eligible_for_certified_a_share and policy.admission != (
            "INDEPENDENT_ISSUER_EVIDENCE"
        ):
            raise ValueError("A-share production standard needs independent adoption review")
        if not policy.eligible_for_certified_a_share and policy.admission != (
            "UNSUPPORTED_A_SHARE_FORMAL_SOURCE"
        ):
            raise ValueError("unsupported A-share standard cannot have production admission")
    return registry
