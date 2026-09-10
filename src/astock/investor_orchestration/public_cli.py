"""Public CLI paths over verified read-only execution and reproducible answers.

The input file references existing canonical artifacts. It is not permission to
fetch arbitrary URLs, run model code, change an account, or execute a trade.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from pydantic import Field

from astock.core.errors import StorageError
from astock.investor_orchestration.models import InvestorRequestEnvelope, StrictModel
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore, default_state_path


class RegisteredExecutionInput(StrictModel):
    request: InvestorRequestEnvelope
    artifacts: dict[str, tuple[str, ...]] = Field(max_length=64)


def _existing_service(database: Path | None) -> InvestorOrchestrationService:
    path = database or default_state_path()
    if not path.is_file():
        raise ValueError("initialized canonical state is required")
    return InvestorOrchestrationService(InvestorOrchestrationStore(path))


def _unavailable(exc: Exception, *, diagnostics: bool) -> None:
    result = {
        "status": "UNAVAILABLE",
        "message": "该请求的核验结果暂不可用，不能据此发布投资判断。",
    }
    if diagnostics:
        result["error_class"] = type(exc).__name__
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def register_public_commands(app: typer.Typer) -> None:
    @app.command("publish")
    def publish(
        coverage_receipt_id: Annotated[str, typer.Argument()],
        database: Annotated[Path | None, typer.Option()] = None,
        diagnostics: Annotated[
            bool, typer.Option(help="Only reveal allowlisted error diagnostics.")
        ] = False,
    ) -> None:
        """Reproduce an investor answer from its verified receipt, without re-execution."""
        try:
            answer = _existing_service(database).publish_verified(
                coverage_receipt_id=coverage_receipt_id
            )
        except (ValueError, OSError, sqlite3.DatabaseError, StorageError) as exc:
            _unavailable(exc, diagnostics=diagnostics)
            raise typer.Exit(code=2) from None
        typer.echo(answer.model_dump_json(indent=2))
        if answer.degraded:
            raise typer.Exit(code=2)

    @app.command("execute-registered")
    def execute_registered(
        input_file: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
        database: Annotated[Path | None, typer.Option()] = None,
        current: Annotated[
            bool, typer.Option(help="Freeze CURRENT evidence after acquisition completed.")
        ] = False,
        diagnostics: Annotated[
            bool, typer.Option(help="Include allowlisted request and coverage identities.")
        ] = False,
    ) -> None:
        """Validate registered read-only inputs and publish only a reproducible answer."""
        try:
            if input_file.stat().st_size > 2_000_000:
                raise ValueError("registered execution input exceeds its bounded payload size")
            payload = RegisteredExecutionInput.model_validate_json(input_file.read_bytes())
            if sum(len(ids) for ids in payload.artifacts.values()) > 1000:
                raise ValueError("registered execution input exceeds its artifact budget")
            service = _existing_service(database)
            if current:
                execution = service.execute_current_registered(
                    payload.request, artifacts=payload.artifacts
                )
                coverage = execution.coverage
            else:
                _, _, coverage = service.execute_registered(
                    payload.request, artifacts=payload.artifacts
                )
            answer = service.publish_verified(coverage_receipt_id=coverage.receipt_id)
        except (ValueError, OSError, sqlite3.DatabaseError, StorageError) as exc:
            _unavailable(exc, diagnostics=diagnostics)
            raise typer.Exit(code=2) from None
        if diagnostics:
            typer.echo(
                json.dumps(
                    {
                        "request_id": answer.request_id,
                        "coverage_receipt_id": coverage.receipt_id,
                        "coverage_complete": coverage.coverage_complete,
                        "required_capability_coverage": coverage.required_capability_coverage,
                        "answer": answer.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(answer.model_dump_json(indent=2))
        if answer.degraded:
            raise typer.Exit(code=2)
