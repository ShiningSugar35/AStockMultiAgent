from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from astock.candidates.config import load_candidate_scan_config
from astock.financial_sources.config import load_financial_field_mappings
from astock.market_data.reference_config import load_market_reference_config
from astock.paper_trading.operation import load_paper_trading_rules
from astock.providers.config import load_provider_registry
from astock.schemas import Market
from astock.schemas.adaptive import AdaptiveSkillStability

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_candidate_v2_policy_changes_thresholds_without_python_change(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "candidate_scan.yaml").read_text(encoding="utf-8")
    )
    payload["schema_version"] = "candidate-scan-v2"
    payload["minimum_trading_days"] = 30
    payload["minimum_median_turnover_cny"] = 30_000_000
    path = tmp_path / "candidate-v2.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    policy = load_candidate_scan_config(path)

    assert policy.rules_version == "candidate-scan-v2"
    assert policy.minimum_trading_days == 30
    assert int(policy.minimum_median_turnover_cny) == 30_000_000


def test_market_reference_route_order_is_configuration_not_source_code(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "market_reference.yaml").read_text(encoding="utf-8")
    )
    identity = payload["routes"]["instrument.identity"]
    identity[1], identity[2] = identity[2], identity[1]
    path = tmp_path / "market-reference-v2.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")

    config = load_market_reference_config(path, registry)

    assert [item.provider_id for item in config.route("instrument.identity")] == [
        item["provider_id"] for item in identity
    ]


def test_paper_effective_date_is_taken_from_rulebook(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "paper_trading_rules.yaml").read_text(encoding="utf-8")
    )
    payload["effective_from"] = "2026-08-15"
    path = tmp_path / "paper-rules.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    rules = load_paper_trading_rules(path)

    assert rules.effective_from == date(2026, 8, 15)
    with pytest.raises(Exception, match="effective-dated price-limit rule"):
        rules.price_limit_bps(
            market=Market.XSHG,
            board="MAIN",
            risk_status="NORMAL",
            trade_date=date(2026, 8, 14),
        )


def test_adaptive_stability_threshold_fields_are_policy_supplied_integers() -> None:
    fields = AdaptiveSkillStability.model_fields

    assert fields["required_independent_decision_count"].annotation is int
    assert fields["required_walk_forward_fold_count"].annotation is int
    assert fields["required_market_state_count"].annotation is int


def test_financial_field_mapping_accepts_third_provider_without_loader_branch(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "financial_field_mappings.yaml").read_text(encoding="utf-8")
    )
    payload["fields"]["TOTAL_ASSETS"]["provider_fields"]["third-financial"] = "assets_total_v3"
    path = tmp_path / "financial-mappings-v2.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    mappings = load_financial_field_mappings(path)
    total_assets = next(item for item in mappings if item.field_code.value == "TOTAL_ASSETS")

    assert total_assets.provider_field("third-financial") == "assets_total_v3"
