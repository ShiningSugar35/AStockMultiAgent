"""Explicit diagnostics must survive safe fallbacks without leaking raw exceptions."""
from __future__ import annotations

import json

from astock.core.artifact_reading import ArtifactReadError
from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector
from astock.investor_orchestration.recommendation_fulfillment import (
    RecommendationFulfillmentAssessment,
    RecommendationFulfillmentService,
    RecommendationFulfillmentState,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from tests.unit.test_investor_answer_projection import environment as environment
from tests.unit.test_investor_orchestration_guards import good_run


def fail_projection(*args, **kwargs):
    raise ArtifactReadError("HISTORICAL_RECEIPT_REQUIRES_REFRESH",
                            artifact_type="RecommendationResearchReceipt")


def test_projection_schema_failure_is_not_reported_as_missing_market_facts(
    environment, monkeypatch,
):
    _, _, _, coverage, _, _ = good_run(environment)
    monkeypatch.setattr(VerifiedAnswerProjector, "freeze", fail_projection)
    diagnostic = {"obsolete": "do-not-retain-this"}
    result = InvestorOrchestrationService(environment.store).publish_verified(
        coverage_receipt_id=coverage.receipt_id, diagnostics=diagnostic,
    )
    assert result.degraded
    assert "处理故障" in result.model_dump_json()
    assert "关键资料尚未核实完整" not in result.model_dump_json()
    assert "HISTORICAL_RECEIPT" not in result.model_dump_json()
    assert diagnostic["code"] == "HISTORICAL_RECEIPT_REQUIRES_REFRESH"
    assert diagnostic["next_action"] == "RESUME_CURRENT_RESEARCH"
    assert "obsolete" not in diagnostic


def test_cli_diagnostics_explicitly_show_the_swallowed_projection_failure(environment, monkeypatch):
    from typer.testing import CliRunner

    from astock.investor_orchestration.cli import app

    _, _, _, coverage, _, _ = good_run(environment)
    monkeypatch.setattr(VerifiedAnswerProjector, "freeze", fail_projection)
    args = ["publish", coverage.receipt_id, "--database", str(environment.store.path)]
    plain = CliRunner().invoke(app, args)
    assert plain.exit_code == 2
    assert json.loads(plain.stdout)["degraded"]
    assert "HISTORICAL_RECEIPT_REQUIRES_REFRESH" not in plain.stdout
    developer = CliRunner().invoke(app, [*args, "--diagnostics"])
    assert developer.exit_code == 2
    payload = json.loads(developer.stdout)
    assert payload["answer"]["degraded"]
    assert payload["diagnostic"]["code"] == "HISTORICAL_RECEIPT_REQUIRES_REFRESH"


def test_direct_publication_cannot_bypass_unsatisfied_recommendation_target(
    environment, monkeypatch,
):
    _, _, _, coverage, _, _ = good_run(environment)

    def unsatisfied(_self, request, _coverage):
        return RecommendationFulfillmentAssessment(
            request_id=request.request_id,
            minimum_actionable_count=3,
            target_actionable_count=5,
            state=RecommendationFulfillmentState.GENERATE_NEXT_SEED_BATCH,
            satisfied=False,
            next_action="GENERATE_NEXT_SEED_BATCH_WITH_EXCLUSIONS",
        )

    monkeypatch.setattr(RecommendationFulfillmentService, "assess", unsatisfied)
    diagnostic = {}
    answer = InvestorOrchestrationService(environment.store).publish_verified(
        coverage_receipt_id=coverage.receipt_id, diagnostics=diagnostic,
    )
    assert answer.degraded
    assert "不作为最终荐股结果" in answer.conclusion
    assert "观察对象" in answer.reasons[0]
    assert diagnostic == {
        "code": "RECOMMENDATION_TARGET_NOT_SATISFIED",
        "state": "GENERATE_NEXT_SEED_BATCH",
        "next_action": "GENERATE_NEXT_SEED_BATCH_WITH_EXCLUSIONS",
    }


def test_success_clears_prior_diagnostics(environment):
    _, _, _, coverage, _, _ = good_run(environment)
    diagnostic = {"code": "STALE_FAILURE"}
    answer = InvestorOrchestrationService(environment.store).publish_verified(
        coverage_receipt_id=coverage.receipt_id, diagnostics=diagnostic,
    )
    assert not answer.degraded
    assert diagnostic == {}
