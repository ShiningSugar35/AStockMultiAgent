"""CLI registration for Phase 11 prospective trial and statistics governance."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.schemas.prospective import ProspectiveTrialRecordRequest
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.governance import ProspectiveGovernanceService


def register_prospective_governance_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach Phase 11 all-trials and independence-governance commands."""

    def service() -> ProspectiveGovernanceService:
        paths, state, objects = services()
        policy = load_shadow_evaluation_policy(paths.root / "configs" / "shadow_evaluation.yaml")
        return ProspectiveGovernanceService(state, objects, policy)

    @app.command("prospective-governance-register")
    def prospective_governance_register(
        study_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(service().register_default_config(study_id))

    @app.command("prospective-trial-register")
    def prospective_trial_register(
        request_file: Annotated[Path, typer.Argument()],
    ) -> None:
        request = ProspectiveTrialRecordRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().register_trial(request))

    @app.command("prospective-trials-report")
    def prospective_trials_report(study_id: Annotated[str, typer.Argument()]) -> None:
        emit(service().all_trials_report(study_id))

    @app.command("prospective-statistics-plan")
    def prospective_statistics_plan(study_id: Annotated[str, typer.Argument()]) -> None:
        emit(service().statistics_plan(study_id))

    @app.command("prospective-governance-audit")
    def prospective_governance_audit(
        artifact_id: Annotated[str, typer.Argument()],
    ) -> None:
        result = service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_prospective_governance_commands"]
