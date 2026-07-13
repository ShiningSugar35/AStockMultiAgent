"""Run, context budget, and Codex write-back contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

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
