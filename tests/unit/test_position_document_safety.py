"""Independent WP-24 acceptance counterexamples; isolated synthetic positions only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from astock.investor_orchestration.models import (
    InvestorSessionPreflightReceipt,
    LaneSnapshot,
    MaterialEventView,
    PortfolioLane,
    PositionView,
    RegimeAvailability,
    UnifiedPortfolioContext,
)
from astock.investor_orchestration.position_documents import PositionDocumentProjector
from astock.investor_orchestration.proactive_monitoring import ProactiveMonitorPlanner
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

T0 = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
AuditStatus = Literal["PASS", "DEGRADED", "FAILED", "NOT_APPLICABLE"]


def subjects() -> ResearchSubjectRegistryService:
    return cast(
        ResearchSubjectRegistryService,
        SimpleNamespace(current_watchlist=lambda **kwargs: ()),
    )


def receipt(
    *,
    at: datetime = T0,
    quantity: str | None = "100",
    market_value: str = "1200",
    audit: AuditStatus = "PASS",
    revision: str = "r1",
    events: tuple[MaterialEventView, ...] = (),
) -> InvestorSessionPreflightReceipt:
    positions = (
        ()
        if quantity is None
        else (
            PositionView(
                account_id="fixture-account",
                lane=PortfolioLane.ACTUAL,
                instrument_id="XSHG:600000",
                quantity=Decimal(quantity),
                average_cost=Decimal("10"),
                cost_status="EXACT",
                market_value=Decimal(market_value),
                source_revision=revision,
            ),
        )
    )
    actual = LaneSnapshot(
        lane=PortfolioLane.ACTUAL,
        account_ids=("fixture-account",),
        positions=positions,
        source_revision=revision,
        audit_status=audit,
        warnings=("SOURCE_TEMPORARILY_UNAVAILABLE",) if audit != "PASS" else (),
    )
    paper = LaneSnapshot(lane=PortfolioLane.PAPER, source_revision="p1", audit_status="PASS")
    return InvestorSessionPreflightReceipt(
        receipt_id="fixture-receipt-" + at.isoformat() + revision,
        request_id="fixture-request",
        as_of=at,
        source_revision_vector={"actual": revision, "paper": "p1"},
        context=UnifiedPortfolioContext(
            as_of=at,
            actual=actual,
            paper=paper,
            aggregate_revision=revision,
            material_events=events,
        ),
        regime=RegimeAvailability(),
        freshness="FRESH" if audit == "PASS" else "DEGRADED",
        receipt_hash="fixture-hash",
    )


def snapshot_bytes(root: Path):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_later_unchanged_activation_does_not_rewrite_documents(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    before = snapshot_bytes(projector.root)
    result = projector.update(receipt(at=T0 + timedelta(minutes=1)))
    assert result["changed"] is False, "Wall-clock-only change must not be material"
    assert snapshot_bytes(projector.root) == before


@pytest.mark.parametrize("audit", ["FAILED", "DEGRADED", "NOT_APPLICABLE"])
def test_unreadable_lane_cannot_fabricate_closure(tmp_path: Path, audit: AuditStatus):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    projector.update(
        receipt(at=T0 + timedelta(minutes=1), quantity=None, audit=audit, revision="missing")
    )
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    assert state["active"], "Unknown/unavailable holdings were presented as a liquidation"
    assert not state["closed"], "A failed read is not a sell/close event"


def test_changed_valuation_is_visible_even_when_quantity_and_cost_are_unchanged(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    before = (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    result = projector.update(receipt(market_value="1300"))
    after = (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    assert result["changed"] is True, "Canonical valuation changed but projection did not"
    assert after != before, "Price/value delta must be visible, not just a changed internal hash"


def test_first_observed_price_is_frozen_while_current_valuation_moves(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt(market_value="1200"))
    projector.update(receipt(at=T0 + timedelta(minutes=1), market_value="1300", revision="r2"))
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    item = next(iter(state["active"].values()))
    assert item["entry_snapshot"]["first_observed_price"] == "12"
    assert item["price_from_valuation"] == "13"
    document = (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    assert "首次记录时按持仓估值折算每股 12 元" in document
    assert "按该估值折算每股 13 元" in document


def test_latest_research_can_advance_without_rewriting_entry_snapshot(tmp_path, monkeypatch):
    projector = PositionDocumentProjector(tmp_path, subjects())

    def research_snapshot(_instrument: str, as_of: datetime) -> dict[str, object]:
        version = "首次研究" if as_of == T0 else "后续复核"
        return {"as_of": as_of.isoformat(), "investment_thesis": version}

    monkeypatch.setattr(projector, "_formal_research_snapshot", research_snapshot)
    projector.update(receipt(at=T0, revision="r1"))
    projector.update(receipt(at=T0 + timedelta(minutes=1), revision="r2"))
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    item = next(iter(state["active"].values()))
    assert item["entry_snapshot"]["formal_research"]["investment_thesis"] == "首次研究"
    assert item["current_research"]["investment_thesis"] == "后续复核"
    document = (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    assert "投资逻辑：首次研究" in document
    assert "最新复核" in document
    assert "投资逻辑：后续复核" in document


def test_compact_formal_research_snapshot_keeps_full_chain_without_future_backfill():
    payload = {
        "as_of": T0.isoformat(),
        "candidate_narratives": [
            {
                "instrument_id": "XSHG:600000",
                "investment_thesis": "现金流改善与行业供给收缩共同支撑盈利质量。",
                "why_now": "估值回到历史中枢以下，等待需求确认。",
            }
        ],
        "fundamentals": [
            {
                "instrument_id": "XSHG:600000",
                "analyzed_complete_years": 5,
                "analyzed_quarters": 12,
                "ttm_reconstructed": True,
            }
        ],
        "candidate_rankings": [{"instrument_id": "XSHG:600000", "industry_id": "bank"}],
        "industries": [
            {
                "industry_id": "bank",
                "cycle_phase": "修复",
                "competitive_intensity": "中等",
                "industry_risks": ["净息差继续收窄"],
            }
        ],
        "macro": {
            "macro_regime": "温和修复",
            "liquidity_regime": "中性偏松",
            "risk_appetite": "中性",
            "policy_bias": "稳增长",
        },
        "financial_quality": [
            {
                "instrument_id": "XSHG:600000",
                "accounting_quality_score": "88",
                "audit_opinion": "标准无保留",
                "critical_veto": False,
                "red_flags": ["关注资产质量迁徙"],
            }
        ],
        "governance": [
            {
                "instrument_id": "XSHG:600000",
                "governance_score": "85",
                "management_stability": "稳定",
                "critical_veto": False,
                "red_flags": [],
            }
        ],
        "valuations": [{"instrument_id": "XSHG:600000", "current_price": "12.34"}],
    }
    compact = PositionDocumentProjector._compact_research_snapshot(
        payload, "XSHG:600000", T0 + timedelta(minutes=1)
    )
    assert compact["research_price"] == "12.34"
    assert "5个完整年度" in str(compact["fundamental"])
    assert "周期位置：修复" in str(compact["industry"])
    assert "宏观：温和修复" in str(compact["macro"])
    assert "标准无保留" in str(compact["financial_audit"])
    assert "管理层稳定性：稳定" in str(compact["governance"])
    assert (
        PositionDocumentProjector._compact_research_snapshot(
            payload, "XSHG:600000", T0 - timedelta(seconds=1)
        )
        == {}
    )


def test_new_material_event_is_visible_without_overwriting_entry_snapshot(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    marker = "测试公告：经营现金流预测需要复核"
    event = MaterialEventView(
        event_id="fixture-event",
        instrument_id="XSHG:600000",
        severity="HIGH",
        available_at=T0,
        event_type="OFFICIAL_DISCLOSURE",
        summary=marker,
        source_revision="event-r2",
    )
    result = projector.update(receipt(events=(event,)))
    assert result["changed"] is True
    assert marker in (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    assert marker not in json.dumps(
        next(iter(state["active"].values()))["entry_snapshot"], ensure_ascii=False
    )


def test_corrupt_projection_cache_must_not_erase_closed_history(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    projector.update(receipt(at=T0 + timedelta(days=1), quantity=None, revision="r2"))
    archive = projector.root / "已结束交易.md"
    before = archive.read_bytes()
    assert b"600000" in before
    (projector.root / ".projection.json").write_text("{broken-json", encoding="utf-8")
    try:
        projector.update(receipt(at=T0 + timedelta(days=2), quantity=None, revision="r2"))
    except (OSError, ValueError):
        pass  # Safe degradation may explicitly refuse reconstruction without evidence.
    assert archive.read_bytes() == before, "Corrupt optional cache erased confirmed closed archive"


def test_monitor_plan_cannot_silently_omit_held_subjects():
    r = receipt()
    held = tuple(
        r.context.actual.positions[0].model_copy(update={"instrument_id": f"XSHG:{600000 + i:06d}"})
        for i in range(51)
    )
    actual = r.context.actual.model_copy(update={"positions": held})
    r = r.model_copy(update={"context": r.context.model_copy(update={"actual": actual})})
    planner = ProactiveMonitorPlanner(
        subjects(), Path.cwd() / "configs/scheduled_investor_tracking_v1.yaml"
    )
    plan = planner.plan(r)
    assert plan is not None
    assert len(plan.subjects) == 51, (
        "Per-run budget must batch/defer explicitly, never drop holdings from the plan"
    )
    assert tuple(item for batch in plan.subject_batches for item in batch) == plan.subjects
    assert all(len(batch) <= 50 for batch in plan.subject_batches)


def test_missing_readable_file_is_repaired_without_economic_change(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    document = projector.root / "当前持仓.md"
    before = document.read_bytes()
    document.unlink()
    projector.update(receipt(at=T0 + timedelta(minutes=1)))
    assert document.read_bytes() == before


def test_last_position_closure_clears_obsolete_monitor_plan(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    projector.update(receipt(at=T0 + timedelta(minutes=1), quantity=None))
    plan_text = (projector.root / "主动跟踪计划.md").read_text(encoding="utf-8")
    assert "600000" not in plan_text
    assert "没有" in plan_text


def test_missing_account_identity_preserves_previous_position(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    r = receipt(at=T0 + timedelta(minutes=1), quantity=None)
    r = r.model_copy(
        update={
            "context": r.context.model_copy(
                update={"actual": r.context.actual.model_copy(update={"account_ids": ()})}
            )
        }
    )
    projector.update(r)
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    assert state["active"] and not state["closed"]


def test_older_observation_does_not_resurrect_closed_position(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    projector.update(receipt(at=T0 + timedelta(minutes=1), quantity=None))
    before = snapshot_bytes(projector.root)
    result = projector.update(receipt())
    assert result["changed"] is False
    assert result["deferred"] is True
    assert snapshot_bytes(projector.root) == before


def test_competing_writer_defers_without_losing_existing_state(tmp_path):
    from astock.investor_orchestration.run_ownership import schedule_run_ownership

    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    before = snapshot_bytes(projector.root)
    with schedule_run_ownership(
        projector.root / ".projection.json", "position-documents", "current"
    ):
        result = PositionDocumentProjector(tmp_path, subjects()).update(receipt(quantity=None))
    assert result.get("deferred") is True
    assert snapshot_bytes(projector.root) == before


def test_interrupted_multifile_write_recovers_same_generation(tmp_path, monkeypatch):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    original_write = projector._write

    def fail_archive_once(path, text):
        if path.name == "已结束交易.md":
            raise OSError("synthetic interrupted publish")
        return original_write(path, text)

    monkeypatch.setattr(projector, "_write", fail_archive_once)
    with pytest.raises(OSError, match="synthetic"):
        projector.update(receipt(at=T0 + timedelta(minutes=1), quantity=None))
    assert (projector.root / ".projection.pending.json").exists()
    monkeypatch.setattr(projector, "_write", original_write)
    projector.update(receipt(at=T0 + timedelta(minutes=2), quantity=None))
    state = json.loads((projector.root / ".projection.json").read_text(encoding="utf-8"))
    assert not state["active"] and len(state["closed"]) == 1
    assert "600000" in (projector.root / "已结束交易.md").read_text(encoding="utf-8")
    assert "600000" not in (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    assert not (projector.root / ".projection.pending.json").exists()
    assert not list(projector.root.glob("*.tmp"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("entry_snapshot", []),
        ("current_events", []),
        ("current_events", {"x": "bad"}),
    ],
)
def test_semantically_corrupt_cache_safely_preserves_documents(tmp_path, field, value):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    cache = projector.root / ".projection.json"
    state = json.loads(cache.read_text(encoding="utf-8"))
    next(iter(state["active"].values()))[field] = value
    cache.write_text(json.dumps(state), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in projector.root.glob("*.md")}
    with pytest.raises(ValueError):
        projector.update(receipt(at=T0 + timedelta(minutes=1)))
    assert {p.name: p.read_bytes() for p in projector.root.glob("*.md")} == before


def test_naive_cached_time_is_safe_failure_not_uncaught_type_error(tmp_path):
    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    cache = projector.root / ".projection.json"
    state = json.loads(cache.read_text(encoding="utf-8"))
    state["as_of"] = "2026-09-14T07:00:00"
    cache.write_text(json.dumps(state), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in projector.root.glob("*.md")}
    with pytest.raises(ValueError):
        projector.update(receipt(at=T0 + timedelta(minutes=1)))
    assert {p.name: p.read_bytes() for p in projector.root.glob("*.md")} == before


def test_invalid_pending_shape_is_rejected_before_any_document_write(tmp_path):
    from astock.investor_orchestration.utils import content_hash

    projector = PositionDocumentProjector(tmp_path, subjects())
    projector.update(receipt())
    before = {p.name: p.read_bytes() for p in projector.root.glob("*.md")}
    invalid = {"active": {}, "closed": [], "watch": [], "as_of": T0.isoformat()}
    (projector.root / ".projection.pending.json").write_text(
        json.dumps({"state": invalid, "hash": content_hash(invalid)}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        projector.update(receipt())
    assert {p.name: p.read_bytes() for p in projector.root.glob("*.md")} == before


def test_optional_document_sqlite_failure_never_escapes_preflight_boundary(tmp_path, monkeypatch):
    from astock.investor_orchestration.preflight import InvestorSessionPreflightService
    from astock.investor_orchestration.store import InvestorOrchestrationStore

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = InvestorSessionPreflightService(store)

    def fail_optional_projection(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic optional projection failure")

    monkeypatch.setattr(PositionDocumentProjector, "update", fail_optional_projection)
    service._update_position_documents(receipt())


def test_position_document_root_never_follows_state_database_outside_repository() -> None:
    from astock.investor_orchestration.preflight import InvestorSessionPreflightService

    repository_root = Path(__file__).resolve().parents[2]
    outside_state = Path("C:/outside-astock-test/state.sqlite")
    resolved_root = InvestorSessionPreflightService._position_document_project_root(outside_state)
    assert resolved_root == repository_root

    isolated_state = repository_root / ".ai-bridge" / "tmp" / "state.sqlite"
    assert (
        InvestorSessionPreflightService._position_document_project_root(isolated_state)
        == isolated_state.parent
    )
