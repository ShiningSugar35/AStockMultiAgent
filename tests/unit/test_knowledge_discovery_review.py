from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astock.knowledge.discovery_catalog import KnowledgeDiscoveryCatalogCompiler
from astock.knowledge.discovery_review import KnowledgeDiscoveryDispositionReviewer
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import (
    KnowledgeAdmissionBasis,
    KnowledgeConditionState,
    KnowledgeDiscoveryDisposition,
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeProviderStatus,
    KnowledgeSkillInventoryMember,
    KnowledgeSkillInventorySnapshot,
)


def _member(
    *,
    ordinal: int,
    final_skill_id: str,
    skill_name: str,
    module: DirectSkillModule,
    object_hash: str,
    origin: str,
) -> KnowledgeSkillInventoryMember:
    return KnowledgeSkillInventoryMember(
        member_ordinal=ordinal,
        final_skill_id=final_skill_id,
        skill_name=skill_name,
        primary_module=module,
        secondary_modules=[],
        decision_question=f"{skill_name}是否被当前公司证据支持？",
        core_principle=f"{skill_name}必须以可验证事实、反证和失效条件共同判断。",
        source_hashes=[f"{ordinal:x}".rjust(64, "0")],
        artifact_id=f"Skill:{final_skill_id}",
        object_hash=object_hash,
        admission_basis=KnowledgeAdmissionBasis.APPROVED,
        skill_origin=origin,
        status="READY_FOR_SHADOW",
    )


def _inventory(object_store) -> KnowledgeSkillInventorySnapshot:
    release_ref = object_store.put_json(
        {
            "schema_version": "semantic-admission-registry-release-v1",
            "release_id": "release:current",
            "created_at": datetime(2026, 9, 20, 2, 30, tzinfo=UTC).isoformat(),
        }
    )
    semantic_research = object_store.put_json(
        {
            "final_skill_id": "skill:semantic-business",
            "family": "BUSINESS_MODEL",
            "industry_scope": "SEMICONDUCTORS",
            "holding_horizon": "LONG",
            "negative_signals": ["客户留存与单位经济恶化"],
            "invalidation_conditions": ["商业模式核心假设被财报或客户证据否定"],
        }
    )
    semantic_timing = object_store.put_json(
        {
            "final_skill_id": "skill:semantic-trend",
            "family": "TECHNICAL_PATTERN",
            "industry_scope": "MARKET_WIDE",
            "holding_horizon": "SHORT",
        }
    )
    direct_research = object_store.put_json(
        {
            "final_skill_id": "skill:direct-quality",
            "status": "READY_FOR_SHADOW",
            "primary_module": "FUNDAMENTAL_RESEARCH",
        }
    )
    direct_portfolio = object_store.put_json(
        {
            "final_skill_id": "skill:direct-position",
            "status": "READY_FOR_SHADOW",
            "primary_module": "PORTFOLIO_CONSTRUCTION",
        }
    )
    status = KnowledgeProviderStatus(
        run_id="direct:current",
        status=KnowledgeProviderReadiness.READY,
        mode=KnowledgeProviderMode.REGISTRY_RELEASE,
        reason_code="AUDITED_SEMANTIC_REGISTRY_READY",
        total_skill_count=4,
        ready_skill_count=2,
        pending_review_count=0,
        approved_count=2,
        rejected_count=0,
        eligible_skill_count=4,
        registry_release_id="release:current",
        registry_artifact_id="Release:current",
        registry_object_hash=release_ref.sha256,
    )
    members = [
        _member(
            ordinal=1,
            final_skill_id="skill:semantic-business",
            skill_name="商业模式验证",
            module=DirectSkillModule.FUNDAMENTAL_RESEARCH,
            object_hash=semantic_research.sha256,
            origin="SEMANTIC_OVERLAY",
        ),
        _member(
            ordinal=2,
            final_skill_id="skill:semantic-trend",
            skill_name="趋势形态观察",
            module=DirectSkillModule.POSITION_RISK_MANAGEMENT,
            object_hash=semantic_timing.sha256,
            origin="SEMANTIC_OVERLAY",
        ),
        _member(
            ordinal=3,
            final_skill_id="skill:direct-quality",
            skill_name="盈利质量复核",
            module=DirectSkillModule.FUNDAMENTAL_RESEARCH,
            object_hash=direct_research.sha256,
            origin="DIRECT",
        ),
        _member(
            ordinal=4,
            final_skill_id="skill:direct-position",
            skill_name="组合仓位治理",
            module=DirectSkillModule.PORTFOLIO_CONSTRUCTION,
            object_hash=direct_portfolio.sha256,
            origin="DIRECT",
        ),
    ]
    return KnowledgeSkillInventorySnapshot(
        run_id="direct:current",
        provider_status=status,
        members=members,
        member_count=4,
        result_hash="f" * 64,
    )


def test_discovery_reviewer_routes_active_skill_metadata_and_compiles_exactly(
    state,
    object_store,
) -> None:
    inventory = _inventory(object_store)
    reviewer = KnowledgeDiscoveryDispositionReviewer(object_store)
    batch = reviewer.review(inventory)

    assert [spec.final_skill_id for spec in batch.specs] == sorted(
        member.final_skill_id for member in inventory.members
    )
    by_id = {spec.final_skill_id: spec for spec in batch.specs}
    business = by_id["skill:semantic-business"]
    assert business.disposition is KnowledgeDiscoveryDisposition.SEMANTIC_DISCOVERY
    assert len(business.conditions) == 1
    condition = business.conditions[0]
    assert condition.applicability == ["GENERAL_INDUSTRIAL"]
    assert condition.prerequisites == []
    assert condition.missing_state is KnowledgeConditionState.UNKNOWN
    assert "industry_scope:SEMICONDUCTORS" in condition.invalidation_dependencies
    assert "holding_horizon:LONG" in condition.invalidation_dependencies

    assert (
        by_id["skill:semantic-trend"].disposition
        is KnowledgeDiscoveryDisposition.TIMING_CONTEXT
    )
    assert (
        by_id["skill:direct-position"].disposition
        is KnowledgeDiscoveryDisposition.PORTFOLIO_REPORT_GOVERNANCE
    )
    assert (
        by_id["skill:direct-quality"].disposition
        is KnowledgeDiscoveryDisposition.SEMANTIC_DISCOVERY
    )

    record = KnowledgeDiscoveryCatalogCompiler(state, object_store).compile(
        inventory,
        batch,
    )
    assert record.catalog.member_count == 4
    assert record.catalog.registry_object_hash == inventory.provider_status.registry_object_hash


def test_discovery_reviewer_maps_financial_and_unknown_industry_profiles(object_store) -> None:
    inventory = _inventory(object_store)
    reviewer = KnowledgeDiscoveryDispositionReviewer(object_store)
    business_member = next(
        member for member in inventory.members if member.final_skill_id == "skill:semantic-business"
    )
    base_payload = reviewer._payload(business_member.object_hash)

    financial_ref = object_store.put_json(
        {
            **base_payload,
            "final_skill_id": "skill:financial",
            "industry_scope": "FINANCIALS",
        }
    )
    financial = business_member.model_copy(
        update={
            "final_skill_id": "skill:financial",
            "object_hash": financial_ref.sha256,
        }
    )
    spec = reviewer._spec(financial)
    assert spec.conditions[0].applicability == ["BANK", "INSURANCE", "SECURITIES"]

    unknown_ref = object_store.put_json(
        {
            **base_payload,
            "final_skill_id": "skill:unknown",
            "industry_scope": "UNSPECIFIED",
        }
    )
    unknown = business_member.model_copy(
        update={
            "final_skill_id": "skill:unknown",
            "object_hash": unknown_ref.sha256,
        }
    )
    spec = reviewer._spec(unknown)
    assert spec.conditions[0].applicability == ["*"]


def test_discovery_reviewer_rejects_skill_payload_identity_drift(object_store) -> None:
    inventory = _inventory(object_store)
    reviewer = KnowledgeDiscoveryDispositionReviewer(object_store)
    member = inventory.members[0]
    bad_ref = object_store.put_json(
        {
            "final_skill_id": "skill:another",
            "family": "BUSINESS_MODEL",
        }
    )
    bad_member = member.model_copy(update={"object_hash": bad_ref.sha256})

    with pytest.raises(ValueError, match="identity drift"):
        reviewer._spec(bad_member)
