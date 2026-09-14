"""Independent public-output and derived-document recovery regressions for WP-24."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from astock.investor_orchestration.models import (
    InvestorSessionPreflightReceipt,
    LaneSnapshot,
    PortfolioLane,
    PositionView,
    RegimeAvailability,
    UnifiedPortfolioContext,
)
from astock.investor_orchestration.position_documents import PositionDocumentProjector
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.research.presentation import (
    ResponseGateway,
    audit_public_answer,
    classify_response_mode,
)
from astock.research.presentation_policy import load_presentation_policy
from astock.schemas.presentation import ResearchNarrativeBundle, ResponseMode, ResponseTaskType

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def _receipt(*, empty: bool = False) -> InvestorSessionPreflightReceipt:
    actual = LaneSnapshot(
        lane=PortfolioLane.ACTUAL, account_ids=("test-account",),
        positions=() if empty else (PositionView(
            account_id="test-account", lane=PortfolioLane.ACTUAL,
            instrument_id="XSHG:600000", quantity=Decimal("100"),
            average_cost=Decimal("10"), cost_status="EXACT", source_revision="r1",
        ),),
        source_revision="r2" if empty else "r1", audit_status="PASS",
    )
    paper = LaneSnapshot(lane=PortfolioLane.PAPER, source_revision="p1", audit_status="PASS")
    at = NOW + timedelta(minutes=1) if empty else NOW
    return InvestorSessionPreflightReceipt(
        receipt_id="test-receipt-empty" if empty else "test-receipt-held",
        request_id="test-request", as_of=at,
        source_revision_vector={"actual": actual.source_revision, "paper": "p1"},
        context=UnifiedPortfolioContext(as_of=at, actual=actual, paper=paper,
                                        aggregate_revision=actual.source_revision),
        regime=RegimeAvailability(), freshness="FRESH", receipt_hash="test-receipt-hash",
    )


def _projector(tmp_path: Path) -> PositionDocumentProjector:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    return PositionDocumentProjector(tmp_path, ResearchSubjectRegistryService(store))


def test_wp24_missing_derived_markdown_is_repaired_from_valid_projection(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    projector.update(_receipt())
    target = projector.root / "当前持仓.md"
    expected = target.read_bytes()
    target.unlink()
    projector.update(_receipt())
    assert target.exists(), "An unchanged fingerprint must not hide a missing user document"
    assert target.read_bytes() == expected


def test_wp24_empty_scope_does_not_leave_a_stale_active_monitor_plan(tmp_path: Path) -> None:
    projector = _projector(tmp_path)
    projector.update(_receipt())
    plan = projector.root / "主动跟踪计划.md"
    assert "600000" in plan.read_text(encoding="utf-8")
    projector.update(_receipt(empty=True))
    assert not plan.exists() or "600000" not in plan.read_text(encoding="utf-8"), (
        "An empty confirmed scope must retire the old generated plan, not retain held subjects"
    )


def test_wp24_public_renderer_uses_its_supplied_term_policy() -> None:
    marker = "未来经营现金流按当前价值衡量的方式"
    policy = replace(load_presentation_policy(), term_explanations={"DCF": marker})
    gateway = ResponseGateway(policy)
    context = gateway.context("解释估值", task_type=ResponseTaskType.DEEP_RESEARCH)
    narrative = ResearchNarrativeBundle(
        subject="估值方法", task_type=ResponseTaskType.DEEP_RESEARCH,
        headline="DCF 关注企业未来经营现金流。", reasons=["现金流预测需要结合需求与成本变化。"],
    )
    result = gateway.render(context, narrative=narrative)
    assert marker in result.text, (
        "Custom policy must not be replaced with global glossary at rendering"
    )
    assert not result.safe_fallback_used


def test_wp24_explicit_development_request_enables_developer_view() -> None:
    assert (
        classify_response_mode("本次任务是开发需求，请优化系统的回复格式。")
        is ResponseMode.DEVELOPER
    )
    assert classify_response_mode("本次没有开发需求，请分析公司研发投入。") is ResponseMode.INVESTOR


def test_wp24_snake_case_citation_is_not_a_machine_state() -> None:
    result = audit_public_answer("盈利改善仍取决于现金流，资料见 [report_note]。")
    assert "RAW_MACHINE_STATE_EXPOSED" not in result.finding_codes


def test_wp24_investor_text_never_exposes_private_project_path() -> None:
    result = audit_public_answer(r"研究记录位于 D:\AStockMultiAgent\user_state\holdings.md。")
    assert not result.safe_to_send


def test_wp24_style_cleanup_cannot_turn_complete_facts_into_a_data_gap() -> None:
    gateway = ResponseGateway()
    context = gateway.context("分析现金流", task_type=ResponseTaskType.DEEP_RESEARCH)
    narrative = ResearchNarrativeBundle(
        subject="经营分析", task_type=ResponseTaskType.DEEP_RESEARCH,
        headline="收入增长12%，现金流改善不是借款增加，而是回款加快。",
        reasons=["需求未改善时仍需控制仓位。"],
        risks=["应收账款逾期会削弱现金流。"],
    )
    result = gateway.render(context, narrative=narrative)
    assert "12%" in result.text, "Style issues must not discard verified financial data"
    assert "需求未改善" in result.text, "Editing must preserve the investment condition"
    assert "应收账款逾期" in result.text, "Editing must preserve the risk statement"
    assert "资料尚不足" not in result.text, "Style cleanup failure is not an evidence gap"
    assert "CHINESE_STYLE_TEMPLATE_CONTRAST" in result.audit.finding_codes
    assert not result.safe_fallback_used


def test_wp24_style_warning_does_not_bypass_fact_drift_or_machine_leak() -> None:
    source = "收入增长12%，现金流改善不是借款增加，而是回款加快。"
    drift = audit_public_answer(source.replace("12%", "21%"), source_text=source)
    assert not drift.safe_to_send
    assert drift.fact_drift_detected
    leaked = source + "当前 artifact_id=private-state。"
    unsafe = audit_public_answer(leaked, source_text=leaked)
    assert not unsafe.safe_to_send
    assert unsafe.internal_implementation_exposed


def test_wp24_raw_unverified_formulaic_draft_still_requires_style_review() -> None:
    result = audit_public_answer("现金流改善不是借款增加，而是回款加快。")
    assert not result.safe_to_send
    assert "CHINESE_STYLE_TEMPLATE_CONTRAST" in result.finding_codes
