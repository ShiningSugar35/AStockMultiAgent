"""CLI surface for same-request current research continuation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.providers.supplemental_cli import register_supplemental_commands
from astock.research.continuation import CurrentResearchContinuationService
from astock.research.web_tools import advance_current_web
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuationRequest,
    CurrentResearchEvidenceBinding,
)
from astock.schemas.web_tools import CurrentResearchWebBatch, HostWebCapabilities
from astock.web_discovery_cli import register_web_discovery_commands


def register_current_research_continuation_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Register durable acquisition, evidence binding, team, and readiness transitions."""

    register_supplemental_commands(app, services, emit)
    from astock.experiments.cli import register_commands as register_experiment_commands
    from astock.research.quality_cli import register_commands as register_quality_commands
    from astock.technical.cli import register_commands as register_technical_commands

    register_technical_commands(app, services, emit)
    register_experiment_commands(app, services, emit)
    register_quality_commands(app, services, emit)
    # Additive extension point: cli.py and runtime_cli.py are frozen by
    # serenity-local-adaptation-v2 and must remain byte-identical.
    register_web_discovery_commands(app, emit)
    from astock.research.web_tools_cli import register_web_tool_commands

    register_web_tool_commands(app, services, emit)

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
                "web_batch": CurrentResearchWebBatch.model_json_schema(),
            }
        )

    @app.command("research-current-continuation-start")
    def research_current_continuation_start(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
        host_capabilities: Annotated[
            Path | None,
            typer.Option(
                "--host-capabilities", exists=True, file_okay=True, dir_okay=False, readable=True,
            ),
        ] = None,
    ) -> None:
        request = CurrentResearchContinuationRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        host = (
            HostWebCapabilities.model_validate_json(host_capabilities.read_text(encoding="utf-8"))
            if host_capabilities is not None else None
        )
        paths, state, objects = services()
        service = CurrentResearchContinuationService(paths, state, objects)
        record = service.start(request)
        batch = CurrentResearchWebBatch(
            continuation_id=record.continuation_id, host_capabilities=host,
        )
        emit(advance_current_web(
            paths, state, objects, record.continuation_id, batch, continuation=service,
        ))

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
        web_batch: Annotated[
            Path | None,
            typer.Option("--web-batch", exists=True, file_okay=True, dir_okay=False, readable=True),
        ] = None,
    ) -> None:
        batch = (
            CurrentResearchWebBatch.model_validate_json(web_batch.read_text(encoding="utf-8"))
            if web_batch is not None else None
        )
        paths, state, objects = services()
        service = CurrentResearchContinuationService(paths, state, objects)
        emit(advance_current_web(
            paths, state, objects, continuation_id, batch,
            continuation=service, resume_unbound=True,
        ))

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
