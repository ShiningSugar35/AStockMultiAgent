from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from astock.core.hashing import content_hash
from astock.research import (
    load_research_skill_registry,
    validate_registry_open_source_audits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_serenity_audits_resolve_precise_files_and_local_contracts() -> None:
    registry = load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml")
    manifests = validate_registry_open_source_audits(registry, PROJECT_ROOT)

    assert {manifest.audit_id for manifest in manifests} == {
        "serenity-muxuuu-c2fe93de-local-v3",
        "serenity-haskaomni-332037ea-local-v3",
    }
    assert all(manifest.license_id == "MIT" for manifest in manifests)
    assert all(not manifest.source_vendored for manifest in manifests)
    assert all(not manifest.normal_runtime_network_required for manifest in manifests)
    assert all(
        content_hash(manifest.local_patch_set) == manifest.local_patch_sha256
        for manifest in manifests
    )
    assert all(manifest.local_adaptation_files for manifest in manifests)
    assert {
        mapping.local_contract_version
        for manifest in manifests
        for mapping in manifest.local_mappings
    } == {
        "industry-bottleneck-v2",
        "event-to-alpha-v2",
        "growth-probability-v2",
        "growth-valuation-v2",
        "daily-trend-health-v2",
        "research-memo-composer-v2",
    }
    assert all(
        content_hash(
            [{"path": item.path, "sha256": item.sha256} for item in manifest.local_adaptation_files]
        )
        == manifest.local_adaptation_sha256
        for manifest in manifests
    )
    external_skills = [
        skill
        for skill in registry.skills
        if any(reference.startswith("audit:") for reference in skill.source_references)
    ]
    assert {skill.skill_id for skill in external_skills} == {
        "IndustryBottleneckSkill",
        "EventToAlphaSkill",
        "GrowthProbabilitySkill",
        "GrowthValuationLens",
        "DailyTrendHealthSkill",
        "ResearchMemoComposer",
    }
    assert all(
        not any(
            reference.startswith("https://github.com/") for reference in skill.source_references
        )
        for skill in external_skills
    )


def test_serenity_audit_rejects_local_adaptation_drift(tmp_path: Path) -> None:
    registry = load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml")
    manifests = validate_registry_open_source_audits(registry, PROJECT_ROOT)
    paths = {
        "configs/research_skills.yaml",
        *registry.open_source_audit_manifest_files,
        *(item.path for manifest in manifests for item in manifest.local_adaptation_files),
    }
    for relative in paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)

    tampered = tmp_path / manifests[0].local_adaptation_files[0].path
    tampered.write_bytes(tampered.read_bytes() + b"\n# tampered\n")

    with pytest.raises(ValueError, match="open-source local adaptation drift"):
        load_research_skill_registry(tmp_path / "configs" / "research_skills.yaml")
