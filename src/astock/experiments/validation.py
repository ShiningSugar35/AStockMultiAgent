"""Bounded, chronological strategy experiments; never an execution engine.

Evaluator implementations are trusted host code, never request-supplied Python.
Registered evidence proves provenance and bindings, not absence of lookahead in
its producer. Fixed-fill stress is an accounting sensitivity, not a new replay.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from time import process_time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from astock.core.artifact_reading import decode_registered_artifact
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas.paper import ReplayExecutionReport
from astock.shadow.statistics import maximum_drawdown, mean

VERSION = "strategy-validation-v1"
Hash = str


def digest(value: Any) -> Hash:
    """Hash all fields, including parameter names that core.content_hash omits."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return sha256_bytes(canonical_json_bytes(value))


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Policy(Frozen):
    version: Literal["strategy-validation-policy-v1"] = "strategy-validation-policy-v1"
    max_request_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_artifact_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    max_bars: int = Field(default=10000, ge=20, le=100000)
    max_candidates: int = Field(default=16, ge=1, le=128)
    max_folds: int = Field(default=8, ge=1, le=32)
    max_stresses: int = Field(default=9, ge=1, le=25)
    max_parameter_bytes: int = Field(default=4096, ge=2, le=65536)
    max_work_units: int = Field(default=1000000, ge=1, le=20000000)
    max_cpu_seconds: float = Field(default=30, gt=0, le=300)
    max_cached_evidence: int = Field(default=8, ge=0, le=32, strict=True)


class Candidate(Frozen):
    candidate_id: str = Field(min_length=1, max_length=64)
    strategy_version: str = Field(min_length=1, max_length=128)
    parameters: dict[str, JsonValue]


class Stress(Frozen):
    stress_id: str = Field(min_length=1, max_length=64)
    fee_multiplier: Decimal = Field(default=Decimal("1"), ge=1, le=10)
    extra_slippage_bps: Decimal = Field(default=Decimal("0"), ge=0, le=1000)


class Request(Frozen):
    schema_version: Literal["strategy-experiment-request-v1"] = "strategy-experiment-request-v1"
    study_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    executor_version: str = Field(min_length=1, max_length=128)
    input_release_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark_id: str = Field(min_length=1, max_length=128)
    timestamps: tuple[datetime, ...]
    # Optional actual label information ends. Empty retains the explicit fixed horizon.
    label_end_times: tuple[datetime, ...] = ()
    candidates: tuple[Candidate, ...]
    stresses: tuple[Stress, ...] = (Stress(stress_id="base"),)
    min_train_bars: int = Field(ge=1)
    test_bars: int = Field(ge=1)
    step_bars: int = Field(ge=1)
    fold_count: int = Field(ge=1)
    holdout_start: int = Field(ge=1)
    label_horizon_bars: int = Field(ge=0)
    purge_bars: int = Field(ge=0)
    embargo_bars: int = Field(ge=0)
    selection_metric: Literal["MEAN_NET_EXCESS_RETURN"] = "MEAN_NET_EXCESS_RETURN"
    hypothesis: str | None = Field(default=None, min_length=1, max_length=4096)
    analysis_timing: Literal[
        "UNSPECIFIED", "DECLARED_BEFORE_ANALYSIS", "POST_HOC"
    ] = "UNSPECIFIED"
    stability_score_tolerance: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("hypothesis")
    @classmethod
    def nonblank_hypothesis(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("hypothesis must be nonblank or explicitly unspecified")
        return value

    @field_validator("timestamps", "label_end_times")
    @classmethod
    def utc_times(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(v.tzinfo is None or v.utcoffset() is None for v in values):
            raise ValueError("timestamps require explicit timezone")
        normalized = tuple(v.astimezone(UTC) for v in values)
        return normalized

    @model_validator(mode="after")
    def identities(self) -> Request:
        if any(a >= b for a, b in zip(self.timestamps, self.timestamps[1:], strict=False)):
            raise ValueError("timestamps must be strictly increasing")
        if self.label_end_times and (
            len(self.label_end_times) != len(self.timestamps)
            or any(
                end < start
                for start, end in zip(self.timestamps, self.label_end_times, strict=True)
            )
        ):
            raise ValueError("label intervals must align and end at or after their observations")
        ids = [c.candidate_id for c in self.candidates]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("candidate identities must be nonempty and unique")
        if len({digest(c.parameters) for c in self.candidates}) != len(self.candidates):
            # Two separately versioned strategies can legitimately share parameters.
            if len(
                {
                    digest({"version": c.strategy_version, "parameters": c.parameters})
                    for c in self.candidates
                }
            ) != len(self.candidates):
                raise ValueError("duplicate strategy/parameter trial")
        stress_ids = [s.stress_id for s in self.stresses]
        if not stress_ids or len(stress_ids) != len(set(stress_ids)):
            raise ValueError("stress identities must be nonempty and unique")
        base = self.stresses[0]
        if base.fee_multiplier != 1 or base.extra_slippage_bps != 0:
            raise ValueError("first stress must be the unchanged execution cost basis")
        if self.step_bars < self.test_bars:
            raise ValueError("overlapping test windows are unsupported")
        if not self.holdout_start < len(self.timestamps):
            raise ValueError("final holdout must be nonempty")
        # Also rejects NaN nested inside parameter JSON values.
        canonical_json_bytes(self.model_dump(mode="json"))
        return self


class Window(Frozen):
    start: int = Field(ge=0)
    stop: int = Field(gt=0)

    @model_validator(mode="after")
    def nonempty(self) -> Window:
        if self.start >= self.stop:
            raise ValueError("empty or reversed window")
        return self

    @property
    def size(self) -> int:
        return self.stop - self.start


class Fold(Frozen):
    number: int
    train: Window
    test: Window


class Plan(Frozen):
    schema_version: Literal["strategy-experiment-plan-v1"] = "strategy-experiment-plan-v1"
    implementation_version: str = VERSION
    policy: Policy
    request: Request
    folds: tuple[Fold, ...]
    final_train: Window
    final_holdout: Window
    work_units: int


def make_plan(request: Request, policy: Policy | None = None) -> Plan:
    policy = policy or Policy()
    # Revalidate copies: frozen Pydantic models can still contain mutable JSON maps.
    request = Request.model_validate_json(request.model_dump_json())
    if len(canonical_json_bytes(request.model_dump(mode="json"))) > policy.max_request_bytes:
        raise ValueError("request exceeds policy byte budget")
    if len(request.timestamps) > policy.max_bars:
        raise ValueError("bar budget exceeded")
    if len(request.candidates) > policy.max_candidates or request.fold_count > policy.max_folds:
        raise ValueError("candidate or fold budget exceeded")
    if len(request.stresses) > policy.max_stresses:
        raise ValueError("stress budget exceeded")
    if any(
        len(canonical_json_bytes(c.parameters)) > policy.max_parameter_bytes
        for c in request.candidates
    ):
        raise ValueError("parameter budget exceeded")
    gap = request.label_horizon_bars + request.purge_bars + request.embargo_bars
    def training_stop(stop: int, test_start: int) -> int:
        if not request.label_end_times:
            return stop
        boundary = request.timestamps[test_start]
        # Retain a chronological prefix; never fit a scaler/selector on a label
        # whose information interval reaches the evaluation period.
        crossing = [i for i in range(stop) if request.label_end_times[i] >= boundary]
        result = min(crossing, default=stop)
        if result < request.min_train_bars:
            raise ValueError("actual label overlap leaves insufficient training history")
        return result

    folds: list[Fold] = []
    for number in range(request.fold_count):
        start = request.min_train_bars + gap + number * request.step_bars
        stop = start + request.test_bars
        if stop > request.holdout_start - gap:
            raise ValueError("fold or its label/purge/embargo buffer reaches final holdout")
        if request.label_end_times and any(
            request.label_end_times[i] >= request.timestamps[request.holdout_start]
            for i in range(start, stop)
        ):
            raise ValueError("OOS label information reaches the final holdout")
        folds.append(
            Fold(
                number=number + 1,
                train=Window(start=0, stop=training_stop(start - gap, start)),
                test=Window(start=start, stop=stop),
            )
        )
    final_train = Window(
        start=0, stop=training_stop(request.holdout_start - gap, request.holdout_start)
    )
    # A conservative operation bound: every row/candidate/stress plus selected tests.
    rows = sum(f.train.size for f in folds) + final_train.size
    tests = sum(f.test.size for f in folds) + len(request.timestamps) - request.holdout_start
    units = (rows * len(request.candidates) + tests) * len(request.stresses)
    if units > policy.max_work_units:
        raise ValueError("CPU work-unit budget exceeded")
    return Plan(
        policy=policy,
        request=request,
        folds=tuple(folds),
        final_train=final_train,
        final_holdout=Window(start=request.holdout_start, stop=len(request.timestamps)),
        work_units=units,
    )


class Query(Frozen):
    schema_version: Literal["strategy-evaluation-query-v1"] = "strategy-evaluation-query-v1"
    plan_hash: Hash
    executor_version: str
    input_release_hash: Hash
    candidate: Candidate
    benchmark_id: str
    phase: Literal["TRAIN", "OOS", "FINAL_TRAIN", "FINAL_HOLDOUT", "DIAGNOSTIC"]
    fold_number: int
    window: Window
    selection_hash: Hash | None = None


class Row(Frozen):
    period_end: datetime
    gross_return: Decimal = Field(gt=-1)
    fee_return: Decimal = Field(ge=0)
    slippage_return: Decimal = Field(ge=0)
    turnover: Decimal = Field(ge=0)
    benchmark_return: Decimal = Field(gt=-1)

    @field_validator("period_end")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("period end requires timezone")
        return value.astimezone(UTC)


class Evidence(Frozen):
    schema_version: Literal["strategy-evaluation-evidence-v1"] = "strategy-evaluation-evidence-v1"
    query_hash: Hash
    producer_version: str
    input_release_hash: Hash
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    # Referenced registered artifacts can be replay reports or other approved
    # deterministic executor outputs. This wrapper adds the missing P&L contract.
    execution_artifact_ids: tuple[str, ...] = Field(min_length=1)
    rows: tuple[Row, ...] = Field(min_length=1)
    accounting_basis: Literal["PERIOD_START_NAV_GROSS_MINUS_FEES_MINUS_SLIPPAGE"]
    boundary_policy: Literal["INDEPENDENT_WINDOW_NO_UNACCOUNTED_CARRY"]


class Budget:
    """Cooperative budget; host evaluator must check within expensive loops."""

    def __init__(self, seconds: float) -> None:
        self.deadline = process_time() + seconds

    def check(self) -> None:
        if process_time() > self.deadline:
            raise ValueError("CPU time budget exhausted")


class Evaluator(Protocol):
    def evaluate(self, query: Query, budget: Budget) -> Evidence:
        """Evaluate only the supplied half-open window; never choose parameters.

        TRAIN must not inspect later bars/features/labels. OOS/FINAL_HOLDOUT
        require selection_hash. The coordinator cannot sandbox host Python.
        """
        ...


class TrialRecorder(Protocol):
    """Synchronous immutable observations; failures never authorize a retry."""

    def started(self, query: Query) -> None: ...

    def succeeded(self, query: Query, trial: dict[str, Any]) -> None: ...

    def failed(self, query: Query, stage: str, error: Exception) -> None: ...


class Selection(Frozen):
    phase: Literal["OOS", "FINAL_HOLDOUT"]
    fold_number: int
    candidate: Candidate
    train_window: Window
    training_evidence_hashes: tuple[Hash, ...]
    training_score: Decimal


class Commitment(Frozen):
    schema_version: Literal["strategy-selection-commitment-v1"] = "strategy-selection-commitment-v1"
    plan_hash: Hash
    study_id: str
    run_id: str
    holdout_identity: Hash
    selections: tuple[Selection, ...]


class Seal(Protocol):
    def __call__(self, commitment: Commitment) -> str:
        """Atomically claim study/run/holdout and register signed frozen selection.

        Reject prior consumption or conflicting plans BEFORE returning a receipt.
        Existing state/artifact infrastructure owns cross-session enforcement.
        No account, order or fill writes are permitted.
        """
        ...


def aligned(evidence: Evidence, query: Query, plan: Plan) -> None:
    if evidence.query_hash != digest(query):
        raise ValueError("evidence/query binding mismatch")
    if (
        evidence.producer_version != query.executor_version
        or evidence.input_release_hash != query.input_release_hash
    ):
        raise ValueError("executor/input binding mismatch")
    w = query.window
    if len(evidence.rows) != w.size:
        raise ValueError("missing or surplus benchmark/strategy observations")
    for index, row in enumerate(evidence.rows, start=w.start):
        if row.period_end != plan.request.timestamps[index]:
            raise ValueError("benchmark/strategy time alignment mismatch")


def metrics(evidence: Evidence, stress: Stress) -> dict[str, Any]:
    """Reconstruct net per-period results from a common period-start NAV basis."""
    with localcontext() as context:
        context.prec = 34
        net = [
            r.gross_return
            - r.fee_return * stress.fee_multiplier
            - r.slippage_return
            - r.turnover * stress.extra_slippage_bps / Decimal(10000)
            for r in evidence.rows
        ]
        if any(r < -1 for r in net):
            raise ValueError("stress implies loss exceeding period-start NAV")
        excess = [n - r.benchmark_return for n, r in zip(net, evidence.rows, strict=True)]
        score = mean(excess)
        assert score is not None
        nav = Decimal(1)
        benchmark_nav = Decimal(1)
        for n, row in zip(net, evidence.rows, strict=True):
            nav *= 1 + n
            benchmark_nav *= 1 + row.benchmark_return
        positive_fraction = Decimal(sum(x > 0 for x in excess)) / Decimal(len(excess))
        return {
            "stress_id": stress.stress_id,
            "sample_count": len(net),
            "mean_net_excess_return": score,
            "net_compounded_return": nav - 1,
            "benchmark_compounded_return": benchmark_nav - 1,
            "maximum_drawdown": maximum_drawdown(net),
            "positive_excess_period_fraction": positive_fraction,
            "confidence_interval": None,
            "interval_note": "USE_CLUSTERED_PROSPECTIVE_INFERENCE_FOR_DEPENDENT_RETURNS",
            "inference_label": "DESCRIPTIVE_DEPENDENT_PERIODS_NO_ADMISSION",
            "stress_basis": "FIXED_EXECUTIONS_ACCOUNTING_SENSITIVITY",
            "turnover_sum": sum((row.turnover for row in evidence.rows), Decimal(0)),
            "turnover_mean": mean([row.turnover for row in evidence.rows]),
            "turnover_maximum": max(row.turnover for row in evidence.rows),
            "turnover_basis": "SUM_OF_TRADED_NOTIONAL_OVER_EACH_PERIOD_START_NAV",
            "fee_return_sum": sum(
                (row.fee_return * stress.fee_multiplier for row in evidence.rows), Decimal(0),
            ),
            "slippage_return_sum": sum(
                (row.slippage_return + row.turnover * stress.extra_slippage_bps / Decimal(10000)
                 for row in evidence.rows), Decimal(0),
            ),
            "cost_summary_basis": "SUM_OF_PERIOD_NAV_FRACTIONS_NOT_A_CASH_AMOUNT",
        }


def _numeric_parameters(candidate: Candidate) -> dict[str, Decimal] | None:
    parameters = candidate.parameters
    if not parameters or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in parameters.values()
    ):
        return None
    return {key: Decimal(str(value)) for key, value in parameters.items()}


def _parameter_neighbors(
    candidates: tuple[Candidate, ...], points: dict[str, dict[str, Decimal]],
) -> dict[str, set[str]]:
    """Observed adjacent grid points only; never interpolate untested parameters."""
    neighbors: dict[str, set[str]] = {item.candidate_id: set() for item in candidates}
    axes = sorted(points[candidates[0].candidate_id])
    for axis in axes:
        slices: dict[tuple[tuple[str, Decimal], ...], list[tuple[Decimal, str]]] = {}
        for identity, point in points.items():
            fixed = tuple((key, point[key]) for key in axes if key != axis)
            slices.setdefault(fixed, []).append((point[axis], identity))
        for values in slices.values():
            ordered = sorted(values)
            for left, right in zip(ordered, ordered[1:], strict=False):
                neighbors[left[1]].add(right[1])
                neighbors[right[1]].add(left[1])
    return neighbors


def _observed_region(
    winner: str, scores: dict[str, Decimal], neighbors: dict[str, set[str]], tolerance: Decimal,
) -> list[str]:
    eligible = {identity for identity, score in scores.items()
                if scores[winner] - score <= tolerance}
    visited: set[str] = set()
    pending = [winner]
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        pending.extend((neighbors[identity] & eligible) - visited)
    return sorted(visited)


def training_parameter_stability(
    plan: Plan, commitment: Commitment, trials: list[dict[str, Any]],
) -> dict[str, Any]:
    """A training-only descriptive grid; never evidence of out-of-sample robustness."""
    candidates = plan.request.candidates
    result: dict[str, Any] = {
        "status": "UNKNOWN", "reason_code": None,
        "basis": "OBSERVED_ADJACENT_TRAINING_PARAMETER_GRID",
        "score_tolerance": plan.request.stability_score_tolerance,
        "market_robustness_claim": False, "interpolation_allowed": False, "windows": [],
    }
    parsed = {item.candidate_id: _numeric_parameters(item) for item in candidates}
    if len(candidates) < 2 or any(point is None for point in parsed.values()):
        result["reason_code"] = "INSUFFICIENT_COMPARABLE_NUMERIC_PARAMETER_POINTS"
        return result
    points = {key: point for key, point in parsed.items() if point is not None}
    if (
        len({item.strategy_version for item in candidates}) != 1
        or len({tuple(sorted(point)) for point in points.values()}) != 1
    ):
        result["reason_code"] = "HETEROGENEOUS_STRATEGY_OR_PARAMETER_DIMENSIONS"
        return result
    dimension_count = len(next(iter(points.values())))
    if len(points) * dimension_count * dimension_count > plan.policy.max_work_units:
        result["reason_code"] = "PARAMETER_GRID_WORK_BUDGET"
        return result
    if len({tuple(sorted(point.items())) for point in points.values()}) != len(points):
        result["reason_code"] = "DUPLICATE_NUMERIC_PARAMETER_POINT"
        return result
    neighbors = _parameter_neighbors(candidates, points)
    tolerance = plan.request.stability_score_tolerance
    for selection in commitment.selections:
        phase = "FINAL_TRAIN" if selection.phase == "FINAL_HOLDOUT" else "TRAIN"
        matches = [trial for trial in trials if (
            trial["query"]["phase"] == phase
            and trial["query"]["fold_number"] == selection.fold_number
            and trial["query"]["window"] == selection.train_window.model_dump(mode="json")
        )]
        scores = {
            trial["query"]["candidate"]["candidate_id"]:
                Decimal(str(trial["metrics"][0]["mean_net_excess_return"]))
            for trial in matches
        }
        if len(matches) != len(candidates) or set(scores) != set(points):
            raise ValueError("training stability requires every frozen candidate exactly once")
        winner = selection.candidate.candidate_id
        adjacent = sorted(neighbors[winner])
        observed = [winner, *adjacent]
        region = (
            _observed_region(winner, scores, neighbors, tolerance) if tolerance is not None else []
        )
        result["windows"].append({
            "phase": phase, "fold_number": selection.fold_number,
            "train_window": selection.train_window.model_dump(mode="json"),
            "selected_candidate_id": winner, "neighbor_candidate_ids": adjacent,
            "neighbor_score_differences": {
                key: scores[key] - scores[winner] for key in adjacent
            },
            "observed_score_min": min(scores[key] for key in observed),
            "observed_score_max": max(scores[key] for key in observed),
            "stable_observed_candidate_ids": region if adjacent and tolerance is not None else None,
            "region_reason_code": (
                "NO_OBSERVED_ADJACENT_PARAMETER" if not adjacent else
                "TOLERANCE_NOT_PREREGISTERED" if tolerance is None else None
            ),
        })
    result["status"] = "DESCRIPTIVE_TRAINING_ONLY"
    return result



def select_training_result(
    request: Request, phase: str, number: int, window: Window,
    target: Literal["OOS", "FINAL_HOLDOUT"], trials: list[dict[str, Any]],
) -> Selection:
    matches = [trial for trial in trials if (
        trial["query"]["phase"] == phase and trial["query"]["fold_number"] == number
        and trial["query"]["window"] == window.model_dump(mode="json")
    )]
    by_id = {trial["query"]["candidate"]["candidate_id"]: trial for trial in matches}
    candidates = sorted(request.candidates, key=lambda item: item.candidate_id)
    if len(matches) != len(candidates) or set(by_id) != {item.candidate_id for item in candidates}:
        raise ValueError("selection requires every frozen training candidate exactly once")
    for item in candidates:
        if by_id[item.candidate_id]["query"]["candidate"] != item.model_dump(mode="json"):
            raise ValueError("recorded training candidate parameters mismatch")
    scores = {
        key: Decimal(str(trial["metrics"][0]["mean_net_excess_return"]))
        for key, trial in by_id.items()
    }
    winner = min(candidates, key=lambda item: (-scores[item.candidate_id], item.candidate_id))
    return Selection(
        phase=target, fold_number=number, candidate=winner, train_window=window,
        training_evidence_hashes=tuple(
            by_id[item.candidate_id]["evidence_hash"] for item in candidates
        ),
        training_score=scores[winner.candidate_id],
    )


def build_experiment_report(
    plan: Plan, commitment: Commitment, receipt: str, trials: list[dict[str, Any]],
) -> dict[str, Any]:
    """One report builder for live execution and explicitly requested recorded recovery."""
    selections = []
    for selection in commitment.selections:
        phase = "FINAL_TRAIN" if selection.phase == "FINAL_HOLDOUT" else "TRAIN"
        expected = select_training_result(
            plan.request, phase, selection.fold_number, selection.train_window,
            selection.phase, trials,
        )
        if digest(expected) != digest(selection):
            raise ValueError("registered selection differs from recorded training results")
        selections.append({
            "phase": phase, "fold_number": selection.fold_number,
            "selected_candidate_id": selection.candidate.candidate_id,
            "nonselected_candidate_ids": sorted(
                candidate.candidate_id for candidate in plan.request.candidates
                if candidate.candidate_id != selection.candidate.candidate_id
            ),
            "rule": "MAX_MEAN_NET_EXCESS_RETURN_THEN_LEXICAL_CANDIDATE_ID",
        })
    report = {
        "schema_version": "strategy-experiment-report-v1",
        "authority": "RESEARCH_ONLY", "implementation_version": VERSION,
        "plan_hash": digest(plan), "policy_hash": digest(plan.policy),
        "input_release_hash": plan.request.input_release_hash,
        "hypothesis": plan.request.hypothesis,
        "hypothesis_status": "PROVIDED" if plan.request.hypothesis is not None else "UNSPECIFIED",
        "analysis_timing": plan.request.analysis_timing,
        "timing_basis": "CALLER_DECLARATION_NOT_PROOF_OF_UNTOUCHED_ECONOMIC_DATA",
        "commitment": commitment.model_dump(mode="json"), "commitment_hash": digest(commitment),
        "selection_receipt": receipt, "trials": trials, "selection_outcomes": selections,
        "parameter_stability": training_parameter_stability(plan, commitment, trials),
        "trust_boundary": "HOST_EXECUTOR_AND_REGISTERED_EVIDENCE_NOT_LOOKAHEAD_PROOF",
        "statistical_authority": "DESCRIPTIVE_ONLY_USE_EXISTING_PROSPECTIVE_GOVERNANCE",
        "paper_ledger_write_allowed": False, "recommendation_authority": False,
    }
    report["report_hash"] = digest(report)
    if len(canonical_json_bytes(report)) > plan.policy.max_artifact_bytes:
        raise ValueError("experiment report exceeds artifact budget")
    return report



class Coordinator:
    """One attempt per instance, including failed attempts; no implicit retry."""

    def __init__(
        self, plan: Plan, evaluator: Evaluator, seal: Seal,
        *, recorder: TrialRecorder | None = None,
    ) -> None:
        rebuilt = make_plan(plan.request, plan.policy)
        if digest(rebuilt) != digest(plan):
            raise ValueError("plan is not canonical")
        self._plan_json = plan.model_dump_json()
        self.evaluator = evaluator
        self.seal = seal
        self.recorder = recorder
        self._used = False

    def run(self) -> dict[str, Any]:
        if self._used:
            raise ValueError("run already consumed")
        self._used = True
        plan = Plan.model_validate_json(self._plan_json)
        request = plan.request
        plan_hash = digest(plan)
        budget = Budget(plan.policy.max_cpu_seconds)
        trials: list[dict[str, Any]] = []
        selections: list[Selection] = []

        def evaluate(
            phase: Any,
            number: int,
            window: Window,
            candidate: Candidate,
            selection_hash: str | None = None,
        ) -> tuple[Evidence, Decimal]:
            budget.check()
            query = Query(
                plan_hash=plan_hash,
                executor_version=request.executor_version,
                input_release_hash=request.input_release_hash,
                candidate=candidate,
                benchmark_id=request.benchmark_id,
                phase=phase,
                fold_number=number,
                window=window,
                selection_hash=selection_hash,
            )
            if self.recorder is not None:
                self.recorder.started(query)
            stage = "EVALUATE"
            try:
                # A fresh copy prevents a host evaluator mutating frozen parameter maps.
                evidence = self.evaluator.evaluate(
                    Query.model_validate_json(query.model_dump_json()), budget
                )
                stage = "VALIDATE_EVIDENCE"
                budget.check()
                evidence = Evidence.model_validate_json(evidence.model_dump_json())
                aligned(evidence, query, plan)
                stage = "CALCULATE_METRICS"
                values = [metrics(evidence, stress) for stress in request.stresses]
                budget.check()
            except Exception as exc:
                if self.recorder is not None:
                    self.recorder.failed(query, stage, exc)
                raise
            trial = {
                "query": query.model_dump(mode="json"),
                "query_hash": digest(query),
                "evidence_hash": digest(evidence),
                "source_snapshot_ids": evidence.source_snapshot_ids,
                "execution_artifact_ids": evidence.execution_artifact_ids,
                "metrics": values,
                "evaluation_status": "SUCCEEDED",
                "score_classification": (
                    "POSITIVE_EXCESS" if values[0]["mean_net_excess_return"] > 0
                    else "NON_POSITIVE_EXCESS"
                ),
            }
            if self.recorder is not None:
                self.recorder.succeeded(query, trial)
            trials.append(trial)
            return evidence, values[0]["mean_net_excess_return"]

        training: list[
            tuple[int, Window, Literal["TRAIN", "FINAL_TRAIN"], Literal["OOS", "FINAL_HOLDOUT"]]
        ] = [(f.number, f.train, "TRAIN", "OOS") for f in plan.folds]
        training.append((0, plan.final_train, "FINAL_TRAIN", "FINAL_HOLDOUT"))
        # All selections, including final selection, are fixed before ANY test read.
        # Scores never use OOS results or stress scenarios to change the winner.
        for number, window, phase, target in training:
            for candidate in sorted(request.candidates, key=lambda c: c.candidate_id):
                evaluate(phase, number, window, candidate)
            selections.append(
                select_training_result(request, phase, number, window, target, trials)
            )
        commitment = Commitment(
            plan_hash=plan_hash,
            study_id=request.study_id,
            run_id=request.run_id,
            holdout_identity=digest(
                {
                    "input_release_hash": request.input_release_hash,
                    "benchmark_id": request.benchmark_id,
                    "period_ends": request.timestamps[plan.final_holdout.start :],
                }
            ),
            selections=tuple(selections),
        )
        budget.check()
        receipt = self.seal(commitment)
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError("selection commitment was not registered")
        budget.check()
        for selection in selections:
            window = (
                plan.final_holdout
                if selection.phase == "FINAL_HOLDOUT"
                else plan.folds[selection.fold_number - 1].test
            )
            evaluate(
                selection.phase,
                selection.fold_number,
                window,
                selection.candidate,
                digest(selection),
            )
        report = build_experiment_report(plan, commitment, receipt, trials)
        budget.check()
        return report



def diagnose(plan: Plan, query: Query, evidence: Evidence) -> dict[str, Any]:
    """Result-window diagnostics explicitly carry no training/OOS assertion."""
    if query.phase != "DIAGNOSTIC" or query.selection_hash is not None:
        raise ValueError("diagnostic query required")
    if query.plan_hash != digest(plan) or query.window.stop > len(plan.request.timestamps):
        raise ValueError("diagnostic plan/window mismatch")
    request = plan.request
    if (
        query.input_release_hash != request.input_release_hash
        or query.executor_version != request.executor_version
        or query.benchmark_id != request.benchmark_id
        or not any(digest(query.candidate) == digest(item) for item in request.candidates)
    ):
        raise ValueError("diagnostic query is not bound to the selected plan")
    aligned(evidence, query, plan)
    return {
        "authority": "RESEARCH_ONLY",
        "classification": "RESULT_WINDOW_DIAGNOSTIC",
        "evidence_hash": digest(evidence),
        "oos_assertion": False,
        "metrics": [metrics(evidence, s) for s in plan.request.stresses],
    }


class RegistryReader(Protocol):
    def artifact_record(self, artifact_id: str) -> Any: ...


class ObjectReader(Protocol):
    def get_bytes(self, sha256: str, /) -> bytes: ...


class RegisteredEvidenceEvaluator:
    """Read registered wrapper evidence and verify referenced replay reports.

    resolve is host-owned and receives a window-scoped query. It may invoke an
    existing executor or resolve a registered artifact; requests supply no code,
    module paths, executable expressions, SQL, or arbitrary filesystem paths.
    ReplayExecutionReport alone has no P&L and is therefore never treated as a
    return stream. The wrapper must be produced by an approved host normalizer.
    """

    def __init__(
        self,
        state: RegistryReader,
        objects: ObjectReader,
        resolve: Callable[[Query, Budget], str],
        policy: Policy,
        allowed_execution_types: frozenset[str] = frozenset({"ReplayExecutionReport"}),
    ) -> None:
        self.state = state
        self.objects = objects
        self.resolve = resolve
        self.policy = policy
        self.allowed_execution_types = allowed_execution_types
        # Bounded LRU: repeated artifacts avoid decode; long studies never retain all evidence.
        self._decoded: OrderedDict[tuple[str, str, str], Any] = OrderedDict()

    def _read(self, artifact_id: str, kind: str, model: Any) -> Any:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != kind:
            raise ValueError("registered artifact type mismatch")
        object_hash = str(record["object_hash"])
        key = (artifact_id, object_hash, str(record["schema_version"]))
        if key in self._decoded:
            self._decoded.move_to_end(key)
            return self._decoded[key]
        raw = self.objects.get_bytes(object_hash)
        if len(raw) > self.policy.max_artifact_bytes or sha256_bytes(raw) != object_hash:
            raise ValueError("registered object size/hash mismatch")
        value = decode_registered_artifact(
            raw, model, registry_schema_version=str(record["schema_version"])
        )
        if self.policy.max_cached_evidence:
            self._decoded[key] = value
            while len(self._decoded) > self.policy.max_cached_evidence:
                self._decoded.popitem(last=False)
        return value

    def evaluate(self, query: Query, budget: Budget) -> Evidence:
        budget.check()
        # A registered wrapper is not a trusted producer of accounting facts.
        # Do not silently upgrade it into an executable train/OOS experiment.
        if query.phase != "DIAGNOSTIC":
            raise ValueError("admitted execution return normalizer is not implemented")
        artifact_id = self.resolve(query, budget)
        evidence = self._read(artifact_id, "StrategyEvaluationEvidence", Evidence)
        if evidence.query_hash != digest(query) or len(evidence.rows) > self.policy.max_bars:
            raise ValueError("registered evidence query/size mismatch")
        if len(evidence.execution_artifact_ids) > self.policy.max_bars:
            raise ValueError("execution reference budget exceeded")
        for execution_id in dict.fromkeys(evidence.execution_artifact_ids):
            budget.check()
            record = self.state.artifact_record(execution_id)
            if record is None or str(record["type"]) not in self.allowed_execution_types:
                raise ValueError("executor artifact type is not host-admitted")
            kind = str(record["type"])
            if kind == "ReplayExecutionReport":
                report = self._read(execution_id, kind, ReplayExecutionReport)
                if report.checkpoint is None or report.checkpoint.missing_bars:
                    raise ValueError("replay evidence lacks complete checkpoint")
            else:
                # No guessed shadow/prospective schema. Host integration must add
                # an explicit versioned decoder before admitting another type.
                raise ValueError("execution artifact decoder unavailable")
        budget.check()
        return evidence
