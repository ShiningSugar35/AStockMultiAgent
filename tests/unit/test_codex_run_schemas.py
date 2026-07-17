from __future__ import annotations

import pytest
from pydantic import ValidationError

from astock.schemas import (
    CodexArtifactReference,
    CodexRunInputManifest,
)


def test_strict_codex_manifest_requires_registered_inputs() -> None:
    with pytest.raises(ValidationError, match="require registered artifact inputs"):
        CodexRunInputManifest(
            legacy_artifact_paths=["private-local-path.json"],
            require_registered_output=True,
        )


def test_codex_manifest_rejects_duplicate_artifact_ids_and_hashes() -> None:
    first = CodexArtifactReference(
        artifact_id="BaseCasePack:base:1",
        artifact_type="BaseCasePack",
        object_sha256="a" * 64,
    )
    with pytest.raises(ValidationError, match="artifact id values must be unique"):
        CodexRunInputManifest(artifact_references=[first, first])
    with pytest.raises(ValidationError, match="artifact object hash values must be unique"):
        CodexRunInputManifest(
            artifact_references=[
                first,
                first.model_copy(update={"artifact_id": "BaseCasePack:base:2"}),
            ]
        )
