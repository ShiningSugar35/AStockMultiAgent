"""Project and runtime path discovery without global mutable configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path
    runtime: Path
    objects: Path
    parquet: Path
    manifests: Path
    state_db: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> ProjectPaths:
        configured = os.environ.get("ASTOCK_PROJECT_ROOT")
        if configured:
            root = Path(configured).expanduser().resolve()
        else:
            current = (start or Path.cwd()).resolve()
            root = next(
                (
                    candidate
                    for candidate in (current, *current.parents)
                    if (candidate / "pyproject.toml").is_file()
                ),
                current,
            )
        runtime_override = os.environ.get("ASTOCK_RUNTIME_ROOT")
        runtime = (
            Path(runtime_override).expanduser().resolve() if runtime_override else root / "runtime"
        )
        return cls(
            root=root,
            runtime=runtime,
            objects=runtime / "objects" / "sha256",
            parquet=runtime / "data" / "parquet",
            manifests=runtime / "manifests",
            state_db=runtime / "state.sqlite",
        )

    @property
    def logs(self) -> Path:
        return self.runtime / "logs"

    @property
    def logging_policy(self) -> Path:
        return self.root / "configs" / "logging_policy.yaml"

    def ensure_directories(self) -> None:
        for path in (
            self.runtime,
            self.objects,
            self.parquet,
            self.manifests,
            self.runtime / "artifacts",
            self.runtime / "codex_runs",
            self.runtime / "checkpoints",
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)
