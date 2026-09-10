"""Machine-verifiable audit contracts for adapted open-source methods."""

from __future__ import annotations

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel


class OpenSourceAuditedFile(AStockModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relative_path(self) -> OpenSourceAuditedFile:
        parts = self.path.replace("\\", "/").split("/")
        if self.path.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("audited upstream files require safe relative paths")
        return self


class OpenSourceLocalMapping(AStockModel):
    local_contract_id: str = Field(min_length=1)
    local_contract_version: str = Field(min_length=1)
    upstream_files: list[str] = Field(min_length=1)
    adaptation_decision: str = Field(min_length=1)


class OpenSourceLocalAdaptationRelease(AStockModel):
    release_id: str = Field(min_length=1)
    release_version: str = Field(min_length=1)
    local_adaptation_files: list[OpenSourceAuditedFile] = Field(min_length=1)
    local_adaptation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgrade_policy: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_release(self) -> OpenSourceLocalAdaptationRelease:
        paths = [item.path for item in self.local_adaptation_files]
        if len(paths) != len(set(paths)):
            raise ValueError("open-source local adaptation release paths must be unique")
        return self


class OpenSourceAuditManifest(AStockModel):
    audit_manifest_version: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
    upstream_repository: str = Field(min_length=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    license_id: str = Field(min_length=1)
    license_path: str = Field(min_length=1)
    license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_files: list[OpenSourceAuditedFile] = Field(min_length=1)
    local_mappings: list[OpenSourceLocalMapping] = Field(min_length=1)
    local_patch_set: list[str] = Field(min_length=1)
    local_patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local_adaptation_files: list[OpenSourceAuditedFile] = Field(default_factory=list)
    local_adaptation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    local_adaptation_release_id: str | None = Field(default=None, min_length=1)
    normal_runtime_network_required: bool = False
    source_vendored: bool = False
    upgrade_policy: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest_sets(self) -> OpenSourceAuditManifest:
        paths = [item.path for item in self.reviewed_files]
        if len(paths) != len(set(paths)):
            raise ValueError("open-source audited file paths must be unique")
        if self.license_path not in paths:
            raise ValueError("open-source license must be one of the audited files")
        license_file = next(item for item in self.reviewed_files if item.path == self.license_path)
        if license_file.sha256 != self.license_sha256:
            raise ValueError("open-source license hash must match its audited file")
        contracts = [item.local_contract_id for item in self.local_mappings]
        if len(contracts) != len(set(contracts)):
            raise ValueError("open-source local contract mappings must be unique")
        known_paths = set(paths)
        if any(
            path not in known_paths for item in self.local_mappings for path in item.upstream_files
        ):
            raise ValueError("open-source mappings must reference audited upstream files")
        local_paths = [item.path for item in self.local_adaptation_files]
        if len(local_paths) != len(set(local_paths)):
            raise ValueError("open-source local adaptation file paths must be unique")
        legacy_local = bool(self.local_adaptation_files)
        shared_release = self.local_adaptation_release_id is not None
        if legacy_local == shared_release:
            raise ValueError("open-source audit must use exactly one local adaptation mode")
        if legacy_local and self.local_adaptation_sha256 is None:
            raise ValueError("legacy open-source audit requires a local adaptation hash")
        if shared_release and self.local_adaptation_sha256 is not None:
            raise ValueError("shared open-source audit must not duplicate a local adaptation hash")
        if self.normal_runtime_network_required or self.source_vendored:
            raise ValueError("adapted research methods must remain offline and non-vendored")
        return self


__all__ = [
    "OpenSourceAuditedFile",
    "OpenSourceAuditManifest",
    "OpenSourceLocalAdaptationRelease",
    "OpenSourceLocalMapping",
]
