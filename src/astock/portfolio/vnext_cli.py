"""CLI registration for Phase 10 portfolio risk and attribution tooling."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.committee.config import load_committee_rules
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.service import PortfolioService
from astock.portfolio.vnext import PortfolioVNextService
from astock.schemas.portfolio_vnext import (
    PortfolioAttributionRequest,
    PortfolioRiskExplanationRequest,
    PortfolioStressRequest,
)


def register_portfolio_vnext_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach additive, non-executing Phase 10 commands."""

    def service() -> PortfolioVNextService:
        paths, state, objects = services()
        reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(paths.parquet),
            paths.root / "tests" / "fixtures" / "reference",
        )
        portfolio = PortfolioService(
            state,
            objects,
            reference,
            load_committee_rules(paths.root / "configs" / "committee_rules.yaml"),
        )
        return PortfolioVNextService(state, objects, portfolio, paths.root)

    @app.command("portfolio-vnext-schema")
    def portfolio_vnext_schema() -> None:
        emit(
            {
                "risk_explanation_request": PortfolioRiskExplanationRequest.model_json_schema(),
                "stress_request": PortfolioStressRequest.model_json_schema(),
                "attribution_request": PortfolioAttributionRequest.model_json_schema(),
            }
        )

    @app.command("portfolio-risk-explain")
    def portfolio_risk_explain(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioRiskExplanationRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().explain_risk(request))

    @app.command("portfolio-stress")
    def portfolio_stress(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioStressRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().stress(request))

    @app.command("portfolio-attribution")
    def portfolio_attribution(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioAttributionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().attribute(request))

    @app.command("portfolio-vnext-audit")
    def portfolio_vnext_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_portfolio_vnext_commands"]
