"""CLI registration for portfolio transition and user-declared holdings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.committee.config import load_committee_rules
from astock.external_accounts import ExternalAccountRepository
from astock.local_portfolio import LocalPortfolioService
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.decision import PortfolioDecisionService
from astock.portfolio.service import PortfolioService
from astock.schemas.external_accounts import (
    ExternalAccountEventDraft,
    ExternalAccountIdentity,
    ExternalAccountImportFormat,
    ExternalAccountImportPreview,
    ExternalAccountImportReceipt,
    ExternalAccountKind,
    ExternalAccountProjection,
)
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

    def external_accounts() -> tuple[Any, ExternalAccountRepository, LocalPortfolioService]:
        paths, state, objects = services()
        return (
            paths,
            ExternalAccountRepository(state, objects),
            LocalPortfolioService(paths.root, state),
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

    @app.command("external-account-schema")
    def external_account_schema() -> None:
        emit(
            {
                "account": ExternalAccountIdentity.model_json_schema(),
                "event_draft": ExternalAccountEventDraft.model_json_schema(),
                "import_preview": ExternalAccountImportPreview.model_json_schema(),
                "import_receipt": ExternalAccountImportReceipt.model_json_schema(),
                "projection": ExternalAccountProjection.model_json_schema(),
            }
        )

    @app.command("external-account-create")
    def external_account_create(
        account_id: Annotated[str, typer.Argument()],
        display_name: Annotated[str, typer.Argument()],
        account_kind: Annotated[str, typer.Option("--kind")] = ExternalAccountKind.MANUAL.value,
    ) -> None:
        paths, repository, _ = external_accounts()
        identity = repository.create_account(
            account_id=account_id,
            display_name=display_name,
            account_kind=ExternalAccountKind(account_kind.strip().upper()),
        )
        repository.write_markdown_projection(paths.root)
        emit(identity)

    @app.command("external-account-list")
    def external_account_list() -> None:
        _, repository, _ = external_accounts()
        emit(repository.list_accounts())

    @app.command("external-account-event-append")
    def external_account_event_append(
        request_file: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
    ) -> None:
        paths, repository, _ = external_accounts()
        request = ExternalAccountEventDraft.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        inserted, duplicates = repository.append_drafts([request])
        projection = repository.write_markdown_projection(paths.root)
        emit(
            {
                "status": "APPENDED" if inserted else "DUPLICATE",
                "inserted_event_ids": sorted(inserted),
                "duplicate_event_ids": sorted(duplicates),
                "projection": projection,
                "paper_ledger_write_allowed": False,
                "broker_execution_allowed": False,
            }
        )

    @app.command("external-account-import-preview")
    def external_account_import_preview(
        source_file: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
        source_format: Annotated[str | None, typer.Option("--format")] = None,
    ) -> None:
        _, repository, _ = external_accounts()
        format_value = (
            ExternalAccountImportFormat(source_format.strip().upper())
            if source_format is not None
            else None
        )
        emit(repository.preview_import(source_file, source_format=format_value))

    @app.command("external-account-import-confirm")
    def external_account_import_confirm(
        batch_id: Annotated[str, typer.Argument()],
        source_file: Annotated[
            Path | None,
            typer.Option("--source-file", exists=True, file_okay=True, dir_okay=False),
        ] = None,
    ) -> None:
        paths, repository, _ = external_accounts()
        receipt = repository.confirm_import(batch_id, source_path=source_file)
        repository.write_markdown_projection(paths.root)
        emit(receipt)

    @app.command("external-account-projection")
    def external_account_projection(account_id: Annotated[str, typer.Argument()]) -> None:
        _, repository, _ = external_accounts()
        emit(repository.projection(account_id))

    @app.command("external-account-audit")
    def external_account_audit(account_id: Annotated[str, typer.Argument()]) -> None:
        _, repository, _ = external_accounts()
        result = repository.audit(account_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)

    @app.command("external-account-migrate-legacy-default")
    def external_account_migrate_legacy_default() -> None:
        paths, repository, local = external_accounts()
        result = repository.migrate_legacy_default_account(local)
        result["projection"] = repository.write_markdown_projection(paths.root)
        emit(result)

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
