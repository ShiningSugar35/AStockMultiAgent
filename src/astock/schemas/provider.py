"""Strict provider-registry and health-probe contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from astock.schemas.market import CompletenessSemantics, SourceClass


class ProviderTransport(StrEnum):
    HTTP = "HTTP"
    SDK_TCP = "SDK_TCP"


class ProviderOfficiality(StrEnum):
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    SECONDARY_STRUCTURED = "SECONDARY_STRUCTURED"


class ProviderProbeMode(StrEnum):
    RECORDED = "RECORDED"
    LIVE = "LIVE"


class ProviderHealthStatus(StrEnum):
    NOT_PROBED = "NOT_PROBED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    CORRUPT = "CORRUPT"


class ProviderProbeFailureCode(StrEnum):
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_429 = "HTTP_429"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    DATA_QUALITY = "DATA_QUALITY"
    CAPABILITY_NOT_PROBED = "CAPABILITY_NOT_PROBED"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderDefinition(_StrictModel):
    provider_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    adapter_class: str = Field(min_length=3)
    capabilities: list[str] = Field(min_length=1)
    source_class: SourceClass
    formal_capabilities: list[str] = Field(default_factory=list)
    completeness_semantics: dict[str, CompletenessSemantics] = Field(default_factory=dict)
    independence_group: str = Field(min_length=1)
    cache_ttl_seconds: int = Field(default=0, ge=0, le=31_536_000)
    cost_class: str = Field(default="LOW", pattern=r"^(LOW|MEDIUM|HIGH)$")
    transport: ProviderTransport
    officiality: ProviderOfficiality
    live_supported: bool
    timeout_seconds: float = Field(gt=0, le=120)
    priority: int = Field(default=100, ge=0, le=10000)
    transport_profile: str | None = None
    fixture_subdir: str | None = None
    probe_operation: str = Field(min_length=1)
    probe_target: dict[str, str | int] = Field(default_factory=dict)
    recorded_fixture: str = Field(min_length=1)
    external_capability_id: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    production_backup: bool = False

    @field_validator("capabilities")
    @classmethod
    def _unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("provider capabilities must be unique")
        if any(not item or item.strip() != item for item in value):
            raise ValueError("provider capabilities must be non-empty and trimmed")
        return value

    @field_validator("recorded_fixture")
    @classmethod
    def _safe_relative_fixture(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ":" in normalized.split("/")[0]:
            raise ValueError("recorded_fixture must be project-relative")
        if ".." in normalized.split("/"):
            raise ValueError("recorded_fixture may not traverse parent directories")
        return normalized

    @field_validator("adapter_class")
    @classmethod
    def _safe_adapter_class(cls, value: str) -> str:
        module, separator, class_name = value.rpartition(":")
        if not separator or not module.startswith("astock.") or not class_name.isidentifier():
            raise ValueError("adapter_class must be astock.module:ClassName")
        return value

    @field_validator("fixture_subdir")
    @classmethod
    def _safe_fixture_subdir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/").strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise ValueError("fixture_subdir must stay inside the configured fixture root")
        return normalized

    @model_validator(mode="after")
    def _source_catalog_contract(self) -> ProviderDefinition:
        capability_set = set(self.capabilities)
        if not set(self.formal_capabilities).issubset(capability_set):
            raise ValueError("formal_capabilities must be a subset of capabilities")
        if not set(self.completeness_semantics).issubset(capability_set):
            raise ValueError("completeness_semantics keys must be provider capabilities")
        if self.source_class is SourceClass.PRIMARY_OFFICIAL_WEB and (
            self.officiality is not ProviderOfficiality.PRIMARY_OFFICIAL
        ):
            raise ValueError("PRIMARY_OFFICIAL_WEB sources must be PRIMARY_OFFICIAL")
        if self.source_class is SourceClass.SECONDARY_STRUCTURED and (
            self.officiality is not ProviderOfficiality.SECONDARY_STRUCTURED
        ):
            raise ValueError("SECONDARY_STRUCTURED sources must use matching officiality")
        if self.production_backup and not self.external_capability_id:
            raise ValueError("production backup provider requires external_capability_id")
        if self.external_capability_id and not self.production_backup:
            raise ValueError("external_capability_id is reserved for production backup providers")
        return self


class ProviderRegistry(_StrictModel):
    registry_version: str = Field(min_length=1)
    capability_gaps: list[str]
    providers: list[ProviderDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_registry_values(self) -> ProviderRegistry:
        provider_ids = [item.provider_id for item in self.providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("provider_id values must be unique")
        if len(set(self.capability_gaps)) != len(self.capability_gaps):
            raise ValueError("capability_gaps must be unique")
        supplied = {capability for item in self.providers for capability in item.capabilities}
        overlap = supplied.intersection(self.capability_gaps)
        if overlap:
            raise ValueError(f"implemented capabilities cannot also be gaps: {sorted(overlap)}")
        return self


class ProviderProbeReport(_StrictModel):
    schema_version: str = "provider-probe-report-v1"
    probe_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str
    registry_version: str
    capability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_mode: ProviderProbeMode
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: int = Field(ge=0)
    status: ProviderHealthStatus
    failure_code: ProviderProbeFailureCode | None = None
    failure_count: int = Field(ge=0, le=1)
    checked_capabilities: list[str]
    capability_gaps: list[str]
    safe_metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_outcome(self) -> ProviderProbeReport:
        if self.status == ProviderHealthStatus.HEALTHY and self.failure_code is not None:
            raise ValueError("HEALTHY report cannot carry failure_code")
        if self.status != ProviderHealthStatus.HEALTHY and self.failure_code is None:
            raise ValueError("non-HEALTHY report requires failure_code")
        expected_failure_marker = 0 if self.status == ProviderHealthStatus.HEALTHY else 1
        if self.failure_count != expected_failure_marker:
            raise ValueError(
                "probe report failure_count is an immutable zero-or-one event marker"
            )
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        if len(set(self.checked_capabilities)) != len(self.checked_capabilities):
            raise ValueError("checked_capabilities must be unique")
        if len(set(self.capability_gaps)) != len(self.capability_gaps):
            raise ValueError("capability_gaps must be unique")
        if set(self.checked_capabilities).intersection(self.capability_gaps):
            raise ValueError("checked capabilities cannot also be capability gaps")
        return self


class ProviderStatusReport(_StrictModel):
    schema_version: str = "provider-status-v1"
    provider_id: str
    registry_version: str
    capabilities: list[str]
    checked_capabilities: list[str]
    capability_gaps: list[str]
    transport: ProviderTransport
    officiality: ProviderOfficiality
    live_supported: bool
    status: ProviderHealthStatus
    last_probe_at: datetime | None = None
    probe_mode: ProviderProbeMode | None = None
    report_artifact_id: str | None = None
    report_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_code: ProviderProbeFailureCode | None = None
    failure_count: int = Field(default=0, ge=0)


__all__ = [
    "ProviderDefinition",
    "ProviderHealthStatus",
    "ProviderOfficiality",
    "ProviderProbeFailureCode",
    "ProviderProbeMode",
    "ProviderProbeReport",
    "ProviderRegistry",
    "ProviderStatusReport",
    "ProviderTransport",
]
