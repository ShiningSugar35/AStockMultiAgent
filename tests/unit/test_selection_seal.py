"""An admitted host must atomically consume study/run and final holdout once."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.experiments.selection_seal import (
    ExperimentRunUnavailable,
    RegisteredExperimentRun,
    RegisteredSelectionSeal,
)
from astock.experiments.validation import (
    Budget,
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
    renamed = _plan(run_id="run2", benchmark_id="OTHER")
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, renamed)(_commitment(renamed))
    revised = make_plan(renamed.request.model_copy(update={"input_release_hash": "d" * 64}))
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, revised)(_commitment(revised))
    overlapping = _plan(run_id="overlap", benchmark_id="OTHER", difference=2)
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, overlapping)(_commitment(overlapping))
    independent = _plan(run_id="run2", benchmark_id="OTHER", difference=40)
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


def test_legacy_hash_only_claim_does_not_prove_an_untouched_family(stores):
    state, objects = stores
    ref = objects.put_json({"legacy": "dates unavailable"})
    state.register_artifact(
        artifact_id="StrategySelectionHoldout:legacy", artifact_type="StrategySelectionHoldout",
        schema_version="strategy-selection-claim-v1", object_hash=ref.sha256, input_hashes=[],
    )
    plan = _plan()
    with pytest.raises(ValueError, match="legacy holdout family dates"):
        RegisteredSelectionSeal(state, objects, plan)(_commitment(plan))



class RecordedInterruption(BaseException):
    """Test-only process death: no terminal outcome may be invented."""


def _recording_host(state, objects, plan, *, fail_on=None, interrupt_on=None):
    # The unit host is recorded research evidence, not a historical market producer.
    artifact = objects.put_json({"recorded_unit_execution": True})
    state.register_artifact(
        artifact_id="recorded-execution", artifact_type="StrategyDailyExecution",
        schema_version="unit-fixture-v1", object_hash=artifact.sha256, input_hashes=[],
    )

    class Host:
        calls: list[Query] = []

        def evaluate(self, query: Query, budget: Budget) -> Evidence:
            budget.check()
            self.calls.append(query)
            if fail_on == len(self.calls):
                raise ValueError("[REDACTED_SECRET]")
            if interrupt_on == len(self.calls):
                raise RecordedInterruption()
            return Evidence(
                query_hash=digest(query), producer_version=query.executor_version,
                input_release_hash=query.input_release_hash, source_snapshot_ids=("recorded",),
                execution_artifact_ids=("recorded-execution",),
                accounting_basis="PERIOD_START_NAV_GROSS_MINUS_FEES_MINUS_SLIPPAGE",
                boundary_policy="INDEPENDENT_WINDOW_NO_UNACCOUNTED_CARRY",
                rows=tuple(Row(
                    period_end=plan.request.timestamps[i], gross_return=Decimal("-0.01"),
                    fee_return=Decimal("0.001"), slippage_return=Decimal("0.001"),
                    turnover=Decimal("0.2"), benchmark_return=Decimal("0.005"),
                ) for i in range(query.window.start, query.window.stop)),
            )

    return Host()


def _read_record(state, objects, identity):
    record = state.artifact_record(identity)
    assert record is not None
    return json.loads(objects.get_bytes(str(record["object_hash"])))


def test_registered_run_keeps_full_plan_negative_trials_and_readonly_success_replay(
    stores, monkeypatch,
):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    assert run.begin() is None
    frozen = _read_record(state, objects, run.artifact_id)
    assert frozen["plan"] == plan.model_dump(mode="json")
    assert frozen["hypothesis_status"] == "UNSPECIFIED"
    host = _recording_host(state, objects, plan)
    report = Coordinator(plan, host, run.seal, recorder=run).run()
    saved = run.complete(report)
    assert len(saved["trials"]) == 9
    assert all(row["score_classification"] == "NON_POSITIVE_EXCESS" for row in saved["trials"])
    assert _read_record(state, objects, run.result_id)["report"] == saved

    def no_write(*args, **kwargs):
        raise AssertionError("successful replay must not write or evaluate")

    monkeypatch.setattr(objects, "put_bytes", no_write)
    monkeypatch.setattr(state, "register_artifact", no_write)
    replay = RegisteredExperimentRun(state, objects, plan, "recorded-market").begin()
    assert replay == saved
    assert len(host.calls) == 9


def test_failed_trial_keeps_previous_negative_and_unattempted_candidates_without_secrets(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    assert run.begin() is None
    host = _recording_host(state, objects, plan, fail_on=2)
    with pytest.raises(ValueError, match="REDACTED_SECRET") as caught:
        Coordinator(plan, host, run.seal, recorder=run).run()
    failure = run.fail("COORDINATE", caught.value)
    assert failure["experiment_status"] == "FAILED"
    attempts = failure["details"]["attempts"]
    assert [item["status"] for item in attempts] == [
        "SUCCEEDED", "FAILED", "NOT_ATTEMPTED", "NOT_ATTEMPTED", "NOT_ATTEMPTED", "NOT_ATTEMPTED",
    ]
    assert failure["details"]["selection_sealed"] is False
    with state.connect() as connection:
        hashes = connection.execute(
            "SELECT object_hash FROM artifact_registry WHERE type LIKE 'StrategyExperiment%'",
        ).fetchall()
    assert all(b"[REDACTED_SECRET]" not in objects.get_bytes(row["object_hash"]) for row in hashes)
    with pytest.raises(ExperimentRunUnavailable) as repeated:
        RegisteredExperimentRun(state, objects, plan, "recorded-market").begin(
            recover_recorded=True,
        )
    assert repeated.value.report["experiment_status"] == "FAILED"
    assert len(host.calls) == 2


def test_interrupted_attempt_is_incomplete_and_never_automatically_replayed(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    run.begin()
    host = _recording_host(state, objects, plan, interrupt_on=2)
    with pytest.raises(RecordedInterruption):
        Coordinator(plan, host, run.seal, recorder=run).run()
    assert state.artifact_record(run.result_id) is None
    for recover in (False, True):
        with pytest.raises(ExperimentRunUnavailable) as blocked:
            RegisteredExperimentRun(state, objects, plan, "recorded-market").begin(
                recover_recorded=recover,
            )
        assert blocked.value.report["experiment_status"] == "INCOMPLETE"
        assert blocked.value.report["details"]["attempts"][1]["status"] == "INCOMPLETE"
        assert blocked.value.report["automatic_retry_allowed"] is False
    assert len(host.calls) == 2
    assert state.artifact_record("StrategySelectionStudyRun:" + run.key) is None


def test_explicit_recorded_recovery_builds_report_without_evaluation_or_new_holdout(
    stores, monkeypatch,
):
    state, objects = stores
    plan = _plan()
    original = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    original.begin()
    host = _recording_host(state, objects, plan)
    result = Coordinator(plan, host, original.seal, recorder=original).run()
    assert state.artifact_record(original.result_id) is None
    with pytest.raises(ExperimentRunUnavailable):
        RegisteredExperimentRun(state, objects, plan, "recorded-market").begin()

    def forbidden(*args, **kwargs):
        raise AssertionError("recorded recovery cannot claim another holdout")

    monkeypatch.setattr(RegisteredSelectionSeal, "__call__", forbidden)
    recovered = RegisteredExperimentRun(state, objects, plan, "recorded-market").begin(
        recover_recorded=True,
    )
    assert recovered is not None
    assert recovered["commitment_hash"] == result["commitment_hash"]
    assert recovered["selection_receipt"] == result["selection_receipt"]
    assert len(recovered["trials"]) == 9
    assert len(host.calls) == 9
    assert recovered["paper_ledger_write_allowed"] is False
    assert recovered["market_validation_claim"] is False


def test_recorded_recovery_refuses_corrupt_attempt_and_does_not_write_success(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    run.begin()
    host = _recording_host(state, objects, plan)
    Coordinator(plan, host, run.seal, recorder=run).run()
    with state.connect() as connection:
        row = connection.execute(
            "SELECT object_hash FROM artifact_registry "
            "WHERE type='StrategyExperimentAttemptResult' "
            "ORDER BY artifact_id LIMIT 1",
        ).fetchone()
    assert row is not None
    objects.path_for(row["object_hash"]).write_bytes(b"corrupted recorded result")
    with pytest.raises(StorageError):
        RegisteredExperimentRun(state, objects, plan, "recorded-market").begin(
            recover_recorded=True,
        )
    assert state.artifact_record(run.result_id) is None


def test_concurrent_experiment_reservation_has_one_owner_and_no_implicit_retry(stores):
    state, objects = stores
    plan = _plan()

    def begin(_):
        try:
            result = RegisteredExperimentRun(state, objects, plan, "recorded-market").begin()
            return "OWNER" if result is None else "UNEXPECTED_RESULT"
        except ExperimentRunUnavailable as exc:
            return exc.report["experiment_status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, (0, 1)))
    assert sorted(results) == ["INCOMPLETE", "OWNER"]


def test_preexisting_selection_without_run_record_cannot_be_reexecuted(stores):
    state, objects = stores
    plan = _plan()
    RegisteredSelectionSeal(state, objects, plan)(_commitment(plan))
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    with pytest.raises(ExperimentRunUnavailable) as consumed:
        run.begin()
    assert consumed.value.report["experiment_status"] == "LEGACY_SELECTION_CONSUMED"
    assert state.artifact_record(run.artifact_id) is None


def test_failed_oos_keeps_consumed_holdout_and_records_failed_query(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    run.begin()
    host = _recording_host(state, objects, plan, fail_on=7)
    with pytest.raises(ValueError) as caught:
        Coordinator(plan, host, run.seal, recorder=run).run()
    failure = run.fail("COORDINATE", caught.value)
    assert failure["details"]["selection_sealed"] is True
    attempts = failure["details"]["attempts"]
    assert attempts[6]["phase"] == "OOS"
    assert attempts[6]["status"] == "FAILED"
    assert attempts[7]["status"] == "NOT_ATTEMPTED"
    changed = _plan(run_id="renamed")
    with pytest.raises(ValueError, match="already been claimed"):
        RegisteredSelectionSeal(state, objects, changed)(_commitment(changed))


def test_recovery_requires_every_registered_family_claim(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    run.begin()
    host = _recording_host(state, objects, plan)
    Coordinator(plan, host, run.seal, recorder=run).run()
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM artifact_registry WHERE artifact_id = "
            "(SELECT artifact_id FROM artifact_registry "
            "WHERE type='StrategySelectionFamilySession' ORDER BY artifact_id LIMIT 1)",
        )
    with pytest.raises(ValueError, match="missing or inconsistent"):
        RegisteredExperimentRun(state, objects, plan, "recorded-market").begin(
            recover_recorded=True,
        )
    assert state.artifact_record(run.result_id) is None


def test_same_owner_cannot_repeat_a_query_with_another_coordinator(stores):
    state, objects = stores
    plan = _plan()
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    run.begin()
    host = _recording_host(state, objects, plan)
    Coordinator(plan, host, run.seal, recorder=run).run()
    with pytest.raises(ValueError, match="already started"):
        Coordinator(plan, host, run.seal, recorder=run).run()
    assert len(host.calls) == 9


def test_registered_plan_byte_budget_rejects_before_reserving_run(stores):
    state, objects = stores
    original = _plan()
    plan = make_plan(
        original.request, original.policy.model_copy(update={"max_artifact_bytes": 1024}),
    )
    run = RegisteredExperimentRun(state, objects, plan, "recorded-market")
    with pytest.raises(ValueError, match="plan exceeds artifact budget"):
        run.begin()
    assert state.artifact_record(run.artifact_id) is None
