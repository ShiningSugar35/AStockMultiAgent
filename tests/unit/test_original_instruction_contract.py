from __future__ import annotations

import tomllib
from pathlib import Path

from astock.schemas import (
    AuthorCollectionCoverageReport,
    AuthorSkillCoverage,
    CollectionCheckpoint,
    CommitteeAccessPolicy,
    ContextBudgetReport,
    DataQualityReport,
    HoldingReviewPack,
    PositionActionProposal,
    PositionMonitoringPlan,
    ReplayQuality,
    RunManifest,
    RunMode,
    SourceAccessDecision,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fields(model: type) -> set[str]:
    return set(model.model_fields)


def test_root_documents_separate_design_plan_and_accepted_facts() -> None:
    design = (PROJECT_ROOT / "低成本A股多Agent投研系统方案.md").read_text(
        encoding="utf-8"
    )
    plan = (PROJECT_ROOT / "开发计划.md").read_text(encoding="utf-8")
    acceptance = (PROJECT_ROOT / "验收报告.md").read_text(encoding="utf-8")
    for required in (
        "总方案只写长期设计",
        "不自动向券商发送订单",
        "官方/已验证 API 或本地数据 → MCP → Browser → Manual Task",
        "SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate",
        "Phase 8",
    ):
        assert required in design
    for required in (
        "本文件只保存当前未完成任务",
        "维护契约",
        "当前没有未完成的代码发布主线",
        "独立长期运行/数据义务",
    ):
        assert required in plan
    for completed_history in (
        "External Dependency Resilience v1",
        "Portfolio & Holding Decision Skills v1",
        "PHD-1",
        "PHD-7",
        "s8 — 显式提交、推送、Tag/GitHub Release 与 clean worktree",
        "当前未完成主线：External Dependency Resilience v1",
        "Phase 5：COMPLETE",
        "Phase 5 已完成后的不可变边界",
        "composite registry",
        "Priority 1：可恢复的单股票 Research Runtime",
    ):
        assert completed_history not in plan
    for required in (
        "本报告只记录已实现",
        "当前能力",
        "Phase 5 采集覆盖",
        "论证级语义链",
        "三位知乎作者真实视觉证据与视觉增强 Skill",
        "VisualEvidencePack",
        "COMPOSITE_REGISTRY_READY",
        "《价值投资功法》视觉证据链",
        "OpenCode 边界",
        "research-skills-v3",
    ):
        assert required in acceptance
    assert "K5-D5" not in plan
    assert "K5-D6" not in plan
    assert "Spark 首次实现" not in acceptance
    assert "Sol takeover" not in acceptance
    assert "SER-1" not in plan
    assert "SER-2" not in plan
    assert "SER-3" not in plan
    assert "P5X-1：" not in plan
    assert "P5X-2：" not in plan
    assert "P5X-3：" not in plan
    assert "P5X-4：" not in plan
    assert "P5X-5" not in plan
    assert "DeepSeek 执行" not in plan
    assert "人工审核" not in plan
    assert "K5-D3：" not in plan
    assert "K5-D4-R：" not in plan
    assert "2P + A + 14" not in plan
    assert "旧段落级链" not in design


def test_original_instruction_public_schemas_keep_all_required_fields() -> None:
    expected = {
        SourceAccessDecision: {
            "source_id",
            "requested_capability",
            "selected_transport",
            "selection_reason",
            "fallback_chain",
            "request_started_at",
            "request_finished_at",
            "result_hash",
            "failure_class",
            "rate_limit_state",
        },
        ContextBudgetReport: {
            "selected_skills",
            "selected_artifacts",
            "artifact_byte_size",
            "estimated_text_tokens",
            "full_documents_to_open",
            "evidence_excerpts_to_open",
            "expected_browser_steps",
            "expected_mcp_calls",
            "expected_api_calls",
            "duplicate_inputs_avoided",
        },
        AuthorSkillCoverage: {
            "author_id",
            "source_snapshot_ids",
            "selection_skill_coverage",
            "entry_skill_coverage",
            "holding_skill_coverage",
            "add_skill_coverage",
            "trim_skill_coverage",
            "exit_skill_coverage",
            "risk_skill_coverage",
            "evidence_count_by_stage",
            "coverage_status",
            "missing_stages",
            "review_status",
        },
        PositionMonitoringPlan: {
            "position_id",
            "company_id",
            "decision_id",
            "thesis_summary",
            "entry_assumptions",
            "holding_horizon",
            "key_value_drivers",
            "validation_metrics",
            "monitoring_sources",
            "monitoring_cadence",
            "price_rules",
            "fundamental_rules",
            "event_rules",
            "add_conditions",
            "trim_conditions",
            "exit_conditions",
            "invalidation_conditions",
            "manual_information_needs",
            "last_review_at",
            "next_review_at",
            "skill_versions",
            "evidence_snapshot_id",
        },
        HoldingReviewPack: {
            "position_id",
            "as_of",
            "new_market_data",
            "new_disclosures",
            "new_regulatory_events",
            "new_industry_data",
            "new_news_leads",
            "manual_evidence_updates",
            "thesis_strength_change",
            "risk_change",
            "triggered_rules",
            "unresolved_conflicts",
            "recommended_action",
            "action_confidence",
            "evidence_ids",
            "next_review_conditions",
        },
        AuthorCollectionCoverageReport: {
            "discovered_count",
            "scheduled_count",
            "success_count",
            "failed_count",
            "restricted_count",
            "skipped_duplicate_count",
            "updated_count",
            "missing_count",
            "last_page_or_cursor",
            "terminal_condition",
        },
        CollectionCheckpoint: {
            "author",
            "content_type",
            "listing_page",
            "listing_cursor",
            "content_id",
            "comment_page",
            "comment_cursor",
            "nested_reply_cursor",
        },
    }
    for model, required in expected.items():
        assert required <= _fields(model)
    quality_fields = _fields(DataQualityReport)
    assert {
        "requested_start",
        "requested_end",
        "actual_start",
        "actual_end",
        "bar_count",
        "missing_sessions",
        "duplicate_bars",
        "ohlc_errors",
        "volume_unit",
        "adjustment_mode",
        "timestamp_semantics",
        "provider_latency_ms",
        "provider_status",
    } <= quality_fields


def test_codex_committee_replay_and_real_order_boundaries_are_executable_contracts() -> None:
    assert set(ReplayQuality) == {
        ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
        ReplayQuality.SINGLE_SOURCE_5M,
        ReplayQuality.PROVIDER_1H_APPROX,
        ReplayQuality.DAILY_OPEN_MODEL,
        ReplayQuality.DAILY_CLOSE_MODEL,
        ReplayQuality.DAILY_CONSERVATIVE,
        ReplayQuality.UNREPLAYABLE,
    }
    assert RunMode.CODEX_INTERACTIVE in set(RunMode)
    assert RunMode.DETERMINISTIC in set(RunMode)
    assert "status" in _fields(RunManifest)
    committee = CommitteeAccessPolicy()
    assert not any(
        (
            committee.network_access,
            committee.api_access,
            committee.mcp_access,
            committee.browser_access,
            committee.full_document_access,
            committee.new_research_allowed,
        )
    )
    assert committee.missing_evidence_action == "NEEDS_INFO"
    assert PositionActionProposal.model_fields["requires_user_confirmation"].default is True


def test_python_runtime_dependency_and_private_material_boundaries() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    dependencies = "\n".join(project["project"]["dependencies"]).lower()
    assert "finrobot" not in dependencies
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("runtime/**", "*.pdf", "*.doc", "*.docx", "cookies/", "browser-profile/"):
        assert pattern in ignored
