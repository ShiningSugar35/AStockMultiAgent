"""Run, context budget, and Codex write-back contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class RunMode(StrEnum):
    CODEX_INTERACTIVE = "CODEX_INTERACTIVE"
    DETERMINISTIC = "DETERMINISTIC"
    MANUAL_PACKET = "MANUAL_PACKET"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    NEEDS_INFO = "NEEDS_INFO"
    FAILED = "FAILED"


class CodexArtifactRole(StrEnum):
    PRIMARY = "PRIMARY"
    PARENT = "PARENT"
    CONTEXT = "CONTEXT"


class CodexArtifactReference(AStockModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: CodexArtifactRole = CodexArtifactRole.CONTEXT


class CodexRunInputManifest(AStockModel):
    manifest_version: Literal["codex-run-input-v2"] = "codex-run-input-v2"
    selected_skills: list[str] = Field(default_factory=list)
    artifact_references: list[CodexArtifactReference] = Field(default_factory=list)
    legacy_artifact_paths: list[str] = Field(default_factory=list)
    require_registered_output: bool = False

    @model_validator(mode="after")
    def validate_frozen_inputs(self) -> CodexRunInputManifest:
        for label, values in (
            ("selected Skill", self.selected_skills),
            (
                "artifact id",
                [item.artifact_id for item in self.artifact_references],
            ),
            (
                "artifact object hash",
                [item.object_sha256 for item in self.artifact_references],
            ),
            ("legacy artifact path", self.legacy_artifact_paths),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Codex run {label} values must be unique")
        if self.require_registered_output and not self.artifact_references:
            raise ValueError("strict Codex runs require registered artifact inputs")
        return self


class RunManifest(AStockModel):
    run_id: str
    mode: RunMode
    request_hash: str
    as_of: AwareDatetime
    node_plans: list[dict[str, Any]] = Field(default_factory=list)
    input_hashes: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    policy_version: str
    provider_versions: dict[str, str] = Field(default_factory=dict)
    status: RunStatus = RunStatus.PENDING


class ContextBudgetReport(AStockModel):
    selected_skills: list[str] = Field(default_factory=list)
    selected_artifacts: list[str] = Field(default_factory=list)
    artifact_byte_size: int = Field(default=0, ge=0)
    estimated_text_tokens: int = Field(default=0, ge=0)
    full_documents_to_open: list[str] = Field(default_factory=list)
    evidence_excerpts_to_open: list[str] = Field(default_factory=list)
    expected_browser_steps: int = Field(default=0, ge=0)
    expected_mcp_calls: int = Field(default=0, ge=0)
    expected_api_calls: int = Field(default=0, ge=0)
    duplicate_inputs_avoided: list[str] = Field(default_factory=list)


class CommitteeAccessPolicy(AStockModel):
    """Frozen-input-only contract for an investment committee invocation."""

    frozen_artifact_hashes: list[str] = Field(default_factory=list)
    network_access: Literal[False] = False
    api_access: Literal[False] = False
    mcp_access: Literal[False] = False
    browser_access: Literal[False] = False
    full_document_access: Literal[False] = False
    new_research_allowed: Literal[False] = False
    missing_evidence_action: Literal["NEEDS_INFO"] = "NEEDS_INFO"
    investigation_task_required: Literal[True] = True


class CodexDraft(AStockModel):
    artifact_type: str
    payload: dict[str, Any]
    citations: dict[str, str] = Field(default_factory=dict)
    requested_commands: list[dict[str, Any]] = Field(default_factory=list)


class ValidationReport(AStockModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifact_hash: str | None = None
