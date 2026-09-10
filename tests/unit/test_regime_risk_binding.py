"""Regime integrity and canonical user-cap regressions in an isolated database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from astock.investor_orchestration.models import MarketRegimeFeatureSnapshot, RegimeState
from astock.investor_orchestration.regime import MarketRegimeService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.schemas.portfolio_decision import PortfolioIntentProfile

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> MarketRegimeService:
    store = InvestorOrchestrationStore(tmp_path_factory.mktemp("regime-risk") / "state.sqlite")
    store.initialize()
    return MarketRegimeService(store, ROOT / "configs/market_regime_v2.yaml")


def features(**updates: Any) -> MarketRegimeFeatureSnapshot:
    values: dict[str, Any] = {
        "feature_snapshot_id": "risk-test-features",
        "as_of": datetime.now(UTC),
        "trend_score": 0.8,
        "breadth_score": 0.8,
        "tail_risk_score": 0.6,
        "liquidity_score": 0.5,
        "valuation_fragility_score": 0.0,
        "earnings_diffusion_score": 0.2,
        "macro_credit_score": 0.1,
        "family_coverage": dict.fromkeys(
            ("trend", "breadth", "tail", "liquidity", "valuation", "earnings", "macro"), 1.0
        ),
    }
    values.update(updates)
    return MarketRegimeFeatureSnapshot(**values)


def test_canonical_portfolio_intent_fields_are_not_ignored(service: MarketRegimeService) -> None:
    snapshot = service.infer(features(), persist=False)
    profile = PortfolioIntentProfile(
        portfolio_id="test-portfolio",
        as_of=snapshot.as_of,
        max_total_exposure=0.35,
        max_single_position=0.04,
        minimum_cash_weight=0.5,
        maximum_turnover_weight=0.08,
    )
    overlay = service.overlay(snapshot, profile, persist=False)
    assert overlay.user_limit_binding["max_total_equity_weight"] == 0.35
    assert overlay.user_limit_binding["max_single_name_weight"] == 0.04
    assert overlay.user_limit_binding["max_turnover"] == 0.08
    assert float(overlay.user_limit_binding["effective_total_equity_cap"]) <= 0.35
    assert float(overlay.user_limit_binding["effective_single_name_cap"]) <= 0.04


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), "not-a-number", True])
def test_invalid_user_limit_cannot_be_silently_bound(
    service: MarketRegimeService, value: Any
) -> None:
    snapshot = service.infer(features(), persist=False)
    with pytest.raises(ValueError, match="limit|finite|numeric|range"):
        service.overlay(snapshot, {"max_total_equity_weight": value}, persist=False)


def test_conflicting_legacy_and_canonical_limit_names_are_rejected(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(features(), persist=False)
    with pytest.raises(ValueError, match="conflict"):
        service.overlay(
            snapshot, {"max_total_exposure": 0.2, "max_total_equity_weight": 0.8}, persist=False
        )


def test_zero_risk_profile_is_preserved_and_never_replaced_by_default(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(features(), persist=False)
    overlay = service.overlay(
        snapshot,
        {
            "max_total_exposure": 0.0,
            "max_single_position": 0.0,
            "minimum_cash_weight": 1.0,
        },
        persist=False,
    )
    assert overlay.user_limit_binding["effective_total_equity_cap"] == 0
    assert overlay.user_limit_binding["effective_single_name_cap"] == 0


def test_missing_critical_value_cannot_be_certified_by_claimed_full_coverage(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(features(breadth_score=None), persist=False)
    assert snapshot.selected_state is RegimeState.UNCLASSIFIED
    assert snapshot.coverage < 1.0


def test_caller_model_copy_cannot_bypass_normalized_features(service: MarketRegimeService) -> None:
    corrupted = features().model_copy(update={"trend_score": float("nan")})
    with pytest.raises(ValueError):
        service.infer(corrupted, persist=False)


def test_future_feature_snapshot_is_rejected(service: MarketRegimeService) -> None:
    with pytest.raises(ValueError, match="future"):
        service.infer(features(as_of=datetime.now(UTC) + timedelta(days=1)), persist=False)


def test_partial_family_dictionary_does_not_claim_complete_coverage(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(features(family_coverage={"trend": 1.0}), persist=False)
    assert snapshot.coverage < 0.2
    assert snapshot.selected_state is RegimeState.UNCLASSIFIED


def test_missing_auxiliary_scores_do_not_create_zero_valued_bull_evidence(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(
        features(earnings_diffusion_score=None, valuation_fragility_score=None), persist=False
    )
    assert snapshot.baseline_state is not RegimeState.HEALTHY_BULL


def test_invalid_nested_threshold_is_rejected_at_configuration_load(
    service: MarketRegimeService,
    tmp_path: Path,
) -> None:
    config = yaml.safe_load((ROOT / "configs/market_regime_v2.yaml").read_text(encoding="utf-8"))
    config["panic"]["tail_risk_max"] = float("nan")
    path = tmp_path / "bad-regime.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="finite|threshold"):
        MarketRegimeService(service.store, path)


def test_overlay_rejects_a_foreign_policy_snapshot(service: MarketRegimeService) -> None:
    snapshot = service.infer(features(), persist=False)
    with pytest.raises(ValueError, match="policy"):
        service.overlay(
            snapshot.model_copy(update={"policy_version": "unknown"}), None, persist=False
        )


def test_persisted_overlay_cannot_rebind_a_snapshot_id_to_another_state(
    service: MarketRegimeService,
) -> None:
    snapshot = service.infer(features(), persist=True)
    alternate = (
        RegimeState.PANIC
        if snapshot.selected_state is not RegimeState.PANIC
        else RegimeState.HEALTHY_BULL
    )
    changed = snapshot.model_copy(update={"selected_state": alternate})
    with pytest.raises(ValueError, match="exact registered"):
        service.overlay(changed, None, persist=True)
    overlay = service.overlay(snapshot, None, persist=True)
    assert overlay.regime_snapshot_id == snapshot.snapshot_id


def test_runtime_policy_mutation_is_not_a_silent_risk_limit_upgrade(
    service: MarketRegimeService,
) -> None:
    isolated = MarketRegimeService(service.store, ROOT / "configs/market_regime_v2.yaml")
    isolated.config["state_overlay"]["HEALTHY_BULL"]["total_risk_multiplier"] = 100
    with pytest.raises(ValueError, match="policy changed"):
        isolated.infer(features(), persist=False)


def test_risk_caps_are_monotone_for_frozen_profile(service: MarketRegimeService) -> None:
    snapshot = service.infer(features(), persist=False)
    profile = {"max_total_exposure": 0.6, "max_single_position": 0.1, "minimum_cash_weight": 0.3}
    states = [
        RegimeState.PANIC,
        RegimeState.TREND_BEAR,
        RegimeState.RISK_OFF_RANGE,
        RegimeState.NEUTRAL_RANGE,
        RegimeState.HEALTHY_BULL,
    ]
    caps = [
        service.overlay(
            snapshot.model_copy(update={"selected_state": state}), profile, persist=False
        ).user_limit_binding
        for state in states
    ]
    for metric, maximum in (
        ("effective_total_equity_cap", 0.6),
        ("effective_single_name_cap", 0.1),
    ):
        values = [float(item[metric]) for item in caps]
        assert values == sorted(values)
        assert max(values) <= maximum
