"""Conservative, auditable validation input closure; unknown reads expand to full.

This is a selector for the existing runner, not a second verifier or PASS cache.
Runtime object/state reads are declared separately from versioned input reads.
"""

from __future__ import annotations

import ast
from pathlib import Path, PureWindowsPath
from typing import Any

# Broad, reviewed declarations. No declaration means full-workspace fallback.
# Runtime-only methods consume caller-provided fixtures/temporary object stores;
# fixtures are always included independently, including newly added files.
_DYNAMIC_INPUTS = {
    "astock.core.state": ["migrations/**/*"],
    "astock.core.object_store": [],
    "astock.core.logging": ["configs/**/*"],
    "astock.core.credentials": [],
    "astock.core.project_root": ["pyproject.toml"],
    "astock.settings": ["configs/**/*"],
}
_READ_METHODS = {
    "read_text",
    "read_bytes",
    "read_csv",
    "read_parquet",
    "glob",
    "rglob",
    "iterdir",
    "listdir",
    "scandir",
    "walk",
    "load",
    "open",
}


def _module_index(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory in ("src", "tests", "scripts"):
        for path in (root / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            parts = list(path.relative_to(root / "src" if directory == "src" else root).parts)
            parts[-1] = parts[-1].removesuffix(".py")
            if parts[-1] == "__init__":
                parts.pop()
            result[".".join(parts)] = relative
    return result


def validation_input_closure(root: Path, tests: list[str]) -> dict[str, Any]:
    root = root.resolve()
    index = _module_index(root)
    reverse = {path: module for module, path in index.items()}
    queue: list[str] = []
    for node in tests:
        relative = node.split("::", 1)[0].replace("\\", "/")
        path = root / relative
        if (
            Path(relative).is_absolute()
            or PureWindowsPath(relative).drive
            or ".." in Path(relative).parts
            or not relative.startswith("tests/")
            or not relative.endswith(".py")
            or not path.resolve().is_relative_to(root)
            or not path.is_file()
        ):
            raise ValueError("validation test must be an existing project-relative file")
        queue.append(relative)
        for parent in path.parents:
            conftest = parent / "conftest.py"
            if conftest.is_file():
                queue.append(conftest.relative_to(root).as_posix())
            if parent == root:
                break
    if not queue:
        raise ValueError("validation closure requires explicit tests")
    selected: set[str] = set()
    declarations: dict[str, list[str]] = {}
    unknown: list[str] = []
    # Dependency/version/config/fixture membership belongs to every recorded run.
    patterns = {
        "pyproject.toml",
        "uv.lock",
        "conftest.py",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        ".gitignore",
        ".gitattributes",
        ".pre-commit-config.yaml",
        ".github/**/*",
        "configs/**/*",
        "migrations/**/*",
        "tests/fixtures/**/*",
        "third_party/audits/**/*",
        "third_party/qualifications/**/*",
        "scripts/run_local_quality.py",
        "scripts/plan_validation.py",
        "scripts/validation_inputs.py",
    }
    while queue:
        relative = queue.pop()
        if relative in selected:
            continue
        selected.add(relative)
        module = reverse.get(relative, "")
        try:
            tree = ast.parse((root / relative).read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError):
            unknown.append(f"unparseable:{relative}")
            continue
        imported: set[str] = set()
        dynamic = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "pytest_plugins":
                unknown.append(f"dynamic-pytest-plugin:{relative}:{node.lineno}")
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = module.split(".")
                    if not relative.endswith("/__init__.py"):
                        package.pop()
                    base = ".".join(
                        package[: len(package) - node.level + 1] + ([base] if base else [])
                    )
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Call):
                called = node.func
                name = (
                    called.attr
                    if isinstance(called, ast.Attribute)
                    else called.id
                    if isinstance(called, ast.Name)
                    else ""
                )
                if name in {"__import__", "import_module", "exec", "eval"}:
                    unknown.append(f"dynamic-code:{relative}:{node.lineno}")
                if name in _READ_METHODS:
                    # File opened only for output is not a versioned input.
                    if name == "open":
                        mode = (
                            node.args[1]
                            if len(node.args) > 1
                            else next(
                                (item.value for item in node.keywords if item.arg == "mode"), None
                            )
                        )
                        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                            if (
                                any(letter in mode.value for letter in "wax")
                                and "+" not in mode.value
                            ):
                                continue
                    dynamic = True
        if dynamic:
            if module in _DYNAMIC_INPUTS:
                declarations[module] = _DYNAMIC_INPUTS[module]
                patterns.update(_DYNAMIC_INPUTS[module])
            else:
                unknown.append(f"undeclared-read:{relative}")
        for imported_name in imported:
            candidate = imported_name
            while candidate and candidate not in index:
                candidate = candidate.rpartition(".")[0]
            if candidate:
                queue.append(index[candidate])
                # Package initialization is executable validation input.
                parts = candidate.split(".")
                for count in range(1, len(parts)):
                    parent = index.get(".".join(parts[:count]))
                    if parent is not None:
                        queue.append(parent)
    for pattern in patterns:
        selected.update(
            path.relative_to(root).as_posix()
            for path in root.glob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
        )
    return {
        "version": "validation-input-closure-v1",
        "scope": "FULL_UNKNOWN_READ" if unknown else "DEPENDENCY_CLOSURE",
        "files": sorted(selected),
        "directory_patterns": sorted(patterns),
        "dynamic_read_declarations": declarations,
        "conservative_reasons": sorted(set(unknown)),
        "coverage_proven": False,
    }
