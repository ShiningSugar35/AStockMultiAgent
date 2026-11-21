"""Atomic selection freeze using the existing Artifact Registry, never a second store.

Two unique registry identities prevent replay of either study/run or the final
holdout. BEGIN IMMEDIATE makes both claims atomic across processes. A prior
claim is *always* rejected, even if its selection bytes happen to be identical.
This is not a backtest producer or permission to use unverified returns.
"""

from __future__ import annotations

import json
from typing import Final

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore, utc_now_text
from astock.experiments.validation import Commitment, Plan, digest, make_plan

_CLAIM_VERSION: Final = "strategy-selection-claim-v1"


class RegisteredSelectionSeal:
    """One-time, two-key durable selection claim for a frozen experiment plan."""

    def __init__(self, state: StateStore, objects: ObjectStore, plan: Plan) -> None:
        if digest(make_plan(plan.request, plan.policy)) != digest(plan):
            raise ValueError("selection seal requires a canonical experiment plan")
        self._state = state
        self._objects = objects
        self._plan = Plan.model_validate_json(plan.model_dump_json())
        self._plan_hash = digest(self._plan)

    def __call__(self, commitment: Commitment) -> str:
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

        study_run_id = "StrategySelectionStudyRun:" + digest(
            {"study_id": request.study_id, "run_id": request.run_id}
        )
        holdout_id = "StrategySelectionHoldout:" + expected_holdout
        frozen = {
            "schema_version": _CLAIM_VERSION,
            "commitment": commitment.model_dump(mode="json"),
            "commitment_hash": digest(commitment),
        }
        raw = canonical_json_bytes(frozen)
        if len(raw) > self._plan.policy.max_artifact_bytes:
            raise ValueError("selection commitment exceeds artifact budget")

        # Immutable object can be safely prepared before locking SQLite; failed
        # claims never become registered and are collected by the existing GC.
        obj = self._objects.put_bytes(raw)
        now = utc_now_text()
        input_hashes = json.dumps(
            [self._plan_hash, request.input_release_hash, expected_holdout],
            separators=(",", ":"),
        )
        with self._state.transaction() as connection:
            already = connection.execute(
                "SELECT artifact_id FROM artifact_registry WHERE artifact_id IN (?,?) LIMIT 1",
                (study_run_id, holdout_id),
            ).fetchone()
            if already is not None:
                raise ValueError("study/run or final holdout has already been claimed")
            connection.executemany(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    (identity, kind, _CLAIM_VERSION, obj.sha256, input_hashes, now)
                    for identity, kind in (
                        (study_run_id, "StrategySelectionStudyRun"),
                        (holdout_id, "StrategySelectionHoldout"),
                    )
                ),
            )
        # An authenticated, one-time source of the selection, not approval of
        # return streams, performance metrics, or a production investment action.
        return study_run_id


__all__ = ["RegisteredSelectionSeal"]
