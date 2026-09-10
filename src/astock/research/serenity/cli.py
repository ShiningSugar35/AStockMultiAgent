"""CLI registration for deterministic Serenity adaptation helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.market_data.storage import CanonicalMarketStore
from astock.research.repository import ResearchRepository
from astock.research.serenity.compiler import SerenityInputCompiler
from astock.schemas.serenity.compiler import CanonicalDailyTrendCompileRequest


def register_serenity_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    """Attach read/compile-only Serenity helpers; none of these commands writes a trade ledger."""

    def compiler() -> tuple[SerenityInputCompiler, ResearchRepository]:
        paths, state, objects = services()
        return (
            SerenityInputCompiler(
                state,
                objects,
                CanonicalMarketStore(paths.parquet, paths.manifests),
            ),
            ResearchRepository(state, objects),
        )

    @app.command("research-serenity-compiler-schema")
    def research_serenity_compiler_schema() -> None:
        emit(
            {
                "daily_trend": CanonicalDailyTrendCompileRequest.model_json_schema(),
                "fundamental_binding": {
                    "bundle_artifact_id": "FundamentalModelBundle artifact id",
                    "expected_company_id": "6-digit company id",
                    "expected_as_of": "ISO-8601 aware datetime",
                },
                "paper_ledger_write_allowed": False,
                "broker_execution_allowed": False,
            }
        )

    @app.command("research-serenity-daily-compile")
    def research_serenity_daily_compile(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
        evidence_pack_id: Annotated[str, typer.Argument(help="FrozenEvidencePack pack id.")],
    ) -> None:
        request = CanonicalDailyTrendCompileRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        service, repository = compiler()
        evidence_pack = repository.get_evidence_pack(evidence_pack_id)
        if evidence_pack is None:
            raise typer.BadParameter("unknown frozen evidence pack id")
        emit(service.compile_daily_trend(request, evidence_pack=evidence_pack))

    @app.command("research-serenity-fundamental-binding")
    def research_serenity_fundamental_binding(
        bundle_artifact_id: Annotated[
            str,
            typer.Argument(help="Registered FundamentalModelBundle artifact id."),
        ],
        company_id: Annotated[str, typer.Option("--company-id")],
        as_of: Annotated[str, typer.Option("--as-of")],
    ) -> None:
        service, _ = compiler()
        emit(
            service.bind_fundamental_model(
                bundle_artifact_id,
                expected_company_id=company_id,
                expected_as_of=datetime.fromisoformat(as_of),
            )
        )


__all__ = ["register_serenity_commands"]
