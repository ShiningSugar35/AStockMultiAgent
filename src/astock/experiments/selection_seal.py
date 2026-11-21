"""Atomic selection freeze using the existing Artifact Registry, never a second store.

Registry identities prevent replay of the study/run, exact final holdout and
its market-family dates. BEGIN IMMEDIATE makes all claims atomic across processes. A prior
claim is *always* rejected, even if its selection bytes happen to be identical.
This is not a backtest producer or permission to use unverified returns.
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any, Final
from zoneinfo import ZoneInfo

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore, utc_now_text
from astock.experiments.validation import (
    Budget,
    Commitment,
    Plan,
    Query,
    build_experiment_report,
    digest,
    make_plan,
)

_CLAIM_VERSION: Final = "strategy-selection-claim-v2"


class RegisteredSelectionSeal:
    """One-time durable study, holdout and family-session selection claim plan."""

    def __init__(self, state: StateStore, objects: ObjectStore, plan: Plan) -> None:
        if digest(make_plan(plan.request, plan.policy)) != digest(plan):
            raise ValueError("selection seal requires a canonical experiment plan")
        self._state = state
        self._objects = objects
        self._plan = Plan.model_validate_json(plan.model_dump_json())
        self._plan_hash = digest(self._plan)

    def validate_commitment(self, commitment: Commitment) -> Commitment:
        # Freeze mutable JSON inside nested parameters and refuse altered shapes.
        commitment = Commitment.model_validate_json(commitment.model_dump_json())
        request = self._plan.request
        expected_holdout = digest(
            {
                "input_release_hash": request.input_release_hash,
                "benchmark_id": request.benchmark_id,
                "period_ends": request.timestamps[self._plan.final_holdout.start :],
            }
        )
        if (
            commitment.plan_hash != self._plan_hash
            or commitment.study_id != request.study_id
            or commitment.run_id != request.run_id
            or commitment.holdout_identity != expected_holdout
        ):
            raise ValueError("selection commitment plan or holdout binding mismatch")

        expected = [
            ("OOS", fold.number, fold.train)
            for fold in self._plan.folds
        ] + [("FINAL_HOLDOUT", 0, self._plan.final_train)]
        if len(commitment.selections) != len(expected):
            raise ValueError("selection commitment is missing an experimental fold")
        allowed_candidates = {digest(c) for c in request.candidates}
        for selection, (phase, fold_number, train_window) in zip(
            commitment.selections, expected, strict=True
        ):
            if (
                selection.phase != phase
                or selection.fold_number != fold_number
                or selection.train_window != train_window
                or digest(selection.candidate) not in allowed_candidates
                or len(selection.training_evidence_hashes) != len(request.candidates)
                or any(
                    len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
                    for h in selection.training_evidence_hashes
                )
            ):
                raise ValueError("selection commitment has an invalid frozen winner")

        return commitment

    def claim_spec(self, commitment: Commitment) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        request = self._plan.request
        expected_holdout = commitment.holdout_identity
        study_run_id = "StrategySelectionStudyRun:" + digest(
            {"study_id": request.study_id, "run_id": request.run_id}
        )
        holdout_id = "StrategySelectionHoldout:" + expected_holdout
        # The economic family is fixed by this A-share daily experiment service,
        # never by caller aliases, release hashes, benchmark, or ticker subsets.
        # Claim each Shanghai session so overlapping/rebucketed holdouts cannot
        # masquerade as untouched data under a different run name.
        family_dates = sorted({
            stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for stamp in request.timestamps[self._plan.final_holdout.start :]
        })
        family_ids = [
            "StrategySelectionFamilySession:" + digest({"family": "CN_EQUITIES_DAILY", "date": day})
            for day in family_dates
        ]
        claims = [
            (study_run_id, "StrategySelectionStudyRun"),
            (holdout_id, "StrategySelectionHoldout"),
            *((identity, "StrategySelectionFamilySession") for identity in family_ids),
        ]
        frozen = {
            "economic_family": "CN_EQUITIES_DAILY",
            "holdout_session_dates": family_dates,
            "schema_version": _CLAIM_VERSION,
            "commitment": commitment.model_dump(mode="json"),
            "commitment_hash": digest(commitment),
        }
        return claims, frozen

    def __call__(self, commitment: Commitment) -> str:
        commitment = self.validate_commitment(commitment)
        request = self._plan.request
        claims, frozen = self.claim_spec(commitment)
        raw = canonical_json_bytes(frozen)
        if len(raw) > self._plan.policy.max_artifact_bytes:
            raise ValueError("selection commitment exceeds artifact budget")

        # Immutable object can be safely prepared before locking SQLite; failed
        # claims never become registered and are collected by the existing GC.
        obj = self._objects.put_bytes(raw)
        now = utc_now_text()
        input_hashes = json.dumps(
            [self._plan_hash, request.input_release_hash, commitment.holdout_identity],
            separators=(",", ":"),
        )
        with self._state.transaction() as connection:
            # v1 retained only a hash of the dates. Its economic overlap cannot
            # be recovered by guessing from a renamed run/release. Historical
            # claims remain immutable until their actual dates are migrated.
            legacy = connection.execute(
                "SELECT 1 FROM artifact_registry WHERE type='StrategySelectionHoldout' "
                "AND schema_version<>? LIMIT 1", (_CLAIM_VERSION,),
            ).fetchone()
            if legacy is not None:
                raise ValueError("legacy holdout family dates require verified migration")
            for offset in range(0, len(claims), 400):
                identities = tuple(identity for identity, _ in claims[offset : offset + 400])
                placeholders = ",".join("?" for _ in identities)
                already = connection.execute(
                    "SELECT artifact_id FROM artifact_registry "
                    f"WHERE artifact_id IN ({placeholders}) LIMIT 1",
                    identities,
                ).fetchone()
                if already is not None:
                    raise ValueError("study/run or economic final holdout has already been claimed")
            connection.executemany(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    (identity, kind, _CLAIM_VERSION, obj.sha256, input_hashes, now)
                    for identity, kind in claims
                ),
            )
        # Selection is a provenance claim, never investment or account authority.
        return claims[0][0]

    def read_commitment(self) -> Commitment | None:
        """Read the exact existing atomic claim, including every family-session member."""
        request = self._plan.request
        identity = "StrategySelectionStudyRun:" + digest({
            "study_id": request.study_id, "run_id": request.run_id,
        })
        record = self._state.artifact_record(identity)
        if record is None:
            return None
        object_hash = str(record["object_hash"])
        path = self._objects.path_for(object_hash)
        if path.stat().st_size > self._plan.policy.max_artifact_bytes:
            raise ValueError("selection commitment exceeds artifact budget")
        raw = self._objects.get_bytes(object_hash)
        frozen = json.loads(raw)
        if not isinstance(frozen, dict):
            raise ValueError("invalid registered selection shape")
        commitment = self.validate_commitment(Commitment.model_validate(frozen.get("commitment")))
        claims, expected = self.claim_spec(commitment)
        if canonical_json_bytes(expected) != raw:
            raise ValueError("registered selection payload mismatch")
        inputs = [self._plan_hash, request.input_release_hash, commitment.holdout_identity]
        with closing(self._state.connect()) as connection:
            for offset in range(0, len(claims), 400):
                batch = claims[offset : offset + 400]
                placeholders = ",".join("?" for _ in batch)
                records = connection.execute(
                    "SELECT artifact_id,type,schema_version,object_hash,input_hashes_json "
                    f"FROM artifact_registry WHERE artifact_id IN ({placeholders})",
                    tuple(identity for identity, _ in batch),
                ).fetchall()
                indexed = {row["artifact_id"]: row for row in records}
                for artifact_id, kind in batch:
                    row = indexed.get(artifact_id)
                    if row is None or (
                        row["type"] != kind or row["schema_version"] != _CLAIM_VERSION
                        or row["object_hash"] != object_hash
                        or json.loads(row["input_hashes_json"]) != inputs
                    ):
                        raise ValueError("registered selection claim is missing or inconsistent")
        return commitment



_RECORD_VERSION: Final = "strategy-experiment-record-v1"


def _safe_failure(stage: str, error: Exception) -> dict[str, str]:
    """Only fixed classifications; never exception messages, paths or environment values."""
    if isinstance(error, OSError):
        category = "IO_ERROR"
    elif isinstance(error, (ValueError, TypeError)):
        category = "INPUT_OR_CONTRACT_ERROR"
    else:
        category = "EXECUTOR_ERROR"
    return {"stage": stage, "error_type": category, "reason_code": category}


class ExperimentRunUnavailable(ValueError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("registered experiment requires explicit review or recorded recovery")
        self.report = report


class RegisteredExperimentRun:
    """Bounded append-only run/attempt artifacts in the existing registry.

    The run reservation is exclusive. Successful replay is a verified read.
    Incomplete recovery can only reuse a complete set of already persisted results;
    it never evaluates another query or claims another holdout.
    """

    def __init__(
        self, state: StateStore, objects: ObjectStore, plan: Plan, market_release_artifact_id: str,
    ) -> None:
        if digest(make_plan(plan.request, plan.policy)) != digest(plan):
            raise ValueError("experiment requires a canonical plan")
        self.state, self.objects = state, objects
        self.plan = Plan.model_validate_json(plan.model_dump_json())
        self.plan_hash = digest(self.plan)
        self.key = digest({"study_id": plan.request.study_id, "run_id": plan.request.run_id})
        self.artifact_id = "StrategyExperimentRun:" + self.key
        self.result_id = "StrategyExperimentResult:" + self.key
        self.seal = RegisteredSelectionSeal(state, objects, self.plan)
        self._owned = False
        self.manifest = {
            "schema_version": _RECORD_VERSION, "record_kind": "StrategyExperimentRun",
            "run_artifact_id": self.artifact_id, "plan": self.plan.model_dump(mode="json"),
            "plan_hash": self.plan_hash, "market_release_artifact_id": market_release_artifact_id,
            "hypothesis_status": (
                "PROVIDED" if plan.request.hypothesis is not None else "UNSPECIFIED"
            ),
            "registration_scope": "BEFORE_THIS_EXECUTION_NOT_PROOF_OF_PREVIOUS_DATA_EXPOSURE",
            "input_hashes": [self.plan_hash, plan.request.input_release_hash],
        }
        self.manifest_hash = digest(self.manifest)
        self._training = self._make_training_queries()
        self._training_hashes = {digest(query) for query in self._training}

    def _read(self, artifact_id: str, kind: str) -> dict[str, Any] | None:
        row = self.state.artifact_record(artifact_id)
        if row is None:
            return None
        if row["type"] != kind or row["schema_version"] != _RECORD_VERSION:
            raise ValueError("experiment artifact registry type mismatch")
        path = self.objects.path_for(str(row["object_hash"]))
        if path.stat().st_size > self.plan.policy.max_artifact_bytes:
            raise ValueError("experiment artifact exceeds byte budget")
        payload = json.loads(self.objects.get_bytes(str(row["object_hash"])))
        if not isinstance(payload, dict) or (
            payload.get("schema_version") != _RECORD_VERSION
            or payload.get("record_kind") != kind
            or payload.get("run_artifact_id") != self.artifact_id
            or payload.get("input_hashes") != row["input_hashes"]
            or (kind != "StrategyExperimentRun"
                and payload.get("input_hashes") != [self.manifest_hash])
        ):
            raise ValueError("experiment artifact payload/lineage mismatch")
        return payload

    def _reserve(
        self, artifact_id: str, kind: str, object_hash: str, inputs: list[str],
    ) -> bool:
        with self.state.transaction() as connection:
            present = connection.execute(
                "SELECT 1 FROM artifact_registry WHERE artifact_id=?", (artifact_id,),
            ).fetchone()
            if present is not None:
                return False
            if kind == "StrategyExperimentRun":
                legacy = connection.execute(
                    "SELECT 1 FROM artifact_registry WHERE artifact_id=?",
                    ("StrategySelectionStudyRun:" + self.key,),
                ).fetchone()
                if legacy is not None:
                    raise ExperimentRunUnavailable(
                        self._unavailable("LEGACY_SELECTION_CONSUMED", {}),
                    )
            connection.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                (artifact_id, kind, _RECORD_VERSION, object_hash,
                 json.dumps(inputs, separators=(",", ":")), utc_now_text()),
            )
        return True

    def _put(
        self, artifact_id: str, kind: str, fields: dict[str, Any], *, exclusive: bool = False,
    ) -> None:
        payload = {
            "schema_version": _RECORD_VERSION, "record_kind": kind,
            "run_artifact_id": self.artifact_id, "input_hashes": [self.manifest_hash],
            **fields,
        }
        raw = canonical_json_bytes(payload)
        if len(raw) > self.plan.policy.max_artifact_bytes:
            raise ValueError("experiment artifact exceeds byte budget")
        reference = self.objects.put_bytes(raw)
        if exclusive:
            if not self._reserve(artifact_id, kind, reference.sha256, payload["input_hashes"]):
                raise ValueError(
                    "experiment query already started; automatic reevaluation forbidden"
                )
        else:
            self.state.register_artifact(
                artifact_id=artifact_id, artifact_type=kind, schema_version=_RECORD_VERSION,
                object_hash=reference.sha256, input_hashes=payload["input_hashes"],
            )

    def begin(self, *, recover_recorded: bool = False) -> dict[str, Any] | None:
        existing = self._read(self.artifact_id, "StrategyExperimentRun")
        if existing is not None:
            return self._replay(existing, recover_recorded=recover_recorded)
        raw = canonical_json_bytes(self.manifest)
        if len(raw) > self.plan.policy.max_artifact_bytes:
            raise ValueError("experiment plan exceeds artifact budget")
        reference = self.objects.put_bytes(raw)
        self._owned = self._reserve(
            self.artifact_id, "StrategyExperimentRun", reference.sha256,
            self.manifest["input_hashes"],
        )
        if self._owned:
            return None
        existing = self._read(self.artifact_id, "StrategyExperimentRun")
        if existing is None:
            raise ValueError("experiment reservation disappeared")
        return self._replay(existing, recover_recorded=recover_recorded)

    def _replay(self, manifest: dict[str, Any], *, recover_recorded: bool) -> dict[str, Any]:
        if canonical_json_bytes(manifest) != canonical_json_bytes(self.manifest):
            raise ValueError("experiment run identity is bound to a different plan or release")
        result = self._read(self.result_id, "StrategyExperimentResult")
        if result is not None:
            if result.get("status") == "SUCCEEDED":
                report = result.get("report")
                if not isinstance(report, dict) or (
                    report.get("plan_hash") != self.plan_hash
                    or report.get("experiment_run_artifact_id") != self.artifact_id
                    or report.get("report_hash") != digest({
                        key: value for key, value in report.items() if key != "report_hash"
                    })
                ):
                    raise ValueError("registered experiment report binding mismatch")
                self._verify_completed_report(report)
                return report
            raise ExperimentRunUnavailable(self._unavailable("FAILED", result))
        if recover_recorded:
            report = self._recover()
            if report is not None:
                return report
        raise ExperimentRunUnavailable(self._unavailable("INCOMPLETE", self._inventory()))

    def _query_id(self, query: Query, kind: str) -> str:
        return kind + ":" + self.key + ":" + digest(query)

    def _query_contract(self, query: Query) -> None:
        request = self.plan.request
        if (
            query.plan_hash != self.plan_hash or query.executor_version != request.executor_version
            or query.input_release_hash != request.input_release_hash
            or query.benchmark_id != request.benchmark_id
            or not any(digest(query.candidate) == digest(item) for item in request.candidates)
        ):
            raise ValueError("attempt is not bound to the frozen experiment")
        if digest(query) in self._training_hashes:
            return
        expected = self._expected_queries(self.seal.read_commitment())
        if digest(query) not in {digest(item) for item in expected}:
            raise ValueError("attempt phase, window or selection identity mismatch")

    def started(self, query: Query) -> None:
        if not self._owned:
            raise ValueError("only the reserved experiment owner may evaluate")
        self._query_contract(query)
        self._put(self._query_id(query, "StrategyExperimentAttempt"), "StrategyExperimentAttempt", {
            "status": "STARTED", "query": query.model_dump(mode="json"),
            "query_hash": digest(query),
        }, exclusive=True)

    def succeeded(self, query: Query, trial: dict[str, Any]) -> None:
        if trial.get("query_hash") != digest(query) or (
            trial.get("query") != query.model_dump(mode="json")
        ):
            raise ValueError("successful attempt query binding mismatch")
        self._finish_attempt(query, {"status": "SUCCEEDED", "trial": trial})

    def failed(self, query: Query, stage: str, error: Exception) -> None:
        if stage not in {"EVALUATE", "VALIDATE_EVIDENCE", "CALCULATE_METRICS"}:
            raise ValueError("unknown experiment failure stage")
        self._finish_attempt(query, {"status": "FAILED", "failure": _safe_failure(stage, error)})

    def _finish_attempt(self, query: Query, fields: dict[str, Any]) -> None:
        if not self._owned or self._read(
            self._query_id(query, "StrategyExperimentAttempt"), "StrategyExperimentAttempt",
        ) is None:
            raise ValueError("attempt completion has no owned start")
        self._put(
            self._query_id(query, "StrategyExperimentAttemptResult"),
            "StrategyExperimentAttemptResult",
            {"query_hash": digest(query), **fields},
        )

    def _make_training_queries(self) -> list[Query]:
        request = self.plan.request
        training = [("TRAIN", fold.number, fold.train) for fold in self.plan.folds]
        training.append(("FINAL_TRAIN", 0, self.plan.final_train))
        common = {
            "plan_hash": self.plan_hash, "executor_version": request.executor_version,
            "input_release_hash": request.input_release_hash, "benchmark_id": request.benchmark_id,
        }
        queries = [
            Query.model_validate({
                **common, "phase": phase, "fold_number": number, "window": window,
                "candidate": candidate,
            })
            for phase, number, window in training
            for candidate in sorted(request.candidates, key=lambda item: item.candidate_id)
        ]
        return queries

    def _expected_queries(self, commitment: Commitment | None) -> list[Query]:
        queries = list(self._training)
        request = self.plan.request
        if commitment is not None:
            for selection in commitment.selections:
                window = (
                    self.plan.final_holdout if selection.phase == "FINAL_HOLDOUT"
                    else self.plan.folds[selection.fold_number - 1].test
                )
                queries.append(Query(
                    plan_hash=self.plan_hash, executor_version=request.executor_version,
                    input_release_hash=request.input_release_hash,
                    benchmark_id=request.benchmark_id,
                    phase=selection.phase, fold_number=selection.fold_number,
                    window=window, candidate=selection.candidate, selection_hash=digest(selection),
                ))
        return queries

    def _inventory(self) -> dict[str, Any]:
        budget = Budget(self.plan.policy.max_cpu_seconds)
        commitment = self.seal.read_commitment()
        rows = []
        for query in self._expected_queries(commitment):
            budget.check()
            start_id = self._query_id(query, "StrategyExperimentAttempt")
            result_id = self._query_id(query, "StrategyExperimentAttemptResult")
            started = self._read(start_id, "StrategyExperimentAttempt")
            terminal = self._read(result_id, "StrategyExperimentAttemptResult")
            if started is not None and (
                started.get("query") != query.model_dump(mode="json")
                or started.get("query_hash") != digest(query)
            ):
                raise ValueError("recorded attempt start binding mismatch")
            if terminal is not None and (
                started is None or terminal.get("query_hash") != digest(query)
                or terminal.get("status") not in {"SUCCEEDED", "FAILED"}
            ):
                raise ValueError("recorded attempt terminal binding mismatch")
            rows.append({
                "query_hash": digest(query), "phase": query.phase, "fold_number": query.fold_number,
                "candidate_id": query.candidate.candidate_id,
                "status": terminal["status"] if terminal is not None else (
                    "INCOMPLETE" if started is not None else "NOT_ATTEMPTED"
                ),
                "start_artifact_id": start_id if started is not None else None,
                "result_artifact_id": result_id if terminal is not None else None,
            })
        return {
            "attempts": rows, "selection_sealed": commitment is not None,
            "unselected_oos_slots": 0 if commitment is not None else len(self.plan.folds) + 1,
        }

    def _recorded_trials(
        self, inventory: dict[str, Any],
    ) -> tuple[Commitment | None, list[dict[str, Any]]]:
        commitment = self.seal.read_commitment()
        trials = []
        for item in inventory["attempts"]:
            if item["status"] != "SUCCEEDED":
                continue
            result = self._read(item["result_artifact_id"], "StrategyExperimentAttemptResult")
            if result is None:
                raise ValueError("recorded attempt result disappeared")
            trial = result.get("trial")
            if not isinstance(trial, dict) or trial.get("query_hash") != item["query_hash"]:
                raise ValueError("recorded successful attempt payload mismatch")
            query = Query.model_validate(trial.get("query"))
            if digest(query) != item["query_hash"]:
                raise ValueError("recorded successful query mismatch")
            for identity in trial.get("execution_artifact_ids", []):
                row = self.state.artifact_record(identity)
                if row is None or not self.objects.verify(str(row["object_hash"])):
                    raise ValueError("recorded execution evidence is unavailable")
            trials.append(trial)
        return commitment, trials

    def _recover(self) -> dict[str, Any] | None:
        budget = Budget(self.plan.policy.max_cpu_seconds)
        inventory = self._inventory()
        if not inventory["selection_sealed"] or any(
            item["status"] != "SUCCEEDED" for item in inventory["attempts"]
        ):
            return None
        commitment, trials = self._recorded_trials(inventory)
        if commitment is None:
            return None
        report = build_experiment_report(
            self.plan, commitment, self.seal.claim_spec(commitment)[0][0][0], trials,
        )
        budget.check()
        self._owned = True
        return self.complete(report)

    def _verify_completed_report(self, report: dict[str, Any]) -> None:
        inventory = self._inventory()
        if not inventory["selection_sealed"] or any(
            item["status"] != "SUCCEEDED" for item in inventory["attempts"]
        ):
            raise ValueError("successful experiment requires every sealed attempt result")
        commitment, trials = self._recorded_trials(inventory)
        if commitment is None or (
            report.get("plan_hash") != self.plan_hash
            or canonical_json_bytes(report.get("trials")) != canonical_json_bytes(trials)
            or report.get("commitment") != commitment.model_dump(mode="json")
        ):
            raise ValueError("experiment report differs from recorded attempt lineage")

    def complete(self, report: dict[str, Any]) -> dict[str, Any]:
        if not self._owned:
            raise ValueError("experiment completion has no reserved owner")
        self._verify_completed_report(report)
        report = {
            **report,
            "experiment_run_artifact_id": self.artifact_id,
            "experiment_run_object_hash": self.manifest_hash,
            "experiment_result_artifact_id": self.result_id,
            "execution_model": "LAGGED_SIGNAL_DAILY_CLOSE_MARK_TO_MARKET",
            "market_validation_claim": False,
            "trust_boundary": "HOST_ADMITTED_CANONICAL_RELEASE_AND_LAGGED_DAILY_CLOSE_MODEL",
        }
        report.pop("report_hash", None)
        report["report_hash"] = digest(report)
        self._put(self.result_id, "StrategyExperimentResult", {
            "status": "SUCCEEDED", "report": report,
        })
        self._owned = False
        return json.loads(canonical_json_bytes(report))

    def fail(self, stage: str, error: Exception) -> dict[str, Any]:
        if not self._owned or stage not in {"CONSTRUCT_EVALUATOR", "COORDINATE"}:
            raise ValueError("unknown or unowned run failure stage")
        details = {
            "status": "FAILED", "failure": _safe_failure(stage, error),
            **self._inventory(),
        }
        self._put(self.result_id, "StrategyExperimentResult", details)
        self._owned = False
        return self._unavailable("FAILED", details)

    def _unavailable(self, status: str, details: dict[str, Any]) -> dict[str, Any]:
        if len(canonical_json_bytes(details)) > self.plan.policy.max_artifact_bytes // 2:
            attempts = details.get("attempts", [])
            details = {
                "details_truncated": True,
                "attempt_status_counts": {
                    state: sum(item["status"] == state for item in attempts)
                    for state in ("NOT_ATTEMPTED", "INCOMPLETE", "SUCCEEDED", "FAILED")
                },
                "failure": details.get("failure"),
            }
        return {
            "authority": "RESEARCH_ONLY", "status": "UNAVAILABLE",
            "experiment_status": status, "experiment_run_artifact_id": self.artifact_id,
            "experiment_result_artifact_id": (
                self.result_id if status == "FAILED" else None
            ),
            "details": details,
            "reason_code": (
                "EXPERIMENT_RECORDED_FAILURE" if status == "FAILED" else
                "LEGACY_SELECTION_ALREADY_CONSUMED_NO_RUN_RECORD"
                if status == "LEGACY_SELECTION_CONSUMED" else
                "EXPERIMENT_INCOMPLETE_REQUIRES_EXPLICIT_RECORDED_RECOVERY"
            ),
            "automatic_retry_allowed": False, "holdout_claim_allowed": False,
        }


__all__ = ["ExperimentRunUnavailable", "RegisteredExperimentRun", "RegisteredSelectionSeal"]
