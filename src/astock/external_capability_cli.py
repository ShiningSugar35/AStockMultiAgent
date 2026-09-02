"""CLI surface for optional external capability qualification and revocation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.external_capabilities import ExternalCapabilityService
from astock.schemas.external_capabilities import (
    CapabilityQualificationReport,
    CapabilityQualificationRequest,
    CapabilityRevocation,
)
from astock.settings import ProjectPaths


def register_external_capability_commands(
    app: typer.Typer,
    services: Callable[[], tuple[ProjectPaths, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("external-capability-schema")
    def external_capability_schema() -> None:
        emit(
            {
                "qualification_request": CapabilityQualificationRequest.model_json_schema(),
                "qualification_report": CapabilityQualificationReport.model_json_schema(),
                "revocation": CapabilityRevocation.model_json_schema(),
            }
        )

    @app.command("external-capability-list")
    def external_capability_list() -> None:
        paths, state, objects = services()
        service = ExternalCapabilityService(paths.root, state, objects)
        emit(
            {
                "registry_version": service.registry.registry_version,
                "capabilities": [
                    service.status(item.capability_id) for item in service.registry.capabilities
                ],
            }
        )

    @app.command("external-capability-status")
    def external_capability_status(capability_id: Annotated[str, typer.Argument()]) -> None:
        paths, state, objects = services()
        emit(ExternalCapabilityService(paths.root, state, objects).status(capability_id))

    @app.command("external-capability-qualify")
    def external_capability_qualify(
        report_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        paths, state, objects = services()
        report = CapabilityQualificationReport.model_validate_json(report_file.read_bytes())
        saved = ExternalCapabilityService(paths.root, state, objects).register_qualification(report)
        emit(saved)

    @app.command("external-capability-revoke")
    def external_capability_revoke(
        revocation_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        paths, state, objects = services()
        revocation = CapabilityRevocation.model_validate_json(revocation_file.read_bytes())
        saved = ExternalCapabilityService(paths.root, state, objects).revoke(revocation)
        emit(saved)


__all__ = ["register_external_capability_commands"]
