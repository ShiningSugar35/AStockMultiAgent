"""Low-context candidate input staging helpers for MCP/web-agent workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.candidates import (
    CandidateParquetStore,
    CandidateRepository,
    CandidateScanService,
    ProductionCandidateInputVerifier,
    load_candidate_scan_config,
)
from astock.candidates.seeds import ResearchSeedService
from astock.core.hashing import content_hash
from astock.providers.eastmoney_reference import EastMoneyReferenceProvider
from astock.schemas.candidates import CandidateInputRelease, CandidateScanRequest
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

    def seed_service() -> ResearchSeedService:
        paths, state, objects = services()
        provider = EastMoneyReferenceProvider(
            objects,
            state,
            paths.root / "tests" / "fixtures" / "reference" / "eastmoney",
        )
        return ResearchSeedService(
            project_root=paths.root,
            state=state,
            objects=objects,
            provider=provider,
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
        request = ResearchSeedRequest(
            as_of=timestamp,
            live=live,
            max_total_seeds=max_total_seeds,
            max_market_seeds=max_market_seeds,
            max_expert_seeds_per_author=max_expert_seeds_per_author,
            created_at=timestamp,
        )
        emit(seed_service().generate(request))

    @app.command("research-seeds-status")
    def research_seeds_status() -> None:
        emit(seed_service().status())

    @app.command("research-seeds-audit")
    def research_seeds_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        result = seed_service().audit(artifact_id)
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
