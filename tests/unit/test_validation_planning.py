"""Selection contracts: smaller suites are explicit, unknown impact stays visible."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.audit_gate_inventory import inventory
from scripts.plan_validation import normalize_path, plan_validation

ROOT = Path(__file__).resolve().parents[2]


def test_documentation_change_does_not_force_full_code_suite():
    plan = plan_validation(ROOT, ["AGENTS.md"])
    assert plan["scope"] == "IMPACT", plan["full_suite_reasons"]
    assert 0 < plan["test_file_count"] < plan["repository_test_file_count"]
    assert plan["review_required"] and not plan["coverage_proven"]


def test_local_financial_fix_runs_financial_negative_suite_not_every_domain():
    plan = plan_validation(ROOT, ["src/astock/financial_sources/certification.py"])
    assert plan["scope"] == "IMPACT", plan["full_suite_reasons"]
    assert "tests/integration/test_financial_sources.py" in plan["tests"]
    assert "tests/unit/test_financial_pdf_real_layout.py" in plan["tests"]
    assert plan["test_file_count"] < plan["repository_test_file_count"]


@pytest.mark.parametrize("path", [
    "migrations/999_change.sql", "src/astock/core/state.py", "unknown.py",
    "tests/helpers/shared_case.py", "tests/fixtures/shared.json",
])
def test_shared_or_unknown_impact_cannot_silently_skip_tests(path):
    plan = plan_validation(ROOT, [path])
    assert plan["scope"] == "FULL"
    assert plan["full_suite_reasons"]
    assert plan["test_file_count"] == plan["repository_test_file_count"]


def test_shards_cover_each_selected_test_file_exactly_once():
    plan = plan_validation(ROOT, ["src/astock/research/continuation.py"], shards=3)
    flattened = [path for shard in plan["shards"] for path in shard]
    assert sorted(flattened) == plan["tests"]
    assert len(flattened) == len(set(flattened))


@pytest.mark.parametrize("path", ["../bad.py", "D:/outside.py", "/outside.py", "..\\bad.py"])
def test_paths_cannot_escape_project(path):
    with pytest.raises(ValueError):
        normalize_path(path)


def test_stale_policy_rule_escalates_instead_of_empty_success(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("", encoding="utf-8")
    policy = {
        "version": "validation-impact-v1",
        "rules": [{"name": "stale", "changed": ["a.py"], "tests": ["tests/test_missing.py"]}],
    }
    (tmp_path / "configs/validation_impact.yaml").write_text(
        yaml.safe_dump(policy), encoding="utf-8",
    )
    plan = plan_validation(tmp_path, ["a.py"])
    assert plan["scope"] == "FULL"
    assert "stale test rule" in plan["full_suite_reasons"][0]


def test_inventory_includes_non_keyword_conditional_and_schema_gate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text(
        "from pydantic import Field\nx = Field(ge=0)\nif x < 2:\n    raise ValueError('x')\n",
        encoding="utf-8",
    )
    result = inventory(tmp_path)
    kinds = {kind for site in result["sites"] for kind in site["kinds"]}
    assert {"Conditional", "Raise", "SchemaConstraint"} <= kinds
    assert not result["semantic_audit_complete"]
    assert result["scan_errors"] == []


def test_inventory_includes_sql_ci_and_nonkeyword_exception_gates(tmp_path):
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations/001.sql").write_text(
        "CREATE TABLE facts (id TEXT UNIQUE, amount REAL CHECK (amount >= 0));\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/check.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("steps:\n  - run: pytest tests\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text(
        "x = schema.Field(ge=0)\ntry:\n    f()\nexcept OSError:\n    pass\n",
        encoding="utf-8",
    )
    result = inventory(tmp_path)
    sites = result["sites"]
    assert any(s["path"].endswith("001.sql") and "SQLConstraint" in s["kinds"] for s in sites)
    assert any(s["path"] == ".github/workflows/check.yaml" for s in sites)
    assert any("SchemaConstraint" in s["kinds"] for s in sites)
    assert any("ExceptionHandler" in s["kinds"] for s in sites)
    assert not result["semantic_audit_complete"]


def test_inventory_reports_unreadable_source_instead_of_aborting(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    blocked = tmp_path / "src/blocked.py"
    blocked.write_text("if True: pass\n", encoding="utf-8")
    original = Path.read_bytes

    def read_bytes(path):
        if path == blocked:
            raise PermissionError("synthetic unreadable source")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    result = inventory(tmp_path)
    assert result["scan_errors"] == [{"path": "src/blocked.py", "error": "PermissionError"}]
    assert not result["semantic_audit_complete"]


def test_mixed_windows_separators_are_normalized_before_validation():
    assert normalize_path("src\\astock/providers\\runtime.py") == "src/astock/providers/runtime.py"


def test_inventory_semantic_review_requires_exact_hash_and_adjudicated_findings(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src/example.py"
    target.write_text("if value:\n    raise ValueError('bad')\n", encoding="utf-8")
    baseline = inventory(tmp_path)
    file_sha256 = baseline["file_hashes"]["src/example.py"]
    review_index = {
        "version": "gate-semantic-review-index-v1",
        "findings_adjudicated": True,
        "files": {
            "src/example.py": {
                "file_sha256": file_sha256,
                "review_id": "review:example",
            }
        },
    }

    reviewed = inventory(tmp_path, semantic_review_index=review_index)
    assert reviewed["semantic_audit_complete"]
    assert reviewed["semantic_review_coverage"] == {
        "review_index_version": "gate-semantic-review-index-v1",
        "candidate_file_count": 1,
        "reviewed_file_count": 1,
        "candidate_site_count": len(reviewed["sites"]),
        "reviewed_site_count": len(reviewed["sites"]),
        "unreviewed_file_count": 0,
        "findings_adjudicated": True,
    }
    assert {site["review_status"] for site in reviewed["sites"]} == {
        "FILE_SEMANTIC_REVIEWED"
    }
    assert {site["semantic_review_id"] for site in reviewed["sites"]} == {
        "review:example"
    }

    findings_open = dict(review_index, findings_adjudicated=False)
    open_result = inventory(tmp_path, semantic_review_index=findings_open)
    assert not open_result["semantic_audit_complete"]
    assert {site["review_status"] for site in open_result["sites"]} == {
        "FILE_REVIEWED_FINDINGS_OPEN"
    }

    target.write_text("if changed:\n    raise ValueError('changed')\n", encoding="utf-8")
    stale = inventory(tmp_path, semantic_review_index=review_index)
    assert not stale["semantic_audit_complete"]
    assert stale["semantic_review_coverage"]["reviewed_site_count"] == 0
    assert {site["review_status"] for site in stale["sites"]} == {"STALE_FILE_REVIEW"}


def test_public_investment_exhaustion_is_not_an_endless_continue():
    from astock.investor_orchestration.closure import InvestmentRequestClosurePolicy
    from tests.unit.test_investment_request_closure import _coverage, _plan, _request

    request = _request()
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"FINANCIAL_INTEGRITY"})
    decision = InvestmentRequestClosurePolicy.evaluate(
        request, plan, coverage, automatic_resolution_exhausted=True
    )
    assert decision.state.value == "PUBLIC_DATA_UNAVAILABLE"
    assert not decision.same_request_continuation_required
    assert not decision.private_user_input_required
    assert decision.investment_conclusion_blocked


def test_quality_runner_configures_legacy_streams_without_losing_unicode(monkeypatch):
    import io
    import sys

    from scripts.run_local_quality import configure_console

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    configure_console()
    stream.write("\u00a0中文诊断")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "\u00a0中文诊断"


def test_quality_runner_accepts_capture_streams_without_reconfigure(monkeypatch):
    import io
    import sys

    from scripts.run_local_quality import configure_console

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    configure_console()
