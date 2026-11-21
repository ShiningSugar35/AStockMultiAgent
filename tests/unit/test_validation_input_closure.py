"""Dependency closure excludes unrelated docs, while uncertainty and data membership fail safe."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_local_quality
from scripts.validation_inputs import validation_input_closure


def _file(root: Path, name: str, value: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _project(root: Path) -> None:
    _file(root, "src/astock/__init__.py", "")
    _file(root, "src/astock/pure.py", "VALUE = 1\n")
    _file(
        root,
        "tests/test_value.py",
        "from astock.pure import VALUE\ndef test_value(): assert VALUE\n",
    )
    _file(root, "configs/active.yaml", "version: one\n")
    _file(root, "tests/fixtures/sample.json", "{}\n")


def test_dependency_closure_and_full_workspace_snapshot_are_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    before = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert before["scope"] == "DEPENDENCY_CLOSURE"
    assert "src/astock/pure.py" in before["files"]
    input_hash = run_local_quality.fingerprint(before["files"])
    workspace_hash = run_local_quality.fingerprint()
    _file(tmp_path, "docs/unrelated.md", "Unrelated developer note\n")
    after = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert run_local_quality.fingerprint(after["files"]) == input_hash
    assert run_local_quality.fingerprint() != workspace_hash
    _file(tmp_path, "src/astock/pure.py", "VALUE = 2\n")
    assert run_local_quality.fingerprint(after["files"]) != input_hash


@pytest.mark.parametrize(
    "relative",
    [
        "configs/active.yaml",
        "tests/fixtures/sample.json",
        "tests/fixtures/new.json",
        "migrations/new.sql",
        "third_party/qualifications/new.json",
    ],
)
def test_config_fixture_and_directory_membership_cannot_be_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    before = validation_input_closure(tmp_path, ["tests/test_value.py"])
    fingerprint = run_local_quality.fingerprint(before["files"])
    _file(tmp_path, relative, "changed\n")
    after = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert relative in after["files"]
    assert run_local_quality.fingerprint(after["files"]) != fingerprint


@pytest.mark.parametrize(
    "dynamic",
    [
        "from pathlib import Path\nVALUE = Path('docs/secret-input.md').read_text()\n",
        "import importlib\nVALUE = importlib.import_module('astock.unregistered')\n",
    ],
)
def test_undeclared_reads_and_dynamic_imports_expand_conservatively(
    tmp_path: Path,
    dynamic: str,
) -> None:
    _project(tmp_path)
    _file(tmp_path, "src/astock/pure.py", dynamic)
    closure = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert closure["scope"] == "FULL_UNKNOWN_READ"
    assert closure["conservative_reasons"]
    assert closure["coverage_proven"] is False


@pytest.mark.parametrize("extra", [
    "tests", "tests/unit", "/tmp/test_extra.py", "D:/project/tests/test_extra.py",
    "--pyargs", "-c", "--ignore=tests/test_value.py",
])
def test_planned_runner_rejects_omitted_collection_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str,
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="validation plan"):
        run_local_quality.planned_pytest_targets(
            ["tests/test_value.py", extra], ["tests/test_value.py"],
        )


def test_planned_runner_allows_nodes_and_filter_options_without_expanding_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    assert run_local_quality.planned_pytest_targets(
        ["-q", "-k", "value", "--maxfail=1", "tests/test_value.py::test_value"],
        ["tests/test_value.py"],
    ) == ["tests/test_value.py::test_value"]
    with pytest.raises(ValueError, match="explicitly covered"):
        run_local_quality.planned_pytest_targets(
            ["tests/test_value.py"], ["tests/test_value.py::test_value"],
        )


def test_root_conftest_is_a_validation_input_and_plugin_loading_expands_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    _file(tmp_path, "conftest.py", "VALUE = 1\n")
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    before = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert "conftest.py" in before["files"]
    checksum = run_local_quality.fingerprint(before["files"])
    _file(tmp_path, "conftest.py", "VALUE = 2\n")
    assert run_local_quality.fingerprint(before["files"]) != checksum
    _file(tmp_path, "conftest.py", "pytest_plugins = ['custom_fixture_plugin']\n")
    after = validation_input_closure(tmp_path, ["tests/test_value.py"])
    assert after["scope"] == "FULL_UNKNOWN_READ"
    assert any(
        reason.startswith("dynamic-pytest-plugin:") for reason in after["conservative_reasons"]
    )
