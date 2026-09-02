"""CLI registration for storage lifecycle and operations SLO commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import typer

from astock.operations import StorageLifecyclePlan, StorageLifecycleService


def register_operations_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def lifecycle_service() -> StorageLifecycleService:
        paths, state, objects = services()
        return StorageLifecycleService(paths, state, objects)

    @app.command("storage-lifecycle-plan")
    def storage_lifecycle_plan() -> None:
        """Scan eligible cleanup candidates without deleting anything."""
        service = lifecycle_service()
        plan = service.plan()
        service.persist_plan(plan)
        emit(
            {
                "status": "PLANNED",
                "plan_id": plan.plan_id,
                "scanned_file_count": plan.scanned_file_count,
                "scanned_bytes": plan.scanned_bytes,
                "eligible_file_count": plan.eligible_file_count,
                "eligible_bytes": plan.eligible_bytes,
                "referenced_object_count": plan.referenced_object_count,
                "scan_truncated": plan.scan_truncated,
                "runtime_bytes": plan.runtime_bytes,
                "object_store_bytes": plan.object_store_bytes,
                "temp_bytes": plan.temp_bytes,
                "report_bytes": plan.report_bytes,
                "watermark_status": plan.watermark_status,
                "deletion_requires_confirmation": plan.deletion_requires_confirmation,
                "candidates": [item.model_dump(mode="json") for item in plan.candidates],
            }
        )

    @app.command("storage-lifecycle-audit")
    def storage_lifecycle_audit(
        plan_id: Annotated[str, typer.Argument(help="Plan ID from a prior plan run.")],
    ) -> None:
        """Audit a lifecycle plan for safety before running deletion."""
        service = lifecycle_service()
        plan = _load_cached_plan(service, plan_id)
        if plan is None:
            emit({"status": "PLAN_NOT_FOUND", "plan_id": plan_id})
            raise typer.Exit(code=2)
        report = service.audit(plan)
        service.record_audit(report)
        emit(
            {
                "status": report.status,
                "plan_id": report.plan_id,
                "finding_codes": report.finding_codes,
                "protected_referenced_objects": report.protected_referenced_objects,
                "eligible_file_count": report.eligible_file_count,
                "eligible_bytes": report.eligible_bytes,
            }
        )
        if report.status != "PASS":
            raise typer.Exit(code=3)

    @app.command("storage-lifecycle-run")
    def storage_lifecycle_run(
        plan_id: Annotated[str, typer.Argument(help="Plan ID from a prior plan run.")],
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        """Execute deletion for an audited plan. Requires --confirm."""
        if not confirm:
            emit({"status": "CONFIRMATION_REQUIRED", "plan_id": plan_id})
            raise typer.Exit(code=2)
        service = lifecycle_service()
        plan = _load_cached_plan(service, plan_id)
        if plan is None:
            emit({"status": "PLAN_NOT_FOUND", "plan_id": plan_id})
            raise typer.Exit(code=2)
        try:
            run = service.run(plan, confirm=True)
            service.record_run(run)
        except ValueError as exc:
            emit({"status": "REJECTED", "error": str(exc)})
            raise typer.Exit(code=3) from exc
        emit(
            {
                "status": "COMPLETED",
                "plan_id": run.plan_id,
                "confirmed": run.confirmed,
                "deleted_file_count": run.deleted_file_count,
                "deleted_bytes": run.deleted_bytes,
                "skipped_file_count": run.skipped_file_count,
                "skip_reasons": run.skip_reasons,
            }
        )

    @app.command("operations-slo-report")
    def operations_slo_report() -> None:
        """Generate and persist the current operational SLO report."""
        service = lifecycle_service()
        report = service.operations_slo_report()
        service.record_slo_snapshot(report)
        emit(report)


def _load_cached_plan(
    service: StorageLifecycleService, plan_id: str
) -> StorageLifecyclePlan | None:
    """Load the exact persisted plan so audit/run can resume across CLI processes."""
    return service.load_plan(plan_id)


__all__ = ["register_operations_commands"]
