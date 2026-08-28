"""Low-context candidate input staging helpers for MCP/web-agent workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from astock.candidates import (
    CandidateParquetStore,
    CandidateRepository,
    CandidateScanService,
    ProductionCandidateInputVerifier,
    load_candidate_scan_config,
)
from astock.candidates.promotion import ResearchSeedPromotionService
from astock.candidates.seeds import (
    ResearchSeedProviderRouter,
    ResearchSeedService,
    SeedSnapshotProvider,
)
from astock.core.hashing import content_hash
from astock.documents import DisclosureEnumerationProvider
from astock.financial_sources.service import FinancialSourceService
from astock.financial_sources.storage import FinancialSourceParquetStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.paper_trading.operation import load_paper_trading_rules
from astock.research.team import detect_hardware_budget, load_research_team_policy
from astock.schemas.candidate_promotion import SeedPromotionRequest
from astock.schemas.candidates import CandidateInputRelease, CandidateScanRequest
from astock.schemas.market import CompletenessSemantics
from astock.schemas.research_seeds import ResearchSeedRequest


def register_candidate_input_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Register one-file candidate staging/run commands without exposing SQLite writes."""

    def candidate_service() -> tuple[Any, CandidateScanService]:
        paths, state, objects = services()
        parquet = CandidateParquetStore(paths.parquet / "candidates")
        service = CandidateScanService(
            CandidateRepository(state),
            objects,
            parquet,
            load_candidate_scan_config(paths.root / "configs" / "candidate_scan.yaml"),
            ProductionCandidateInputVerifier(
                state,
                objects,
                paths.parquet,
                paths.root / "tests" / "fixtures" / "reference",
            ),
        )
        return paths, service

    def seed_service(*, live: bool = False) -> ResearchSeedService:
        paths, state, objects = services()
        reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(paths.parquet),
            paths.root / "tests" / "fixtures" / "reference",
        )
        seed_providers: list[SeedSnapshotProvider] = []
        definitions = (
            reference.provider_factory.definitions_for_capability(
                "market.seed_snapshot",
                require_complete=True,
            )
            if live
            else [
                definition
                for definition in reference.provider_registry.providers
                if "market.seed_snapshot" in definition.capabilities
                and definition.completeness_semantics.get("market.seed_snapshot")
                is CompletenessSemantics.FULL_UNIVERSE
            ]
        )
        for definition in definitions:
            candidate = reference.provider_factory.create(definition.provider_id)
            fetch = getattr(candidate, "fetch_seed_snapshot", None)
            if callable(fetch):
                seed_providers.append(cast(SeedSnapshotProvider, candidate))
        provider = ResearchSeedProviderRouter(
            providers=seed_providers,
            minimum_rows_by_market=reference.config.minimum_instrument_records,
            state=state,
            objects=objects,
        )
        return ResearchSeedService(
            project_root=paths.root,
            state=state,
            objects=objects,
            provider=provider,
        )

    def promotion_service() -> ResearchSeedPromotionService:
        from astock.research.trading_classification import TradingClassificationService

        paths, state, objects = services()
        _, scan_service = candidate_service()
        reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(paths.parquet),
            paths.root / "tests" / "fixtures" / "reference",
        )
        return ResearchSeedPromotionService(
            project_root=paths.root,
            state=state,
            objects=objects,
            reference=reference,
            candidates=scan_service,
            financial_sources=FinancialSourceService(
                state,
                objects,
                FinancialSourceParquetStore(paths.parquet / "financial_sources"),
                paths.root,
            ),
            trading_classification=TradingClassificationService(
                state,
                objects,
                reference=reference,
                trading_rules=load_paper_trading_rules(
                    paths.root / "configs" / "paper_trading_rules.yaml"
                ),
            ),
            cninfo=reference.provider_factory.create_for_capability(
                "disclosure.enumerate",
                DisclosureEnumerationProvider,
                formal_use=True,
                require_complete=True,
            ),
        )

    @app.command("research-seeds-schema")
    def research_seeds_schema() -> None:
        emit(ResearchSeedRequest.model_json_schema())

    @app.command("research-seeds")
    def research_seeds(
        as_of: Annotated[str | None, typer.Option("--as-of")] = None,
        live: Annotated[bool, typer.Option("--live")] = False,
        max_total_seeds: Annotated[int, typer.Option(min=5, max=100)] = 40,
        max_market_seeds: Annotated[int, typer.Option(min=0, max=60)] = 20,
        max_expert_seeds_per_author: Annotated[int, typer.Option(min=0, max=30)] = 10,
    ) -> None:
        timestamp = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        paths, _, _ = services()
        team_policy = load_research_team_policy(paths.root / "configs" / "research_team.yaml")
        hardware = detect_hardware_budget(team_policy)
        request = ResearchSeedRequest(
            as_of=timestamp,
            live=live,
            max_total_seeds=max_total_seeds,
            max_market_seeds=max_market_seeds,
            max_expert_seeds_per_author=max_expert_seeds_per_author,
            market_fetch_workers=min(3, hardware.provider_workers),
            expert_overlay_max_priority_bonus=team_policy.expert_overlay_max_priority_bonus,
            created_at=timestamp,
        )
        emit(seed_service(live=live).generate(request))

    @app.command("research-seeds-status")
    def research_seeds_status() -> None:
        emit(seed_service().status())

    @app.command("research-seeds-audit")
    def research_seeds_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = seed_service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)

    @app.command("research-seeds-promote")
    def research_seeds_promote(
        seed_report_artifact_id: Annotated[str, typer.Argument()],
        live: Annotated[bool, typer.Option("--live")] = False,
        max_seeds: Annotated[int, typer.Option(min=1, max=60)] = 20,
    ) -> None:
        request = SeedPromotionRequest(
            seed_report_artifact_id=seed_report_artifact_id,
            max_seeds=max_seeds,
            live=live,
            created_at=datetime.now(UTC),
        )
        report = promotion_service().promote(request)
        emit(report)
        if report.status.value == "NEEDS_INFO":
            raise typer.Exit(code=3)

    @app.command("research-seeds-promote-status")
    def research_seeds_promote_status() -> None:
        emit(promotion_service().status())

    @app.command("research-seeds-promote-audit")
    def research_seeds_promote_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = promotion_service().audit(artifact_id)
        emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)

    @app.command("candidate-input-schema")
    def candidate_input_schema() -> None:
        emit(CandidateInputRelease.model_json_schema())

    @app.command("candidate-input-stage")
    def candidate_input_stage(
        release_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    ) -> None:
        release = CandidateInputRelease.model_validate_json(
            release_file.read_text(encoding="utf-8")
        )
        _, service = candidate_service()
        object_hash = service.stage_input_release(release)
        emit(
            {
                "status": "STAGED",
                "input_release_id": release.input_release_id,
                "input_release_object_hash": object_hash,
                "as_of": release.as_of,
                "next_command": "candidate-scan",
            }
        )

    @app.command("candidate-input-run")
    def candidate_input_run(
        release_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
        formal_historical: Annotated[bool, typer.Option()] = False,
        live: Annotated[bool, typer.Option()] = False,
    ) -> None:
        release = CandidateInputRelease.model_validate_json(
            release_file.read_text(encoding="utf-8")
        )
        _, service = candidate_service()
        object_hash = service.stage_input_release(release)
        request = CandidateScanRequest(
            request_id="candidate-input-run:"
            + content_hash(
                {
                    "input_release_id": release.input_release_id,
                    "input_release_object_hash": object_hash,
                    "formal_historical": formal_historical,
                    "live": live,
                }
            ),
            input_release_id=release.input_release_id,
            input_release_object_hash=object_hash,
            as_of=release.as_of,
            formal_historical=formal_historical,
            live=live,
            created_at=release.as_of,
        )
        emit(service.scan(request))


__all__ = ["register_candidate_input_commands"]
