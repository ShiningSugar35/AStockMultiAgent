from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.experiments.validation import (
    Budget,
    Candidate,
    Coordinator,
    Evidence,
    Policy,
    Query,
    RegisteredEvidenceEvaluator,
    Request,
    Row,
    Stress,
    diagnose,
    digest,
    make_plan,
    metrics,
)


def request(**updates):
    values = dict(
        study_id="study",
        run_id="run",
        executor_version="executor-v1",
        input_release_hash="a" * 64,
        benchmark_id="benchmark",
        timestamps=tuple(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(32)),
        candidates=(
            Candidate.model_validate(
                dict(candidate_id="a", strategy_version="v1", parameters={"n": 2})
            ),
            Candidate.model_validate(
                dict(candidate_id="b", strategy_version="v1", parameters={"n": 3})
            ),
        ),
        min_train_bars=4,
        test_bars=2,
        step_bars=3,
        fold_count=2,
        holdout_start=25,
        label_horizon_bars=2,
        purge_bars=1,
        embargo_bars=1,
    )
    values.update(updates)
    return Request.model_validate(dict(**values))


def evidence(query, plan, value=Decimal("0.02")):
    return Evidence.model_validate(
        dict(
            query_hash=digest(query),
            producer_version=query.executor_version,
            input_release_hash=query.input_release_hash,
            source_snapshot_ids=("snapshot",),
            execution_artifact_ids=("execution",),
            accounting_basis="PERIOD_START_NAV_GROSS_MINUS_FEES_MINUS_SLIPPAGE",
            boundary_policy="INDEPENDENT_WINDOW_NO_UNACCOUNTED_CARRY",
            rows=tuple(
                Row(
                    period_end=plan.request.timestamps[i],
                    gross_return=value,
                    fee_return=Decimal("0.001"),
                    slippage_return=Decimal("0.001"),
                    turnover=Decimal("1"),
                    benchmark_return=Decimal("0.005"),
                )
                for i in range(query.window.start, query.window.stop)
            ),
        )
    )


class Spy:
    def __init__(self, plan, future=Decimal("0.4")):
        self.plan = plan
        self.future = future
        self.calls = []
        self.sealed = False
        self.commitment = None

    def evaluate(self, query, budget):
        budget.check()
        self.calls.append(query)
        if query.phase in {"OOS", "FINAL_HOLDOUT"}:
            assert self.sealed
            assert query.selection_hash
            assert query.candidate.candidate_id == "a"
            value = self.future
        else:
            assert not self.sealed
            barrier = (
                self.plan.final_holdout.start
                if query.phase == "FINAL_TRAIN"
                else self.plan.folds[query.fold_number - 1].test.start
            )
            r = self.plan.request
            assert (
                query.window.stop - 1 + r.label_horizon_bars
                < barrier - r.purge_bars - r.embargo_bars
            )
            value = Decimal("0.02") if query.candidate.candidate_id == "a" else Decimal("0.01")
        return evidence(query, self.plan, value)

    def seal(self, commitment):
        assert not self.sealed
        assert all(q.phase in {"TRAIN", "FINAL_TRAIN"} for q in self.calls)
        self.sealed = True
        self.commitment = commitment
        return "registered-selection-receipt"


def test_selection_is_sealed_before_any_test_and_all_training_trials_are_reported():
    plan = make_plan(request())
    spy = Spy(plan)
    coordinator = Coordinator(plan, spy, spy.seal)
    report = coordinator.run()
    assert report["authority"] == "RESEARCH_ONLY"
    assert not report["paper_ledger_write_allowed"]
    assert not report["recommendation_authority"]
    assert len(report["trials"]) == 9
    assert [q.phase for q in spy.calls[:6]] == [
        "TRAIN",
        "TRAIN",
        "TRAIN",
        "TRAIN",
        "FINAL_TRAIN",
        "FINAL_TRAIN",
    ]
    assert [q.phase for q in spy.calls[6:]] == ["OOS", "OOS", "FINAL_HOLDOUT"]
    assert spy.commitment is not None
    assert all(s.candidate.candidate_id == "a" for s in spy.commitment.selections)
    with pytest.raises(ValueError, match="consumed"):
        coordinator.run()


def test_future_result_changes_do_not_change_historical_or_final_selection():
    plan = make_plan(request())
    left, right = Spy(plan, Decimal("0.8")), Spy(plan, Decimal("-0.5"))
    first = Coordinator(plan, left, left.seal).run()
    second = Coordinator(plan, right, right.seal).run()
    assert first["commitment_hash"] == second["commitment_hash"]
    assert first["trials"][:6] == second["trials"][:6]
    assert first["trials"][6:] != second["trials"][6:]


def test_changing_future_training_observations_cannot_change_earlier_fold_selection():
    plan = make_plan(request())

    class ChangedLaterTraining(Spy):
        def evaluate(self, query, budget):
            result = super().evaluate(query, budget)
            first_stop = self.plan.folds[0].train.stop
            # Changes are confined to bars unavailable to the first selection.
            changed = tuple(
                row.model_copy(update={"gross_return": Decimal("-0.2")})
                if query.window.start + offset >= first_stop
                else row
                for offset, row in enumerate(result.rows)
            )
            return result.model_copy(update={"rows": changed})

    left = Spy(plan)
    right = ChangedLaterTraining(plan)
    a = Coordinator(plan, left, left.seal).run()
    b = Coordinator(plan, right, right.seal).run()
    assert a["commitment"]["selections"][0] == b["commitment"]["selections"][0]


def test_seal_rejection_prevents_all_test_reads_and_attempt_is_consumed():
    plan = make_plan(request())
    spy = Spy(plan)

    def reject(commitment):
        raise ValueError("holdout already claimed")

    coordinator = Coordinator(plan, spy, reject)
    with pytest.raises(ValueError, match="claimed"):
        coordinator.run()
    assert all(q.phase in {"TRAIN", "FINAL_TRAIN"} for q in spy.calls)
    with pytest.raises(ValueError, match="consumed"):
        coordinator.run()


def test_cost_stress_recomputes_net_and_excess():
    plan = make_plan(request())
    query = Query(
        plan_hash=digest(plan),
        executor_version="executor-v1",
        input_release_hash="a" * 64,
        candidate=plan.request.candidates[0],
        benchmark_id="benchmark",
        phase="TRAIN",
        fold_number=1,
        window=plan.folds[0].train,
    )
    result = evidence(query, plan)
    base = metrics(result, Stress.model_validate(dict(stress_id="base")))
    stress = metrics(
        result,
        Stress.model_validate(dict(stress_id="cost", fee_multiplier=2, extra_slippage_bps=10)),
    )
    assert base["mean_net_excess_return"] == Decimal("0.013")
    assert stress["mean_net_excess_return"] == Decimal("0.011")
    assert stress["net_compounded_return"] < base["net_compounded_return"]


@pytest.mark.parametrize("fault", ["missing", "time", "binding", "benchmark"])
def test_invalid_evidence_refuses_statistics(fault):
    plan = make_plan(request())
    spy = Spy(plan)
    original = spy.evaluate

    def broken(query, budget):
        result = original(query, budget)
        if fault == "missing":
            return result.model_copy(update={"rows": result.rows[:-1]})
        if fault == "time":
            return result.model_copy(update={"rows": tuple(reversed(result.rows))})
        if fault == "binding":
            return result.model_copy(update={"query_hash": "b" * 64})
        # Missing benchmark values must fail model validation, never become zero.
        raw = result.model_dump(mode="json")
        raw["rows"][0].pop("benchmark_return")
        return Evidence.model_validate(raw)

    spy.evaluate = broken
    with pytest.raises(ValueError):
        Coordinator(plan, spy, spy.seal).run()
    assert not spy.sealed


@pytest.mark.parametrize(
    "policy",
    [Policy(max_candidates=1), Policy(max_folds=1), Policy(max_bars=20), Policy(max_work_units=1)],
)
def test_policy_limits(policy):
    with pytest.raises(ValueError):
        make_plan(request(), policy)


def test_fold_future_overlap_and_authority_flags_rejected():
    with pytest.raises(ValueError):
        make_plan(request(holdout_start=13))
    with pytest.raises(ValueError):
        request(step_bars=1)
    raw = request().model_dump(mode="json")
    raw["production_authorized"] = True
    with pytest.raises(ValueError):
        Request.model_validate(raw)


def test_utc_roundtrip_and_all_parameter_keys_affect_hash():
    original = request()
    shifted = original.model_dump(mode="json")
    shifted["timestamps"] = [
        t.astimezone(__import__("datetime").timezone(timedelta(hours=8))).isoformat()
        for t in original.timestamps
    ]
    assert digest(original) == digest(Request.model_validate(shifted))
    assert digest(original) == digest(Request.model_validate_json(original.model_dump_json()))
    assert digest({"parameters": {"created_at": "one"}}) != digest(
        {"parameters": {"created_at": "two"}}
    )
    assert digest({"boundary_timestamp": "one"}) != digest({"boundary_timestamp": "two"})


def test_diagnostic_never_claims_oos():
    plan = make_plan(request())
    query = Query(
        plan_hash=digest(plan),
        executor_version="executor-v1",
        input_release_hash="a" * 64,
        candidate=plan.request.candidates[0],
        benchmark_id="benchmark",
        phase="DIAGNOSTIC",
        fold_number=0,
        window=plan.final_holdout,
    )
    report = diagnose(plan, query, evidence(query, plan))
    assert report["classification"] == "RESULT_WINDOW_DIAGNOSTIC"
    assert report["oos_assertion"] is False


def test_registered_adapter_checks_object_hash_and_never_accepts_unregistered_stream():
    plan = make_plan(request())
    query = Query(
        plan_hash=digest(plan),
        executor_version="executor-v1",
        input_release_hash="a" * 64,
        candidate=plan.request.candidates[0],
        benchmark_id="benchmark",
        phase="DIAGNOSTIC",
        fold_number=1,
        window=plan.folds[0].train,
    )
    raw = canonical_json_bytes(evidence(query, plan).model_dump(mode="json"))

    class State:
        def artifact_record(self, artifact_id):
            if artifact_id == "wrapper":
                return {
                    "type": "StrategyEvaluationEvidence",
                    "schema_version": "strategy-evaluation-evidence-v1",
                    "object_hash": sha256_bytes(raw),
                }
            return None

    class Objects:
        def get_bytes(self, object_hash):
            return raw

    adapter = RegisteredEvidenceEvaluator(State(), Objects(), lambda q, b: "wrapper", Policy())
    with pytest.raises(ValueError, match="host-admitted"):
        adapter.evaluate(query, Budget(5))

    class CorruptObjects:
        def get_bytes(self, object_hash):
            return raw + b" "

    adapter = RegisteredEvidenceEvaluator(
        State(), CorruptObjects(), lambda q, b: "wrapper", Policy()
    )
    with pytest.raises(ValueError, match="hash"):
        adapter.evaluate(query, Budget(5))


def test_registered_evidence_cache_remains_small_and_reuses_recent_objects() -> None:
    """Keep memory independent of the number of evaluated candidate windows."""

    plan = make_plan(request())
    query = Query(
        plan_hash=digest(plan),
        executor_version=plan.request.executor_version,
        input_release_hash=plan.request.input_release_hash,
        candidate=plan.request.candidates[0],
        benchmark_id=plan.request.benchmark_id,
        phase="DIAGNOSTIC",
        fold_number=1,
        window=plan.folds[0].train,
    )
    raw = canonical_json_bytes(evidence(query, plan).model_dump(mode="json"))
    digest_bytes = sha256_bytes(raw)

    class State:
        def artifact_record(self, artifact_id):
            return {
                "type": "StrategyEvaluationEvidence",
                "schema_version": "strategy-evaluation-evidence-v1",
                "object_hash": digest_bytes,
            }

    class Objects:
        calls = 0

        def get_bytes(self, object_hash):
            assert object_hash == digest_bytes
            self.calls += 1
            return raw

    objects = Objects()
    adapter = RegisteredEvidenceEvaluator(
        State(), objects, lambda q, b: "wrapper", Policy(max_cached_evidence=2)
    )
    for key in ("a", "b", "a", "c"):
        adapter._read(key, "StrategyEvaluationEvidence", Evidence)
    assert objects.calls == 3
    assert len(adapter._decoded) == 2
    assert tuple(key[0] for key in adapter._decoded) == ("a", "c")

    adapter._read("b", "StrategyEvaluationEvidence", Evidence)
    assert objects.calls == 4
    assert len(adapter._decoded) == 2

    uncached = RegisteredEvidenceEvaluator(
        State(), objects, lambda q, b: "wrapper", Policy(max_cached_evidence=0)
    )
    uncached._read("a", "StrategyEvaluationEvidence", Evidence)
    uncached._read("a", "StrategyEvaluationEvidence", Evidence)
    assert objects.calls == 6
    assert len(uncached._decoded) == 0
    with pytest.raises(ValueError):
        Policy(max_cached_evidence=33)


def canonical_fixture():
    """Synthetic accounting fixture, explicitly NOT historical market validation."""
    from astock.experiments.market_evaluator import VERSION, FrozenMarketRelease, Session
    from astock.schemas.market import (
        Frequency,
        Market,
        MarketBar,
        TimestampSemantics,
        VolumeUnit,
    )
    from astock.schemas.paper import ReplayFeeSchedule

    sessions = []
    closes = [10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15]
    for i, close in enumerate(closes):
        timestamp = datetime(2025, 1, 6, 7, tzinfo=UTC) + timedelta(days=i)

        def bar(
            symbol: str, market: Market, price: int,
            i: int = i, timestamp: datetime = timestamp,
        ) -> MarketBar:
            decimal_price = Decimal(price)
            return MarketBar(
                observation_id=f"synthetic-{symbol}-{i}",
                provider_id="synthetic",
                symbol=symbol,
                market=market,
                frequency=Frequency.D1,
                timestamp=timestamp,
                timestamp_semantics=TimestampSemantics.BAR_END,
                open=decimal_price,
                high=decimal_price,
                low=decimal_price,
                close=decimal_price,
                volume=Decimal(100000),
                volume_unit=VolumeUnit.SHARE,
            )

        sessions.append(
            Session(
                bar=bar("600000", Market.XSHG, close),
                benchmark=bar("000300", Market.INDEX, 100 + i),
                suspended=False,
                limit_down=Decimal(1),
                limit_up=Decimal(100),
            )
        )
    release = FrozenMarketRelease(
        symbol="600000",
        benchmark_id="000300",
        source_snapshot_ids=("synthetic-only",),
        sessions=tuple(sessions),
        corporate_actions=(),
        corporate_action_coverage="TERMS_VERIFIED_EXHAUSTIVE",
        calendar_coverage="EXCHANGE_SESSIONS_EXHAUSTIVE",
        fees=(
            ReplayFeeSchedule(
                rule_version="synthetic-fees",
                effective_from=date(2020, 1, 1),
                applicable_markets=[Market.XSHG],
                commission_rate=Decimal("0.001"),
                minimum_commission_fen=500,
                stamp_tax_sell_rate=Decimal("0.001"),
                transfer_fee_rate=Decimal("0.00001"),
            ),
        ),
        initial_cash_fen=1000000,
        slippage_bps=Decimal(10),
        maximum_participation=Decimal("0.1"),
    )
    req = Request(
        study_id="synthetic",
        run_id="one",
        executor_version=VERSION,
        input_release_hash=digest(release),
        benchmark_id=release.benchmark_id,
        timestamps=tuple(s.bar.timestamp for s in sessions),
        candidates=(
            Candidate(
                candidate_id="one",
                strategy_version="lagged-momentum-v1",
                parameters={"lookback": 1, "shares": 100},
            ),
        ),
        min_train_bars=6,
        test_bars=2,
        step_bars=2,
        fold_count=1,
        holdout_start=9,
        label_horizon_bars=0,
        purge_bars=0,
        embargo_bars=0,
    )
    return release, make_plan(req)


def register_canonical(state, objects, release):
    from astock.schemas.evidence import SourceSnapshot

    source = objects.put_bytes(b"Synthetic accounting fixture, not historical market evidence")
    for identity in release.source_snapshot_ids:
        state.register_snapshot(
            SourceSnapshot(
                snapshot_id=identity,
                source_id="synthetic-only",
                object_sha256=source.sha256,
                fetched_at=datetime(2025, 2, 1, tzinfo=UTC),
                available_to_system_at=datetime(2025, 2, 1, tzinfo=UTC),
                mime="text/plain",
                byte_size=len(objects.get_bytes(source.sha256)),
            )
        )
    obj = objects.put_bytes(canonical_json_bytes(release.model_dump(mode="json")))
    identity = "StrategyCanonicalMarketRelease:" + obj.sha256
    state.register_artifact(
        artifact_id=identity,
        artifact_type="StrategyCanonicalMarketRelease",
        schema_version=release.schema_version,
        object_hash=obj.sha256,
        input_hashes=[],
    )
    return identity


def canonical_query(plan, **updates):
    values = dict(
        plan_hash=digest(plan),
        executor_version=plan.request.executor_version,
        input_release_hash=plan.request.input_release_hash,
        candidate=plan.request.candidates[0],
        benchmark_id=plan.request.benchmark_id,
        phase="TRAIN",
        fold_number=1,
        window=plan.folds[0].train,
    )
    values.update(updates)
    return Query.model_validate(values)


@pytest.fixture
def canonical_stores(tmp_path):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore

    metadata = InvestorOrchestrationStore(tmp_path / "isolated.sqlite")
    metadata.initialize()
    return StateStore(metadata.path), ObjectStore(tmp_path / "objects")


def test_canonical_accounting_prices_benchmark_fees_and_reproduction(canonical_stores):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    evaluator = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, release), plan
    )
    q = canonical_query(plan)
    result = evaluator.evaluate(q, Budget(5))
    assert result == evaluator.evaluate(q, Budget(5))
    assert result.rows[2].gross_return == 0  # enters at close 12, no earlier gain
    assert result.rows[2].fee_return == Decimal(501) / 1000000
    assert result.rows[2].slippage_return == Decimal(100) / 1000000
    assert result.rows[3].gross_return == Decimal("0.01000601361418212345619717450187563")
    assert result.rows[1].benchmark_return == Decimal("0.01")
    # The down move is visible only on the following session, when the sale occurs.
    assert result.rows[4].turnover == 0
    assert result.rows[5].fee_return > 0
    execution = state.artifact_record(result.execution_artifact_ids[0])
    import json

    facts = json.loads(objects.get_bytes(execution["object_hash"]))
    assert [f["quantity_delta"] for f in facts["fills"]] == [100, -100]
    assert facts["fills"][1]["fees_fen"] == 611
    assert facts["ending_shares"] == 0


@pytest.mark.parametrize("fault", ["benchmark", "adjusted", "missing", "fee", "action", "identity"])
def test_canonical_input_fails_closed(canonical_stores, fault):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    raw = release.model_dump(mode="json")
    if fault == "benchmark":
        raw["sessions"][0]["benchmark"]["timestamp"] = "2020-01-01T00:00:00Z"
    elif fault == "adjusted":
        raw["sessions"][0]["bar"]["adjustment_mode"] = "QFQ"
    elif fault == "missing":
        raw["sessions"].pop()
    elif fault == "fee":
        raw["fees"][0]["applicable_markets"] = ["XSHE"]
    elif fault == "identity":
        raw["sessions"][0]["bar"]["symbol"] = "600001"
    else:
        raw["corporate_actions"] = [
            dict(
                event_id="split",
                symbol="600000",
                event_type="SPLIT",
                ex_date="2025-01-08",
                ratio="2",
                source_id="test",
                rule_version="test",
            )
        ]
    release = FrozenMarketRelease.model_validate(raw)
    plan = make_plan(plan.request.model_copy(update={"input_release_hash": digest(release)}))
    with pytest.raises(ValueError):
        CanonicalMarketEvaluator(state, objects, register_canonical(state, objects, release), plan)


@pytest.mark.parametrize("restriction", ["suspended", "upper", "participation", "cash"])
def test_canonical_execution_restrictions(canonical_stores, restriction):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    raw = release.model_dump(mode="json")
    if restriction == "suspended":
        raw["sessions"][2]["suspended"] = True
    elif restriction == "upper":
        raw["sessions"][2]["limit_up"] = "12"
    elif restriction == "participation":
        raw["maximum_participation"] = "0.0001"
    else:
        raw["initial_cash_fen"] = 10000
    release = FrozenMarketRelease.model_validate(raw)
    plan = make_plan(plan.request.model_copy(update={"input_release_hash": digest(release)}))
    evaluator = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, release), plan
    )
    result = evaluator.evaluate(canonical_query(plan), Budget(5))
    assert result.rows[2].turnover == 0


def test_canonical_future_changes_do_not_change_earlier_accounting(canonical_stores):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    first = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, release), plan
    )
    expected = first.evaluate(canonical_query(plan), Budget(5)).rows
    raw = release.model_dump(mode="json")
    for session in raw["sessions"][6:]:
        for key in ("open", "high", "low", "close"):
            session["bar"][key] = "99"
    changed = FrozenMarketRelease.model_validate(raw)
    changed_plan = make_plan(
        plan.request.model_copy(update={"input_release_hash": digest(changed)})
    )
    second = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, changed), changed_plan
    )
    assert second.evaluate(canonical_query(changed_plan), Budget(5)).rows == expected


def test_canonical_verified_cash_dividend_record_entitlement(canonical_stores):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    raw = release.model_dump(mode="json")
    raw["corporate_actions"] = [
        dict(
            event_id="cash",
            symbol="600000",
            event_type="CASH_DIVIDEND",
            record_date="2025-01-08",
            ex_date="2025-01-09",
            pay_date="2025-01-09",
            cash_fen=10,
            source_id="synthetic",
            rule_version="synthetic",
        )
    ]
    changed = FrozenMarketRelease.model_validate(raw)
    plan = make_plan(plan.request.model_copy(update={"input_release_hash": digest(changed)}))
    evaluator = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, changed), plan
    )
    result = evaluator.evaluate(canonical_query(plan), Budget(5))
    assert result.rows[3].gross_return == Decimal("0.01100661497560033580181689195206319")


def test_canonical_oos_requires_actual_registered_selection(canonical_stores):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    evaluator = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, release), plan
    )
    with pytest.raises(ValueError, match="seal unavailable"):
        evaluator.evaluate(
            canonical_query(plan, phase="OOS", window=plan.folds[0].test, selection_hash="a" * 64),
            Budget(5),
        )


@pytest.mark.parametrize("fault", ["same_day", "lot", "diagnostic", "source"])
def test_canonical_no_t0_or_unsealed_holdout_escape(canonical_stores, fault):
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    if fault == "same_day":
        raw = release.model_dump(mode="json")
        for kind in ("bar", "benchmark"):
            raw["sessions"][1][kind]["timestamp"] = "2025-01-06T08:00:00Z"
        release = FrozenMarketRelease.model_validate(raw)
        plan = make_plan(
            plan.request.model_copy(
                update={
                    "input_release_hash": digest(release),
                    "timestamps": tuple(s.bar.timestamp for s in release.sessions),
                }
            )
        )
    if fault == "lot":
        candidate = plan.request.candidates[0].model_copy(
            update={"parameters": {"lookback": 1, "shares": 1}}
        )
        plan = make_plan(plan.request.model_copy(update={"candidates": (candidate,)}))
    artifact = register_canonical(state, objects, release)
    if fault == "source":

        class MissingSource:
            artifact_record = state.artifact_record

            def get_snapshot(self, identity):
                return None

        state = MissingSource()
    with pytest.raises(ValueError):
        evaluator = CanonicalMarketEvaluator(state, objects, artifact, plan)
        if fault == "diagnostic":
            evaluator.evaluate(
                canonical_query(plan, phase="DIAGNOSTIC", window=plan.final_holdout), Budget(5)
            )


def test_canonical_signal_does_not_read_execution_close_and_limit_down_blocks_exit(
    canonical_stores,
):
    import json

    from astock.experiments.market_evaluator import CanonicalMarketEvaluator, FrozenMarketRelease

    state, objects = canonical_stores
    release, plan = canonical_fixture()
    raw = release.model_dump(mode="json")
    # An unexpected decline at execution time must not cancel the lagged buy.
    for key in ("open", "high", "low", "close"):
        raw["sessions"][2]["bar"][key] = "9"
    raw["sessions"][3]["limit_down"] = "13"
    changed = FrozenMarketRelease.model_validate(raw)
    plan = make_plan(plan.request.model_copy(update={"input_release_hash": digest(changed)}))
    evaluator = CanonicalMarketEvaluator(
        state, objects, register_canonical(state, objects, changed), plan
    )
    result = evaluator.evaluate(canonical_query(plan), Budget(5))
    assert result.rows[2].turnover > 0
    assert result.rows[3].turnover == 0
    record = state.artifact_record(result.execution_artifact_ids[0])
    execution = json.loads(objects.get_bytes(record["object_hash"]))
    assert execution["fills"][0]["price_fen"] == 901
