"""Standards eligibility remains versioned, additive and never weakens identities."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from astock.financial_integrity.config import load_financial_rule_registry
from astock.financial_integrity.standard_policies import load_financial_standard_policies

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs" / "financial_presentation_standards.yaml"
RULES = load_financial_rule_registry(ROOT / "configs" / "financial_rules.yaml")


def test_active_production_standards_preserve_all_core_accounting_rules() -> None:
    registry = load_financial_standard_policies(POLICY, RULES)
    assert registry.production_enabled
    by_id = {p.standard_id: p for p in registry.profiles}
    assert {"PRC_ASBE_CAS30_2014", "PRC_ASBE_CAS30_2026", "IFRS_18"} <= set(by_id)
    assert by_id["PRC_ASBE_CAS30_2026"].eligible_for_certified_a_share
    assert not by_id["IFRS_18"].eligible_for_certified_a_share
    for policy in registry.profiles:
        assert set(policy.accounting_core_rule_ids) == {
            "balance_sheet_identity", "cash_flow_identity",
        }


def test_standard_policy_rejects_accounting_identity_exemption(tmp_path: Path) -> None:
    payload = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    fake = copy.deepcopy(payload)
    fake["profiles"][1]["accounting_core_rule_ids"] = [
        "balance_sheet_identity", "cfo_net_profit_ratio",
    ]
    p = tmp_path / "policies.yaml"
    p.write_text(yaml.safe_dump(fake, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot remove core"):
        load_financial_standard_policies(p, RULES)


def test_standard_policy_rejects_unsupported_formal_ifrs_promotions(tmp_path: Path) -> None:
    payload = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    fake = copy.deepcopy(payload)
    fake["profiles"][-1]["eligible_for_certified_a_share"] = True
    p = tmp_path / "policies.yaml"
    p.write_text(yaml.safe_dump(fake, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="independent adoption review"):
        load_financial_standard_policies(p, RULES)


def test_standard_policy_rejects_unknown_accounting_rule(tmp_path: Path) -> None:
    payload = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    payload["profiles"][1]["noncore_rule_ids"] = ["fabricated_profit_rule"]
    p = tmp_path / "policies.yaml"
    p.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown rules"):
        load_financial_standard_policies(p, RULES)
