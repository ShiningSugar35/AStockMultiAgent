"""CLI registration for knowledge-storage compaction and cold archives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Any

import typer

from astock.knowledge.cold_archive import KnowledgeColdArchiveService
from astock.knowledge.parquet_compaction import ParquetKnowledgeCompactor


def register_knowledge_storage_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def archive_service() -> KnowledgeColdArchiveService:
        paths, state, objects = services()
        return KnowledgeColdArchiveService(state, objects, runtime_root=paths.runtime)

    @app.command("knowledge-cold-archive-plan")
    def cold_archive_plan() -> None:
        plan = archive_service().plan()
        emit(
            {
                "status": "PLANNED",
                "latest_migration": plan.latest_migration,
                "source_db_size_bytes": plan.source_db_size_bytes,
                "archive_row_count": plan.archive_row_count,
                "protected_row_count": plan.protected_row_count,
                "tables": [asdict(item) for item in plan.tables],
            }
        )

    @app.command("knowledge-cold-archive-run")
    def cold_archive_run(
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED"})
            raise typer.Exit(code=2)
        emit(archive_service().archive())

    @app.command("knowledge-cold-archive-audit")
    def cold_archive_audit(
        archive_id: Annotated[str | None, typer.Option("--archive-id")] = None,
    ) -> None:
        report = archive_service().audit(archive_id)
        emit(report)
        if report["status"] not in {"PASS", "NOT_FOUND"}:
            raise typer.Exit(code=3)

    @app.command("knowledge-cold-archive-restore")
    def cold_archive_restore(
        archive_id: Annotated[str, typer.Argument()],
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED", "archive_id": archive_id})
            raise typer.Exit(code=2)
        emit(archive_service().restore(archive_id))

    @app.command("knowledge-parquet-compact")
    def parquet_compact(
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED"})
            raise typer.Exit(code=2)
        paths, _, _ = services()
        emit(ParquetKnowledgeCompactor(paths.parquet).compact_all())

    @app.command("knowledge-parquet-compact-audit")
    def parquet_compact_audit() -> None:
        paths, _, _ = services()
        report = ParquetKnowledgeCompactor(paths.parquet).audit()
        emit(report)
        if report["status"] != "PASS":
            raise typer.Exit(code=3)

    @app.command("state-vacuum")
    def state_vacuum(
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED"})
            raise typer.Exit(code=2)
        _, state, _ = services()
        emit({"status": "VACUUMED", **state.vacuum()})

    @app.command("knowledge-storage-compact")
    def storage_compact(
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        """Cold-archive historical knowledge, compact tiny Parquet, then VACUUM once."""

        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED"})
            raise typer.Exit(code=2)
        paths, state, objects = services()
        archive = KnowledgeColdArchiveService(
            state,
            objects,
            runtime_root=paths.runtime,
        ).archive()
        parquet = ParquetKnowledgeCompactor(paths.parquet).compact_all()
        vacuum = state.vacuum()
        emit(
            {
                "status": "COMPACTED",
                "archive": archive,
                "parquet": parquet,
                "vacuum": vacuum,
            }
        )


__all__ = ["register_knowledge_storage_commands"]
