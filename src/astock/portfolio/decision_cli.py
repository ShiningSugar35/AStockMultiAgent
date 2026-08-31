"""CLI registration for portfolio transition and user-declared holdings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.committee.config import load_committee_rules
from astock.local_portfolio import LocalPortfolioService
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.decision import PortfolioDecisionService
from astock.portfolio.service import PortfolioService
from astock.schemas.portfolio_decision import (
    ETFProductProfile,
    ETFResearchMetricsRequest,
    HedgeEffectivenessRequest,
    PortfolioComplementScreenRequest,
    PortfolioTransitionRequest,
    UserDeclaredTradeCapture,
)


def register_portfolio_decision_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach read-only transition and external-holding intake commands."""

    def service() -> PortfolioDecisionService:
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
        return PortfolioDecisionService(
            state,
            objects,
            LocalPortfolioService(paths.root, state),
            portfolio,
            paths.root,
        )

    @app.command("portfolio-decision-schema")
    def portfolio_decision_schema() -> None:
        emit(
            {
                "declared_trade_capture": UserDeclaredTradeCapture.model_json_schema(),
                "portfolio_transition_request": PortfolioTransitionRequest.model_json_schema(),
                "hedge_effectiveness_request": HedgeEffectivenessRequest.model_json_schema(),
                "complement_screen_request": PortfolioComplementScreenRequest.model_json_schema(),
                "etf_product_profile": ETFProductProfile.model_json_schema(),
                "etf_research_metrics_request": ETFResearchMetricsRequest.model_json_schema(),
            }
        )

    @app.command("portfolio-import-declared-trade")
    def portfolio_import_declared_trade(
        request_file: Annotated[Path, typer.Argument()],
    ) -> None:
        request = UserDeclaredTradeCapture.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().import_declared_trade(request))

    @app.command("portfolio-local-snapshot")
    def portfolio_local_snapshot() -> None:
        emit(service().snapshot_local_portfolio(as_of=datetime.now(UTC)))

    @app.command("portfolio-etf-profile-register")
    def portfolio_etf_profile_register(
        request_file: Annotated[Path, typer.Argument()],
    ) -> None:
        request = ETFProductProfile.model_validate_json(request_file.read_text(encoding="utf-8"))
        emit(service().register_etf_profile(request))

    @app.command("portfolio-etf-metrics")
    def portfolio_etf_metrics(
        request_file: Annotated[Path, typer.Argument()],
    ) -> None:
        request = ETFResearchMetricsRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().evaluate_etf_metrics(request))

    @app.command("portfolio-complement-screen")
    def portfolio_complement_screen(
        request_file: Annotated[Path, typer.Argument()],
    ) -> None:
        request = PortfolioComplementScreenRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().screen_complements(request))

    @app.command("portfolio-hedge-evaluate")
    def portfolio_hedge_evaluate(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = HedgeEffectivenessRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().evaluate_hedge(request))

    @app.command("portfolio-transition")
    def portfolio_transition(request_file: Annotated[Path, typer.Argument()]) -> None:
        request = PortfolioTransitionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(service().transition(request))

    @app.command("portfolio-decision-status")
    def portfolio_decision_status(portfolio_id: Annotated[str, typer.Argument()]) -> None:
        emit(service().status(portfolio_id))

    @app.command("portfolio-decision-audit")
    def portfolio_decision_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)


__all__ = ["register_portfolio_decision_commands"]
