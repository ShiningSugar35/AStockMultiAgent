"""CLI registration for Phase 12 research-production governance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.core.errors import AStockError
from astock.diagnostics import DiagnosticError, inspect_report
from astock.research.config import load_research_skill_registry
from astock.research.observability import AgentObservabilityService
from astock.research.production import ResearchProductionService
from astock.schemas.agent_observability import AgentTaskObservationRequest, ResearchRequestTrace
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
    *,
    read_only_services: Callable[[], tuple[Any, Any, Any]],
) -> None:
    """Attach additive routing, efficiency, and catalyst commands."""

    def service(*, read_only: bool = False) -> ResearchProductionService:
        paths, state, objects = (read_only_services if read_only else services)()
        registry = load_research_skill_registry(paths.root / "configs" / "research_skills.yaml")
        return ResearchProductionService(state, objects, registry)

    def observability(*, read_only: bool = False) -> AgentObservabilityService:
        paths, state, objects = (read_only_services if read_only else services)()
        return AgentObservabilityService(
            state,
            objects,
            project_root=paths.root,
            manifest_root=paths.manifests,
        )

    def inspect(action: Callable[[], Any]) -> Any:
        try:
            return action()
        except FileNotFoundError:
            return {"status": "NOT_INITIALIZED", "materialized": False}
        except (ValueError, sqlite3.Error, OSError, AStockError) as exc:
            return {
                "status": "UNAVAILABLE",
                "error_code": str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__,
                "materialized": False,
            }

    def saved_report(kind: str, report_id: str | None) -> Any:
        paths, _, _ = read_only_services()
        return inspect_report(paths.root, kind, report_id=report_id, runtime_root=paths.runtime)

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
        lookback_days: Annotated[
            int, typer.Option("--lookback-days", min=0, help="Window for --materialize only.")
        ] = 30,
        materialize: Annotated[
            bool, typer.Option("--materialize", help="Explicitly generate and save a new report.")
        ] = False,
        report_id: Annotated[str | None, typer.Option("--report-id")] = None,
    ) -> None:
        if materialize and report_id is not None:
            raise typer.BadParameter("--report-id and --materialize are mutually exclusive")
        emit(
            observability().report(lookback_days=lookback_days)
            if materialize
            else inspect(lambda: saved_report("AgentObservabilityReport", report_id))
        )

    @app.command("agent-observability-audit")
    def agent_observability_audit(report_id: Annotated[str, typer.Argument()]) -> None:
        result = inspect(lambda: observability(read_only=True).audit(report_id))
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)

    @app.command("research-request-trace-register")
    def research_request_trace_register(request_file: Annotated[Path, typer.Argument()]) -> None:
        trace = ResearchRequestTrace.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(observability().register_request_trace(trace))

    @app.command("research-request-performance-report")
    def research_request_performance_report(
        lookback_days: Annotated[int, typer.Option("--lookback-days", min=0)] = 30,
    ) -> None:
        emit(inspect(lambda: observability(read_only=True).request_performance(
            lookback_days=lookback_days,
        )))

    @app.command("research-evaluation-report")
    def research_evaluation_report(
        lookback_days: Annotated[int, typer.Option("--lookback-days", min=0)] = 30,
        limit: Annotated[int, typer.Option("--limit", min=1, max=2000)] = 500,
        baseline: Annotated[str, typer.Option("--baseline")] = "baseline",
        candidate: Annotated[str, typer.Option("--candidate")] = "candidate",
    ) -> None:
        if baseline == candidate:
            raise typer.BadParameter("--baseline and --candidate must differ")
        emit(inspect(lambda: observability(read_only=True).evaluation_report(
            lookback_days=lookback_days, limit=limit, baseline=baseline, candidate=candidate,
        )))

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
    def research_efficiency_report(
        materialize: Annotated[
            bool, typer.Option("--materialize", help="Explicitly generate and save a new report.")
        ] = False,
        report_id: Annotated[str | None, typer.Option("--report-id")] = None,
    ) -> None:
        if materialize and report_id is not None:
            raise typer.BadParameter("--report-id and --materialize are mutually exclusive")
        emit(
            service().efficiency_report()
            if materialize
            else inspect(lambda: saved_report("SkillEfficiencyReport", report_id))
        )

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
        result = inspect(lambda: service(read_only=True).audit(artifact_id))
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_research_production_commands"]
