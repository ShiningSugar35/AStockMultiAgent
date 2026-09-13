"""Domain rendering contracts using isolated canonical service outputs.

These tests exercise presentation branches, not acquisition, recommendation
admission or the 68 business E2E gate. Full publication authentication is tested
separately in test_investor_answer_projection.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector, VerifiedInputs
from astock.investor_orchestration.models import RequestIntent
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import LedgerService
from astock.schemas.institutional_research import InstitutionalDecisionContext
from astock.schemas.research_runtime import ClassifiedTradeProtocol
from astock.schemas.research_team import ResearchRoleOutput
from tests.integration.test_committee import _service_and_request
from tests.unit.test_investor_orchestration_guards import Environment, good_run, registered


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("answer-domain-rendering")
    store = InvestorOrchestrationStore(root / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(root / "objects" / "sha256")
    ledger = LedgerService(state, objects)
    ledger.initialize_account("guard-paper-account", 100_000)
    environment = Environment(store, state, objects, ledger)
    request, preflight, _, coverage, _, _ = good_run(environment)
    committee, decision_request, artifacts = _service_and_request(root / "recorded-domain", state)
    result = committee.decide(decision_request)
    # The older Phase-4 fixture uses company:000001 while the current
    # classification schema requires a six-digit code. Adapt only the identity
    # in these presentation-unit inputs; they are NOT formal admission records.
    # Authentic service-to-publication checks remain in the separate suite.
    company_id = result.protocol.company_id.removeprefix("company:")
    protocol_value = type(result.protocol).model_validate(
        {
            **result.protocol.model_dump(),
            "company_id": company_id,
        }
    )
    decision_value = type(result.decision).model_validate(
        {
            **result.decision.model_dump(),
            "company_id": company_id,
        }
    )
    protocol_id = registered(environment, protocol_value)
    decision_id = registered(environment, decision_value)
    protocol_record = state.artifact_record(protocol_id)
    decision_record = state.artifact_record(decision_id)
    assert protocol_record is not None and decision_record is not None
    now = datetime.now(UTC)
    evidence_ids = decision_request.assessment.support_evidence_ids
    statement = {
        "statement": "经营增长仍需由持续盈利与现金流共同验证。",
        "claim_ids": [],
        "evidence_ids": evidence_ids,
    }
    opposing = {**statement, "statement": "需求减弱或回款放缓可能削弱原有投资判断。"}
    context = InstitutionalDecisionContext.model_validate(
        {
            "context_id": "render-only-institutional-context",
            "company_id": company_id,
            "as_of": now,
            "created_at": now,
            "fundamental_model_bundle_artifact_id": "render-only-boundary-not-an-admission",
            "fundamental_model_bundle_object_hash": "0" * 64,
            "draft": {
                "decision_question": "核对冻结研究结果的呈现形式",
                "decision_horizon_end": (now + timedelta(days=180)).date(),
                "investment_thesis": statement,
                "variant_perception": statement,
                "key_driver_ids": [
                    "recorded-cash-driver",
                    "recorded-growth-driver",
                    "recorded-margin-driver",
                ],
                "competing_hypotheses": [opposing],
            },
            "claim_ids": [],
            "evidence_ids": sorted(evidence_ids),
            "source_artifact_ids": ["render-only-boundary-not-an-admission"],
            "source_object_hashes": ["0" * 64],
        }
    )
    classified = ClassifiedTradeProtocol.model_validate(
        {
            "protocol_id": "render-only-classification",
            "company_id": company_id,
            "as_of": now,
            "created_at": now,
            "decision_pack_artifact_id": decision_id,
            "decision_pack_object_hash": decision_record["object_hash"],
            "committee_protocol_artifact_id": protocol_id,
            "committee_protocol_object_hash": protocol_record["object_hash"],
            "trading_classification_artifact_id": "render-only-market-classification",
            "trading_classification_object_hash": "1" * 64,
            "committee_outcome": "APPROVE_SIMULATION",
            "final_outcome": "APPROVE_SIMULATION",
            "board": "MAIN",
            "risk_status": "NORMAL",
            "special_regime": "ORDINARY",
            "price_limit_regime": "FIXED",
            "blocking_codes": [],
            "frozen_input_hashes": sorted(
                [
                    decision_record["object_hash"],
                    protocol_record["object_hash"],
                    "1" * 64,
                ]
            ),
            "paper_simulation_allowed": True,
        }
    )
    # Typed envelopes here isolate text rendering. They are deliberately never
    # submitted to output verification or counted as full business admission.
    inputs = VerifiedInputs(
        request.model_copy(
            update={
                "question_time": datetime.now(UTC),
                "normalized_intent": RequestIntent.RESEARCH,
            }
        ),
        preflight,
        coverage,
        {"COMMITTEE": (classified,), "COMPANY_RESEARCH": (context,)},
        (),
        (),
    )
    projector = VerifiedAnswerProjector(RegisteredOutputVerifier(store, objects))
    return projector, inputs, artifacts, environment, result


def test_company_research_keeps_evidence_bound_thesis_and_opposing_case(pipeline) -> None:
    projector, inputs, _, _, _ = pipeline
    output = projector._research(inputs)
    assert "不是对当前买入时点" in output["conclusion"]
    assert any("现金流" in reason for reason in output["reasons"])
    assert any("回款放缓" in risk for risk in output["risks"])
    assert output["actions"] == ()


def test_company_research_without_an_opposing_case_does_not_invent_one(pipeline) -> None:
    projector, inputs, _, _, _ = pipeline
    context = inputs.outputs["COMPANY_RESEARCH"][0]
    assert isinstance(context, InstitutionalDecisionContext)
    altered = context.model_copy(
        update={"draft": context.draft.model_copy(update={"competing_hypotheses": []})}
    )
    with pytest.raises(ValueError, match="competing hypothesis"):
        projector._research(replace(inputs, outputs={"COMPANY_RESEARCH": (altered,)}))


def test_domain_internal_text_cannot_cross_the_final_public_audit(pipeline) -> None:
    projector, inputs, _, _, _ = pipeline
    context = inputs.outputs["COMPANY_RESEARCH"][0]
    assert isinstance(context, InstitutionalDecisionContext)
    bad = context.model_copy(
        update={
            "draft": context.draft.model_copy(
                update={
                    "investment_thesis": context.draft.investment_thesis.model_copy(
                        update={"statement": "MarketPriceAnchor CLAIM_IDS_REQUIRED"}
                    ),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="public presentation audit"):
        projector._derive(
            replace(
                inputs,
                request=inputs.request.model_copy(
                    update={"normalized_intent": RequestIntent.RESEARCH}
                ),
                outputs={"COMPANY_RESEARCH": (bad,)},
            )
        )


def test_monitor_keeps_registered_event_summary_without_claiming_execution(pipeline) -> None:
    projector, inputs, _, _, _ = pipeline
    output = ResearchRoleOutput(
        plan_id="render-only-plan",
        task_id="render-only-event",
        output_contract="CATALYST_RISK",
        summary="已披露的经营变化需要与后续执行进度一起核验。",
    )
    result = projector._monitor(replace(inputs, outputs={"EVENT_RESEARCH": (output,)}))
    assert result["reasons"] == (output.summary,)
    assert any("未经确认的信息不作为交易事实" in risk for risk in result["risks"])
    with pytest.raises(ValueError, match="event research"):
        projector._monitor(replace(inputs, outputs={}))


def test_financial_text_numeric_formatter_is_finite_and_market_identity_is_explicit() -> None:
    from astock.investor_orchestration.answer_projection import _code, _decimal

    assert _decimal(Decimal("12.3400")) == "12.34"
    assert _code("600519.XSHG") == "沪市600519"
    for value in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="finite"):
            _decimal(value)
    with pytest.raises(ValueError, match="unsupported"):
        _code("WRONG:600519")
