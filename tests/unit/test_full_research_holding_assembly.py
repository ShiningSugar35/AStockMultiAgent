from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from astock.investor_orchestration.full_research_assembly import FullResearchReceiptAssembler
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.research.lifecycle_repository import LifecycleRepository
from astock.schemas.knowledge import HoldingReviewPack, PositionAction, PositionMonitoringPlan

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_assembler_projects_canonical_holding_review_with_artifact_lineage(
    tmp_path, monkeypatch
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    assembler = FullResearchReceiptAssembler(store)
    plan = PositionMonitoringPlan(
        plan_id="plan-1",
        position_id="position-1",
        company_id="600001.XSHG",
        decision_id="decision-1",
        decision_reference_status="CANONICAL",
        base_case_id="base-1",
        route_plan_id="route-1",
        memo_id="memo-1",
        as_of=NOW,
        rules_version="rules-v1",
        thesis_summary="核心盈利假设",
        entry_assumptions=[],
        holding_horizon="3-12 months",
        key_value_drivers=[],
        validation_metrics=[],
        monitoring_sources=[],
        monitoring_cadence={},
        price_rules=[],
        fundamental_rules=[],
        event_rules=[],
        add_conditions=[],
        trim_conditions=[],
        exit_conditions=[],
        invalidation_conditions=[],
        manual_information_needs=[],
        next_review_at=NOW + timedelta(days=30),
        skill_versions={},
        evidence_snapshot_id="snapshot-1",
        baseline_evidence_ids=["evidence-baseline-1"],
        coverage_status="COMPLETE",
        created_at=NOW,
    )
    monkeypatch.setattr(
        LifecycleRepository,
        "get_plan",
        lambda _repository, plan_id: plan if plan_id == plan.plan_id else None,
    )

    review = HoldingReviewPack(
        review_id="review-1",
        plan_id=plan.plan_id,
        position_id=plan.position_id,
        as_of=NOW,
        new_market_data=[],
        new_disclosures=[],
        new_regulatory_events=[],
        new_industry_data=[],
        new_news_leads=[],
        manual_evidence_updates=[],
        thesis_strength_change="WEAKENED",
        risk_change="HIGHER",
        triggered_rules=[],
        unresolved_conflicts=[],
        recommended_action=PositionAction.TRIM,
        action_confidence=0.9,
        evidence_ids=["evidence-holding-1"],
        next_review_conditions=["下一份财报发布后复核"],
        current_quantity=500,
        current_weight=0.30,
        target_weight_lower=0.10,
        target_weight_mid=0.15,
        target_weight_upper=0.20,
        target_quantity_min=100,
        target_quantity_max=300,
        preconditions=["确认可售数量"],
        reversal_conditions=["盈利假设重新增强"],
        created_at=NOW,
    )
    review_ref = assembler.objects.put_json(review.model_dump(mode="json"))
    artifact_id = "HoldingReviewPack:review-1"
    assembler.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="HoldingReviewPack",
        schema_version=review.schema_version,
        object_hash=review_ref.sha256,
        input_hashes=[],
    )

    preflight = cast(
        Any,
        SimpleNamespace(context=SimpleNamespace(aggregate_revision="portfolio-revision-1")),
    )
    snapshots = assembler._holding_snapshots((review,), (artifact_id,), preflight)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.instrument_id == plan.company_id
    assert snapshot.recommended_action == "TRIM"
    assert snapshot.portfolio_context_revision == "portfolio-revision-1"
    assert snapshot.source_artifact_id == artifact_id
    assert snapshot.source_object_hash == review_ref.sha256
