from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from astock.knowledge.discovery_catalog import KnowledgeDiscoveryCatalogCompiler
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import (
    KnowledgeAdmissionBasis,
    KnowledgeConditionEvaluation,
    KnowledgeConditionState,
    KnowledgeDiscoveryConditionKind,
    KnowledgeDiscoveryConditionSpec,
    KnowledgeDiscoveryDisposition,
    KnowledgeDiscoveryDispositionBatch,
    KnowledgeDiscoveryDispositionSpec,
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeProviderStatus,
    KnowledgeSkillInventoryMember,
    KnowledgeSkillInventorySnapshot,
)


def _inventory() -> KnowledgeSkillInventorySnapshot:
    status = KnowledgeProviderStatus(
        run_id="run-discovery",
        status=KnowledgeProviderReadiness.READY,
        mode=KnowledgeProviderMode.REGISTRY_RELEASE,
        reason_code="AUDITED_REGISTRY_READY",
        total_skill_count=2,
        ready_skill_count=2,
        pending_review_count=0,
        approved_count=0,
        rejected_count=0,
        eligible_skill_count=2,
        registry_release_id="audited-release:1",
        registry_artifact_id="AuditedRelease:1",
        registry_object_hash="a" * 64,
    )
    members = [
        KnowledgeSkillInventoryMember(
            member_ordinal=1,
            final_skill_id="skill:a",
            skill_name="盈利与现金兑现",
            primary_module=DirectSkillModule.FUNDAMENTAL_RESEARCH,
            secondary_modules=[DirectSkillModule.SOURCING_SCREENING],
            decision_question="盈利是否由现金流支持？",
            core_principle="盈利质量必须与现金兑现交叉核验。",
            source_hashes=["1" * 64],
            artifact_id="Skill:a",
            object_hash="2" * 64,
            admission_basis=KnowledgeAdmissionBasis.READY,
            skill_origin="DIRECT",
            status="READY_FOR_SHADOW",
        ),
        KnowledgeSkillInventoryMember(
            member_ordinal=2,
            final_skill_id="skill:b",
            skill_name="治理异常复核",
            primary_module=DirectSkillModule.FUNDAMENTAL_RESEARCH,
            secondary_modules=[],
            decision_question="治理异常是否改变公司质量判断？",
            core_principle="重大治理异常必须进入深度复核。",
            source_hashes=["3" * 64],
            artifact_id="Skill:b",
            object_hash="4" * 64,
            admission_basis=KnowledgeAdmissionBasis.READY,
            skill_origin="REVISED",
            status="READY_FOR_SHADOW",
        ),
    ]
    return KnowledgeSkillInventorySnapshot(
        run_id="run-discovery",
        provider_status=status,
        members=members,
        member_count=2,
        result_hash="5" * 64,
    )


def _computable_condition() -> KnowledgeDiscoveryConditionSpec:
    return KnowledgeDiscoveryConditionSpec(
        condition_id="cash_realization",
        condition_family="quality_value",
        condition_kind=KnowledgeDiscoveryConditionKind.COMPUTABLE_RULE,
        applicability=["non_financial_corporate"],
        prerequisites=["latest_financial_period_aligned"],
        required_fact_keys=["net_profit_ttm", "operating_cash_flow_ttm"],
        expression="operating_cash_flow_ttm / net_profit_ttm",
        counterevidence=["one_off_working_capital_release"],
        invalidation_dependencies=["latest_financial_period"],
        conflict_group="earnings_quality",
    )


def _batch() -> KnowledgeDiscoveryDispositionBatch:
    return KnowledgeDiscoveryDispositionBatch(
        run_id="run-discovery",
        registry_object_hash="a" * 64,
        compiler_version="skill-discovery-compiler-v1",
        reviewer="independent-reviewer",
        reviewed_at=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
        specs=[
            KnowledgeDiscoveryDispositionSpec(
                final_skill_id="skill:a",
                disposition=KnowledgeDiscoveryDisposition.COMPUTABLE_SCREEN,
                reason_codes=["STRUCTURED_FACTS_AVAILABLE"],
                conditions=[_computable_condition()],
            ),
            KnowledgeDiscoveryDispositionSpec(
                final_skill_id="skill:b",
                disposition=KnowledgeDiscoveryDisposition.DEEP_RESEARCH_REVIEW,
                reason_codes=["REQUIRES_COMPANY_SPECIFIC_EVIDENCE"],
            ),
        ],
    )


def test_discovery_catalog_compiler_binds_exact_inventory(state, object_store) -> None:
    inventory = _inventory()
    compiler = KnowledgeDiscoveryCatalogCompiler(state, object_store)

    first = compiler.compile(inventory, _batch())
    second = compiler.compile(inventory, _batch())

    assert first.catalog.member_count == 2
    assert [item.final_skill_id for item in first.catalog.entries] == ["skill:a", "skill:b"]
    assert first.catalog.entries[0].disposition is KnowledgeDiscoveryDisposition.COMPUTABLE_SCREEN
    assert first.catalog.entries[1].conditions == []
    assert first.catalog.registry_object_hash == inventory.provider_status.registry_object_hash
    assert first.artifact_id == second.artifact_id
    assert first.object_hash == second.object_hash
    assert object_store.verify(first.object_hash)
    assert object_store.verify(first.disposition_batch_object_hash)


def test_discovery_catalog_compiler_rejects_incomplete_or_foreign_specs(
    state,
    object_store,
) -> None:
    inventory = _inventory()
    compiler = KnowledgeDiscoveryCatalogCompiler(state, object_store)
    incomplete = _batch().model_copy(update={"specs": [_batch().specs[0]]})

    with pytest.raises(ValueError, match="coverage mismatch"):
        compiler.compile(inventory, incomplete)

    foreign = _batch().model_copy(update={"registry_object_hash": "f" * 64})
    with pytest.raises(ValueError, match="registry hash"):
        compiler.compile(inventory, foreign)


def test_discovery_disposition_contract_rejects_implicit_scoring_and_bad_logic() -> None:
    with pytest.raises(ValueError, match="semantic discovery condition"):
        KnowledgeDiscoveryConditionSpec(
            condition_id="semantic-bad",
            condition_family="bottleneck",
            condition_kind=KnowledgeDiscoveryConditionKind.SEMANTIC_QUESTION,
            applicability=["semiconductor_equipment"],
            expression="market_share > 0.5",
        )

    with pytest.raises(ValueError, match="non-discovery dispositions"):
        KnowledgeDiscoveryDispositionSpec(
            final_skill_id="skill:timing",
            disposition=KnowledgeDiscoveryDisposition.TIMING_CONTEXT,
            reason_codes=["TIMING_ONLY"],
            conditions=[_computable_condition()],
        )

    with pytest.raises(ValueError, match="computable screen"):
        KnowledgeDiscoveryDispositionSpec(
            final_skill_id="skill:bad-computable",
            disposition=KnowledgeDiscoveryDisposition.COMPUTABLE_SCREEN,
            reason_codes=["BAD_MIX"],
            conditions=[
                KnowledgeDiscoveryConditionSpec(
                    condition_id="semantic-only",
                    condition_family="event",
                    condition_kind=KnowledgeDiscoveryConditionKind.SEMANTIC_QUESTION,
                    applicability=["all"],
                    semantic_question="事件是否通过收入或成本真实传导到公司？",
                )
            ],
        )


@pytest.mark.parametrize(
    "state_value",
    [
        KnowledgeConditionState.SATISFIED,
        KnowledgeConditionState.NOT_SATISFIED,
        KnowledgeConditionState.UNKNOWN,
        KnowledgeConditionState.NOT_APPLICABLE,
    ],
)
def test_discovery_condition_evaluation_is_explicit_four_state(state_value) -> None:
    result = KnowledgeConditionEvaluation(
        condition_id="cash_realization",
        state=state_value,
        evidence_refs=[],
        reason_codes=["EXPLICIT_STATE"],
    )
    assert result.state is state_value


def test_discovery_condition_missing_semantics_cannot_be_reweighted() -> None:
    with pytest.raises(ValueError):
        KnowledgeDiscoveryConditionSpec(
            condition_id="missing-is-not-zero",
            condition_family="quality",
            condition_kind=KnowledgeDiscoveryConditionKind.COMPUTABLE_RULE,
            applicability=["all"],
            expression="roe_ttm > 0",
            missing_state=cast(Any, KnowledgeConditionState.SATISFIED),
        )
