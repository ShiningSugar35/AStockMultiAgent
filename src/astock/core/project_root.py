"""Resolve the AStock project root in source-tree and installed-wheel execution."""

from __future__ import annotations

import os
from pathlib import Path

_MARKERS = ("pyproject.toml", "configs", "migrations")


def _is_project_root(path: Path) -> bool:
    return (path / _MARKERS[0]).is_file() and all((path / item).exists() for item in _MARKERS[1:])


def _walk_candidates(start: Path) -> list[Path]:
    resolved = start.resolve()
    base = resolved if resolved.is_dir() else resolved.parent
    return [base, *base.parents]


def resolve_project_root(*, module_file: Path | None = None, cwd: Path | None = None) -> Path:
    """Return the configured/local project root or fail closed with one actionable error."""

    configured = os.environ.get("ASTOCK_PROJECT_ROOT", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not _is_project_root(candidate):
            raise ValueError("ASTOCK_PROJECT_ROOT does not point to a valid AStock project root")
        return candidate
    seen: set[Path] = set()
    starts = [cwd or Path.cwd()]
    if module_file is not None:
        starts.append(module_file)
    for start in starts:
        for candidate in _walk_candidates(start):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _is_project_root(candidate):
                return candidate
    raise ValueError(
        "Unable to resolve AStock project root; run inside the project tree "
        "or set ASTOCK_PROJECT_ROOT"
    )


__all__ = ["resolve_project_root"]
