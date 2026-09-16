"""Agent tool surface for optional public data; never bypasses formal acquisition."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.providers.supplemental import SupplementalEvidenceService, SupplementalRequest


def register_supplemental_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("research-supplemental-schema")
    def supplemental_schema() -> None:
        emit(SupplementalRequest.model_json_schema())

    @app.command("research-supplemental-acquire")
    def supplemental_acquire(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        live: Annotated[bool, typer.Option("--live/--recorded")] = False,
    ) -> None:
        request = SupplementalRequest.model_validate_json(request_file.read_bytes())
        paths, state, objects = services()
        emit(SupplementalEvidenceService(paths.root, state, objects).collect(request, live=live))
