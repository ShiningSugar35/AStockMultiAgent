"""Additive CLI registration for the recoverable research runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.runtime import ResearchRunService
from astock.research.runtime_readiness import ResearchRuntimeReadinessService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas.research_runtime import ResearchRunRequest, TradingClassificationDraft


def _load_request(path: Path) -> ResearchRunRequest:
    return ResearchRunRequest.model_validate_json(path.read_text(encoding="utf-8"))


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def register_research_runtime_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
    knowledge_provider_factory: Callable[[Any, Any], KnowledgeSkillProvider],
) -> None:
    """Attach staged research and read-only readiness commands to the stable CLI."""

    def runtime() -> ResearchRunService:
        paths, state, objects = services()
        return ResearchRunService(
            project_root=paths.root,
            state=state,
            objects=objects,
            reference_parquet_root=paths.parquet,
            knowledge_provider=knowledge_provider_factory(state, objects),
        )

    def readiness() -> ResearchRuntimeReadinessService:
        paths, state, objects = services()
        return ResearchRuntimeReadinessService(
            project_root=paths.root,
            state=state,
            objects=objects,
            knowledge_provider=knowledge_provider_factory(state, objects),
        )

    @app.command("research-run-plan")
    def research_run_plan(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().plan(_load_request(request_file)))

    @app.command("research-run")
    def research_run(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().run(_load_request(request_file)))

    @app.command("research-run-status")
    def research_run_status(run_id: Annotated[str, typer.Argument()]) -> None:
        result = runtime().status(run_id)
        emit(result if result is not None else {"status": "NOT_RUN", "run_id": run_id})

    @app.command("research-run-audit")
    def research_run_audit(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().audit(run_id))

    @app.command("research-run-recover")
    def research_run_recover(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().recover(run_id))

    @app.command("research-run-benchmark")
    def research_run_benchmark(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().benchmark(_load_request(request_file)))

    @app.command("trading-classification-freeze")
    def trading_classification_freeze(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        draft = TradingClassificationDraft.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(TradingClassificationService(state, objects).freeze(draft))

    @app.command("trading-classification-status")
    def trading_classification_status(
        artifact_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        _, state, objects = services()
        emit(
            TradingClassificationService(state, objects).status(
                artifact_id,
                as_of=_parse_optional_datetime(as_of),
            )
        )

    @app.command("trading-classification-audit")
    def trading_classification_audit(
        artifact_id: Annotated[str, typer.Argument()],
    ) -> None:
        _, state, objects = services()
        emit(TradingClassificationService(state, objects).audit(artifact_id))

    @app.command("research-runtime-readiness")
    def research_runtime_readiness(
        knowledge_run_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(readiness().provider_readiness(knowledge_run_id))

    @app.command("holding-due")
    def holding_due(
        position_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(readiness().holding_due(position_id, as_of=_parse_optional_datetime(as_of)))

    @app.command("holding-prepare")
    def holding_prepare(
        position_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(readiness().holding_prepare(position_id, as_of=_parse_optional_datetime(as_of)))

    @app.command("paper-replay-checkpoint")
    def paper_replay_checkpoint(
        symbol: Annotated[str, typer.Argument()],
        account_id: Annotated[str, typer.Option()] = "default",
    ) -> None:
        emit(readiness().paper_replay_checkpoint(account_id, symbol))

    @app.command("paper-recovery-plan")
    def paper_recovery_plan(
        symbol: Annotated[str, typer.Argument()],
        account_id: Annotated[str, typer.Option()] = "default",
    ) -> None:
        emit(readiness().paper_recovery_plan(account_id, symbol))


__all__ = ["register_research_runtime_commands"]
