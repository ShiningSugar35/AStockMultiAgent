"""Version-scoped reads of immutable artifacts, without changing their authority.

The ObjectStore verifies original bytes before this decoder is called. Financial
PIT annotations are retired metadata, not replacements for source snapshot IDs.
A sealed historical recommendation cannot be upcast into a current recommendation:
its DAG and semantic hash belong to the original contract and must not be rewritten.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

READ_CONTRACT_VERSION = "registered-artifact-read-v1"


class ArtifactReadError(ValueError):
    """Value-free, bounded diagnostics suitable for an explicit developer view."""

    def __init__(
        self, code: str, *, artifact_type: str, issues: tuple[dict[str, Any], ...] = ()
    ) -> None:
        super().__init__(code)
        self.code = code
        self.artifact_type = artifact_type
        self.issues = issues

    def diagnostic(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "artifact_type": self.artifact_type,
            "issues": list(self.issues),
            "next_action": (
                "RESUME_CURRENT_RESEARCH"
                if self.code == "HISTORICAL_RECEIPT_REQUIRES_REFRESH"
                else "REPAIR_ARTIFACT_CONTRACT"
            ),
        }


def validation_issues(exc: ValidationError, model: type[BaseModel]) -> tuple[dict[str, Any], ...]:
    """Only schema-owned property names may appear; never echo input/msg/ctx."""
    fields: set[str] = set()
    stack: list[Any] = [model.model_json_schema()]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            properties = item.get("properties")
            if isinstance(properties, dict):
                fields.update(properties)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return tuple(
        {
            "type": error["type"],
            "location": [
                part if isinstance(part, int) or part in fields else "<unknown-field>"
                for part in error["loc"]
            ],
        }
        for error in exc.errors(include_url=False, include_context=False, include_input=False)[:12]
    )


def _financial_metadata(payload: dict[str, Any]) -> bool:
    normalized = False
    records: list[Any] = [payload]
    numbers = payload.get("verified_numbers", [])
    if isinstance(numbers, list):
        records.extend(numbers)
    for record in records:
        if not isinstance(record, dict) or "pit_ids" not in record:
            continue
        old = record["pit_ids"]
        if (
            record.get("schema_version", "1.0") != "1.0"
            or not isinstance(old, list)
            or any(not isinstance(value, str) or not value.strip() for value in old)
            or "source_snapshot_ids" not in record
        ):
            raise ArtifactReadError(
                "UNSUPPORTED_LEGACY_FINANCIAL_METADATA",
                artifact_type="FinancialIntegrityEvidencePack",
            )
        # The current source IDs must validate independently. Do not replace them
        # with retired PIT IDs, which have a different meaning and namespace.
        del record["pit_ids"]
        normalized = True
    return normalized


def decode_registered_artifact[ModelT: BaseModel](
    raw: bytes, model: type[ModelT], *, registry_schema_version: str
) -> ModelT:
    """Validate a registered read; never write bytes, IDs, timestamps, or hashes."""
    kind = model.__name__
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ArtifactReadError("INVALID_ARTIFACT_JSON", artifact_type=kind) from exc
    if not isinstance(payload, dict):
        raise ArtifactReadError("INVALID_ARTIFACT_SHAPE", artifact_type=kind)
    version = payload.get("schema_version")
    if version is not None and version != registry_schema_version:
        raise ArtifactReadError("REGISTRY_SCHEMA_VERSION_MISMATCH", artifact_type=kind)
    normalized = False
    if kind == "FinancialIntegrityEvidencePack" and registry_schema_version == "1.0":
        normalized = _financial_metadata(payload)
    if kind == "RecommendationResearchReceipt" and (
        "pit_snapshot" in payload or "broker_execution_allowed" in payload
    ):
        # Copy-on-read alone cannot preserve this receipt's semantic signature.
        # Return an actionable recovery classification rather than minting a new
        # hash or converting old WATCH results into a current research answer.
        raise ArtifactReadError("HISTORICAL_RECEIPT_REQUIRES_REFRESH", artifact_type=kind)
    try:
        result = model.model_validate_json(json.dumps(payload) if normalized else raw)
    except ValidationError as exc:
        raise ArtifactReadError(
            "SCHEMA_VALIDATION_FAILED", artifact_type=kind, issues=validation_issues(exc, model)
        ) from exc
    if getattr(result, "schema_version", registry_schema_version) != registry_schema_version:
        raise ArtifactReadError("REGISTRY_SCHEMA_VERSION_MISMATCH", artifact_type=kind)
    return result
