from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.provider import _search_score
from astock.knowledge.skill_audit import KnowledgeSkillAuditService
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_skill_audit import (
    KnowledgeSkillAuditDecision,
    KnowledgeSkillAuditVerdict,
    KnowledgeSkillOrigin,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 14, 1, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> KnowledgeSkillAuditService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    applied = state.migrate()
    assert applied[-1] == "0059"
    return KnowledgeSkillAuditService(
        state,
        ObjectStore(tmp_path / "objects"),
        PROJECT_ROOT,
    )


def test_knowledge_skill_audit_policy_has_authoritative_evidence_and_curated_gaps(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    assert service.audit_policy["expected_source_skill_count"] == 653
    assert service.audit_policy["default_direct_verdict"] == "KEEP_SCOPED"
    assert service.audit_policy["default_visual_verdict"] == "RETIRE"
    assert len(service.evidence) >= 20
    curated = service._curated_models(NOW)
    assert len(curated) == 10
    assert {item.skill_name for item in curated} >= {
        "多重检验与回测过拟合治理",
        "交易成本、容量与买卖滞回门",
        "A股动量与反转必须按周期和市场状态分层",
        "应计、现金转换与利润质量交叉验证",
        "组合风险模型与经济暴露治理",
        "信号衰减与概念漂移监控",
        "参数邻域稳定性与扰动测试",
        "研究样本池必须按时点重建并治理幸存者偏差",
        "因子有效性联合检查IC、换手、分组稳定性与衰减",
    }
    assert all(len(item.external_evidence_ids) >= 2 for item in curated)
    assert all(item.formal_committee_weight_allowed is False for item in curated)
    assert all(item.paper_ledger_write_allowed is False for item in curated)
    assert all(item.broker_execution_allowed is False for item in curated)


def test_skill_specific_evidence_routes_precede_module_fallback(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service._evidence_for_skill(
        DirectSkillModule.FUNDAMENTAL_RESEARCH,
        "基本每股收益应检查加权平均普通股和稀释影响",
    ) == ["IFRS_IAS33_EPS", "MOF_CAS34_EPS"]
    assert set(
        service._evidence_for_skill(
            DirectSkillModule.SOURCING_SCREENING,
            "回测参数需要样本外验证并防止过拟合",
        )
    ) == {"QLIB_PIT", "RFS_MULTIPLE_TESTING_HLZ", "RFS_REPLICATING_ANOMALIES_HXZ"}


def test_accounting_visual_corrections_and_conflict_scope_are_explicit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    templates = service.audit_policy["replacement_templates"]

    assert "普通股加权平均数" in templates["ACCOUNTING_EPS_WEIGHTED_AVERAGE"]["core_principle"]
    assert "交付义务" in templates["ACCOUNTING_DEBT_EQUITY_SUBSTANCE"]["core_principle"]
    assert "可抵扣暂时性差异" in templates["ACCOUNTING_DEFERRED_TAX_TEMP_DIFF"]["core_principle"]
    assert "母公司个别报表并非“没用”" in templates["ACCOUNTING_CONSOLIDATED_SCOPE"][
        "core_principle"
    ]
    assert "CG_MOMENTUM_VS_REVERSAL" in service.audit_policy["conflict_groups"]
    assert "CG_CONCENTRATION_VS_DIVERSIFICATION" in service.audit_policy["conflict_groups"]
    assert len(service.audit_policy["visual_revise_templates"]) == 6


def test_revise_decision_requires_complete_replacement_identity() -> None:
    common = dict(
        created_at=NOW,
        decision_id="decision:test",
        audit_run_id="audit:test",
        source_skill_id="skill:test",
        source_skill_object_hash="a" * 64,
        source_skill_artifact_id="artifact:test",
        skill_origin=KnowledgeSkillOrigin.DIRECT,
        premise_scope="module=FUNDAMENTAL_RESEARCH; horizon=LONG; conditions=test",
        risk_codes=["OVERGENERALIZED_OR_LOGICALLY_UNSAFE"],
        conflict_groups=["CG_VALUATION_POINT_VS_RANGE"],
        external_evidence_ids=["A", "B"],
        rationale="test rationale",
    )

    with pytest.raises(ValueError, match="REVISE requires"):
        KnowledgeSkillAuditDecision.model_validate(
            {**common, "verdict": KnowledgeSkillAuditVerdict.REVISE}
        )

    decision = KnowledgeSkillAuditDecision.model_validate(
        {
            **common,
            "verdict": KnowledgeSkillAuditVerdict.REVISE,
            "replacement_skill_id": "revised:test",
            "replacement_skill_object_hash": "b" * 64,
            "replacement_skill_artifact_id": "artifact:revised:test",
        }
    )
    assert decision.paper_ledger_write_allowed is False
    assert decision.broker_execution_allowed is False


def test_chinese_phrase_search_outweighs_generic_single_character_overlap() -> None:
    query = "多重检验 回测过拟合"
    exact = "多重检验与回测过拟合治理 任何回测规则都要控制多重检验和样本外验证"
    generic = "估值检验需要回到企业现金流并检查历史数据"

    assert _search_score(query, exact) > _search_score(query, generic)
    assert _search_score("每股收益 加权平均普通股", "每股收益按加权平均普通股计算") > 0


def test_0055_migration_has_append_only_audit_and_registry_tables(tmp_path: Path) -> None:
    service = _service(tmp_path)
    expected = {
        "knowledge_skill_audit_run",
        "knowledge_skill_audit_decision",
        "knowledge_skill_audited_registry_release",
        "knowledge_skill_audited_registry_member",
        "knowledge_retired_skill_tombstone",
    }
    with service.state.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert expected <= tables
    assert service.state.integrity_check() == "ok"
