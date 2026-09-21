from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.state import StateStore
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.knowledge.semantic_admission_service import _module
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import (
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeProviderStatus,
    KnowledgeSkillQuery,
)
from astock.schemas.knowledge_semantic_admission import (
    SemanticAdmissionAuditReport,
    SemanticAdmissionDisposition,
    SemanticAdmissionSkill,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)


def _skill_payload() -> dict[str, Any]:
    return {
        "created_at": NOW,
        "final_skill_id": "semantic-admitted:test",
        "group_id": "exact-method:test",
        "skill_name": "预期差与价格反馈验证",
        "primary_module": DirectSkillModule.FUNDAMENTAL_RESEARCH,
        "secondary_modules": [DirectSkillModule.POSITION_RISK_MANAGEMENT],
        "decision_question": "预期差是否被财务证据与价格反馈共同验证？",
        "core_principle": "先形成可证伪的基本面预期，再用财务证据和价格反馈判断市场是否已计价。",
        "applicable_conditions": ["存在可验证的预期与兑现差异。"],
        "reasoning_steps": ["冻结预期。", "核对兑现。", "检查价格反馈。"],
        "required_evidence": ["财报或公告。", "连续行情。"],
        "positive_signals": ["兑现优于冻结预期。"],
        "negative_signals": ["兑现弱于预期或价格拒绝确认。"],
        "invalidation_conditions": ["原始盈利或业务前提被推翻。"],
        "failure_modes": ["把单次价格上涨当成基本面验证。"],
        "family": "VALUATION_EXPECTATIONS",
        "industry_scope": "UNSPECIFIED",
        "holding_horizon": "MEDIUM",
        "method_categories": ["FINANCIAL_QUALITY", "RISK"],
        "candidate_ids": ["candidate:a"],
        "argument_unit_ids": ["argument:a"],
        "source_snapshot_ids": ["snapshot:a"],
        "source_hashes": ["a" * 64, "b" * 64],
        "source_authors": ["zhihu:test"],
        "same_source_lineage_keys": ["zhihu:test:answer:1"],
        "disposition": SemanticAdmissionDisposition.REWRITE_AND_ADMIT,
    }


def test_0078_semantic_admission_migration_is_append_only(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    applied = state.migrate()
    assert "0078" in applied

    expected_tables = {
        "knowledge_semantic_admission_run",
        "knowledge_semantic_admission_skill",
        "knowledge_semantic_admission_group_candidate",
        "knowledge_semantic_admission_decision",
        "knowledge_semantic_admission_release",
        "knowledge_semantic_admission_member",
    }
    expected_triggers = {
        "trg_semantic_admission_run_no_update",
        "trg_semantic_admission_skill_no_update",
        "trg_semantic_admission_decision_no_update",
        "trg_semantic_admission_release_review_closed",
    }
    with state.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
    assert expected_tables <= tables
    assert expected_triggers <= triggers
    assert state.integrity_check() == "ok"


def test_semantic_admission_skill_requires_canonical_unique_lineage() -> None:
    skill = SemanticAdmissionSkill.model_validate(_skill_payload())
    assert skill.source_hashes == ["a" * 64, "b" * 64]
    assert skill.formal_committee_weight_allowed is False
    assert skill.paper_ledger_write_allowed is False

    invalid = _skill_payload()
    invalid["source_hashes"] = ["b" * 64, "a" * 64]
    with pytest.raises(ValidationError, match="sorted unique"):
        SemanticAdmissionSkill.model_validate(invalid)


def test_semantic_admission_audit_pass_is_fail_closed() -> None:
    common = {
        "created_at": NOW,
        "run_id": "semantic-admission-run:test",
        "parent_active_skill_count": 237,
        "raw_candidate_count": 3,
        "mapped_candidate_count": 3,
        "exact_group_count": 2,
        "skill_count": 2,
        "decision_count": 2,
        "approved_count": 2,
        "rejected_count": 0,
        "duplicate_candidate_membership_count": 0,
        "broken_object_count": 0,
        "missing_source_hash_count": 0,
        "foreign_key_violation_count": 0,
        "database_integrity": "ok",
        "finding_codes": [],
    }
    report = SemanticAdmissionAuditReport(status="PASS", **common)
    assert report.status == "PASS"

    with pytest.raises(ValidationError, match="hard gates"):
        SemanticAdmissionAuditReport(
            status="PASS",
            **{**common, "mapped_candidate_count": 2},
        )


def test_module_mapping_separates_research_and_risk_roles() -> None:
    primary, secondary = _module(
        ["BUSINESS_MODEL", "RISK"],
        "BUSINESS_MODEL",
        "客户与单位经济的验证方法",
    )
    assert primary is DirectSkillModule.FUNDAMENTAL_RESEARCH
    assert DirectSkillModule.POSITION_RISK_MANAGEMENT in secondary

    primary, secondary = _module(
        ["RISK", "HOLDING"],
        "POSITION_LIMITS",
        "仓位和风险预算需要匹配损失承受力",
    )
    assert primary is DirectSkillModule.PORTFOLIO_CONSTRUCTION
    assert DirectSkillModule.POSITION_RISK_MANAGEMENT in secondary


class _FakeObjects:
    def verify(self, _: str) -> bool:
        return True


class _FakeCompletionRepository:
    state = cast(Any, None)

    def __init__(self, artifact_hashes: dict[str, str]) -> None:
        self.artifact_hashes = artifact_hashes

    def artifact_object_hash(self, artifact_id: str) -> str | None:
        return self.artifact_hashes.get(artifact_id)


class _FakeSemanticRepository:
    def __init__(self, release: dict[str, Any], members: list[dict[str, Any]]) -> None:
        self._release = release
        self._members = members

    def latest_release(self, _: str) -> dict[str, Any]:
        return self._release

    def members(self, _: str) -> list[dict[str, Any]]:
        return self._members


def _audited_parent() -> KnowledgeProviderStatus:
    return KnowledgeProviderStatus(
        run_id="direct:test",
        status=KnowledgeProviderReadiness.READY,
        mode=KnowledgeProviderMode.REGISTRY_RELEASE,
        reason_code="AUDITED_REGISTRY_READY",
        total_skill_count=3,
        ready_skill_count=3,
        pending_review_count=0,
        approved_count=0,
        rejected_count=0,
        eligible_skill_count=3,
        registry_release_id="audited-release:test",
        registry_artifact_id="audited-artifact:test",
        registry_object_hash="a" * 64,
    )


def test_provider_semantic_overlay_requires_exact_parent_and_extends_count() -> None:
    release = {
        "release_id": "semantic-release:test",
        "release_artifact_id": "semantic-release-artifact:test",
        "release_object_hash": "b" * 64,
        "parent_audited_release_id": "audited-release:test",
        "parent_audited_object_hash": "a" * 64,
        "parent_active_skill_count": 3,
        "approved_skill_count": 2,
        "rejected_skill_count": 0,
        "active_skill_count": 5,
    }
    members = [
        {
            "skill_object_hash": "c" * 64,
            "skill_artifact_id": "skill-artifact:1",
            "source_hashes_json": json.dumps(["e" * 64]),
        },
        {
            "skill_object_hash": "d" * 64,
            "skill_artifact_id": "skill-artifact:2",
            "source_hashes_json": json.dumps(["f" * 64]),
        },
    ]
    completion = _FakeCompletionRepository(
        {
            "semantic-release-artifact:test": "b" * 64,
            "skill-artifact:1": "c" * 64,
            "skill-artifact:2": "d" * 64,
        }
    )
    provider = RepositoryKnowledgeSkillProvider(
        cast(Any, completion), cast(Any, _FakeObjects())
    )
    provider.semantic_admission_repository = cast(
        Any,
        _FakeSemanticRepository(release, members),
    )

    status = provider._semantic_admission_status(_audited_parent())
    assert status.reason_code == "AUDITED_SEMANTIC_REGISTRY_READY"
    assert status.eligible_skill_count == 5
    assert status.total_skill_count == 5
    assert status.approved_count == 2

    drifted = dict(release)
    drifted["parent_audited_object_hash"] = "9" * 64
    provider.semantic_admission_repository = cast(
        Any,
        _FakeSemanticRepository(drifted, members),
    )
    blocked = provider._semantic_admission_status(_audited_parent())
    assert blocked.status is KnowledgeProviderReadiness.NEEDS_INFO
    assert blocked.reason_code == "SEMANTIC_ADMISSION_PARENT_DRIFT"
    assert blocked.eligible_skill_count == 0


def test_provider_select_accepts_semantic_overlay_without_legacy_status_field() -> None:
    source_hash = "e" * 64
    payload = {
        "schema_version": "semantic-admission-skill-v1",
        "final_skill_id": "semantic-admitted:selection-test",
        "group_id": "exact-method:selection-test",
        "skill_name": "预期差与价格反馈验证",
        "primary_module": "FUNDAMENTAL_RESEARCH",
        "secondary_modules": ["POSITION_RISK_MANAGEMENT"],
        "decision_question": "预期差是否被财务证据与价格反馈共同验证？",
        "core_principle": "先形成可证伪的基本面预期，再以财务兑现和价格反馈交叉验证。",
        "applicable_conditions": ["存在可冻结的市场预期。"],
        "reasoning_steps": ["冻结预期。", "核对兑现。"],
        "required_evidence": ["财报与连续行情。"],
        "positive_signals": ["兑现优于预期。"],
        "negative_signals": ["价格拒绝确认。"],
        "invalidation_conditions": ["核心盈利前提被推翻。"],
        "failure_modes": ["把单次上涨当作基本面验证。"],
        "family": "VALUATION_EXPECTATIONS",
        "industry_scope": "UNSPECIFIED",
        "holding_horizon": "MEDIUM",
        "method_categories": ["FINANCIAL_QUALITY", "RISK"],
        "candidate_ids": ["candidate:a"],
        "argument_unit_ids": ["argument:a"],
        "source_snapshot_ids": ["snapshot:a"],
        "source_hashes": [source_hash],
        "source_authors": ["zhihu:test"],
        "same_source_lineage_keys": ["zhihu:test:answer:1"],
        "disposition": "REWRITE_AND_ADMIT",
        "community_source_only": True,
        "factual_use_requires_stronger_source": True,
        "formal_committee_weight_allowed": False,
        "paper_ledger_write_allowed": False,
        "created_at": NOW.isoformat(),
    }
    skill_json = canonical_json_bytes(payload).decode("utf-8")
    skill_hash = sha256_bytes(skill_json.encode("utf-8"))
    row = {
        "member_ordinal": 1,
        "final_skill_id": payload["final_skill_id"],
        "skill_name": payload["skill_name"],
        "primary_module": payload["primary_module"],
        "secondary_modules_json": json.dumps(payload["secondary_modules"]),
        "decision_question": payload["decision_question"],
        "core_principle": payload["core_principle"],
        "source_hashes_json": json.dumps(payload["source_hashes"]),
        "skill_artifact_id": "semantic-skill-artifact:test",
        "skill_object_hash": skill_hash,
        "skill_json": skill_json,
        "admission_basis": "APPROVED",
        "status": "READY_FOR_SHADOW",
        "skill_origin": "SEMANTIC_OVERLAY",
    }
    provider = RepositoryKnowledgeSkillProvider(
        cast(Any, _FakeCompletionRepository({})),
        cast(Any, _FakeObjects()),
    )
    ready = KnowledgeProviderStatus(
        run_id="direct:test",
        status=KnowledgeProviderReadiness.READY,
        mode=KnowledgeProviderMode.REGISTRY_RELEASE,
        reason_code="AUDITED_SEMANTIC_REGISTRY_READY",
        total_skill_count=1,
        ready_skill_count=0,
        pending_review_count=0,
        approved_count=1,
        rejected_count=0,
        eligible_skill_count=1,
        registry_release_id="semantic-release:test",
        registry_artifact_id="semantic-release-artifact:test",
        registry_object_hash="b" * 64,
    )
    provider.status = cast(Any, lambda _run_id: ready)
    provider._eligible_rows = cast(Any, lambda _run_id, _status: [row])

    result = provider.select(
        "direct:test",
        KnowledgeSkillQuery(query="预期差 价格反馈", top_k=3),
    )

    assert result.provider_status.reason_code == "AUDITED_SEMANTIC_REGISTRY_READY"
    assert result.selected_count == 1
    assert result.skills[0].final_skill_id == "semantic-admitted:selection-test"
    assert result.skills[0].admission_basis.value == "APPROVED"
