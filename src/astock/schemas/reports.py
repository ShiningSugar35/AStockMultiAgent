"""Strict contracts for deterministic formal research report publishing."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from astock.schemas.presentation import (
    InvestorPresentationModel,
    PublicReportReference,
    ResearchNarrativeBundle,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_REPORT_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DEVICE_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)


class _ReportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class ReportFormat(StrEnum):
    DOCX = "DOCX"
    MD = "MD"
    PDF = "PDF"


class ReportStatus(StrEnum):
    PENDING = "PENDING"
    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CONFLICT = "CONFLICT"


class ReportRenderer(StrEnum):
    DOCX_PYTHON_DOCX = "DOCX_PYTHON_DOCX"
    MARKDOWN = "MARKDOWN"
    PDF_EXTERNAL = "PDF_EXTERNAL"


class PrivacyLevel(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL_PRIVATE = "INTERNAL_PRIVATE"
    CONFIDENTIAL = "CONFIDENTIAL"
    PRIVATE = "INTERNAL_PRIVATE"
    INTERNAL = "INTERNAL_PRIVATE"


ReportPrivacy = PrivacyLevel


class CitationLevel(StrEnum):
    NONE = "NONE"
    SUMMARY = "SUMMARY"
    FULL = "FULL"
    STANDARD = "SUMMARY"
    DETAILED = "FULL"


class AssetRightsStatus(StrEnum):
    OWNED = "OWNED"
    LICENSED = "LICENSED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    PUBLIC_DISCLOSURE = "PUBLIC_DISCLOSURE"
    FAIR_USE = "FAIR_USE"
    LINK_ONLY = "LINK_ONLY"
    UNKNOWN = "UNKNOWN"
    UNLICENSED = "UNLICENSED"


AssetRights = AssetRightsStatus


class ReportDirectoryPolicy(StrEnum):
    DEFAULT = "DEFAULT"
    ENV_OVERRIDE = "ENV_OVERRIDE"
    KNOWN_FOLDER_DESKTOP = "KNOWN_FOLDER_DESKTOP"
    CONFIGURED_REPORT_ROOT = "CONFIGURED_REPORT_ROOT"
    CONTROLLED_DIRECTORY = "CONTROLLED_DIRECTORY"
    CUSTOM = "CUSTOM"
    KNOWN_FOLDER = "KNOWN_FOLDER_DESKTOP"


class PreferenceKey(StrEnum):
    DEFAULT_LENGTH = "DEFAULT_LENGTH"
    DEFAULT_REPORT_FORMAT = "DEFAULT_REPORT_FORMAT"
    REPORT_DIRECTORY_POLICY = "REPORT_DIRECTORY_POLICY"
    CITATION_LEVEL = "CITATION_LEVEL"
    PRIVACY_DEFAULT = "PRIVACY_DEFAULT"
    PDF_PREFERENCE = "PDF_PREFERENCE"


class PreferenceLength(StrEnum):
    SHORT = "SHORT"
    STANDARD = "STANDARD"
    DETAILED = "DETAILED"


class PdfPreference(StrEnum):
    AUTO = "AUTO"
    PDF_FIRST = "PDF_FIRST"
    PDF_DISABLED = "PDF_DISABLED"
    OPTIONAL = "AUTO"
    REQUIRED = "PDF_FIRST"
    DISABLED = "PDF_DISABLED"


class UnknownRightsAction(StrEnum):
    EXCLUDE = "EXCLUDE"
    FAIL_CLOSED = "FAIL_CLOSED"


def _validate_safe_file_name(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError("output file name must be non-empty")
    if value != value.strip(" ."):
        raise ValueError("output file name may not have leading/trailing spaces or dots")
    if "/" in value or "\\" in value or ".." in value or _CONTROL_RE.search(value):
        raise ValueError("output file name must not contain path or control characters")
    if _WINDOWS_DEVICE_RE.fullmatch(value):
        raise ValueError("output file name is a reserved device name")
    return value


def _validate_relative_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("report reference must be relative")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("report reference contains an unsafe path component")
    return value


class ReportCitation(_ReportModel):
    schema_version: Literal["report-citation-v1"] = "report-citation-v1"
    citation_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    label: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    source_snapshot_id: str | None = Field(default=None, max_length=200)
    evidence_id: str | None = Field(default=None, max_length=200)
    object_hash: Sha256 | None = None
    retrieved_at: AwareDatetime | None = None


CitationEntry = ReportCitation


class CitationManifest(_ReportModel):
    schema_version: Literal["citation-manifest-v1"] = "citation-manifest-v1"
    level: CitationLevel = CitationLevel.SUMMARY
    citations: list[ReportCitation] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> CitationManifest:
        identifiers = [entry.citation_id for entry in self.citations]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("citation ids must be unique")
        if self.level is CitationLevel.NONE and self.citations:
            raise ValueError("citation level NONE cannot contain citations")
        return self


class ReportAsset(_ReportModel):
    schema_version: Literal["report-asset-v1"] = "report-asset-v1"
    asset_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    file_name: str | None = Field(default=None, max_length=255)
    media_type: str | None = Field(default=None, max_length=120)
    byte_size: int | None = Field(default=None, ge=0)
    local_path: str | None = Field(default=None, max_length=2048, exclude=True)
    source_url: str | None = Field(default=None, max_length=2048)
    source_ref: str | None = Field(default=None, max_length=512)
    object_hash: Sha256 | None = None
    rights: AssetRightsStatus = AssetRightsStatus.UNKNOWN
    rights_note: str | None = Field(default=None, max_length=1000)
    alt_text: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=500)
    excluded: bool = False
    exclusion_reason: str | None = Field(default=None, max_length=200)

    @field_validator("file_name")
    @classmethod
    def _safe_optional_file_name(cls, value: str | None) -> str | None:
        return _validate_safe_file_name(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_asset(self) -> ReportAsset:
        if not self.excluded and not any(
            (self.local_path, self.source_url, self.source_ref, self.object_hash)
        ):
            raise ValueError("asset requires a controlled source reference")
        if self.excluded and not self.exclusion_reason:
            raise ValueError("excluded asset requires an exclusion reason")
        if not self.excluded and self.exclusion_reason:
            raise ValueError("non-excluded asset cannot have an exclusion reason")
        return self


AssetEntry = ReportAsset


class AssetManifest(_ReportModel):
    schema_version: Literal["asset-manifest-v1"] = "asset-manifest-v1"
    assets: list[ReportAsset] = Field(default_factory=list, max_length=64)
    total_byte_size: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_assets(self) -> AssetManifest:
        identifiers = [entry.asset_id for entry in self.assets]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("asset ids must be unique")
        known_total = sum(entry.byte_size or 0 for entry in self.assets)
        if self.total_byte_size not in {0, known_total}:
            raise ValueError("asset manifest total_byte_size does not match entries")
        return self


class PdfConverterCapability(_ReportModel):
    schema_version: Literal["pdf-converter-capability-v1"] = "pdf-converter-capability-v1"
    probe_id: str = Field(min_length=1, max_length=128)
    converter_id: str = Field(min_length=1, max_length=128)
    converter_version: str | None = Field(default=None, max_length=120)
    probed_at: AwareDatetime
    probe_ok: bool
    failure_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _validate_probe(self) -> PdfConverterCapability:
        if self.probe_ok and self.failure_reason:
            raise ValueError("successful converter probe cannot contain a failure reason")
        if not self.probe_ok and not self.failure_reason:
            raise ValueError("failed converter probe requires a failure reason")
        return self


class PresentationPreferences(_ReportModel):
    schema_version: Literal["presentation-preferences-v1"] = "presentation-preferences-v1"
    profile_id: str = Field(default="default", min_length=1, max_length=120)
    default_length: PreferenceLength | None = None
    default_report_format: ReportFormat | None = Field(
        default=None,
        validation_alias=AliasChoices("default_report_format", "default_format"),
    )
    report_directory_policy: ReportDirectoryPolicy | None = None
    custom_report_root: str | None = Field(default=None, max_length=2048, exclude=True)
    citation_level: CitationLevel | None = None
    privacy_default: PrivacyLevel | None = None
    pdf_preference: PdfPreference | None = None

    @model_validator(mode="after")
    def _validate_custom_root(self) -> PresentationPreferences:
        if (
            self.report_directory_policy is ReportDirectoryPolicy.CUSTOM
            and not self.custom_report_root
        ):
            raise ValueError("CUSTOM directory policy requires custom_report_root")
        if (
            self.report_directory_policy is not ReportDirectoryPolicy.CUSTOM
            and self.custom_report_root
        ):
            raise ValueError("custom_report_root is only valid with CUSTOM directory policy")
        return self

    @property
    def default_format(self) -> ReportFormat:
        return self.default_report_format or ReportFormat.DOCX


class PreferenceUpdate(_ReportModel):
    schema_version: Literal["preference-update-v1"] = "preference-update-v1"
    key: PreferenceKey
    value: str = Field(min_length=1, max_length=2048)


class ReportRequest(_ReportModel):
    schema_version: Literal["report-request-v1"] = "report-request-v1"
    report_key: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("report_key", "request_id"),
    )
    title: str = Field(default="AStockMultiAgent 正式研究报告", min_length=1, max_length=200)
    format: ReportFormat | None = Field(
        default=None,
        validation_alias=AliasChoices("format", "preferred_format"),
    )
    narrative: ResearchNarrativeBundle | None = None
    presentation: InvestorPresentationModel | None = None
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=16)
    input_artifact_hashes: list[Sha256] = Field(default_factory=list, max_length=16)
    privacy_level: PrivacyLevel | None = None
    citation_level: CitationLevel | None = None
    include_assets: bool = False
    directory_policy: ReportDirectoryPolicy | None = None
    citations: CitationManifest = Field(default_factory=CitationManifest)
    assets: AssetManifest = Field(default_factory=AssetManifest)
    preferences: PresentationPreferences | None = None
    output_name_hint: str | None = Field(default=None, max_length=180)
    requested_at: AwareDatetime | None = None

    @field_validator("report_key")
    @classmethod
    def _valid_report_key(cls, value: str) -> str:
        if not _REPORT_KEY_RE.fullmatch(value):
            raise ValueError("report_key contains unsupported characters")
        return value

    @field_validator("output_name_hint")
    @classmethod
    def _safe_output_hint(cls, value: str | None) -> str | None:
        if value is not None and _CONTROL_RE.search(value):
            raise ValueError("output_name_hint contains control characters")
        return value

    @model_validator(mode="after")
    def _validate_request(self) -> ReportRequest:
        if self.narrative is None and self.presentation is None and not self.input_artifact_ids:
            raise ValueError(
                "report request requires narrative, presentation, or registered artifact input"
            )
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ValueError("input artifact ids must be unique")
        if len(set(self.input_artifact_hashes)) != len(self.input_artifact_hashes):
            raise ValueError("input artifact hashes must be unique")
        return self

    @property
    def request_id(self) -> str:
        return self.report_key

    @property
    def preferred_format(self) -> ReportFormat | None:
        return self.format


class ReportManifest(_ReportModel):
    schema_version: Literal["report-manifest-v1"] = "report-manifest-v1"
    report_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    request_hash: Sha256
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=16)
    input_artifact_hashes: list[Sha256] = Field(default_factory=list, max_length=32)
    template_version: str = Field(min_length=1, max_length=120)
    renderer: ReportRenderer
    renderer_version: str = Field(min_length=1, max_length=160)
    converter: PdfConverterCapability | None = None
    output_format: ReportFormat | None = None
    privacy_level: PrivacyLevel
    citation_level: CitationLevel
    citations: CitationManifest
    assets: AssetManifest
    output_file_name: str | None = Field(default=None, max_length=255)
    output_relative_ref: str | None = Field(default=None, max_length=512)
    output_sha256: Sha256 | None = None
    output_byte_size: int | None = Field(default=None, ge=0)
    publish_status: ReportStatus
    degradation_reason: str | None = Field(default=None, max_length=200)
    publish_attempts: int = Field(default=1, ge=1)
    destination_policy: ReportDirectoryPolicy
    recovered_existing: bool = False
    created_at: AwareDatetime
    published_at: AwareDatetime | None = None
    manifest_artifact_id: str | None = Field(default=None, max_length=200)
    manifest_object_hash: Sha256 | None = None

    @field_validator("output_file_name")
    @classmethod
    def _safe_output_name(cls, value: str | None) -> str | None:
        return _validate_safe_file_name(value) if value is not None else None

    @field_validator("output_relative_ref")
    @classmethod
    def _safe_relative_ref(cls, value: str | None) -> str | None:
        return _validate_relative_ref(value)

    @model_validator(mode="after")
    def _validate_status(self) -> ReportManifest:
        terminal_success = self.publish_status in {ReportStatus.PUBLISHED, ReportStatus.DEGRADED}
        output_fields = (
            self.output_format,
            self.output_file_name,
            self.output_relative_ref,
            self.output_sha256,
            self.output_byte_size,
            self.published_at,
        )
        if terminal_success and any(value is None for value in output_fields):
            raise ValueError("published report manifest requires complete output metadata")
        if terminal_success and (self.output_byte_size or 0) <= 0:
            raise ValueError("published report output must be non-empty")
        if self.publish_status is ReportStatus.DEGRADED and not self.degradation_reason:
            raise ValueError("degraded report requires a degradation reason")
        if self.publish_status is ReportStatus.PUBLISHED and self.degradation_reason:
            raise ValueError("published report cannot contain a degradation reason")
        if self.publish_status is ReportStatus.CONFLICT and not self.degradation_reason:
            raise ValueError("conflict report requires a reason")
        if (self.manifest_artifact_id is None) != (self.manifest_object_hash is None):
            raise ValueError("manifest artifact id and object hash must be set together")
        return self

    @property
    def request_id(self) -> str:
        return self.report_key

    @property
    def report_id(self) -> str:
        return self.report_key

    @property
    def requested_format(self) -> ReportFormat | None:
        return self.output_format

    @property
    def published_format(self) -> ReportFormat | None:
        return self.output_format

    @property
    def status(self) -> ReportStatus:
        return self.publish_status

    @property
    def public_safe_reference(self) -> str | None:
        return self.output_relative_ref

    @property
    def public_reference(self) -> str | None:
        return self.output_relative_ref

    @property
    def excluded_asset_ids(self) -> list[str]:
        return [asset.asset_id for asset in self.assets.assets if asset.excluded]

    @property
    def converter_version(self) -> str | None:
        return self.converter.converter_version if self.converter else None

    @property
    def idempotency_key(self) -> str:
        return f"report:{self.report_key}"


class ReportPublishResult(_ReportModel):
    schema_version: Literal["report-publish-result-v1"] = "report-publish-result-v1"
    report_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    publish_status: ReportStatus
    output_format: ReportFormat | None = None
    output_sha256: Sha256 | None = None
    output_file_name: str | None = Field(default=None, max_length=255)
    output_relative_ref: str | None = Field(default=None, max_length=512)
    public_reference: PublicReportReference | None = None
    degradation_reason: str | None = Field(default=None, max_length=200)
    published_at: AwareDatetime | None = None
    manifest_artifact_id: str | None = Field(default=None, max_length=200)
    recovered_existing: bool = False
    manifest: ReportManifest

    @field_validator("output_file_name")
    @classmethod
    def _safe_result_name(cls, value: str | None) -> str | None:
        return _validate_safe_file_name(value) if value is not None else None

    @field_validator("output_relative_ref")
    @classmethod
    def _safe_result_ref(cls, value: str | None) -> str | None:
        return _validate_relative_ref(value)

    @model_validator(mode="after")
    def _validate_result(self) -> ReportPublishResult:
        if self.publish_status != self.manifest.publish_status:
            raise ValueError("publish result and manifest statuses must match")
        if self.output_sha256 != self.manifest.output_sha256:
            raise ValueError("publish result and manifest output hashes must match")
        return self

    @property
    def request_id(self) -> str:
        return self.report_key

    @property
    def report_id(self) -> str:
        return self.report_key

    @property
    def status(self) -> ReportStatus:
        return self.publish_status

    @property
    def requested_format(self) -> ReportFormat | None:
        return self.output_format

    @property
    def published_format(self) -> ReportFormat | None:
        return self.output_format

    @property
    def safe_file_name(self) -> str | None:
        return self.output_file_name


__all__ = [
    "AssetEntry",
    "AssetManifest",
    "AssetRights",
    "AssetRightsStatus",
    "CitationEntry",
    "CitationLevel",
    "CitationManifest",
    "PdfConverterCapability",
    "PdfPreference",
    "PreferenceKey",
    "PreferenceLength",
    "PreferenceUpdate",
    "PresentationPreferences",
    "PrivacyLevel",
    "ReportAsset",
    "ReportCitation",
    "ReportDirectoryPolicy",
    "ReportFormat",
    "ReportManifest",
    "ReportPrivacy",
    "ReportPublishResult",
    "ReportRenderer",
    "ReportRequest",
    "ReportStatus",
    "UnknownRightsAction",
]
