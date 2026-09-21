"""Quality receipts include effective audit metadata, without hashing runtime noise."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_local_quality


@pytest.mark.parametrize("mutation", ["add", "replace", "remove", "rename"])
def test_quality_fingerprint_tracks_runtime_audit_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    source = tmp_path / "src" / "stable.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    audit = tmp_path / "third_party" / "audits" / "serenity" / "active.json"
    audit.parent.mkdir(parents=True)
    if mutation != "add":
        audit.write_text('{"local_adaptation_sha256": "old"}\n', encoding="utf-8")
    before = run_local_quality.fingerprint()
    if mutation == "remove":
        audit.unlink()
    elif mutation == "rename":
        audit.rename(audit.with_name("replacement.json"))
    else:
        audit.write_text('{"local_adaptation_sha256": "new"}\n', encoding="utf-8")
    assert run_local_quality.fingerprint() != before
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize(
    "relative",
    [
        ".ai-bridge/quality-runs/new/result.json",
        "runtime/latest.json",
        "data/objects/raw.json",
        "third_party/vendor/large-source.txt",
        "src/__pycache__/cached.pyc",
    ],
)
def test_quality_fingerprint_ignores_its_output_and_bulk_runtime_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    before = run_local_quality.fingerprint()
    noise = tmp_path / relative
    noise.parent.mkdir(parents=True)
    noise.write_bytes(b"not an input to the source-tree quality gate")
    assert run_local_quality.fingerprint() == before
