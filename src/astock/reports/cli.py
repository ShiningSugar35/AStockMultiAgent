"""Machine-JSON CLI registration for formal reports and presentation preferences."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from astock.core.errors import AStockError, FailureClass, PublicErrorMapper
from astock.reports.policy import load_report_policy
from astock.reports.preferences import PresentationPreferencesRepository
from astock.reports.service import ReportService
from astock.schemas.reports import (
    AssetManifest,
    CitationManifest,
    PdfConverterCapability,
    PreferenceKey,
    PresentationPreferences,
    ReportManifest,
    ReportPublishResult,
    ReportRequest,
    ReportStatus,
)
from astock.settings import ProjectPaths

_FIELD_BY_KEY: dict[PreferenceKey, str] = {
    PreferenceKey.DEFAULT_LENGTH: "default_length",
    PreferenceKey.DEFAULT_REPORT_FORMAT: "default_report_format",
    PreferenceKey.REPORT_DIRECTORY_POLICY: "report_directory_policy",
    PreferenceKey.CITATION_LEVEL: "citation_level",
    PreferenceKey.PRIVACY_DEFAULT: "privacy_default",
    PreferenceKey.PDF_PREFERENCE: "pdf_preference",
}


def register_report_commands(
    app: typer.Typer,
    services: Callable[[], tuple[ProjectPaths, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def fail(exc: BaseException, *, event: str) -> NoReturn:
        failure: BaseException | FailureClass
        if isinstance(exc, AStockError):
            failure = exc
        elif isinstance(exc, OSError):
            failure = FailureClass.STORAGE
        elif isinstance(exc, (ValueError, ValidationError)):
            failure = FailureClass.POLICY_REJECTED
        else:
            failure = FailureClass.INTERNAL
        summary = PublicErrorMapper.record(
            failure,
            component="reports.cli",
            event=event,
        )
        emit(
            {
                "status": "FAILED",
                "failure_code": summary.failure_class,
                "correlation_id": summary.correlation_id,
                "message": summary.investor_message,
            }
        )
        raise typer.Exit(code=2)

    @app.command("report-schema")
    def report_schema() -> None:
        emit(
            {
                "schema_version": "formal-report-cli-schema-v1",
                "models": {
                    "ReportRequest": ReportRequest.model_json_schema(),
                    "CitationManifest": CitationManifest.model_json_schema(),
                    "AssetManifest": AssetManifest.model_json_schema(),
                    "PdfConverterCapability": PdfConverterCapability.model_json_schema(),
                    "ReportManifest": ReportManifest.model_json_schema(),
                    "ReportPublishResult": ReportPublishResult.model_json_schema(),
                    "PresentationPreferences": PresentationPreferences.model_json_schema(),
                },
                "broker_execution_allowed": False,
            }
        )

    @app.command("preference-get")
    def preference_get(
        key: Annotated[PreferenceKey | None, typer.Argument()] = None,
    ) -> None:
        try:
            _paths, state, _objects = services()
            payload = PresentationPreferencesRepository(state).export_safe()
            if key is None:
                emit(payload)
            else:
                emit({"key": key.value, "value": payload[_FIELD_BY_KEY[key]]})
        except Exception as exc:
            fail(exc, event="preference_get_failed")

    @app.command("preference-set")
    def preference_set(
        key: Annotated[PreferenceKey, typer.Argument()],
        value: Annotated[str, typer.Argument()],
        override: Annotated[bool, typer.Option("--override")] = False,
    ) -> None:
        try:
            _paths, state, _objects = services()
            repository = PresentationPreferencesRepository(state)
            preferences = (
                repository.override(key, value) if override else repository.set(key, value)
            )
            emit(
                {
                    "status": "SAVED",
                    "key": key.value,
                    "layer": "OVERRIDE" if override else "BASE",
                    "preferences": preferences.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            fail(exc, event="preference_set_failed")

    @app.command("preference-delete")
    def preference_delete(key: Annotated[PreferenceKey, typer.Argument()]) -> None:
        try:
            _paths, state, _objects = services()
            preferences = PresentationPreferencesRepository(state).delete(key)
            emit(
                {
                    "status": "OVERRIDE_DELETED",
                    "key": key.value,
                    "preferences": preferences.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            fail(exc, event="preference_delete_failed")

    @app.command("preference-reset")
    def preference_reset(
        key: Annotated[PreferenceKey | None, typer.Argument()] = None,
    ) -> None:
        try:
            _paths, state, _objects = services()
            preferences = PresentationPreferencesRepository(state).reset(key)
            emit(
                {
                    "status": "RESET",
                    "key": key.value if key else None,
                    "preferences": preferences.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            fail(exc, event="preference_reset_failed")

    @app.command("report-policy-status")
    def report_policy_status() -> None:
        try:
            paths, _state, _objects = services()
            policy = load_report_policy(paths.report_policy)
            emit(
                {
                    "schema_version": policy.schema_version,
                    "default_format": policy.default_format.value,
                    "renderer_order": [item.value for item in policy.renderer_order],
                    "template_version": policy.template_version,
                    "pdf_enabled": policy.pdf.enabled,
                    "pdf_converter_kind": policy.pdf.converter,
                    "unknown_asset_rights": policy.assets.unknown_rights.value,
                    "broker_execution_allowed": False,
                }
            )
        except Exception as exc:
            fail(exc, event="report_policy_status_failed")

    @app.command("report-publish")
    def report_publish(
        request_file: Annotated[
            Path,
            typer.Argument(file_okay=True, dir_okay=False, resolve_path=False),
        ],
    ) -> None:
        try:
            paths, state, objects = services()
            request = ReportRequest.model_validate_json(request_file.read_bytes())
            result = ReportService(paths, state, objects).publish(request)
            emit(result)
            if result.publish_status is ReportStatus.CONFLICT:
                raise typer.Exit(code=2)
        except typer.Exit:
            raise
        except Exception as exc:
            fail(exc, event="report_publish_failed")

    @app.command("report-status")
    def report_status(report_key: Annotated[str, typer.Argument()]) -> None:
        try:
            paths, state, objects = services()
            view = ReportService(paths, state, objects).status_view(report_key)
            emit(view if view is not None else {"status": "NOT_FOUND", "report_key": report_key})
        except Exception as exc:
            fail(exc, event="report_status_failed")

    @app.command("report-recover")
    def report_recover(report_key: Annotated[str, typer.Argument()]) -> None:
        try:
            paths, state, objects = services()
            emit(ReportService(paths, state, objects).recover(report_key))
        except Exception as exc:
            fail(exc, event="report_recover_failed")

    # Backward-compatible aliases for the abandoned partial command names. They preserve
    # machine output while routing through the canonical repository semantics.
    @app.command("presentation-preferences-get", hidden=True)
    def presentation_preferences_get() -> None:
        preference_get(None)

    @app.command("presentation-preferences-set", hidden=True)
    def presentation_preferences_set(
        request_file: Annotated[
            Path,
            typer.Argument(file_okay=True, dir_okay=False, resolve_path=False),
        ],
    ) -> None:
        try:
            _paths, state, _objects = services()
            preferences = PresentationPreferences.model_validate_json(request_file.read_bytes())
            saved = PresentationPreferencesRepository(state).set(preferences)
            emit({"status": "SAVED", "preferences": saved.model_dump(mode="json")})
        except Exception as exc:
            fail(exc, event="presentation_preferences_set_failed")

    @app.command("presentation-preferences-delete", hidden=True)
    def presentation_preferences_delete() -> None:
        try:
            _paths, state, _objects = services()
            emit(
                {
                    "status": "RESET",
                    "preferences": PresentationPreferencesRepository(state)
                    .reset()
                    .model_dump(mode="json"),
                }
            )
        except Exception as exc:
            fail(exc, event="presentation_preferences_delete_failed")


__all__ = ["register_report_commands"]
