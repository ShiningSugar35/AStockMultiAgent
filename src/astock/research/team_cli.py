"""CLI surface for durable research-team orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.research.industry_archetypes import IndustryResearchRegistry
from astock.research.team import ResearchTeamService
from astock.schemas.research_team import (
    RecommendationReadinessRequest,
    ResearchCoverageRequest,
    ResearchExecutionBackend,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTeamDepth,
)


def register_research_team_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Register full-market team planning, durable role results, and recommendation gates."""

    def team() -> ResearchTeamService:
        paths, state, objects = services()
        return ResearchTeamService(
            project_root=paths.root,
            state=state,
            objects=objects,
        )

    def industry_registry() -> IndustryResearchRegistry:
        paths, _, _ = services()
        return IndustryResearchRegistry.load(
            paths.root / "configs" / "industry_research_archetypes.yaml"
        )

    @app.command("research-runtime-profile")
    def research_runtime_profile() -> None:
        emit(team().runtime_profile())

    @app.command("research-team-schema")
    def research_team_schema() -> None:
        emit(
            {
                "role_output": ResearchRoleOutput.model_json_schema(),
                "role_result": ResearchRoleResult.model_json_schema(),
                "recommendation_readiness_request": (
                    RecommendationReadinessRequest.model_json_schema()
                ),
                "research_coverage_request": ResearchCoverageRequest.model_json_schema(),
            }
        )

    @app.command("industry-research-archetypes")
    def industry_research_archetypes() -> None:
        emit(industry_registry().inventory())

    @app.command("industry-research-resolve")
    def industry_research_resolve(query: Annotated[str, typer.Argument()]) -> None:
        emit(industry_registry().resolve(query))

    @app.command("research-team-plan")
    def research_team_plan(
        as_of: Annotated[str | None, typer.Option("--as-of")] = None,
        backend: Annotated[ResearchExecutionBackend | None, typer.Option()] = None,
        depth: Annotated[ResearchTeamDepth, typer.Option()] = ResearchTeamDepth.INSTITUTIONAL,
    ) -> None:
        timestamp = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        emit(team().create_full_market_plan(as_of=timestamp, backend=backend, depth=depth))

    @app.command("research-coverage-score")
    def research_coverage_score(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        request = ResearchCoverageRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(team().evaluate_coverage(request))

    @app.command("research-team-status")
    def research_team_status(plan_id: Annotated[str, typer.Argument()]) -> None:
        emit(team().status(plan_id))

    @app.command("research-team-role-output")
    def research_team_role_output(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        request = ResearchRoleOutput.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(team().register_role_output(request))

    @app.command("research-team-task-result")
    def research_team_task_result(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        request = ResearchRoleResult.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(team().register_role_result(request))

    @app.command("research-recommendation-readiness")
    def research_recommendation_readiness(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        request = RecommendationReadinessRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        report = team().evaluate_readiness(request)
        emit(report)
        if not report.formal_recommendation_allowed:
            raise typer.Exit(code=3)


__all__ = ["register_research_team_commands"]
