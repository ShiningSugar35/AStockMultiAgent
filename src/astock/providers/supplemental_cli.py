"""Agent tool surface for optional public data; never bypasses formal acquisition."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.core.credentials import resolve_credential
from astock.providers.config import load_provider_registry
from astock.providers.supplemental import SupplementalEvidenceService, SupplementalRequest


def register_supplemental_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    @app.command("research-supplemental-schema")
    def supplemental_schema() -> None:
        emit(SupplementalRequest.model_json_schema())

    @app.command("provider-credentials-status")
    def provider_credentials_status() -> None:
        paths, _, _ = services()
        registry = load_provider_registry(paths.root / "configs" / "provider_registry.yaml")
        providers = []
        for definition in registry.providers:
            name = definition.credential_environment
            if not name:
                continue
            status = resolve_credential(name, project_root=paths.root).public_status()
            providers.append({"provider_id": definition.provider_id, **status})
        emit(
            {
                "schema_version": "provider-credential-status-v1",
                "dotenv_file": ".env",
                "providers": providers,
                "secret_values_returned": False,
            }
        )

    @app.command("research-supplemental-acquire")
    def supplemental_acquire(
        request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        live: Annotated[bool, typer.Option("--live/--recorded")] = False,
    ) -> None:
        request = SupplementalRequest.model_validate_json(request_file.read_bytes())
        paths, state, objects = services()
        emit(SupplementalEvidenceService(paths.root, state, objects).collect(request, live=live))
