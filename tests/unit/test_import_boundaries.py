"""A real Import Linter graph rejects an injected upward import in an isolated package."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from astock.research.process_worker import run_owned_command

ROOT = Path(__file__).resolve().parents[2]


def test_import_linter_detects_forbidden_dependency(tmp_path: Path) -> None:
    source = tmp_path / "src"
    package = source / "boundary_fixture"
    for module in ("", "core", "providers"):
        directory = package / module
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.importlinter]\nroot_package = "boundary_fixture"\n'
        '[[tool.importlinter.contracts]]\nname = "core has no provider dependency"\n'
        'type = "forbidden"\nsource_modules = ["boundary_fixture.core"]\n'
        'forbidden_modules = ["boundary_fixture.providers"]\n',
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    executable = Path(sys.executable).parent / (
        "lint-imports.exe" if sys.platform == "win32" else "lint-imports"
    )
    command = [str(executable), str(config), "--no-cache", "--no-logo"]
    clean = run_owned_command(
        command,
        project_root=ROOT,
        timeout_seconds=30,
        environment=environment,
        merge_stderr=True,
    )
    assert clean.returncode == 0, clean.stdout
    (package / "core" / "__init__.py").write_text(
        "import boundary_fixture.providers\n",
        encoding="utf-8",
    )
    broken = run_owned_command(
        command,
        project_root=ROOT,
        timeout_seconds=30,
        environment=environment,
        merge_stderr=True,
    )
    assert broken.returncode == 1, broken.stdout
    assert "BROKEN" in broken.stdout
