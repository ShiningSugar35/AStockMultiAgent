"""An admitted host must atomically consume study/run and final holdout once."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.experiments.selection_seal import RegisteredSelectionSeal
from astock.experiments.validation import (
    Candidate,
    Commitment,
    Coordinator,
    Evidence,
    Query,
    Request,
    Row,
    Selection,
    digest,
    make_plan,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore


@pytest.fixture
def stores(tmp_path):
    metadata = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    metadata.initialize()
    return StateStore(metadata.path), ObjectStore(tmp_path / "objects/sha256")


def _plan(*, run_id="run1", benchmark_id="INDEX", difference=0):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    req = Request(
        study_id="fixed-study",
        run_id=run_id,
        executor_version="recorded-executor",
        input_release_hash="a" * 64,
        benchmark_id=benchmark_id,
        timestamps=tuple(start + timedelta(days=i + difference) for i in range(34)),
        candidates=(
            Candidate(candidate_id="trend", strategy_version="v1", parameters={"period": 20}),
            Candidate(candidate_id="revert", strategy_version="v1", parameters={"period": 10}),
        ),
        min_train_bars=4,
        test_bars=2,
        step_bars=3,
        fold_count=2,
        holdout_start=26,
        label_horizon_bars=1,
        purge_bars=1,
        embargo_bars=1,
    )
    return make_plan(req)


def _commitment(plan):
    winners = [
        Selection(
            phase="OOS",
            fold_number=fold.number,
            train_window=fold.train,
            candidate=plan.request.candidates[0],
            training_evidence_hashes=("b" * 64, "c" * 64),
            training_score=Decimal("0.01"),
        )
        for fold in plan.folds
    ]
    winners.append(
        Selection(
            phase="FINAL_HOLDOUT",
            fold_number=0,
            train_window=plan.final_train,
            candidate=plan.request.candidates[0],
            training_evidence_hashes=("b" * 64, "c" * 64),
            training_score=Decimal("0.01"),
        )
    )
    return Commitment(
        plan_hash=digest(plan),
        study_id=plan.request.study_id,
        run_id=plan.request.run_id,
        holdout_identity=digest(
            {
                "input_release_hash": plan.request.input_release_hash,
                "benchmark_id": plan.request.benchmark_id,
                "period_ends": plan.request.timestamps[plan.final_holdout.start :],
            }
        ),
        selections=tuple(winners),
    )


def test_atomic_seal_registers_existing_fact_registry_without_new_table(stores):
    state, objects = stores
    plan = _plan()
    commitment = _commitment(plan)
    seal = RegisteredSelectionSeal(state, objects, plan)
    receipt = seal(commitment)
    assert receipt.startswith("StrategySelectionStudyRun:")
    study = state.artifact_record(receipt)
    holdout = state.artifact_record(
        "StrategySelectionHoldout:" + commitment.holdout_identity
    )
    assert study is not None and holdout is not None
    assert study["object_hash"] == holdout["object_hash"]
    assert study["type"] == "StrategySelectionStudyRun"
    assert holdout["type"] == "StrategySelectionHoldout"
    with pytest.raises(ValueError, match="already been claimed"):
        seal(commitment)


def test_same_study_run_or_same_holdout_cannot_be_reused(stores):
    state, objects = stores
    first = _plan()
    RegisteredSelectionSeal(state, objects, first)(_commitment(first))
    changed_holdout = _plan(difference=1)
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, changed_holdout)(_commitment(changed_holdout))
    changed_run = _plan(run_id="run2")
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, changed_run)(_commitment(changed_run))
    independent = _plan(run_id="run2", benchmark_id="OTHER")
    assert RegisteredSelectionSeal(state, objects, independent)(
        _commitment(independent)
    ).startswith("StrategySelectionStudyRun:")


def test_tampered_commitments_do_not_consume_claim(stores):
    state, objects = stores
    plan = _plan()
    original = _commitment(plan)
    invalid = original.model_copy(update={"holdout_identity": "0" * 64})
    with pytest.raises(ValueError, match="binding mismatch"):
        RegisteredSelectionSeal(state, objects, plan)(invalid)
    invalid_winner = original.selections[0].model_copy(
        update={"candidate": Candidate(candidate_id="fake", strategy_version="v1", parameters={})}
    )
    with pytest.raises(ValueError, match="invalid frozen winner"):
        RegisteredSelectionSeal(state, objects, plan)(
            original.model_copy(update={"selections": (invalid_winner, *original.selections[1:])})
        )
    receipt = RegisteredSelectionSeal(state, objects, plan)(original)
    assert state.artifact_record(receipt) is not None


def test_concurrent_claims_yield_exactly_one_winner(stores):
    state, objects = stores
    plan = _plan()
    commitment = _commitment(plan)

    def claim(_):
        try:
            return RegisteredSelectionSeal(state, objects, plan)(commitment)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert sum(x.startswith("StrategySelectionStudyRun:") for x in results) == 1
    assert sum("already been claimed" in x for x in results) == 1
    assert state.artifact_record(
        "StrategySelectionHoldout:" + commitment.holdout_identity
    ) is not None


def test_coordinator_observes_persistent_selection_before_any_oos_read(stores):
    state, objects = stores
    plan = _plan()
    selected = RegisteredSelectionSeal(state, objects, plan)

    class Host:
        phases = []

        def evaluate(self, query: Query, budget):
            budget.check()
            if query.phase in {"OOS", "FINAL_HOLDOUT"}:
                assert query.selection_hash
                assert state.artifact_record(
                    "StrategySelectionHoldout:" + _commitment(plan).holdout_identity
                ) is not None
            self.phases.append(query.phase)
            return Evidence(
                query_hash=digest(query),
                producer_version=query.executor_version,
                input_release_hash=query.input_release_hash,
                source_snapshot_ids=("fixture",),
                execution_artifact_ids=("fixture",),
                accounting_basis="PERIOD_START_NAV_GROSS_MINUS_FEES_MINUS_SLIPPAGE",
                boundary_policy="INDEPENDENT_WINDOW_NO_UNACCOUNTED_CARRY",
                rows=tuple(
                    Row(
                        period_end=plan.request.timestamps[i],
                        gross_return=Decimal("0.005"),
                        fee_return=Decimal("0.0001"),
                        slippage_return=Decimal("0.0002"),
                        turnover=Decimal("1"),
                        benchmark_return=Decimal("0.001"),
                    )
                    for i in range(query.window.start, query.window.stop)
                ),
            )

    host = Host()
    report = Coordinator(plan, host, selected).run()
    assert report["authority"] == "RESEARCH_ONLY"
    assert report["selection_receipt"].startswith("StrategySelectionStudyRun:")
    assert host.phases[-1] == "FINAL_HOLDOUT"
    with pytest.raises(ValueError, match="already been claimed"):
        Coordinator(plan, Host(), selected).run()
