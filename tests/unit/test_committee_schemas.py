from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from astock.schemas import (
    CommitteeAccessPolicy,
    CommitteeAssessment,
    CommitteeCoverageMetrics,
    CommitteeDecisionScope,
    CommitteeEntryOrderType,
    CommitteePortfolioRiskState,
    CommitteeProtocolDraft,
    CommitteeRatioRange,
)


def _assessment() -> CommitteeAssessment:
    as_of = datetime(2026, 7, 17, tzinfo=UTC)
    evidence_ids = ["evidence:one"]
    return CommitteeAssessment(
        company_id="company:000001",
        scope=CommitteeDecisionScope.NEW_CANDIDATE,
        as_of=as_of,
        expected_return_range=CommitteeRatioRange(
            lower=Decimal("0.10"),
            upper=Decimal("0.20"),
            evidence_ids=evidence_ids,
        ),
        downside_range=CommitteeRatioRange(
            lower=Decimal("-0.20"),
            upper=Decimal("-0.05"),
            evidence_ids=evidence_ids,
        ),
        confidence=Decimal("0.80"),
        coverage=CommitteeCoverageMetrics(
            data_coverage=Decimal("1"),
            evidence_coverage=Decimal("1"),
            specialist_coverage=Decimal("1"),
            pit_coverage=Decimal("1"),
            liquidity_score=Decimal("1"),
            evidence_ids=evidence_ids,
        ),
        portfolio_risk=CommitteePortfolioRiskState(
            current_total_exposure=Decimal("0"),
            post_decision_total_exposure=Decimal("0.04"),
            current_industry_exposure=Decimal("0"),
            post_decision_industry_exposure=Decimal("0.04"),
            max_abs_correlation=Decimal("0.20"),
            portfolio_drawdown=Decimal("0.01"),
            consecutive_loss_count=0,
            evidence_ids=evidence_ids,
        ),
        tradable=True,
        market_data_quality_pass=True,
        current_position=Decimal("0"),
        requested_position=Decimal("0.04"),
        holding_horizon_days=90,
        review_at=as_of + timedelta(days=7),
        support_evidence_ids=evidence_ids,
        protocol=CommitteeProtocolDraft(
            strategy_id="fixture-strategy",
            skill_versions={"FixtureSkill": "v1"},
            earliest_executable_time=as_of + timedelta(days=1),
            entry_rule="paper entry only after the signal",
            entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
            position_size_rule="never exceed the frozen cap",
            price_stop_rule="fixture price stop",
            volatility_stop_rule="fixture volatility stop",
            trailing_stop_rule="fixture trailing stop",
            time_stop_rule="fixture time stop",
            thesis_invalidation_rule="fixture thesis invalidation",
            take_profit_rule="fixture take-profit review",
            review_events=["ANNUAL_REPORT"],
            max_holding_period_days=365,
            cost_model_version="cost-v1",
            fill_model_version="fill-v1",
            evidence_snapshot_id="pack:fixture",
            evidence_ids=evidence_ids,
        ),
    )


def test_committee_access_policy_cannot_enable_external_capabilities() -> None:
    policy = CommitteeAccessPolicy(frozen_artifact_hashes=["a" * 64])
    assert not policy.network_access
    assert policy.missing_evidence_action == "NEEDS_INFO"
    assert policy.investigation_task_required
    for field in (
        "network_access",
        "api_access",
        "mcp_access",
        "browser_access",
        "full_document_access",
        "new_research_allowed",
    ):
        with pytest.raises(ValidationError):
            CommitteeAccessPolicy.model_validate({field: True})


def test_committee_assessment_rejects_future_execution_and_unreferenced_material_signal() -> None:
    assessment = _assessment()
    with pytest.raises(ValidationError, match="earliest executable"):
        CommitteeAssessment.model_validate(
            assessment.model_copy(
                update={
                    "protocol": assessment.protocol.model_copy(
                        update={"earliest_executable_time": assessment.as_of - timedelta(seconds=1)}
                    )
                }
            ).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="material committee signals require evidence"):
        CommitteeAssessment.model_validate(
            assessment.model_copy(update={"manual_emergency_stop": True}).model_dump(
                mode="python"
            )
        )
    with pytest.raises(ValidationError, match="post-decision total exposure"):
        CommitteeAssessment.model_validate(
            assessment.model_copy(
                update={
                    "portfolio_risk": assessment.portfolio_risk.model_copy(
                        update={"post_decision_total_exposure": Decimal("0.01")}
                    )
                }
            ).model_dump(mode="python")
        )


def test_committee_ranges_and_protocol_require_sorted_unique_evidence() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        CommitteeRatioRange(
            lower=Decimal("0.1"),
            upper=Decimal("0.2"),
            evidence_ids=["evidence:z", "evidence:a"],
        )
    with pytest.raises(ValidationError, match="Input should be True"):
        CommitteeProtocolDraft.model_validate(
            {
                **_assessment().protocol.model_dump(mode="python"),
                "requires_user_confirmation": False,
            }
        )
