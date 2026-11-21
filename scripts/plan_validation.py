"""Explain impact-based validation; never infer a PASS or execute tests.

Unknown impact, shared state changes and releases remain conservative. Review the
emitted rationale and add task-specific negative tests before running the existing
run_local_quality.py. This selector is an auditable aid, not a coverage proof.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

if __package__:
    from scripts.validation_inputs import validation_input_closure
else:
    from validation_inputs import validation_input_closure  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or not path.parts:
        raise ValueError("validation paths must stay project-relative")
    return path.as_posix()


def plan_validation(
    root: Path,
    changed_paths: list[str],
    *,
    full_reason: str | None = None,
    shards: int = 1,
) -> dict[str, Any]:
    if not changed_paths or not 1 <= shards <= 32:
        raise ValueError("provide changed paths and 1..32 shards")
    changed = sorted({normalize_path(item) for item in changed_paths})
    config_path = root / "configs" / "validation_impact.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != "validation-impact-v1":
        raise ValueError("unsupported validation impact policy")
    rules = raw.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("empty validation impact policy")
    available = sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))
    selected: set[str] = set()
    explanations: list[dict[str, Any]] = []
    full_reasons: list[str] = [full_reason] if full_reason else []
    for path in changed:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in raw.get("full_suite_paths", [])):
            full_reasons.append(f"shared infrastructure or dependency contract: {path}")
            explanations.append({"path": path, "selection": "FULL"})
            continue
        matched = [
            rule
            for rule in rules
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule["changed"])
        ]
        if not matched:
            full_reasons.append(f"unmapped impact needs explicit review: {path}")
            explanations.append({"path": path, "selection": "UNMAPPED"})
            continue
        if (
            path.startswith("tests/")
            and path not in available
            and all(rule["name"] == "changed-tests" for rule in matched)
        ):
            full_reasons.append(f"shared test helper or fixture needs dependency review: {path}")
        targets: set[str] = set()
        for rule in matched:
            for pattern in rule["tests"]:
                found = {p for p in available if fnmatch.fnmatchcase(p, pattern)}
                if not found:
                    full_reasons.append(f"stale test rule {rule['name']}: {pattern}")
                targets.update(found)
        if path.startswith("tests/") and path.endswith(".py") and path in available:
            targets.add(path)
        selected.update(targets)
        explanations.append(
            {
                "path": path,
                "rules": sorted({rule["name"] for rule in matched}),
                "tests": sorted(targets),
            }
        )
    if full_reasons:
        selected = set(available)
    tests = sorted(selected)
    if not tests:
        raise ValueError("selection must not silently produce zero tests")
    groups = [tests[index::shards] for index in range(shards)]
    return {
        "version": "validation-plan-v1",
        "policy_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "changed_paths": changed,
        "scope": "FULL" if full_reasons else "IMPACT",
        "full_suite_reasons": sorted(set(full_reasons)),
        "explanations": explanations,
        "tests": tests,
        "shards": [group for group in groups if group],
        "test_file_count": len(tests),
        "repository_test_file_count": len(available),
        "input_closure": validation_input_closure(root, tests),
        "coverage_proven": False,
        "review_required": True,
        "run_with": "scripts/run_local_quality.py pytest <one shard> -q",
        "invariants": [
            "Add task-specific negative cases; domain importance alone is not full-suite impact.",
            "Every shard needs terminal evidence on the same code/config/test tree.",
            "Live, integration, lint and type checks remain separate and must be recorded.",
            "Timeout or a missing shard is INCOMPLETE, never PASS.",
        ],
    }


def plan_golden_validation(root: Path) -> dict[str, Any]:
    """Select actual registered-domain cases; no new runner or duplicate fixtures."""
    selection_path = root / "configs" / "validation" / "golden_research_v1.yaml"
    raw = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != "golden-research-selection-v1":
        raise ValueError("unsupported golden research selection")
    manifest_path = root / normalize_path(raw["source_manifest"])
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    scenarios = {item["id"]: item for item in manifest["scenarios"]}
    test_file = normalize_path(raw["test_file"])
    source = (root / test_file).read_bytes()
    functions = {node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    cases = raw["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden selection must contain cases")
    identifiers: set[int] = set()
    tests: list[str] = []
    bindings: list[dict[str, Any]] = []
    for case in cases:
        scenario_id, lane = case["scenario_id"], case["lane"]
        if (
            scenario_id not in scenarios
            or scenario_id in identifiers
            or lane not in {"read", "economic"}
        ):
            raise ValueError("golden case has a missing, repeated or invalid scenario binding")
        function = raw[lane + "_test"]
        if function not in functions:
            raise ValueError("golden test entry no longer exists")
        identifiers.add(scenario_id)
        node_id = f"{test_file}::{function}[{scenario_id}]"
        tests.append(node_id)
        bindings.append(
            {
                "scenario_id": scenario_id,
                "title": scenarios[scenario_id]["title"],
                "test_node": node_id,
            }
        )
    return {
        "version": "validation-plan-v1",
        "scope": "GOLDEN_RECORDED",
        "policy_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "test_source_sha256": hashlib.sha256(source).hexdigest(),
        "full_suite_reasons": [],
        "changed_paths": [],
        "tests": tests,
        "shards": [tests],
        "test_file_count": 1,
        "test_case_count": len(tests),
        "repository_test_file_count": sum(1 for _ in (root / "tests").rglob("test_*.py")),
        "bindings": bindings,
        "coverage_proven": False,
        "review_required": True,
        "evidence_mode": "RECORDED",
        "model_quality_evaluated": False,
        "run_with": "scripts/run_local_quality.py pytest <tests from this plan> -q",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument(
        "--golden",
        action="store_true",
        help="Optional 15-case recorded regression slice.",
    )
    parser.add_argument("--full-reason")
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.golden:
        if args.paths or args.full_reason or args.shards != 1:
            parser.error("--golden is a separate recorded slice, not an impact override")
        result = plan_golden_validation(ROOT)
    else:
        result = plan_validation(ROOT, args.paths, full_reason=args.full_reason, shards=args.shards)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = (ROOT / normalize_path(args.output)).resolve()
        if not destination.is_relative_to(ROOT / ".ai-bridge"):
            raise ValueError("generated validation plans belong under .ai-bridge")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "scope",
                        "full_suite_reasons",
                        "test_file_count",
                        "repository_test_file_count",
                    )
                },
                ensure_ascii=False,
            )
        )
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
