from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from astock.candidates.discovery_templates import (
    compile_discovery_contract_catalog,
    compile_discovery_thesis_definitions,
)
from astock.candidates.discovery_theses import REQUIRED_ROLES, _method_roles
from astock.core.hashing import content_hash
from astock.research.config import load_research_skill_registry
from astock.schemas.knowledge_completion import KnowledgeDiscoveryCatalog

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_HASH = "a" * 64


def _knowledge_catalog() -> KnowledgeDiscoveryCatalog:
    value = KnowledgeDiscoveryCatalog(
        run_id="direct:test",
        registry_release_id="knowledge:test",
        registry_object_hash=REGISTRY_HASH,
        compiler_version="knowledge-test-v1",
        disposition_batch_hash="b" * 64,
        entries=[],
        member_count=0,
        result_hash="c" * 64,
    )
    return value.model_copy(
        update={
            "result_hash": content_hash(
                value.model_dump(mode="json", exclude={"result_hash"})
            )
        }
    )


def test_real_registry_compiles_exact_reviewed_discovery_contracts() -> None:
    registry = load_research_skill_registry(ROOT / "configs" / "research_skills.yaml")
    catalog = compile_discovery_contract_catalog(registry)
    definitions = compile_discovery_thesis_definitions(catalog)

    assert catalog.registry_version == "research-skills-v4"
    assert len(catalog.templates) == 3
    identities = {
        (template.skill_id, template.skill_version)
        for template in catalog.templates
    }
    assert identities == {
        ("IndustryBottleneckSkill", "industry-bottleneck-v2"),
        ("EventToAlphaSkill", "event-to-alpha-v2"),
        ("JuglarCycleStageSkill", "juglar-cycle-stage-v1"),
    }
    assert {definition.channel for definition in definitions} == set(REQUIRED_ROLES)
    for template in catalog.templates:
        assert {condition.role for condition in template.conditions} == set(
            REQUIRED_ROLES[template.channel]
        )
        assert all(condition.source_references for condition in template.conditions)
        assert len({condition.condition_id for condition in template.conditions}) == len(
            template.conditions
        )
    catalog.verify()
    for template in catalog.templates:
        counterevidence = next(
            condition
            for condition in template.conditions
            if condition.role == "counterevidence_review"
        )
        assert "未发现足以否定" in counterevidence.semantic_question


def test_reviewed_contract_roles_execute_without_polluting_knowledge_catalog() -> None:
    registry = load_research_skill_registry(ROOT / "configs" / "research_skills.yaml")
    contracts = compile_discovery_contract_catalog(registry)
    definitions = {
        definition.channel: definition
        for definition in compile_discovery_thesis_definitions(contracts)
    }
    knowledge = _knowledge_catalog()

    event = _method_roles(
        definitions[next(channel for channel in definitions if channel.value == "EVENT")],
        knowledge,
        REGISTRY_HASH,
        "BANK",
        contracts,
    )
    assert len(event) == len(REQUIRED_ROLES[next(c for c in REQUIRED_ROLES if c.value == "EVENT")])
    assert all(applicable for _, applicable in event.values())

    bottleneck_channel = next(c for c in definitions if c.value == "BOTTLENECK")
    bottleneck_bank = _method_roles(
        definitions[bottleneck_channel],
        knowledge,
        REGISTRY_HASH,
        "BANK",
        contracts,
    )
    assert not any(applicable for _, applicable in bottleneck_bank.values())

    cycle_channel = next(c for c in definitions if c.value == "CYCLE")
    cycle_real_estate = _method_roles(
        definitions[cycle_channel],
        knowledge,
        REGISTRY_HASH,
        "REAL_ESTATE",
        contracts,
    )
    assert all(applicable for _, applicable in cycle_real_estate.values())


def test_contract_catalog_tampering_and_version_drift_fail_closed() -> None:
    registry = load_research_skill_registry(ROOT / "configs" / "research_skills.yaml")
    catalog = compile_discovery_contract_catalog(registry)
    tampered = replace(catalog, result_hash="0" * 64)
    with pytest.raises(ValueError, match="content hash"):
        tampered.verify()

    skills = [
        manifest.model_copy(update={"skill_version": "event-to-alpha-v999"})
        if manifest.skill_id == "EventToAlphaSkill"
        else manifest
        for manifest in registry.skills
    ]
    drifted = registry.model_copy(update={"skills": skills})
    with pytest.raises(ValueError, match="version-drifted"):
        compile_discovery_contract_catalog(drifted)
