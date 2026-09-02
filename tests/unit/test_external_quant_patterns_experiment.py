from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, replace
from pathlib import Path

from experiments.external_quant_patterns.fixtures.sample_data import (
    make_astock_events,
    make_lean_events,
    make_qlib_recorder_runs,
    make_rdagent_hypotheses,
    make_rdagent_results,
    make_robustness_backtest_results,
)
from experiments.external_quant_patterns.harness import (
    PatternBenchmarkReport,
    compute_report_id,
    fixture_fingerprint,
    fixture_hash,
    generate_artifacts,
    run_benchmarks,
)
from experiments.external_quant_patterns.patterns.lean_event_ordering import (
    compare_event_orderings,
)
from experiments.external_quant_patterns.patterns.qlib_recorder import (
    build_qlib_recorder_entries,
    compare_recorder_patterns,
)
from experiments.external_quant_patterns.patterns.rdagent_hypothesis import (
    build_hypothesis_iterations,
    build_hypothesis_loop_report,
    compare_hypothesis_loops,
)
from experiments.external_quant_patterns.patterns.rqalpha_robustness import (
    build_robustness_report,
    compare_robustness_reports,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ROOT = PROJECT_ROOT / "experiments" / "external_quant_patterns"


def _assert_report_shape(report: PatternBenchmarkReport) -> None:
    assert report.schema_version == "external-quant-patterns-report-v1"
    assert report.production_module_changed is False
    assert report.network_requests == 0
    assert report.external_model_calls == 0
    assert report.dependencies_borrowed is False
    assert len(report.decisions) == 4
    assert re.match(r"^[0-9a-f]{64}$", report.report_id)
    assert report.report_id == compute_report_id(report)


def test_benchmark_report_is_frozen_offline_and_fully_deterministic() -> None:
    first_report, first_outputs = run_benchmarks()
    second_report, second_outputs = run_benchmarks()

    assert {d.candidate for d in first_report.decisions} == {
        "qlib",
        "rdagent",
        "lean",
        "rqalpha",
    }
    assert first_report.report_id == second_report.report_id
    assert first_report.to_dict() == second_report.to_dict()
    assert first_report.benchmark_metrics.keys() == {
        "qlib_recorder",
        "rdagent_hypothesis",
        "lean_event_ordering",
        "rqalpha_robustness",
    }

    assert first_outputs.keys() == second_outputs.keys()
    for candidate, output in first_outputs.items():
        assert asdict(output) == asdict(second_outputs[candidate])


def test_report_id_is_recomputable_and_content_stable_across_timestamps(
    tmp_path: Path,
) -> None:
    report, _ = run_benchmarks()
    _assert_report_shape(report)

    stamped = generate_artifacts(
        tmp_path / "exp",
        generated_at="2099-01-01T00:00:00Z",
    )
    assert stamped.report_id == report.report_id


def test_report_id_identity_changes_when_benchmark_metrics_change() -> None:
    report, _ = run_benchmarks()
    altered_metrics = dict(report.benchmark_metrics)
    altered_metrics["rqalpha_robustness"] = {
        "variant_count": 99,
        "sharpe_mean": 1.0,
        "sharpe_std": 0.5,
        "max_drawdown_worst": 0.1,
    }
    altered = replace(report, benchmark_metrics=altered_metrics)
    assert compute_report_id(altered) != report.report_id
    assert compute_report_id(altered) != compute_report_id(report)


def test_fixture_hash_is_stable_and_reflects_fixture_content() -> None:
    assert re.match(r"^[0-9a-f]{64}$", fixture_hash())
    assert fixture_hash() == fixture_hash()

    summary = fixture_fingerprint()
    assert summary["recorder_run_count"] == 3
    assert summary["hypothesis_count"] == 3
    assert summary["result_count"] == 3
    assert summary["lean_event_count"] == 6
    assert summary["astock_event_count"] == 6
    assert summary["backtest_variant_count"] == 5
    assert summary["event_types_astock"].index("CLASSIFICATION") == 0
    assert summary["event_types_astock"][-1] == "LEDGER_FILL"


def test_qlib_candidate_metrics_are_deterministic_across_fixture_regeneration() -> None:
    first = make_qlib_recorder_runs()
    second = make_qlib_recorder_runs()
    assert [r.metrics["ic"] for r in first] == [r.metrics["ic"] for r in second]
    assert [r.metrics["rank_ic"] for r in first] == [
        r.metrics["rank_ic"] for r in second
    ]
    assert [r.metrics["turnover"] for r in first] == [
        r.metrics["turnover"] for r in second
    ]
    assert all(0.0 < r.metrics["ic"] < 0.1 for r in first)


def test_qlib_recorder_pattern_matches_astock_observation_semantics() -> None:
    result = compare_recorder_patterns(make_qlib_recorder_runs())
    assert result.recommendation == "ADAPT_PATTERN"
    assert result.qlib_entry_count == 21
    assert result.astock_entry_count == 3
    assert result.unique_param_count == 3
    assert result.unique_artifact_count == 6
    assert result.qlib_granularity == "experiment>run>metric>artifact"
    assert result.astock_granularity == "observation>checkpoint>performance"

    entries = build_qlib_recorder_entries(make_qlib_recorder_runs())
    types = [e.entry_type for e in entries]
    assert types.count("experiment") == 3
    assert types.count("run") == 3
    assert types.count("metric") == 9
    assert types.count("artifact") == 6
    for entry in entries:
        assert re.match(r"^[0-9a-f]{64}$", entry.entry_id)


def test_rdagent_hypothesis_loop_is_shadow_only() -> None:
    result = compare_hypothesis_loops(
        make_rdagent_hypotheses(),
        make_rdagent_results(),
    )
    assert result.recommendation == "SHADOW_EXPERIMENT"
    assert result.rdagent_iterations == 3
    assert result.astock_prospective_entries == 2
    assert result.rdagent_convergence > 0
    assert "overfitting" in result.marginal_value

    iterations = build_hypothesis_iterations(
        make_rdagent_hypotheses(),
        make_rdagent_results(),
    )
    loop_report = build_hypothesis_loop_report(iterations)
    assert loop_report.total_iterations == 3
    assert loop_report.passed_count == 2
    assert loop_report.loop_completed is True
    assert loop_report.best_ic == 0.071


def test_lean_ordering_is_strictly_weaker_than_astock() -> None:
    result = compare_event_orderings(
        make_lean_events(),
        make_astock_events(),
    )
    assert result.recommendation == "REJECT_ORDERING"
    assert result.lean_violates_astock is True
    assert "CLASSIFICATION" not in result.lean_ordering
    assert result.astock_ordering.startswith("CLASSIFICATION")
    assert result.astock_ordering.endswith("LEDGER_FILL")


def test_astock_ordering_has_all_governance_stages_in_order() -> None:
    types = [e.event_type for e in make_astock_events()]
    required = [
        "CLASSIFICATION",
        "COMMITTEE_PROTOCOL",
        "CLASSIFIED_PROTOCOL",
        "EXECUTION_PREPARE",
        "EXECUTION_CONFIRM",
        "LEDGER_FILL",
    ]
    assert types == required
    assert {e.symbol for e in make_astock_events()} == {"600938"}


def test_rqalpha_robustness_is_watch_only() -> None:
    result = compare_robustness_reports(make_robustness_backtest_results())
    assert result.recommendation == "WATCH_PATTERN_ONLY"
    assert "deflated Sharpe" in result.marginal_value
    assert result.variant_count == 5
    assert result.sharpe_mean > 0
    assert result.sharpe_std > 0
    assert result.max_drawdown_worst > 0

    results = make_robustness_backtest_results()
    assert len({r.sharpe for r in results}) == 5
    assert len({r.max_drawdown for r in results}) == 5

    report = build_robustness_report(results)
    assert report.run_count == 5
    assert report.max_drawdown_worst == max(r.max_drawdown for r in results)
    param_keys = {tuple(sorted(dict(r.params).items())) for r in results}
    assert tuple(sorted(report.best_params.items())) in param_keys
    assert len({r.run_id for r in report.runs}) == 5


def test_harness_writes_only_removable_output_artifacts(tmp_path: Path) -> None:
    base = tmp_path / "exp"
    base.mkdir()
    report = generate_artifacts(
        base,
        generated_at="2026-09-02T00:00:00Z",
    )
    assert report.report_id == compute_report_id(report)

    written = sorted(p.name for p in (base / "output").iterdir())
    assert written == [
        "benchmark_report.json",
        "benchmark_report.md",
        "fixture_fingerprint.json",
    ]

    payload = json.loads(
        (base / "output" / "benchmark_report.json").read_text(encoding="utf-8")
    )
    assert payload["report_id"] == report.report_id
    assert payload["production_module_changed"] is False
    assert payload["network_requests"] == 0
    assert payload["dependencies_borrowed"] is False
    assert {d["candidate"] for d in payload["decisions"]} == {
        "qlib",
        "rdagent",
        "lean",
        "rqalpha",
    }


def test_experiment_dir_is_self_contained_and_qualification_reports_exist() -> None:
    assert EXPERIMENT_ROOT.exists()
    assert (EXPERIMENT_ROOT / "harness.py").exists()
    for candidate in ("qlib", "rdagent", "lean", "rqalpha"):
        assert (EXPERIMENT_ROOT / "qualification" / f"{candidate}.md").exists()


def test_qualification_reports_cover_license_and_exit_requirements() -> None:
    required_markers = ["## License", "## Exit / Deletion", "## Maintenance"]
    for candidate in ("qlib", "rdagent", "lean", "rqalpha"):
        content = (
            EXPERIMENT_ROOT / "qualification" / f"{candidate}.md"
        ).read_text(encoding="utf-8")
        for marker in required_markers:
            assert marker in content, f"{candidate}.md missing marker {marker}"
        assert "does **not** install" in content


def test_qualification_snapshot_is_explicit_and_reference_only() -> None:
    snapshot = json.loads(
        (EXPERIMENT_ROOT / "qualification" / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["schema_version"] == "external-quant-qualification-snapshot-v1"
    assert snapshot["observed_at"] == "2026-09-02"
    candidates = {item["candidate"]: item for item in snapshot["candidates"]}
    assert candidates.keys() == {"qlib", "rdagent", "lean", "rqalpha"}
    assert candidates["qlib"]["license_class"] == "MIT"
    assert candidates["rdagent"]["license_class"] == "MIT"
    assert candidates["lean"]["license_class"] == "Apache-2.0"
    assert candidates["rqalpha"]["license_class"].startswith("CUSTOM_NONCOMMERCIAL")
    assert all(item["runtime_dependency_added"] is False for item in candidates.values())
    assert all(item["source_code_copied"] is False for item in candidates.values())


def test_external_frameworks_are_not_default_dependencies_or_production_imports() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()
    for package in ("qlib", "rdagent", "rqalpha", "quantconnect"):
        assert re.search(rf"^[^#\n]*{package}[^#\n]*$", pyproject, flags=re.MULTILINE) is None

    forbidden_imports = re.compile(
        r"(?m)^\s*(?:from|import)\s+(?:qlib|rdagent|rqalpha|QuantConnect)(?:\.|\s|$)"
    )
    for source_path in (PROJECT_ROOT / "src").rglob("*.py"):
        assert forbidden_imports.search(source_path.read_text(encoding="utf-8")) is None


def test_generated_experiment_is_fully_removable(tmp_path: Path) -> None:
    base = tmp_path / "isolated-experiment"
    report = generate_artifacts(base, generated_at="2026-09-02T00:00:00Z")
    assert report.production_module_changed is False
    assert base.exists()
    shutil.rmtree(base)
    assert not base.exists()


def test_modeled_results_do_not_claim_full_framework_superiority() -> None:
    report, _ = run_benchmarks()
    text = (
        report.summary
        + " "
        + " ".join(d.marginal_value for d in report.decisions)
    ).casefold()
    assert "strictly weaker" not in text
    assert "battle-tested" not in text
    assert "not a benchmark" in text or "cannot establish" in text