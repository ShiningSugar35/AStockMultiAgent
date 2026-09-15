from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.investment_expectations import resolve_investment_expectation
from astock.investor_orchestration.investment_objectives import objective_projection
from astock.investor_orchestration.models import InvestorRequestEnvelope
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.schemas.full_research import ModelPortfolioAssumptions, PortfolioPositionPlan
from tests.unit.test_full_research_recommendation import _valuation

DEFAULTS = {"capital_rmb": 100000, "target_annual_return": 1}
NOW = datetime(2026, 9, 15, 2, tzinfo=UTC)


def request(
    text: str, identity: str, *, account: str | None = None, at: datetime = NOW
) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope.model_validate({
        "request_id": identity, "raw_text": text, "question_time": at,
        "user_timezone": "Asia/Shanghai", "normalized_intent": "RESEARCH",
        "side_effect": "READ", "idempotency_key": identity, "account_id": account,
    })


def resolve(text: str = "推荐股票", **kwargs: Any):
    return resolve_investment_expectation(raw_text=text, supplied_assumptions=None,
        defaults=DEFAULTS, **{"state": None, "objects": None, **kwargs})


@pytest.mark.parametrize(("text", "capital", "annual"), [
    ("本金20万，年化30%，推荐组合", "200000", "0.3"),
    ("资金：250,000元，目标年化15％", "250000", "0.15"),
    ("我有10w人民币，年化目标0%", "100000", "0"),
    ("预算120k，年化收益率20%", "120000", "0.2"),
    ("此前本金20万，本次本金15万，年化30%", "150000", "0.3"),
    ("不是本金20万，本金12万，年化30%", "120000", "0.3"),
    ("去年年化80%，本次年化15%，本金10万", "100000", "0.15"),
])
def test_common_current_values(text: str, capital: str, annual: str) -> None:
    value = resolve(text)
    assert value.capital_rmb == Decimal(capital)
    assert value.target_annual_return == Decimal(annual)


@pytest.mark.parametrize("text", ["本金0元推荐股票", "本金-1万元推荐股票", "年化-20%推荐股票"])
def test_explicit_invalid_values_do_not_silently_create_default_money(text: str) -> None:
    with pytest.raises(ValueError, match="核对"):
        resolve(text)


def test_missing_history_db_does_not_create_file_or_block(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "not-created.sqlite")
    value = resolve(state=state, objects=ObjectStore(tmp_path / "objects"))
    assert value.capital_rmb == 100000 and value.target_annual_return == 1
    assert value.notes
    assert not state.path.exists()


def test_account_isolation_future_history_and_unfinished_request_recovery(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    facade = InvestorOrchestrationService(store)
    for envelope in (
        request(
            "本金18万，年化25%，推荐公司", "mine",
            account="actual-a", at=NOW-timedelta(hours=2),
        ),
        request(
            "本金99万，年化300%，推荐公司", "other",
            account="paper-b", at=NOW-timedelta(hours=1),
        ),
        request(
            "本金33万，年化80%，推荐公司", "future",
            account="actual-a", at=NOW+timedelta(hours=1),
        ),
    ):
        facade._bind_original_request(envelope)
    service = FullResearchRecommendationService(store)
    contract = service.request_contract(request("推荐投资组合", "new", account="actual-a"))
    assert contract.portfolio_assumptions.capital_rmb == 180000
    assert contract.portfolio_assumptions.target_annual_return == Decimal("0.25")
    assert contract.portfolio_assumptions.expectation_history_references


def test_expectation_is_frozen_before_workers_and_retries_do_not_drift(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    envelope = request("本金12万，年化20%，推荐股票", "original")
    service = FullResearchRecommendationService(store)
    called = []

    def preflight_build(_request):
        context = service.request_contract(_request)
        assert context.portfolio_assumptions.target_annual_return == Decimal("0.2")
        called.append(True)
        return None

    facade = InvestorOrchestrationService(store,
        preflight_service=cast(Any, SimpleNamespace(build=preflight_build)),
        planner=cast(Any, SimpleNamespace(plan=lambda *a, **k: None)))
    facade.prepare(envelope)
    assert called
    initial = service.request_contract(envelope)
    facade._bind_original_request(
        request("本金50万，年化80%，推荐公司", "later", at=NOW+timedelta(hours=1))
    )
    assert service.request_contract(envelope).model_dump() == initial.model_dump()


def test_registry_scan_is_bounded_without_full_table_search(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    state.register_artifacts([(f"old-{i}", "wanted", "1", "0"*64, []) for i in range(2)]
        + [(f"new-{i}", "unrelated", "1", "0"*64, []) for i in range(100)])
    assert not state.recent_artifact_records("wanted", scan_limit=32)
    assert len(state.recent_artifact_records("wanted", scan_limit=128)) == 2


def test_false_receipt_does_not_become_user_preferences(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    ref = objects.put_json({"request_contract": {"portfolio_assumptions": {
        "capital_rmb": 9999999, "capital_source": "CURRENT_USER"}}})
    state.register_artifact(artifact_id="spoof", artifact_type="RecommendationResearchReceipt",
        schema_version="recommendation-research-receipt-v1",
        object_hash=ref.sha256, input_hashes=[],
    )
    assert resolve(state=state, objects=objects).capital_rmb == 100000


def test_goal_projection_uses_notional_net_of_costs_and_does_not_compare_unknown_periods() -> None:
    assumptions = ModelPortfolioAssumptions(source="MODEL_PORTFOLIO", capital_rmb=100000,
        target_annual_return=1, horizon_min_months=3, horizon_max_months=12,
        target_position_min=1, target_position_max=5)
    valuation = _valuation("600001.XSHG")
    position = PortfolioPositionPlan(instrument_id=valuation.instrument_id, industry_id="industry",
        target_weight=Decimal("0.1002"), target_amount=10020, reference_price=10,
        target_shares=1000, lot_size=100, estimated_cost=12, estimated_slippage=8)
    result = objective_projection(assumptions, (position,), {valuation.instrument_id: valuation})
    assert result["annual_profit_target"] == 100000
    assert result["target_horizon_months"] is None
    assert result["expected_research_profit"] == 10000 * valuation.expected_return_mean - 20
    assert result["objective_status"] == "HORIZON_NOT_COMPARABLE"
    assert result["return_objective_gap"] is None
    known = valuation.model_copy(update={"return_horizon_months": Decimal("12")})
    result = objective_projection(assumptions, (position,), {known.instrument_id: known})
    assert result["objective_status"] == "BELOW_HORIZON_TARGET"
    assert result["return_objective_gap"] == 1-result["expected_research_return"]


def test_high_goal_is_a_separate_condition_and_does_not_change_execution_authority() -> None:
    from tests.unit.test_full_research_recommendation import _candidate, _factor, _sources
    service = FullResearchRecommendationService()
    contract = service.request_contract(request("本金10万，年化100%，推荐股票", "goal-entry"))
    valuation = _valuation("600001.XSHG").model_copy(
        update={"return_horizon_months": Decimal("12")}
    )
    candidates = (_candidate(valuation.instrument_id, _factor(valuation.instrument_id)),)
    portfolio = service.build_portfolio(contract, candidates, (valuation,))
    source = next(s for s in _sources(service) if s.family.value == "MARKET_PRICE")
    executions = service.execution_plans(
        source.available_to_system_at, portfolio, (valuation,),
        {valuation.instrument_id: source},
    )
    assert executions[0].goal_entry_price_ceiling is not None
    assert executions[0].maximum_acceptable_price is not None
    assert executions[0].goal_entry_price_ceiling < executions[0].maximum_acceptable_price
    assert executions[0].instant_quantity_allowed
    assert executions[0].initial_shares is not None
    assert max(p.target_weight for p in portfolio.positions) <= Decimal("0.25")
