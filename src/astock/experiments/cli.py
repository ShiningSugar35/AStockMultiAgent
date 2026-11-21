"""Bounded experiments, registered canonical-market execution and diagnostics.

Execution requires a host-admitted frozen market release and a durable selection
seal. Neither planning nor execution grants investment or production authority.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field

from astock.core.hashing import canonical_json_bytes
from astock.experiments.validation import (
    Budget,
    Plan,
    Policy,
    Query,
    RegisteredEvidenceEvaluator,
    Request,
    Window,
    diagnose,
    digest,
    make_plan,
)

ROOT = Path(__file__).resolve().parents[3]


def bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input byte budget exceeded")
    return raw


def load_policy(path: str | None = None) -> Policy:
    target = Path(path) if path else ROOT / "configs/strategy_validation.yaml"
    value = yaml.safe_load(bounded_read(target, 65536))
    if not isinstance(value, dict):
        raise ValueError("policy must be a mapping")
    return Policy.model_validate(value)


def load_plan(path: str, policy_path: str | None = None) -> Plan:
    policy = load_policy(policy_path)
    request = Request.model_validate_json(bounded_read(Path(path), policy.max_request_bytes))
    return make_plan(request, policy)


def plan_document(path: str, policy_path: str | None = None) -> dict[str, Any]:
    plan = load_plan(path, policy_path)
    return {
        "authority": "RESEARCH_ONLY",
        "status": "PLAN_ONLY_NO_EVALUATION",
        "plan_hash": digest(plan),
        "plan": plan.model_dump(mode="json"),
        "executable_backtest_available": False,
        "missing_capability": "REGISTERED_CANONICAL_MARKET_RELEASE_AND_SELECTION_SEAL",
    }


def run_document(plan: Plan, state: Any, objects: Any, artifact_id: str) -> dict[str, Any]:
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator
    from astock.experiments.selection_seal import RegisteredSelectionSeal
    from astock.experiments.validation import Coordinator

    evaluator = CanonicalMarketEvaluator(state, objects, artifact_id, plan)
    result = Coordinator(plan, evaluator, RegisteredSelectionSeal(state, objects, plan)).run()
    result["execution_model"] = "LAGGED_SIGNAL_DAILY_CLOSE_MARK_TO_MARKET"
    result["market_validation_claim"] = False
    result["trust_boundary"] = "HOST_ADMITTED_CANONICAL_RELEASE_AND_LAGGED_DAILY_CLOSE_MODEL"
    result.pop("report_hash")
    result["report_hash"] = digest(result)
    return result


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str = Field(min_length=1)
    window: Window
    evidence_artifact_id: str = Field(min_length=1)


def register_commands(app: Any, services: Any, emit: Any) -> None:
    def fail() -> None:
        emit(
            {
                "authority": "RESEARCH_ONLY",
                "status": "UNAVAILABLE",
                "reason_code": "STRATEGY_INPUT_OR_REGISTERED_EVIDENCE_INVALID",
            }
        )
        raise typer.Exit(2)

    @app.command("strategy-validation-run")
    def run_command(request: str, market_release_artifact_id: str) -> None:
        try:
            plan = load_plan(request)
            _, state, objects = services()
            result = run_document(plan, state, objects, market_release_artifact_id)
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError, yaml.YAMLError):
            fail()
            return
        emit(json.loads(canonical_json_bytes(result)))

    @app.command("strategy-validation-plan")
    def plan_command(request: str) -> None:
        try:
            result = plan_document(request)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            fail()
            return
        emit(result)

    @app.command("strategy-validation-diagnose")
    def diagnose_command(request: str, diagnostic_request: str) -> None:
        try:
            plan = load_plan(request)
            arguments = DiagnosticRequest.model_validate_json(
                bounded_read(Path(diagnostic_request), plan.policy.max_request_bytes),
            )
            candidate = next(
                (x for x in plan.request.candidates if x.candidate_id == arguments.candidate_id),
                None,
            )
            if candidate is None:
                raise ValueError("diagnostic candidate is not in the frozen plan")
            query = Query(
                plan_hash=digest(plan),
                executor_version=plan.request.executor_version,
                input_release_hash=plan.request.input_release_hash,
                candidate=candidate,
                benchmark_id=plan.request.benchmark_id,
                phase="DIAGNOSTIC",
                fold_number=0,
                window=arguments.window,
            )
            _, state, objects = services()
            evaluator = RegisteredEvidenceEvaluator(
                state,
                objects,
                lambda query, budget: arguments.evidence_artifact_id,
                plan.policy,
            )
            result = diagnose(
                plan, query, evaluator.evaluate(query, Budget(plan.policy.max_cpu_seconds))
            )
            result.update(
                {
                    "return_producer_verified": False,
                    "warning": "结果窗口描述统计，不是执行级回测或样本外有效性证明。",
                    "persistent_writes": False,
                }
            )
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            fail()
            return
        emit(json.loads(canonical_json_bytes(result)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only strategy experiments")
    parser.add_argument("command", choices=["plan", "run"])
    parser.add_argument("request")
    parser.add_argument("--policy")
    parser.add_argument("--state-db")
    parser.add_argument("--objects-dir")
    parser.add_argument("--market-release")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_document(args.request, args.policy)
        else:
            from astock.core.object_store import ObjectStore
            from astock.core.state import StateStore

            if not args.state_db or not args.objects_dir or not args.market_release:
                raise ValueError("execution requires explicit existing stores and market release")
            database, objects_dir = Path(args.state_db).resolve(), Path(args.objects_dir).resolve()
            if (
                not database.is_relative_to(ROOT)
                or not objects_dir.is_relative_to(ROOT)
                or not database.is_file()
                or not objects_dir.is_dir()
            ):
                raise ValueError("execution stores must already exist inside the project")
            result = run_document(
                load_plan(args.request, args.policy),
                StateStore(database),
                ObjectStore(objects_dir),
                args.market_release,
            )
    except (OSError, ValueError, TypeError, sqlite3.DatabaseError, yaml.YAMLError):
        print(
            json.dumps({"authority": "RESEARCH_ONLY", "error": "INVALID_OR_OVER_BUDGET_REQUEST"}),
            file=sys.stderr,
        )
        return 2
    print(canonical_json_bytes(result).decode("utf-8"))
    return 0
