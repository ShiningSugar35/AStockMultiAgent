from __future__ import annotations

from pathlib import Path

import pytest

from astock.core.project_root import resolve_project_root


def _make_project(root: Path) -> None:
    (root / "configs").mkdir(parents=True)
    (root / "migrations").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")


def test_resolve_project_root_from_installed_module_path_and_nested_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _make_project(project)
    installed_module = (
        project / ".venv" / "Lib" / "site-packages" / "astock" / "providers" / "runtime.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# fixture\n", encoding="utf-8")
    nested_cwd = project / ".devbridge" / "pkg-smoke-run"
    nested_cwd.mkdir(parents=True)

    assert resolve_project_root(module_file=installed_module, cwd=nested_cwd) == project.resolve()


def test_resolve_project_root_respects_explicit_environment_root(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "explicit"
    _make_project(project)
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(project))

    assert resolve_project_root(cwd=tmp_path / "elsewhere") == project.resolve()


def test_resolve_project_root_rejects_invalid_explicit_environment_root(
    tmp_path: Path, monkeypatch
) -> None:
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(invalid))

    with pytest.raises(ValueError, match="ASTOCK_PROJECT_ROOT"):
        resolve_project_root(cwd=tmp_path)
