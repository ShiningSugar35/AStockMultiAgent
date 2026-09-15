"""Independent regression cases for the WP-25 continuation; no production data."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.investment_expectations import resolve_investment_expectation
from astock.investor_orchestration.models import InvestorRequestEnvelope, RequestIntent
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash

NOW = datetime(2026, 9, 15, 1, tzinfo=UTC)
DEFAULTS = {"capital_rmb": 100000, "target_annual_return": 1}


def _request(text: str, identity: str, *, account: str | None = None,
             at: datetime = NOW) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope.model_validate({
        "request_id": identity, "question_time": at, "raw_text": text,
        "user_timezone": "Asia/Shanghai", "normalized_intent": "RESEARCH",
        "side_effect": "READ", "idempotency_key": identity, "account_id": account,
    })


def _register_request(state: StateStore, objects: ObjectStore,
                      request: InvestorRequestEnvelope) -> None:
    ref = objects.put_json(request.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"InvestorRequestEnvelope:{content_hash(request.request_id)}",
        artifact_type="InvestorRequestEnvelope", schema_version="investor-request-envelope-v1",
        object_hash=ref.sha256, input_hashes=[],
    )


@pytest.fixture
def stores(tmp_path: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    return state, ObjectStore(tmp_path / "objects" / "sha256")


def _resolve(text: str = "推荐投资组合", *, state: StateStore | None = None,
             objects: ObjectStore | None = None, account: str | None = None,
             supplied: dict[str, Any] | None = None):
    return resolve_investment_expectation(
        raw_text=text, supplied_assumptions=supplied, defaults=DEFAULTS,
        state=state, objects=objects, request_id="current", account_id=account, as_of=NOW,
    )


@pytest.mark.parametrize("text", ["推荐投资组合", "推荐公司", "推荐股票", "推荐标的"])
def test_every_requested_recommendation_phrase_routes_to_full_research(text: str) -> None:
    assert _request(text, "route").normalized_intent == RequestIntent.FULL_RESEARCH_RECOMMENDATION


@pytest.mark.parametrize("text,capital,target", [
    ("本金20万，年化30%，推荐股票", "200000", "0.3"),
    ("本金：200,000元，年化目标：25％，推荐公司", "200000", "0.25"),
    ("我有10w人民币，年化目标100%，推荐投资组合", "100000", "1"),
    ("预算5万元，目标年化0%，推荐标的", "50000", "0"),
    ("不是本金20万，本金30万，年化目标40%，推荐股票", "300000", "0.4"),
])
def test_explicit_current_values_are_not_silently_replaced(
    text: str, capital: str, target: str,
) -> None:
    result = _resolve(text)
    assert result.capital_rmb == Decimal(capital)
    assert result.target_annual_return == Decimal(target)


@pytest.mark.parametrize(
    "text", ["本金0元，推荐股票", "本金-20万元，推荐股票", "年化目标-10%，推荐股票"]
)
def test_explicit_invalid_value_is_not_a_fictitious_default_bankroll(text: str) -> None:
    with pytest.raises(ValueError):
        _resolve(text)


def test_prior_request_is_recoverable_even_without_a_finished_recommendation(stores) -> None:
    state, objects = stores
    _register_request(state, objects, _request(
        "本金18万元，年化目标35%，推荐股票", "prior", at=NOW-timedelta(days=1)))
    result = _resolve(state=state, objects=objects)
    assert result.capital_rmb == Decimal("180000")
    assert result.target_annual_return == Decimal("0.35")
    assert result.capital_source.value == "RECENT_HISTORY"


def test_recent_omission_does_not_erase_an_older_explicit_goal(stores) -> None:
    state, objects = stores
    _register_request(state, objects, _request(
        "本金18万元，年化目标35%，推荐股票", "prior", at=NOW-timedelta(days=2)))
    _register_request(state, objects, _request(
        "推荐公司", "newer", at=NOW-timedelta(days=1)))
    result = _resolve("本金30万元，推荐股票", state=state, objects=objects)
    assert result.capital_rmb == Decimal("300000")
    assert result.target_annual_return == Decimal("0.35")


def test_account_history_does_not_cross_account_boundaries(stores) -> None:
    state, objects = stores
    _register_request(state, objects, _request(
        "本金18万元，年化目标35%，推荐股票", "a", account="account-a", at=NOW-timedelta(days=2)))
    _register_request(state, objects, _request(
        "本金90万元，年化目标90%，推荐股票", "b", account="account-b", at=NOW-timedelta(days=1)))
    result = _resolve(state=state, objects=objects, account="account-a")
    assert (result.capital_rmb, result.target_annual_return) == (Decimal("180000"), Decimal("0.35"))


def test_later_history_is_not_used_for_an_earlier_request(stores) -> None:
    state, objects = stores
    _register_request(state, objects, _request(
        "本金90万元，年化目标90%，推荐股票", "future", at=NOW+timedelta(days=1)))
    result = _resolve(state=state, objects=objects)
    assert (result.capital_rmb, result.target_annual_return) == (Decimal("100000"), Decimal("1"))


def test_missing_history_db_falls_back_without_creating_an_account(tmp_path: Path) -> None:
    path = tmp_path / "never-created.sqlite"
    result = _resolve(state=StateStore(path), objects=ObjectStore(tmp_path / "objects"))
    assert result.capital_rmb == Decimal("100000")
    assert result.target_annual_return == Decimal("1")
    assert not path.exists(), "read-only preference lookup must not create a database"


def test_holding_market_value_is_not_treated_as_investable_principal() -> None:
    result = _resolve("当前持仓市值26万元，年化目标20%，推荐股票")
    assert result.capital_rmb == Decimal("100000")
    assert result.capital_source.value == "DEFAULT"
    assert result.target_annual_return == Decimal("0.2")


def test_same_request_does_not_drift_when_another_request_records_a_new_goal(stores) -> None:
    state, objects = stores
    service = FullResearchRecommendationService(
        InvestorOrchestrationStore(state.path), objects=objects
    )
    request = _request("推荐股票", "frozen")
    first = service.request_contract(request)
    _register_request(state, objects, _request(
        "本金80万元，年化目标20%，推荐公司", "new-history", at=NOW-timedelta(hours=1)))
    second = service.request_contract(request)
    assert first.portfolio_assumptions == second.portfolio_assumptions


def test_explicit_values_do_not_require_history_io() -> None:
    class NoHistory:
        def recent_artifact_records(self, *args, **kwargs):
            raise AssertionError("unnecessary history I/O")
    # Deliberate no-I/O sentinels; the assertion above must remain executable.
    result = _resolve(
        "本金20万元，年化目标20%，推荐公司",
        state=cast(StateStore, NoHistory()), objects=cast(ObjectStore, object()),
    )
    assert result.capital_rmb == Decimal("200000")
    assert result.target_annual_return == Decimal("0.2")


@pytest.mark.parametrize("months", [Decimal("6"), Decimal("12"), Decimal("24")])
def test_compounded_goal_uses_time_not_linear_annual_division(months: Decimal) -> None:
    from astock.investor_orchestration.investment_objectives import compounded_target
    actual = compounded_target(Decimal("1"), months)
    assert abs((1 + actual) ** (Decimal("12") / months) - 2) < Decimal("0.0000000001")


def _objective_fixture(horizon: Decimal | None):
    from astock.schemas.full_research import (
        PortfolioPositionPlan,
        RecommendationValuationSnapshot,
        ValuationScenario,
    )
    assumptions = FullResearchRecommendationService().request_contract(
        _request("本金2万元，年化目标20%，推荐股票", "goal-case")
    ).portfolio_assumptions
    position = PortfolioPositionPlan(
        instrument_id="600001.XSHG", industry_id="TEST", target_weight=Decimal("0.501"),
        target_amount=Decimal("10020"), reference_price=Decimal("10"), target_shares=1000,
        lot_size=100, estimated_cost=Decimal("12"), estimated_slippage=Decimal("8"),
    )
    valuation = RecommendationValuationSnapshot(
        instrument_id=position.instrument_id, method_family="TEST", methods=("DCF", "PE"),
        current_price=Decimal("10"), return_horizon_months=horizon,
        scenarios=tuple(ValuationScenario(scenario=cast(Literal["BEAR", "BASE", "BULL"], n),
            per_share_value=Decimal(v),
            probability=Decimal(p), expected_return=Decimal(r))
            for n, v, p, r in (("BEAR", "8", "0.25", "-0.2"),
                ("BASE", "12", "0.5", "0.2"), ("BULL", "18", "0.25", "0.8"))),
        expected_return_mean=Decimal("0.25"), expected_return_downside=Decimal("-0.2"),
        margin_of_safety=Decimal("2")/12, source_artifact_ids=("test-source",),
        source_object_hashes=("a"*64,),
    )
    return assumptions, position, valuation


def test_scenario_profit_uses_share_notional_and_deducts_entry_cost_once() -> None:
    from astock.investor_orchestration.investment_objectives import objective_projection
    a, p, v = _objective_fixture(Decimal("12"))
    objective = objective_projection(a, (p,), {p.instrument_id: v})
    assert objective["expected_research_profit"] == Decimal("2480")
    assert objective["modeled_downside_loss"] == Decimal("2020")
    assert objective["annual_profit_target"] == Decimal("4000")
    assert objective["expected_research_return"] == Decimal("0.124")
    assert objective["return_objective_gap"] == Decimal("0.076")


def test_unknown_valuation_horizon_does_not_assert_target_achievability() -> None:
    from astock.investor_orchestration.investment_objectives import objective_projection
    a, p, v = _objective_fixture(None)
    objective = objective_projection(a, (p,), {p.instrument_id: v})
    assert objective["objective_status"] == "HORIZON_NOT_COMPARABLE"
    assert objective["return_objective_gap"] is None


def test_unrelated_horizon_does_not_get_compared_to_user_goal() -> None:
    from astock.investor_orchestration.investment_objectives import objective_projection
    a, p, v = _objective_fixture(Decimal("24"))
    objective = objective_projection(a, (p,), {p.instrument_id: v})
    assert objective["objective_status"] == "HORIZON_NOT_COMPARABLE"
    assert objective["return_objective_gap"] is None


def test_goal_price_includes_cost_and_is_not_a_guaranteed_valuation() -> None:
    from astock.investor_orchestration.investment_objectives import goal_entry_ceiling
    _, _, v = _objective_fixture(Decimal("12"))
    cap = goal_entry_ceiling(Decimal("0.2"), v, Decimal("0.002"))
    assert cap is not None
    assert abs(cap - Decimal("12") / Decimal("1.2024")) < Decimal("0.0000001")
    assert v.scenarios[1].per_share_value == Decimal("12")
    assert v.expected_return_mean == Decimal("0.25")


@pytest.mark.parametrize("text", ["本金10万美元，推荐股票", "本金20万港元，推荐公司"])
def test_foreign_currency_is_not_silently_relabelled_rmb(text: str) -> None:
    with pytest.raises(ValueError):
        _resolve(text)



def test_child_decision_reuses_original_request_objective(stores) -> None:
    state, objects = stores
    service = FullResearchRecommendationService(
        InvestorOrchestrationStore(state.path), objects=objects
    )
    original = _request("推荐股票", "parent")
    first = service.request_contract(original)
    _register_request(state, objects, _request(
        "本金80万元，年化目标20%，推荐公司", "late-recorded-prior", at=NOW-timedelta(hours=1)))
    child = original.model_copy(update={
        "request_id": "child", "parent_request_id": "parent", "idempotency_key": "child",
        "decision_time": NOW+timedelta(minutes=5),
        "decision_freeze_artifact_id": "DecisionFreeze:synthetic",
    })
    second = service.request_contract(child)
    assert second.portfolio_assumptions.capital_rmb == first.portfolio_assumptions.capital_rmb
    assert (second.portfolio_assumptions.target_annual_return
            == first.portfolio_assumptions.target_annual_return)


def test_same_id_cannot_silently_reuse_a_changed_structured_objective(stores) -> None:
    state, objects = stores
    service = FullResearchRecommendationService(
        InvestorOrchestrationStore(state.path), objects=objects
    )
    original = _request("推荐股票", "same-id").model_copy(update={"metadata": {
        "portfolio_assumptions": {"capital_rmb": 200000, "target_annual_return": 0.2}
    }})
    service.request_contract(original)
    changed = original.model_copy(update={"metadata": {
        "portfolio_assumptions": {"capital_rmb": 800000, "target_annual_return": 0.8}
    }})
    with pytest.raises(ValueError):
        service.request_contract(changed)
