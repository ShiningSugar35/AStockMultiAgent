"""Offline verification of fixed open-source method adaptations."""

from __future__ import annotations

import subprocess
from pathlib import Path

from astock.core.hashing import content_hash, sha256_bytes
from astock.schemas import OpenSourceAuditManifest, ResearchSkillRegistry


def load_open_source_audit(path: Path) -> OpenSourceAuditManifest:
    try:
        manifest = OpenSourceAuditManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise ValueError(f"cannot read open-source audit manifest: {path.name}") from exc
    if content_hash(manifest.local_patch_set) != manifest.local_patch_sha256:
        raise ValueError(f"open-source local patch hash mismatch: {manifest.audit_id}")
    adaptation_identity = [
        {"path": item.path, "sha256": item.sha256}
        for item in manifest.local_adaptation_files
    ]
    if content_hash(adaptation_identity) != manifest.local_adaptation_sha256:
        raise ValueError(f"open-source local adaptation hash mismatch: {manifest.audit_id}")
    return manifest


def validate_registry_open_source_audits(
    registry: ResearchSkillRegistry,
    project_root: Path,
) -> list[OpenSourceAuditManifest]:
    manifests = [
        load_open_source_audit(_safe_project_path(project_root, relative))
        for relative in registry.open_source_audit_manifest_files
    ]
    by_id = {manifest.audit_id: manifest for manifest in manifests}
    if len(by_id) != len(manifests):
        raise ValueError("open-source audit ids must be unique")
    skills = {skill.skill_id: skill for skill in registry.skills}
    mapped_contracts: set[str] = set()
    for manifest in manifests:
        for local_file in manifest.local_adaptation_files:
            local_path = _safe_project_path(project_root, local_file.path)
            if not local_path.is_file():
                raise ValueError(
                    f"open-source local adaptation file is missing: {local_file.path}"
                )
            if sha256_bytes(local_path.read_bytes()) != local_file.sha256:
                raise ValueError(
                    f"open-source local adaptation drift: {local_file.path}"
                )
        audited_paths = {item.path for item in manifest.reviewed_files}
        for mapping in manifest.local_mappings:
            skill = skills.get(mapping.local_contract_id)
            if skill is None or skill.skill_version != mapping.local_contract_version:
                raise ValueError(
                    f"open-source mapping does not resolve to the frozen local contract: "
                    f"{mapping.local_contract_id}"
                )
            mapped_contracts.add(mapping.local_contract_id)
            expected_references = {
                f"audit:{manifest.audit_id}#{path}" for path in mapping.upstream_files
            }
            if not expected_references.issubset(skill.source_references):
                raise ValueError(
                    f"local contract lacks precise audited source references: {skill.skill_id}"
                )
            if not set(mapping.upstream_files).issubset(audited_paths):
                raise ValueError("open-source local mapping references an unaudited file")
    for skill in registry.skills:
        for reference in skill.source_references:
            if not reference.startswith("audit:"):
                continue
            audit_id, separator, path = reference.removeprefix("audit:").partition("#")
            manifest = by_id.get(audit_id)
            if not separator or not path or manifest is None:
                raise ValueError(f"invalid open-source audit reference: {reference}")
            if path not in {item.path for item in manifest.reviewed_files}:
                raise ValueError(f"open-source reference is not in its audit: {reference}")
            if skill.skill_id not in mapped_contracts:
                raise ValueError(f"audited Skill lacks a local mapping: {skill.skill_id}")
    return manifests


def verify_open_source_tree(
    manifest: OpenSourceAuditManifest,
    source_root: Path,
) -> dict[str, object]:
    root = source_root.resolve()
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    actual_commit = completed.stdout.strip().casefold() if completed.returncode == 0 else None
    mismatches: list[str] = []
    if actual_commit != manifest.commit_sha:
        mismatches.append("COMMIT_SHA_MISMATCH")
    for audited in manifest.reviewed_files:
        path = (root / audited.path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            mismatches.append(f"MISSING:{audited.path}")
            continue
        if sha256_bytes(path.read_bytes()) != audited.sha256:
            mismatches.append(f"HASH_MISMATCH:{audited.path}")
    return {
        "audit_id": manifest.audit_id,
        "status": "PASS" if not mismatches else "FAIL",
        "commit_sha": actual_commit,
        "reviewed_file_count": len(manifest.reviewed_files),
        "mismatches": mismatches,
    }


def _safe_project_path(project_root: Path, relative: str) -> Path:
    root = project_root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("open-source audit manifest path escapes the project root")
    return path


__all__ = [
    "load_open_source_audit",
    "validate_registry_open_source_audits",
    "verify_open_source_tree",
]
