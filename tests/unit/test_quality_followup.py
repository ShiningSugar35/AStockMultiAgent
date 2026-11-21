"""Defect-first regression tests for diagnostics and historical decision comparison."""

from __future__ import annotations

from datetime import timedelta

import pytest

from astock.experiments.validation import Query, diagnose, digest, make_plan
from tests.unit.test_quality_review_regressions import brief, receipt
from tests.unit.test_strategy_validation import evidence, request


def test_diagnostic_query_must_bind_same_strategy_release_executor_and_benchmark() -> None:
    plan = make_plan(request())
    original = Query(
        plan_hash=digest(plan),
        executor_version=plan.request.executor_version,
        input_release_hash=plan.request.input_release_hash,
        candidate=plan.request.candidates[0],
        benchmark_id=plan.request.benchmark_id,
        phase="DIAGNOSTIC",
        fold_number=0,
        window=plan.folds[0].train,
    )
    assert diagnose(plan, original, evidence(original, plan))["oos_assertion"] is False
    mutations = (
        {"input_release_hash": "9" * 64},
        {"executor_version": "foreign-executor"},
        {"benchmark_id": "different-benchmark"},
        {"candidate": plan.request.candidates[0].model_copy(update={"parameters": {"n": 999}})},
    )
    for update in mutations:
        forged = original.model_copy(update=update)
        with pytest.raises(ValueError, match="selected plan"):
            diagnose(plan, forged, evidence(forged, plan))


def test_old_published_receipt_is_valid_for_comparison_not_current_recommendation() -> None:
    _, old = receipt(holding_action="HOLD")
    _, current = receipt(holding_action="EXIT")
    shift = timedelta(days=8)
    historical_dag = old.dag.model_copy(
        update={
            "executions": tuple(
                node.model_copy(update={"completed_at": node.completed_at - shift})
                for node in old.dag.executions
            ),
        }
    )
    historical = old.model_copy(update={"as_of": old.as_of - shift, "dag": historical_dag})
    result = brief(current, previous_receipt=historical)
    assert result["conclusion_changed"] is True
    assert "由“继续持有”调整为“按条件退出”" in result["text"]
    old_only = brief(historical)
    assert old_only["source_facts"]["current"] is None
    assert old_only["public_approved"] is False
