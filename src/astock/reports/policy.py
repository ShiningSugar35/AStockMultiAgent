"""Versioned, fail-closed formal-report publishing policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astock.schemas.reports import (
    CitationLevel,
    PrivacyLevel,
    ReportDirectoryPolicy,
    ReportFormat,
    UnknownRightsAction,
)


@dataclass(frozen=True, slots=True)
class ReportDirectoryPolicyConfig:
    windows_known_folder: str
    server_report_root: Path | None
    fallback: ReportDirectoryPolicy
    allow_env_override: bool
    report_root_env: str


@dataclass(frozen=True, slots=True)
class PdfPolicy:
    enabled: bool
    converter: str
    command: str
    cli_args: tuple[str, ...]
    command_env: str
    probe_timeout_seconds: int
    convert_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ReportDefaults:
    privacy_level: PrivacyLevel
    citation_level: CitationLevel
    include_assets: bool


@dataclass(frozen=True, slots=True)
class AssetPolicy:
    max_assets: int
    max_total_bytes: int
    max_asset_bytes: int
    allowed_media_types: tuple[str, ...]
    unknown_rights: UnknownRightsAction


@dataclass(frozen=True, slots=True)
class StagingPolicy:
    keep_on_error: bool


@dataclass(frozen=True, slots=True)
class ReportPolicy:
    schema_version: str
    default_format: ReportFormat
    renderer_order: tuple[ReportFormat, ...]
    template_version: str
    directory_policy: ReportDirectoryPolicyConfig
    pdf: PdfPolicy
    defaults: ReportDefaults
    assets: AssetPolicy
    staging: StagingPolicy

    @property
    def report_root_env(self) -> str:
        return self.directory_policy.report_root_env

    @property
    def pdf_converter_env(self) -> str:
        return self.pdf.command_env

    @property
    def max_assets(self) -> int:
        return self.assets.max_assets

    @property
    def max_asset_bytes(self) -> int:
        return self.assets.max_asset_bytes

    @property
    def max_citations(self) -> int:
        return 128

    @property
    def pdf_converter_timeout_seconds(self) -> int:
        return self.pdf.convert_timeout_seconds


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _strict_keys(value: Mapping[str, Any], *, name: str, allowed: set[str]) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown:
        raise ValueError(f"{name} contains unsupported keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{name} is missing keys: {sorted(missing)}")


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _strict_bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _non_empty_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _unique_strings(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    values = tuple(_non_empty_string(item, name=name) for item in value)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def load_report_policy(path: Path) -> ReportPolicy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    top = _mapping(raw, name="report policy")
    top_keys = {
        "schema_version",
        "default_format",
        "renderer_order",
        "template_version",
        "directory_policy",
        "pdf",
        "defaults",
        "assets",
        "staging",
    }
    _strict_keys(top, name="report policy", allowed=top_keys)
    if top["schema_version"] != "report-policy-v1":
        raise ValueError("unsupported report policy schema_version")

    renderer_values = _unique_strings(top["renderer_order"], name="renderer_order")
    renderer_order = tuple(ReportFormat(item) for item in renderer_values)
    if ReportFormat.MD not in renderer_order:
        raise ValueError("renderer_order must include the deterministic MD fallback")

    directory = _mapping(top["directory_policy"], name="directory_policy")
    _strict_keys(
        directory,
        name="directory_policy",
        allowed={
            "windows_known_folder",
            "server_report_root",
            "fallback",
            "allow_env_override",
            "report_root_env",
        },
    )
    known_folder = _non_empty_string(directory["windows_known_folder"], name="windows_known_folder")
    if known_folder != "DESKTOP":
        raise ValueError("only the DESKTOP known folder is supported")
    server_root_raw = directory["server_report_root"]
    if server_root_raw is not None and not isinstance(server_root_raw, str):
        raise ValueError("server_report_root must be null or a path string")
    server_root = Path(server_root_raw).expanduser() if server_root_raw else None
    fallback = ReportDirectoryPolicy(str(directory["fallback"]))
    if fallback is not ReportDirectoryPolicy.CONTROLLED_DIRECTORY:
        raise ValueError("directory fallback must be CONTROLLED_DIRECTORY")

    pdf = _mapping(top["pdf"], name="pdf")
    _strict_keys(
        pdf,
        name="pdf",
        allowed={
            "enabled",
            "converter",
            "command",
            "cli_args",
            "command_env",
            "probe_timeout_seconds",
            "convert_timeout_seconds",
        },
    )
    converter = _non_empty_string(pdf["converter"], name="pdf.converter")
    if converter != "external_cli":
        raise ValueError("only the external_cli PDF converter is supported")

    defaults = _mapping(top["defaults"], name="defaults")
    _strict_keys(
        defaults,
        name="defaults",
        allowed={"privacy_level", "citation_level", "include_assets"},
    )

    assets = _mapping(top["assets"], name="assets")
    _strict_keys(
        assets,
        name="assets",
        allowed={
            "max_assets",
            "max_total_bytes",
            "max_asset_bytes",
            "allowed_media_types",
            "unknown_rights",
        },
    )
    allowed_media_types = _unique_strings(
        assets["allowed_media_types"], name="assets.allowed_media_types"
    )
    if any(not item.startswith("image/") for item in allowed_media_types):
        raise ValueError("report assets may only allow image media types")
    max_total_bytes = _positive_int(assets["max_total_bytes"], name="assets.max_total_bytes")
    max_asset_bytes = _positive_int(assets["max_asset_bytes"], name="assets.max_asset_bytes")
    if max_asset_bytes > max_total_bytes:
        raise ValueError("max_asset_bytes cannot exceed max_total_bytes")

    staging = _mapping(top["staging"], name="staging")
    _strict_keys(staging, name="staging", allowed={"keep_on_error"})

    return ReportPolicy(
        schema_version="report-policy-v1",
        default_format=ReportFormat(str(top["default_format"])),
        renderer_order=renderer_order,
        template_version=_non_empty_string(top["template_version"], name="template_version"),
        directory_policy=ReportDirectoryPolicyConfig(
            windows_known_folder=known_folder,
            server_report_root=server_root,
            fallback=fallback,
            allow_env_override=_strict_bool(
                directory["allow_env_override"], name="directory_policy.allow_env_override"
            ),
            report_root_env=_non_empty_string(
                directory["report_root_env"], name="directory_policy.report_root_env"
            ),
        ),
        pdf=PdfPolicy(
            enabled=_strict_bool(pdf["enabled"], name="pdf.enabled"),
            converter=converter,
            command=_non_empty_string(pdf["command"], name="pdf.command"),
            cli_args=_unique_strings(pdf["cli_args"], name="pdf.cli_args"),
            command_env=_non_empty_string(pdf["command_env"], name="pdf.command_env"),
            probe_timeout_seconds=_positive_int(
                pdf["probe_timeout_seconds"], name="pdf.probe_timeout_seconds"
            ),
            convert_timeout_seconds=_positive_int(
                pdf["convert_timeout_seconds"], name="pdf.convert_timeout_seconds"
            ),
        ),
        defaults=ReportDefaults(
            privacy_level=PrivacyLevel(str(defaults["privacy_level"])),
            citation_level=CitationLevel(str(defaults["citation_level"])),
            include_assets=_strict_bool(defaults["include_assets"], name="defaults.include_assets"),
        ),
        assets=AssetPolicy(
            max_assets=_positive_int(assets["max_assets"], name="assets.max_assets"),
            max_total_bytes=max_total_bytes,
            max_asset_bytes=max_asset_bytes,
            allowed_media_types=allowed_media_types,
            unknown_rights=UnknownRightsAction(str(assets["unknown_rights"])),
        ),
        staging=StagingPolicy(
            keep_on_error=_strict_bool(staging["keep_on_error"], name="staging.keep_on_error")
        ),
    )


__all__ = [
    "AssetPolicy",
    "PdfPolicy",
    "ReportDefaults",
    "ReportDirectoryPolicyConfig",
    "ReportPolicy",
    "StagingPolicy",
    "load_report_policy",
]
