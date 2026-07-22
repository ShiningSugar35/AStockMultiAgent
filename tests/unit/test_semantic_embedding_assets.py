from __future__ import annotations

from pathlib import Path

import pytest

from astock.knowledge.semantic_embedding import (
    _write_local_model_manifest,
    install_local_model,
    verify_local_model,
)


def test_model_manifest_excludes_cache_and_never_resigns_tampered_weights(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    (model / ".cache" / "huggingface").mkdir(parents=True)
    (model / ".cache" / "huggingface" / "download.json").write_text(
        "volatile",
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"approved-weight")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")

    manifest = _write_local_model_manifest(model)
    assert all(".cache" not in Path(path).parts for path in manifest.files)
    assert verify_local_model(model) == manifest

    (model / "model.safetensors").write_bytes(b"tampered-weight")
    with pytest.raises(ValueError, match="inference asset hash mismatch"):
        install_local_model(model)
