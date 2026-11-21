"""Lazy additive CLI commands for existing canonical daily releases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import typer


def register_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("research-technical-schema")
    def schema() -> None:
        from astock.schemas.technical import TechnicalIndicatorSnapshot

        emit(TechnicalIndicatorSnapshot.model_json_schema())

    @app.command("research-technical-analyze")
    def analyze(
        release_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option("--as-of")] = None,
        register: Annotated[bool, typer.Option("--register")] = False,
    ) -> None:
        from astock.technical.policy import load_technical_analysis_policy
        from astock.technical.registered import analyze_registered_daily

        try:
            boundary = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
            paths, state, objects = services()
            policy = load_technical_analysis_policy(paths.root / "configs/technical_analysis.yaml")
            snapshot = analyze_registered_daily(
                state,
                objects,
                release_id,
                policy=policy,
                as_of=boundary,
                calendar_path=paths.root / "configs/official_trading_calendar.yaml",
                parquet_root=paths.parquet,
                register=register,
            )
        except (ValueError, OSError, TypeError):
            emit(
                {
                    "status": "UNAVAILABLE",
                    "reason_code": "TECHNICAL_INPUT_NOT_VERIFIED",
                    "recommendation_allowed": False,
                    "paper_order_authority_allowed": False,
                }
            )
            raise typer.Exit(2) from None
        emit(
            {
                "status": "RESEARCH_ONLY",
                "artifact_id": (
                    "TechnicalIndicatorSnapshot:" + snapshot.snapshot_id.removeprefix("technical:")
                    if register
                    else None
                ),
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )
