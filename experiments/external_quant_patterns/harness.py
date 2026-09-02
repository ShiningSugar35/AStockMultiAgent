"""Deterministic, offline benchmark harness for external quant patterns.

Runs the Qlib Recorder, RD-Agent hypothesis loop, LEAN event ordering, and
RQAlpha robustness benchmarks against AStockMultiAgent's existing patterns
and emits an aggregate report with an adopt/adapt/watch/reject decision per
candidate.  Everything is local, deterministic, and fully removable.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from experiments.external_quant_patterns.fixtures.sample_data import (
    make_astock_events,
    make_lean_events,
    make_qlib_recorder_runs,
    make_rdagent_hypotheses,
    make_rdagent_results,
    make_robustness_backtest_results,
)
from experiments.external_quant_patterns.patterns.lean_event_ordering import (
    OrderingComparisonResult,
    compare_event_orderings,
)
from experiments.external_quant_patterns.patterns.qlib_recorder import (
    PatternComparisonResult,
    compare_recorder_patterns,
)
from experiments.external_quant_patterns.patterns.rdagent_hypothesis import (
    LoopComparisonResult,
    compare_hypothesis_loops,
)
from experiments.external_quant_patterns.patterns.rqalpha_robustness import (
    RobustnessComparisonResult,
    compare_robustness_reports,
)

EXPERIMENT_ID = "external-quant-patterns-e03"
SCHEMA_VERSION = "external-quant-patterns-report-v1"


@dataclass(frozen=True)
class PatternDecision:
    """A single candidate decision within the benchmark report."""
    candidate: str
    recommendation: str
    marginal_value: str
    duplicate_scope: str
    environment_notes: str
    exit_notes: str
    object_hash: str = field(default="")


@dataclass(frozen=True)
class PatternBenchmarkReport:
    """Aggregate report for all four external candidate benchmarks."""
    schema_version: str
    report_id: str
    experiment_id: str
    generated_at: str
    fixture_hash: str
    production_module_changed: bool
    network_requests: int
    external_model_calls: int
    dependencies_borrowed: bool
    decisions: tuple[PatternDecision, ...]
    benchmark_metrics: dict[str, Any]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "experiment_id": self.experiment_id,
            "generated_at": self.generated_at,
            "fixture_hash": self.fixture_hash,
            "production_module_changed": self.production_module_changed,
            "network_requests": self.network_requests,
            "external_model_calls": self.external_model_calls,
            "dependencies_borrowed": self.dependencies_borrowed,
            "decisions": [
                {
                    "candidate": d.candidate,
                    "recommendation": d.recommendation,
                    "object_hash": d.object_hash,
                    "marginal_value": d.marginal_value,
                    "duplicate_scope": d.duplicate_scope,
                    "environment_notes": d.environment_notes,
                    "exit_notes": d.exit_notes,
                }
                for d in self.decisions
            ],
            "benchmark_metrics": self.benchmark_metrics,
            "summary": self.summary,
        }

    def decision_by_candidate(self, candidate: str) -> PatternDecision:
        for decision in self.decisions:
            if decision.candidate == candidate:
                return decision
        raise KeyError(candidate)


def fixture_fingerprint() -> dict[str, Any]:
    """Deterministic assertion-relevant fixture facts with quality semantics."""
    qlib_runs = make_qlib_recorder_runs()
    assertions = {
        "recorder_run_count": len(qlib_runs),
        "first_ic": qlib_runs[0].metrics["ic"] if qlib_runs else None,
        "hypothesis_count": len(make_rdagent_hypotheses()),
        "result_count": len(make_rdagent_results()),
        "lean_event_count": len(make_lean_events()),
        "astock_event_count": len(make_astock_events()),
        "backtest_variant_count": len(make_robustness_backtest_results()),
        "event_types_lean": [e.event_type for e in make_lean_events()],
        "event_types_astock": [e.event_type for e in make_astock_events()],
    }
    return assertions


def fixture_hash() -> str:
    payload = json.dumps(fixture_fingerprint(), sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def _decision(
    candidate: str,
    recommendation: str,
    marginal_value: str,
    duplicate_scope: str,
    environment_notes: str,
    exit_notes: str,
) -> PatternDecision:
    payload = json.dumps(
        {
            "candidate": candidate,
            "recommendation": recommendation,
            "marginal_value": marginal_value,
            "duplicate_scope": duplicate_scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PatternDecision(
        candidate=candidate,
        recommendation=recommendation,
        marginal_value=marginal_value,
        duplicate_scope=duplicate_scope,
        environment_notes=environment_notes,
        exit_notes=exit_notes,
        object_hash=sha256_bytes(payload.encode("utf-8")),
    )


def _benchmark_metrics(
    recorder: PatternComparisonResult,
    loop: LoopComparisonResult,
    ordering: OrderingComparisonResult,
    robustness: RobustnessComparisonResult,
) -> dict[str, Any]:
    """Extract the numeric benchmark outputs into a JSON-safe, hashable dict."""
    return {
        "qlib_recorder": {
            "qlib_entry_count": recorder.qlib_entry_count,
            "astock_entry_count": recorder.astock_entry_count,
            "unique_param_count": recorder.unique_param_count,
            "unique_artifact_count": recorder.unique_artifact_count,
        },
        "rdagent_hypothesis": {
            "rdagent_iterations": loop.rdagent_iterations,
            "astock_prospective_entries": loop.astock_prospective_entries,
            "rdagent_convergence": round(loop.rdagent_convergence, 4),
            "astock_formal_events": loop.astock_formal_events,
        },
        "lean_event_ordering": {
            "lean_events": ordering.lean_events,
            "astock_events": ordering.astock_events,
            "lean_violates_astock": ordering.lean_violates_astock,
        },
        "rqalpha_robustness": {
            "variant_count": robustness.variant_count,
            "sharpe_mean": robustness.sharpe_mean,
            "sharpe_std": robustness.sharpe_std,
            "max_drawdown_worst": robustness.max_drawdown_worst,
        },
    }


def run_benchmarks() -> tuple[PatternBenchmarkReport, dict[str, Any]]:
    """Run all four benchmarks deterministically and produce an aggregate report.

    Returns a tuple of (report, benchmark_outputs).  The benchmark outputs map
    candidate -> raw comparison result for downstream assertions.
    """
    recorder: PatternComparisonResult = compare_recorder_patterns(
        make_qlib_recorder_runs()
    )
    loop: LoopComparisonResult = compare_hypothesis_loops(
        make_rdagent_hypotheses(),
        make_rdagent_results(),
    )
    ordering: OrderingComparisonResult = compare_event_orderings(
        make_lean_events(),
        make_astock_events(),
    )
    robustness: RobustnessComparisonResult = compare_robustness_reports(
        make_robustness_backtest_results()
    )

    decisions = (
        _decision(
            candidate="qlib",
            recommendation=recorder.recommendation,
            marginal_value=recorder.marginal_value,
            duplicate_scope=(
                "AStock already records observation/checkpoint/performance and "
                "uses an immutable ObjectStore; Qlib Recorder adds experiment-level "
                "run grouping only."
            ),
            environment_notes=(
                "No Qlib code imported; simulation is a minimal local model of "
                "Recorder file semantics. Removable by deleting this directory."
            ),
            exit_notes=(
                "Zero default dependencies added; delete "
                "experiments/external_quant_patterns to exit."
            ),
        ),
        _decision(
            candidate="rdagent",
            recommendation=loop.recommendation,
            marginal_value=loop.marginal_value,
            duplicate_scope=(
                "AStock uses prospective/shadow governance (PBO, DSR, walk-forward); "
                "RD-Agent's automated factor iteration loop is intentionally "
                "NOT_ADMITTED in Phase 8."
            ),
            environment_notes=(
                "RD-Agent LLM loop not executed; simulation uses the deterministic "
                "fixture hypotheses and results only."
            ),
            exit_notes=(
                "No rdagent/langchain dependency; no API keys. Removable by "
                "deleting this directory."
            ),
        ),
        _decision(
            candidate="lean",
            recommendation=ordering.recommendation,
            marginal_value=ordering.marginal_value,
            duplicate_scope=(
                "The simplified LEAN-inspired fixture omits AStock governance stages; "
                "this local result does not characterize the full LEAN engine. "
                "Robustness ideas remain separate model-risk candidates."
            ),
            environment_notes=(
                "LEAN engine not installed; only the ordering model is simulated "
                "with deterministic events."
            ),
            exit_notes=(
                "No .NET/LEAN artifacts created; removable by deleting this directory."
            ),
        ),
        _decision(
            candidate="rqalpha",
            recommendation=robustness.recommendation,
            marginal_value=robustness.marginal_value,
            duplicate_scope=(
                "AStock already has block bootstrap and deflated Sharpe; RQAlpha's "
                "parameter grid adds no new standalone robustness capability."
            ),
            environment_notes=(
                "RQAlpha is not installed; the robustness model uses five deterministic "
                "local variants only. The repository license includes material commercial-use "
                "restrictions, so runtime/code adoption is rejected without authorization."
            ),
            exit_notes=(
                "No rqalpha package or module imported; removable by deleting "
                "this directory."
            ),
        ),
    )

    summary = (
        "The fixed-fixture comparisons support only pattern-level decisions: Qlib-style "
        "experiment grouping is an ADAPT pattern; the modeled RD-Agent-style hypothesis "
        "loop remains SHADOW only; selected walk-forward/parameter-perturbation ideas "
        "remain WATCH items. The simplified event-order fixture does not justify replacing "
        "the project's classification/protocol/ledger chain, but it is not a benchmark of "
        "the full LEAN or RQAlpha engines. The modeled robustness grid adds no demonstrated "
        "marginal value beyond existing block-bootstrap/deflated-Sharpe governance."
    )

    report = PatternBenchmarkReport(
        schema_version=SCHEMA_VERSION,
        report_id="0" * 64,
        experiment_id=EXPERIMENT_ID,
        generated_at="2026-09-02T00:00:00Z",
        fixture_hash=fixture_hash(),
        production_module_changed=False,
        network_requests=0,
        external_model_calls=0,
        dependencies_borrowed=False,
        decisions=decisions,
        benchmark_metrics=_benchmark_metrics(recorder, loop, ordering, robustness),
        summary=summary,
    )
    report_id = compute_report_id(report)
    report = PatternBenchmarkReport(
        schema_version=report.schema_version,
        report_id=report_id,
        experiment_id=report.experiment_id,
        generated_at=report.generated_at,
        fixture_hash=report.fixture_hash,
        production_module_changed=report.production_module_changed,
        network_requests=report.network_requests,
        external_model_calls=report.external_model_calls,
        dependencies_borrowed=report.dependencies_borrowed,
        decisions=report.decisions,
        benchmark_metrics=report.benchmark_metrics,
        summary=report.summary,
    )

    benchmark_outputs = {
        "qlib_recorder": recorder,
        "rdagent_hypothesis": loop,
        "lean_event_ordering": ordering,
        "rqalpha_robustness": robustness,
    }
    return report, benchmark_outputs


def _canonical_payload(report: PatternBenchmarkReport) -> dict[str, Any]:
    """Deterministic hash payload: excludes report_id and the runtime timestamp.

    Leaving generated_at out keeps report_id a pure content identity that can be
    recomputed and verified from any report instance regardless of when it ran.
    """
    payload = report.to_dict()
    payload.pop("report_id", None)
    payload.pop("generated_at", None)
    return payload


def compute_report_id(report: PatternBenchmarkReport) -> str:
    """Recompute the content-based report id so callers can verify it."""
    return sha256_bytes(canonical_json_bytes(_canonical_payload(report)))


def _experiment_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or Path(__file__).resolve().parent).resolve()


def _write_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def generate_artifacts(
    base_dir: Path | None = None,
    *,
    generated_at: str | None = None,
) -> PatternBenchmarkReport:
    """Run the benchmark suite and write a report plus a summary document."""
    root = _experiment_dir(base_dir)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    report, _ = run_benchmarks()
    timestamp = generated_at or datetime.now(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stamped = PatternBenchmarkReport(
        schema_version=report.schema_version,
        report_id=report.report_id,
        experiment_id=report.experiment_id,
        generated_at=timestamp,
        fixture_hash=report.fixture_hash,
        production_module_changed=report.production_module_changed,
        network_requests=report.network_requests,
        external_model_calls=report.external_model_calls,
        dependencies_borrowed=report.dependencies_borrowed,
        decisions=report.decisions,
        benchmark_metrics=report.benchmark_metrics,
        summary=report.summary,
    )
    _write_json(output_dir / "benchmark_report.json", stamped.to_dict())
    (output_dir / "benchmark_report.md").write_text(
        _render_markdown_report(stamped),
        encoding="utf-8",
        newline="\n",
    )
    _write_json(
        output_dir / "fixture_fingerprint.json",
        fixture_fingerprint(),
    )
    return stamped


def _render_markdown_report(report: PatternBenchmarkReport) -> str:
    lines = [
        "# E-03 外部量化模式对拍报告",
        "",
        f"- 实验：`{report.experiment_id}`",
        f"- 报告 ID：`{report.report_id}`",
        f"- 数据生成时点：`{report.generated_at}`",
        f"- fixture hash：`{report.fixture_hash}`",
        "- 生产模块改动：无",
        "- 网络请求：0",
        "- 外部模型调用：0",
        f"- 默认依赖接入：{'是' if report.dependencies_borrowed else '否'}",
        "",
        "## 候选逐项裁决",
        "",
        "| 候选 | 裁决 | 环境/退出 |",
        "|---|---|---|",
    ]
    for decision in report.decisions:
        lines.append(
            "| "
            f"{decision.candidate} | {decision.recommendation} | "
            f"{decision.environment_notes} |"
        )
    lines.extend(
        [
            "",
            "## 基准数值（deterministic benchmark metrics）",
            "",
            "| 候选 | 指标 | 数值 |",
            "|---|---|---|",
        ]
    )
    for candidate, metrics in report.benchmark_metrics.items():
        for metric, value in metrics.items():
            lines.append(f"| {candidate} | {metric} | {value} |")
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            report.summary,
            "",
            "## 边界",
            "",
            "本实验没有改动生产 Provider、Evidence、Committee、组合权重或 "
            "paper ledger；未把 Qlib、RD-Agent、LEAN、RQAlpha 加入 pyproject "
            "默认依赖或生产运行路径；所有结果来自固定本地 fixtures，可删除 "
            "`experiments/external_quant_patterns/` 完整退出。",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the offline external quant pattern benchmark suite."
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Optional fixed timestamp for determinism (ISO 8601 UTC).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    report = generate_artifacts(arguments.base_dir, generated_at=arguments.generated_at)
    print(
        json.dumps(
            {
                "experiment_id": report.experiment_id,
                "report_id": report.report_id,
                "produced_at": report.generated_at,
                "decision_count": len(report.decisions),
                "production_module_changed": report.production_module_changed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())