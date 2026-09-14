from __future__ import annotations

from pathlib import Path

from astock.research.industry_archetypes import IndustryResearchRegistry
from astock.research.industry_methodologies import IndustryMethodologyRegistry
from astock.schemas.industry_methodologies import IndustryMethodologySourceKind
from astock.schemas.research_team import IndustryResearchArchetype

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _registry() -> IndustryMethodologyRegistry:
    archetypes = IndustryResearchRegistry.load(
        PROJECT_ROOT / "configs" / "industry_research_archetypes.yaml"
    )
    return IndustryMethodologyRegistry.load(
        PROJECT_ROOT / "configs" / "industry_methodologies.yaml",
        archetypes=archetypes,
    )


def test_public_core_methodologies_cover_every_known_industry_archetype() -> None:
    registry = _registry()
    inventory = registry.inventory()

    assert inventory["known_archetype_count"] == 22
    assert inventory["covered_archetype_count"] == 22
    assert inventory["uncovered_archetype_ids"] == []
    source_count = inventory["source_count"]
    methodology_count = inventory["methodology_count"]
    assert isinstance(source_count, int) and source_count >= 8
    assert isinstance(methodology_count, int) and methodology_count >= 10
    assert not inventory["private_skill_required_for_analysis"]
    assert not inventory["specialist_draft_required"]
    assert not inventory["fact_authority_allowed"]
    assert not inventory["recommendation_allowed"]


def test_bank_methodology_uses_public_core_without_private_skill_or_specialist_draft() -> None:
    bundle = _registry().resolve("城商行银行")

    assert bundle.status == "MATCHED"
    assert bundle.archetype_id == "BANK"
    assert "GENERIC_COMPETITIVE_CORE" in bundle.methodology_ids
    assert "BANKING_CORE" in bundle.methodology_ids
    assert {item.source_id for item in bundle.sources} >= {
        "acca-cfa-sector-analysis",
        "cfa-industry-competitive-analysis",
        "damodaran-financial-services-valuation",
        "fdic-qbp",
    }
    assert not bundle.private_skill_required_for_analysis
    assert not bundle.specialist_draft_required
    assert not bundle.fact_authority_allowed
    assert not bundle.recommendation_allowed


def test_biotech_methodology_binds_regulatory_drug_development_framework() -> None:
    bundle = _registry().resolve("创新药研发")

    assert bundle.status == "MATCHED"
    assert bundle.archetype_id == "BIOTECH"
    assert "BIOTECH_CORE" in bundle.methodology_ids
    assert "fda-drug-development" in {item.source_id for item in bundle.sources}
    healthcare = next(
        item
        for item in bundle.methodologies
        if item.methodology_id == "BIOTECH_CORE"
    )
    assert "管线阶段" in healthcare.operating_metrics
    assert "rNPV" in healthcare.valuation_lenses


def test_unknown_industry_falls_back_to_generic_methodology_without_need_info_contract() -> None:
    bundle = _registry().resolve("尚未纳入内部archetype的新型产业")

    assert bundle.status == "GENERIC_FALLBACK"
    assert bundle.archetype_id is None
    assert bundle.methodology_ids == ["GENERIC_COMPETITIVE_CORE"]
    assert not bundle.private_skill_required_for_analysis
    assert not bundle.specialist_draft_required
    assert not bundle.fact_authority_allowed
    assert not bundle.recommendation_allowed


def test_methodology_sources_are_method_only_and_high_quality_classes() -> None:
    registry = _registry()
    allowed = {
        IndustryMethodologySourceKind.PROFESSIONAL_STANDARD,
        IndustryMethodologySourceKind.REGULATOR,
        IndustryMethodologySourceKind.INDUSTRY_STANDARD,
        IndustryMethodologySourceKind.ACADEMIC_DATA,
        IndustryMethodologySourceKind.INTERGOVERNMENTAL,
    }

    assert registry.sources
    for source in registry.sources.values():
        assert source.source_kind in allowed
        assert source.methodology_only
        assert not source.fact_authority_allowed
        assert not source.recommendation_allowed
        assert str(source.url).startswith("https://")

def test_sector_specific_methods_do_not_leak_incompatible_metrics() -> None:
    registry = _registry()

    software = registry.resolve("SaaS软件")
    software_method = next(
        item for item in software.methodologies if item.methodology_id == "SOFTWARE_CORE"
    )
    assert "ARR或经常性收入" in software_method.operating_metrics
    assert "AISC或全成本" not in software_method.operating_metrics
    assert "产能利用率" not in software_method.operating_metrics

    chemicals = registry.resolve("化学制品")
    chemical_method = next(
        item for item in chemicals.methodologies if item.methodology_id == "CHEMICALS_CORE"
    )
    assert "产品价差" in chemical_method.operating_metrics
    assert "AISC或全成本" not in chemical_method.operating_metrics
    assert "wgc-aisc" not in chemical_method.source_ids

    construction = registry.resolve("水泥建材")
    construction_method = next(
        item
        for item in construction.methodologies
        if item.methodology_id == "CONSTRUCTION_MATERIALS_CORE"
    )
    assert "ASP或工程毛利率" in construction_method.operating_metrics
    assert "book-to-bill" not in construction_method.operating_metrics

def test_methodology_resolution_is_deterministic_across_registry_reloads() -> None:
    first_registry = _registry()
    second_registry = _registry()

    first = first_registry.resolve("银行")
    second = second_registry.resolve("银行")

    assert first == second
    assert first.created_at == first_registry.released_at
    assert second.created_at == second_registry.released_at
    assert first_registry.inventory()["released_at"] == second_registry.inventory()["released_at"]

def test_new_known_archetype_without_sector_pack_falls_back_without_failure() -> None:
    baseline = _registry()
    future_archetype = IndustryResearchArchetype(
        archetype_id="FUTURE_QUANTUM_MATERIAL",
        name="未来量子材料",
        aliases=["量子材料"],
        key_metrics=["单位产出"],
        valuation_methods=["DCF"],
        key_risks=["技术路线"],
        created_at=baseline.released_at,
    )
    future_archetypes = IndustryResearchRegistry(
        [*baseline.archetypes.archetypes, future_archetype],
        registry_version="future-test",
    )
    future_registry = IndustryMethodologyRegistry(
        registry_version=baseline.registry_version,
        released_at=baseline.released_at,
        archetypes=future_archetypes,
        sources=list(baseline.sources.values()),
        methodologies=list(baseline.methodologies.values()),
    )

    bundle = future_registry.resolve("量子材料")

    assert bundle.status == "ARCHETYPE_FALLBACK"
    assert bundle.archetype_id == "FUTURE_QUANTUM_MATERIAL"
    assert bundle.methodology_ids == ["GENERIC_COMPETITIVE_CORE"]
    assert future_registry.inventory()["uncovered_archetype_ids"] == [
        "FUTURE_QUANTUM_MATERIAL"
    ]
    assert not bundle.private_skill_required_for_analysis
    assert not bundle.specialist_draft_required

