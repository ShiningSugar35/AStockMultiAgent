"""Explicit host handoff and budgeted Web tool commands."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.research.web_tools import WebToolExecutionService, load_web_tool_policy
from astock.schemas.web_tools import HostWebCapabilities, HostWebResult, WebToolRequest
from astock.settings import ProjectPaths


def _read_input(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Web tool input exceeds configured byte budget")
    return value


def register_web_tool_commands(
    app: typer.Typer, services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def service() -> WebToolExecutionService:
        paths, state, objects = services()
        return WebToolExecutionService(paths.root, state, objects)

    def failure(exc: Exception) -> None:
        # Pydantic/HTTP errors can contain arbitrary input; never echo raw exception text.
        emit({"status": "REJECTED", "reason_code": type(exc).__name__})
        raise typer.Exit(2)

    @app.command("web-tool-schema")
    def schema() -> None:
        emit({
            "request": WebToolRequest.model_json_schema(),
            "host_capabilities": HostWebCapabilities.model_json_schema(),
            "host_result": HostWebResult.model_json_schema(),
            "policy": load_web_tool_policy(ProjectPaths.discover().root).model_dump(),
            "transport_contract": (
                "Host invokes its observed tool; Python never scrapes a host session."
            ),
        })

    @app.command("web-tool-run")
    def run(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        host_capabilities: Annotated[
            Path | None, typer.Option("--host-capabilities", exists=True, dir_okay=False)
        ] = None,
    ) -> None:
        try:
            policy = load_web_tool_policy(ProjectPaths.discover().root)
            request = WebToolRequest.model_validate_json(
                _read_input(request_file, policy.max_response_bytes)
            )
            host = None if host_capabilities is None else HostWebCapabilities.model_validate_json(
                _read_input(host_capabilities, policy.max_response_bytes)
            )
            emit(service().run(request, host))
        except (ValueError, OSError) as exc:
            failure(exc)

    @app.command("web-tool-complete")
    def complete(
        result_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    ) -> None:
        try:
            policy = load_web_tool_policy(ProjectPaths.discover().root)
            response = HostWebResult.model_validate_json(
                _read_input(result_file, policy.max_response_bytes)
            )
            emit(service().complete_host(response))
        except (ValueError, OSError) as exc:
            failure(exc)

    @app.command("web-tool-cancel")
    def cancel(key: Annotated[str, typer.Argument()]) -> None:
        try:
            runtime = service()
            emit(runtime.repository.cancel(key, runtime.clock()))
        except (ValueError, OSError) as exc:
            failure(exc)
