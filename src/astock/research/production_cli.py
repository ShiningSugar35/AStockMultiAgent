"""CLI registration for Phase 12 research-production governance."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.research.config import load_research_skill_registry
from astock.research.observability import AgentObservabilityService
from astock.research.production import ResearchProductionService
from astock.schemas.agent_observability import AgentTaskObservationRequest
from astock.schemas.research_production import (
    CatalystMonitorRequest,
    CatalystRecordRequest,
    ResearchNeedVector,
    ResearchProductionRouteNeedsInfo,
    SkillUsageEvent,
)


def register_research_production_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach additive routing, efficiency, and catalyst commands."""

    def service() -> ResearchProductionService:
        paths, state, objects = services()
        registry = load_research_skill_registry(paths.root / "configs" / "research_skills.yaml")
        return ResearchProductionService(state, objects, registry)

    def observability() -> AgentObservabilityService:
        paths, state, objects = services()
        return AgentObservabilityService(
            state,
            objects,
            project_root=paths.root,
            manifest_root=paths.manifests,
        )

    @app.command("research-production-schema")
    def research_production_schema() -> None:
        emit(
            {
                "need_vector": ResearchNeedVector.model_json_schema(),
                "route_needs_info": ResearchProductionRouteNeedsInfo.model_json_schema(),
                "usage_event": SkillUsageEvent.model_json_schema(),
                "catalyst_request": CatalystRecordRequest.model_json_schema(),
                "catalyst_monitor_request": CatalystMonitorRequest.model_json_schema(),
            }
        )

    @app.command("agent-observation-schema")
    def agent_observation_schema() -> None:
        emit(AgentTaskObservationRequest.model_json_schema())

    @app.command("agent-observation-register")
    def agent_observation_register(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = AgentTaskObservationRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(observability().register(request))

    @app.command("agent-observability-report")
    def agent_observability_report(
        lookback_days: Annotated[int, typer.Option("--lookback-days")] = 30,
    ) -> None:
        emit(observability().report(lookback_days=lookback_days))

    @app.command("agent-observability-audit")
    def agent_observability_audit(report_id: Annotated[str, typer.Argument()]) -> None:
        result = observability().audit(report_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)

    @app.command("research-priority")
    def research_priority(request_file: Annotated[Path, typer.Argument()]) -> None:
        need = ResearchNeedVector.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(service().schedule(need))

    @app.command("research-production-route")
    def research_production_route(request_file: Annotated[Path, typer.Argument()]) -> None:
        need = ResearchNeedVector.model_validate_json(request_file.read_text(encoding="utf-8"))
        result = service().route_for_user(need)
        emit(result)
        if isinstance(result, ResearchProductionRouteNeedsInfo):
            raise typer.Exit(code=3)

    @app.command("research-usage-register")
    def research_usage_register(request_file: Annotated[Path, typer.Argument()]) -> None:
        event = SkillUsageEvent.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(service().record_usage(event))

    @app.command("research-efficiency-report")
    def research_efficiency_report() -> None:
        emit(service().efficiency_report())

    @app.command("catalyst-register")
    def catalyst_register(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = CatalystRecordRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().register_catalyst(request))

    @app.command("catalyst-monitor")
    def catalyst_monitor(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = CatalystMonitorRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().monitor_catalyst(request))

    @app.command("research-production-audit")
    def research_production_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_research_production_commands"]
