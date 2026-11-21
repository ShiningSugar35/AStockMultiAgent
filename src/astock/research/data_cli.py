"""Thin data commands over the canonical providers, artifacts and continuation."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.settings import ProjectPaths


def register_research_data_commands(
    app: typer.Typer, services: Callable[[], tuple[ProjectPaths, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("forecast-sample-capture")
    def forecast_sample_capture(
        company_id: str, market: str, start: str, end: str,
        max_pages: int = 2, page_size: int = 50,
    ) -> None:
        from astock.providers.forecasts import EastMoneyForecastProvider
        from astock.schemas import Market

        _paths, state, objects = services()
        capture = EastMoneyForecastProvider(objects, state).capture(
            company_id, Market(market), date.fromisoformat(start), date.fromisoformat(end),
            max_pages=max_pages, page_size=page_size,
        )
        emit(capture)

    @app.command("forecast-sample-aggregate")
    def forecast_sample_aggregate(
        company_id: str, market: str, fiscal_year: int, as_of: str,
        start: str, end: str, capture_ids: Annotated[list[str], typer.Option("--capture-id")],
    ) -> None:
        from astock.providers.forecasts import SampledForecastCapture, aggregate_forecasts
        from astock.schemas import Market

        _paths, state, objects = services()
        captures = []
        for identifier in capture_ids:
            record = state.artifact_record(identifier)
            if record is None or record["type"] != "SampledForecastCapture":
                raise ValueError("unknown forecast capture")
            captures.append(SampledForecastCapture.model_validate_json(
                objects.get_bytes(str(record["object_hash"]))
            ))
        result = aggregate_forecasts(
            captures, company_id=company_id, market=Market(market),
            target_fiscal_year=fiscal_year, as_of=datetime.fromisoformat(as_of),
            window_start=date.fromisoformat(start), window_end=date.fromisoformat(end),
            state=state, objects=objects,
        )
        emit({
            "artifact_id": result.artifact_id,
            "aggregate": result,
        })

    @app.command("tushare-valuation-capture")
    def tushare_valuation_capture(company_id: str, market: str, session: str) -> None:
        from astock.providers.tushare_reference import TushareReferenceProvider
        from astock.schemas import Market

        paths, state, objects = services()
        provider = TushareReferenceProvider(objects, state, project_root=paths.root)
        result = provider.valuation_metrics(
            company_id, Market(market), date.fromisoformat(session)
        )
        emit({"artifact_id": result.artifact_id, "capture": result})

    @app.command("tushare-macro-capture")
    def tushare_macro_capture(endpoint: str, period: str) -> None:
        from astock.providers.tushare_reference import TushareReferenceProvider

        if endpoint != "cn_gdp" and endpoint != "cn_cpi":
            raise ValueError("only reviewed GDP/CPI endpoints are supported")
        paths, state, objects = services()
        result = TushareReferenceProvider(objects, state, project_root=paths.root).macro_metrics(
            endpoint, period
        )
        emit({"artifact_id": result.artifact_id, "capture": result})

    @app.command("tushare-macro-compare")
    def tushare_macro_compare(
        capture_id: str, metric: str, release_family: str, as_of: str,
    ) -> None:
        from astock.investor_orchestration.macro import (
            ImmutableRawObjectStore,
            OfficialMacroCaptureService,
        )
        from astock.investor_orchestration.store import InvestorOrchestrationStore

        paths, state, objects = services()
        service = OfficialMacroCaptureService(
            InvestorOrchestrationStore(paths.state_db),
            config_path=paths.root / "configs" / "macro_current_releases_v1.yaml",
            object_store=ImmutableRawObjectStore(paths.objects),
        )
        emit(service.compare_tushare(
            capture_artifact_id=capture_id, metric=metric, release_family=release_family,
            as_of=datetime.fromisoformat(as_of), state=state, objects=objects,
        ))

    @app.command("web-original-resolve")
    def web_original_resolve(
        selection_file: Annotated[
            Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True)
        ],
    ) -> None:
        from astock.research.web_tools import WebToolExecutionService, advance_current_web
        from astock.schemas.web_tools import (
            CurrentResearchWebBatch,
            WebOfficialDocumentSelection,
            WebToolRequest,
        )

        paths, state, objects = services()
        web = WebToolExecutionService(paths.root, state, objects)
        with selection_file.open("rb") as handle:
            payload = handle.read(web.policy.max_response_bytes + 1)
        if len(payload) > web.policy.max_response_bytes:
            raise ValueError("Original selection exceeds the Web input budget")
        selection = WebOfficialDocumentSelection.model_validate_json(payload)
        cursor = web.repository.get(selection.operation_id)
        if cursor is None:
            raise ValueError("Original selection has no registered Web operation")
        request = WebToolRequest.model_validate(cursor["request"])
        if request.continuation_id is None:
            raise ValueError("Original selection requires a continuation")
        emit(advance_current_web(
            paths, state, objects, request.continuation_id,
            CurrentResearchWebBatch(
                continuation_id=request.continuation_id, originals=[selection],
            ),
        ))
