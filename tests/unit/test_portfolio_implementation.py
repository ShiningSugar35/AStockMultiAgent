"""Exercise the real transition consumer with recorded canonical-reader responses."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from astock.investor_orchestration.models import LaneSnapshot, PortfolioLane, PositionView
from astock.portfolio.allocation import revalidate_implementation
from astock.schemas.portfolio import (
    PortfolioAllocationConstraints,
    PortfolioAnalysisReport,
    PortfolioCandidateInput,
    PortfolioConstructionReport,
    PortfolioConstructionRequest,
)
from astock.schemas.portfolio_decision import PortfolioImplementationCostInput
from tests.unit.test_portfolio_decision import (
    AS_OF,
    _register_model,
    _register_official_capture,
    _transition_fixture,
)


def inputs(
    tmp_path, monkeypatch, *, held=.02, target=.1, available=True,
    drift=False, cap=.25, nav_fen=10_000_000,
):
    service, request = _transition_fixture(tmp_path)
    nav = nav_fen
    quantity = round(held*nav/1000)
    quantities = {"600000": quantity, "600001": quantity}
    request = request.model_copy(update={
        "portfolio_nav_fen": nav, "current_quantities": quantities,
        "implementation_account_id": "recorded-account", "implementation_lane": "PAPER",
    })
    current, _ = service._load_model(
        request.current_analysis_artifact_id, "PortfolioAnalysisReport",
        PortfolioAnalysisReport,
    )
    current = current.model_copy(update={
        "assets": [item.model_copy(update={"weight": held}) for item in current.assets],
        "metrics": current.metrics.model_copy(update={
            "invested_weight": 2*held, "cash_weight": 1-2*held,
        }),
    })
    current_id = "PortfolioAnalysisReport:implementation"
    _register_model(service.state, service.objects, current_id, "PortfolioAnalysisReport", current)
    source = _register_official_capture(
        service, "OfficialWebDocumentCapture:implementation-fees",
        observed_at=AS_OF-timedelta(days=1),
    )
    costs = [PortfolioImplementationCostInput(
        instrument_id="XSHG:"+key, estimated_round_trip_cost_bps=20,
        source_artifact_ids=["OfficialWebDocumentCapture:implementation-fees"],
        source_object_hashes=[source], verified=True, created_at=AS_OF,
    ) for key in quantities]
    original = PortfolioConstructionRequest(
        portfolio_id=request.intent.portfolio_id, as_of=AS_OF, target_total_exposure=2*target,
        candidates=[PortfolioCandidateInput(
            company_id=key, classified_protocol_artifact_id="recorded-protocol:"+key,
            risk_group="shared", created_at=AS_OF,
        ) for key in quantities],
        allocation_constraints=PortfolioAllocationConstraints(group_caps={"shared": cap}),
        created_at=AS_OF,
    )
    original_id, original_hash = service._freeze(
        "PortfolioConstructionRequest", original.schema_version, original.model_dump(mode="json")
    )
    construction, _ = service._load_model(
        request.target_construction_artifact_id,
        "PortfolioConstructionReport", PortfolioConstructionReport,
    )
    construction = construction.model_copy(update={
        "source_artifact_ids": [original_id], "source_object_hashes": [original_hash],
        "proposals": [item.model_copy(update={
            "weights": {key: target for key in quantities}, "cash_weight": 1-2*target,
        }) for item in construction.proposals],
    })
    construction_id = "PortfolioConstructionReport:implementation"
    _register_model(
        service.state, service.objects, construction_id, "PortfolioConstructionReport",
        construction, input_hashes=[original_hash],
    )
    request = request.model_copy(update={
        "current_analysis_artifact_id": current_id,
        "target_construction_artifact_id": construction_id, "implementation_costs": costs,
    })

    class Reader:
        def __init__(self, *args):
            pass

        def bounded_health_check(self):
            return True, None

        def revision_vector(self):
            return {"paper": "recorded-revision"}

        def lane_snapshot(self, *args, **kwargs):
            return LaneSnapshot(
                lane=PortfolioLane.PAPER, account_ids=("recorded-account",),
                positions=tuple(PositionView(
                    account_id="recorded-account", lane=PortfolioLane.PAPER,
                    instrument_id="XSHG:"+key, quantity=Decimal(qty+int(drift)),
                    available_quantity=Decimal(qty if available else 0),
                    source_revision="recorded-revision",
                ) for key, qty in quantities.items()),
                cash_by_account={"recorded-account": Decimal(str((1-2*held)*nav/100))},
                known_cash=Decimal(str((1-2*held)*nav/100)), unknown_cash=False,
                source_revision="recorded-revision", audit_status="PASS",
            )

    monkeypatch.setattr("astock.portfolio.implementation.CanonicalStateProjectionReader", Reader)
    return service, request


def test_transition_calls_shared_exit_after_existing_lot_and_band_projection(tmp_path, monkeypatch):
    # A 100-share lot is 0.5% of NAV, allowing a distinct feasible band endpoint.
    service, request = inputs(tmp_path, monkeypatch, nav_fen=20_000_000)
    with patch(
        "astock.portfolio.implementation.revalidate_implementation",
        wraps=revalidate_implementation,
    ) as shared:
        report = service.transition(request)
    assessment = report.implementation_assessment
    assert assessment is not None and assessment.status == "RESEARCH_PROJECTED"
    assert shared.call_count == 1
    assert sum(assessment.projected_weights.values()) < sum(report.target.weights.values())
    assert assessment.total_l1_turnover is not None
    assert assessment.one_off_cost_fraction == pytest.approx(
        assessment.total_l1_turnover*.001
    )
    assert assessment.annualized_volatility != report.target.annualized_volatility
    assert not assessment.optimality_claim and not assessment.execution_allowed
    assert not report.paper_ledger_write_allowed


def test_transition_cannot_apply_continuous_target_to_changed_account(tmp_path, monkeypatch):
    service, request = inputs(tmp_path, monkeypatch, drift=True)
    assessment = service.transition(request).implementation_assessment
    assert assessment is not None
    assert assessment.status == "STALE_CONTEXT"
    assert assessment.reason_codes == ["CURRENT_QUANTITY_DRIFT"]
    assert not assessment.projected_weights


def test_transition_rejects_same_day_unavailable_stock_sales(tmp_path, monkeypatch):
    service, request = inputs(tmp_path, monkeypatch, held=.2, available=False)
    assessment = service.transition(request).implementation_assessment
    assert assessment is not None
    assert assessment.status == "UNQUALIFIED"
    assert assessment.reason_codes[0].startswith("T1_AVAILABLE_QUANTITY_EXCEEDED:")
    assert not assessment.projected_weights


def test_hold_band_cannot_hide_post_projection_group_breach(tmp_path, monkeypatch):
    service, request = inputs(tmp_path, monkeypatch, held=.095, target=.09, cap=.18)
    assessment = service.transition(request).implementation_assessment
    assert assessment is not None
    assert assessment.status == "INFEASIBLE"
    assert "GROUP:shared" in assessment.reason_codes
    assert not assessment.projected_weights


def test_no_account_proof_preserves_research_but_declines_implementation(tmp_path):
    service, request = _transition_fixture(tmp_path)
    report = service.transition(request)
    assessment = report.implementation_assessment
    assert assessment is not None
    assert assessment.status == "UNQUALIFIED"
    assert assessment.reason_codes == ["CANONICAL_ACCOUNT_BINDING_REQUIRED"]
    assert report.target.weights
