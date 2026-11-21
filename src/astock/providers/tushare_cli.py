"""Thin commands for the guarded Tushare query and capability-only MCP probe."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from astock.core.errors import ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.tushare_structured import TushareQueryRequest, TushareStructuredProvider
from astock.settings import ProjectPaths


def register_tushare_commands(
    app: typer.Typer,
    services: Callable[[], tuple[ProjectPaths, StateStore, ObjectStore]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("tushare-schema")
    def tushare_schema(endpoint: Annotated[str | None, typer.Option()] = None) -> None:
        paths, state, objects = services()
        provider = TushareStructuredProvider(objects, state, project_root=paths.root)
        try:
            emit(provider.schema(endpoint))
        except ProviderError as exc:
            emit({"status": "REJECTED", "failure_class": exc.failure_class.value})
            raise typer.Exit(2) from None
        finally:
            provider.close()

    @app.command("tushare-query")
    def tushare_query(
        endpoint: Annotated[str, typer.Option()],
        params_json: Annotated[str, typer.Option()] = "{}",
        fields: Annotated[str, typer.Option()] = "",
        max_rows: Annotated[int, typer.Option(min=1, max=10000)] = 6000,
        refresh: Annotated[bool, typer.Option()] = False,
    ) -> None:
        paths, state, objects = services()
        provider = TushareStructuredProvider(objects, state, project_root=paths.root)
        try:
            request = TushareQueryRequest.model_validate({
                "endpoint": endpoint, "params": json.loads(params_json),
                "fields": tuple(item.strip() for item in fields.split(",") if item.strip()),
                "max_rows": max_rows, "refresh": refresh,
            })
            emit(provider.query(request))
        except ProviderError as exc:
            emit({
                "status": "UNAVAILABLE", "failure_class": exc.failure_class.value,
                **{key: exc.details[key] for key in (
                    "status_code", "redirect_present", "redirect_same_origin",
                    "canonical_trailing_slash",
                ) if key in exc.details},
            })
            raise typer.Exit(2) from None
        except (ValidationError, ValueError, TypeError):
            emit({"status": "REJECTED", "failure_class": "INVALID_QUERY"})
            raise typer.Exit(2) from None
        finally:
            provider.close()

    @app.command("tushare-mcp-probe")
    def tushare_mcp_probe() -> None:
        from astock.providers.tushare_mcp import probe_tushare_mcp

        paths, state, objects = services()
        provider = TushareStructuredProvider(objects, state, project_root=paths.root)
        try:
            emit(probe_tushare_mcp(provider))
        except ProviderError as exc:
            emit({
                "status": "UNAVAILABLE", "failure_class": exc.failure_class.value,
                **{key: exc.details[key] for key in (
                    "status_code", "redirect_present", "redirect_same_origin",
                    "canonical_trailing_slash",
                ) if key in exc.details},
            })
            raise typer.Exit(2) from None
        finally:
            provider.close()
