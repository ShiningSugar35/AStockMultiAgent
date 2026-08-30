"""CLI surface for same-request current research continuation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.research.continuation import CurrentResearchContinuationService
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuationRequest,
    CurrentResearchEvidenceBinding,
)


def register_current_research_continuation_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Register durable acquisition, evidence binding, team, and readiness transitions."""

    def continuation() -> CurrentResearchContinuationService:
        paths, state, objects = services()
        return CurrentResearchContinuationService(paths, state, objects)

    @app.command("research-current-continuation-schema")
    def research_current_continuation_schema() -> None:
        emit(
            {
                "request": CurrentResearchContinuationRequest.model_json_schema(),
                "automatic_resolution": CurrentResearchAutomaticResolution.model_json_schema(),
                "evidence_binding": CurrentResearchEvidenceBinding.model_json_schema(),
            }
        )

    @app.command("research-current-continuation-start")
    def research_current_continuation_start(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        request = CurrentResearchContinuationRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(continuation().start(request))

    @app.command("research-current-continuation-resolve")
    def research_current_continuation_resolve(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        resolution = CurrentResearchAutomaticResolution.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(continuation().apply_automatic_resolution(resolution))

    @app.command("research-current-continuation-bind")
    def research_current_continuation_bind(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        binding = CurrentResearchEvidenceBinding.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(continuation().bind_external_evidence(binding))

    @app.command("research-current-continuation-resume")
    def research_current_continuation_resume(
        continuation_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(continuation().resume(continuation_id))

    @app.command("research-current-continuation-advance")
    def research_current_continuation_advance(
        continuation_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(continuation().advance_team(continuation_id))

    @app.command("research-current-continuation-status")
    def research_current_continuation_status(
        continuation_id: Annotated[str, typer.Argument()],
    ) -> None:
        report = continuation().status(continuation_id)
        emit(report)
        if report["status"] == "NOT_FOUND":
            raise typer.Exit(code=2)


__all__ = ["register_current_research_continuation_commands"]
