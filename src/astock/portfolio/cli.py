"""CLI registration for portfolio diagnostics and constrained construction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.committee.config import load_committee_rules
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.service import PortfolioService
from astock.schemas.portfolio import PortfolioAnalysisRequest, PortfolioConstructionRequest


def register_portfolio_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach read-only portfolio analysis and proposal commands."""

    def portfolio_service() -> PortfolioService:
        paths, state, objects = services()
        reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(paths.parquet),
            paths.root / "tests" / "fixtures" / "reference",
        )
        return PortfolioService(
            state,
            objects,
            reference,
            load_committee_rules(paths.root / "configs" / "committee_rules.yaml"),
        )

    @app.command("portfolio-schema")
    def portfolio_schema() -> None:
        emit(
            {
                "analysis_request": PortfolioAnalysisRequest.model_json_schema(),
                "construction_request": PortfolioConstructionRequest.model_json_schema(),
            }
        )

    @app.command("portfolio-evaluate")
    def portfolio_evaluate(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioAnalysisRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(portfolio_service().analyze(request))

    @app.command("portfolio-paper-evaluate")
    def portfolio_paper_evaluate(
        account_id: Annotated[str, typer.Option()] = "paper",
        portfolio_id: Annotated[str | None, typer.Option()] = None,
        as_of: Annotated[str | None, typer.Option()] = None,
        live: Annotated[bool, typer.Option("--live")] = False,
        lookback_sessions: Annotated[int, typer.Option(min=60, max=504)] = 120,
    ) -> None:
        timestamp = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        request = PortfolioAnalysisRequest(
            portfolio_id=portfolio_id or f"paper:{account_id}",
            account_id=account_id,
            as_of=timestamp,
            live=live,
            lookback_sessions=lookback_sessions,
        )
        emit(portfolio_service().analyze(request))

    @app.command("portfolio-construct")
    def portfolio_construct(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioConstructionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(portfolio_service().construct(request))

    @app.command("portfolio-status")
    def portfolio_status(
        portfolio_id: Annotated[str, typer.Argument()],
        construction: Annotated[bool, typer.Option("--construction")] = False,
    ) -> None:
        emit(portfolio_service().status(portfolio_id, construction=construction))

    @app.command("portfolio-audit")
    def portfolio_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = portfolio_service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_portfolio_commands"]
