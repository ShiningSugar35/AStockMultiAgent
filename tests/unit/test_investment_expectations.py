from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.investment_expectations import resolve_investment_expectation
from astock.investor_orchestration.utils import content_hash
from astock.schemas.full_research import (
    FullResearchRequestContract,
    InvestmentExpectationValueSource,
    ModelPortfolioAssumptions,
    PortfolioConstructionSnapshot,
)

DEFAULTS = {
    "capital_rmb": 100000,
    "target_annual_return": 1.0,
}


def _stores(root: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(root / "state.sqlite")
    state.migrate()
    return state, ObjectStore(root / "objects" / "sha256")


def _record_receipt(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str,
    assumptions: dict[str, object],
) -> None:
    current = datetime.now(UTC)
    model = ModelPortfolioAssumptions.model_validate({
        "source": "MODEL_PORTFOLIO", "capital_rmb": "100000",
        "horizon_min_months": 3, "horizon_max_months": 12,
        "target_position_min": 5, "target_position_max": 8,
        **assumptions,
    })
    contract = FullResearchRequestContract(
        request_id=artifact_id, as_of_timestamp=current,
        raw_text_hash=content_hash(artifact_id), portfolio_assumptions=model,
    )
    ref = objects.put_json(contract.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"FullResearchRequestContract:{content_hash(artifact_id)}",
        artifact_type="FullResearchRequestContract",
        schema_version=contract.schema_version,
        object_hash=ref.sha256, input_hashes=[],
    )


def test_expectation_defaults_to_100k_and_100_percent_without_history() -> None:
    resolved = resolve_investment_expectation(
        raw_text="推荐明天可以买的投资组合",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=None,
        objects=None,
    )

    assert resolved.capital_rmb == Decimal("100000")
    assert resolved.target_annual_return == Decimal("1")
    assert resolved.capital_source is InvestmentExpectationValueSource.DEFAULT
    assert resolved.target_annual_return_source is InvestmentExpectationValueSource.DEFAULT


def test_expectation_reads_explicit_chinese_principal_and_annual_target() -> None:
    resolved = resolve_investment_expectation(
        raw_text="本金20万，年化目标30%，推荐5只股票",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=None,
        objects=None,
    )

    assert resolved.capital_rmb == Decimal("200000")
    assert resolved.target_annual_return == Decimal("0.3")
    assert resolved.capital_source is InvestmentExpectationValueSource.CURRENT_USER
    assert resolved.target_annual_return_source is InvestmentExpectationValueSource.CURRENT_USER


def test_expectation_supports_10w_rmb_and_structured_percent_target() -> None:
    resolved = resolve_investment_expectation(
        raw_text="我有10w人民币，推荐股票",
        supplied_assumptions={"target_annual_return_pct": 45},
        defaults=DEFAULTS,
        state=None,
        objects=None,
    )

    assert resolved.capital_rmb == Decimal("100000")
    assert resolved.target_annual_return == Decimal("0.45")


def test_expectation_inherits_missing_fields_independently_from_recent_history(
    tmp_path: Path,
) -> None:
    state, objects = _stores(tmp_path)
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:01-old",
        assumptions={
            "capital_rmb": "150000",
            "target_annual_return": "0.4",
            "capital_source": "CURRENT_USER",
            "target_annual_return_source": "CURRENT_USER",
        },
    )
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:02-new",
        assumptions={
            "capital_rmb": "220000",
            "target_annual_return": "1",
            "capital_source": "CURRENT_USER",
            "target_annual_return_source": "DEFAULT",
        },
    )

    resolved = resolve_investment_expectation(
        raw_text="年化目标50%，推荐投资组合",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=state,
        objects=objects,
    )

    assert resolved.capital_rmb == Decimal("220000")
    assert resolved.target_annual_return == Decimal("0.5")
    assert resolved.capital_source is InvestmentExpectationValueSource.RECENT_HISTORY
    assert resolved.target_annual_return_source is InvestmentExpectationValueSource.CURRENT_USER


def test_newer_model_default_cannot_hide_older_user_preference(
    tmp_path: Path,
) -> None:
    state, objects = _stores(tmp_path)
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:01-old",
        assumptions={
            "capital_rmb": "150000",
            "target_annual_return": "0.35",
            "capital_source": "CURRENT_USER",
            "target_annual_return_source": "CURRENT_USER",
        },
    )
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:02-new",
        assumptions={
            "capital_rmb": "240000",
            "target_annual_return": "1",
            "capital_source": "CURRENT_USER",
            "target_annual_return_source": "DEFAULT",
        },
    )

    resolved = resolve_investment_expectation(
        raw_text="推荐标的",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=state,
        objects=objects,
    )

    assert resolved.capital_rmb == Decimal("240000")
    assert resolved.target_annual_return == Decimal("0.35")
    assert resolved.capital_source is InvestmentExpectationValueSource.RECENT_HISTORY
    assert resolved.target_annual_return_source is InvestmentExpectationValueSource.RECENT_HISTORY


def test_old_receipt_only_counts_capital_when_user_constraints_recorded_it(tmp_path: Path) -> None:
    state, objects = _stores(tmp_path)
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:legacy-default",
        assumptions={
            "source": "USER",
            "capital_rmb": "888888",
            "user_constraints": {"risk_profile": "HIGH"},
        },
    )

    resolved = resolve_investment_expectation(
        raw_text="推荐公司",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=state,
        objects=objects,
    )

    assert resolved.capital_rmb == Decimal("100000")
    assert resolved.capital_source is InvestmentExpectationValueSource.DEFAULT


def test_corrupt_recent_history_falls_through_without_blocking(tmp_path: Path) -> None:
    state, objects = _stores(tmp_path)
    _record_receipt(
        state,
        objects,
        artifact_id="RecommendationResearchReceipt:01-good",
        assumptions={
            "capital_rmb": "180000",
            "target_annual_return": "0.25",
            "capital_source": "CURRENT_USER",
            "target_annual_return_source": "CURRENT_USER",
        },
    )
    state.register_artifact(
        artifact_id="RecommendationResearchReceipt:99-corrupt",
        artifact_type="RecommendationResearchReceipt",
        schema_version="recommendation-research-receipt-v1",
        object_hash="0" * 64,
        input_hashes=[],
    )

    resolved = resolve_investment_expectation(
        raw_text="推荐股票",
        supplied_assumptions=None,
        defaults=DEFAULTS,
        state=state,
        objects=objects,
    )

    assert resolved.capital_rmb == Decimal("180000")
    assert resolved.target_annual_return == Decimal("0.25")


def test_recent_artifact_records_is_bounded(tmp_path: Path) -> None:
    state, objects = _stores(tmp_path)
    for index in range(3):
        _record_receipt(
            state,
            objects,
            artifact_id=f"RecommendationResearchReceipt:{index}",
            assumptions={"capital_rmb": "100000"},
        )

    assert len(state.recent_artifact_records("FullResearchRequestContract", limit=2)) == 2
    with pytest.raises(ValueError, match="between 1 and 64"):
        state.recent_artifact_records("RecommendationResearchReceipt", limit=65)


def test_new_optional_expectation_fields_do_not_appear_in_legacy_schema_dumps() -> None:
    assumptions = ModelPortfolioAssumptions.model_validate(
        {
            "source": "MODEL_PORTFOLIO",
            "capital_rmb": "100000",
            "risk_profile": "MEDIUM",
            "horizon_min_months": 3,
            "horizon_max_months": 12,
            "long_only": True,
            "margin_allowed": False,
            "short_allowed": False,
            "cash_allowed": True,
            "target_position_min": 5,
            "target_position_max": 8,
            "user_constraints": {},
        }
    )
    assumption_dump = assumptions.model_dump(mode="json")
    assert "target_annual_return" not in assumption_dump
    assert "capital_source" not in assumption_dump
    assert "target_annual_return_source" not in assumption_dump

    portfolio = PortfolioConstructionSnapshot.model_validate(
        {
            "capital": "100000",
            "positions": (),
            "cash": "100000",
            "cash_weight": "1",
            "industry_weights": {},
            "factor_exposures": {},
            "estimated_transaction_cost": "0",
            "estimated_slippage": "0",
            "constraint_violations": (),
        }
    )
    portfolio_dump = portfolio.model_dump(mode="json")
    assert "target_annual_return" not in portfolio_dump
    assert "annual_profit_target" not in portfolio_dump
    assert "objective_status" not in portfolio_dump
