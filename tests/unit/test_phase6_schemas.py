from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from astock.schemas import (
    CommitteeEntryOrderType,
    CommitteeProtocolStatus,
    CommitteeVerdict,
    TradeProtocol,
    TradeProtocolOutcome,
)

_NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def _protocol(verdict: CommitteeVerdict) -> TradeProtocol:
    executable = verdict in {
        CommitteeVerdict.PAPER_ELIGIBLE,
        CommitteeVerdict.PAPER_EXIT,
    }
    blocked = verdict in {
        CommitteeVerdict.PAPER_HOLD,
        CommitteeVerdict.REJECT,
        CommitteeVerdict.NEEDS_INFO,
    }
    return TradeProtocol(
        protocol_id=f"protocol:{verdict.value}",
        decision_id=f"decision:{verdict.value}",
        decision_sha256="a" * 64,
        company_id="300750",
        verdict=verdict,
        protocol_status=(
            CommitteeProtocolStatus.BLOCKED if blocked else CommitteeProtocolStatus.ACTIVE
        ),
        blocking_codes=["VERDICT_BLOCKED"] if blocked else [],
        strategy_id="phase6-test",
        skill_versions={"phase6-test": "v1"},
        signal_time=_NOW,
        earliest_executable_time=_NOW + timedelta(minutes=1),
        holding_horizon_days=30,
        entry_rule="paper only",
        entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
        position_size_rule="user supplied",
        price_stop_rule="frozen rule",
        volatility_stop_rule="frozen rule",
        trailing_stop_rule="frozen rule",
        time_stop_rule="frozen rule",
        thesis_invalidation_rule="frozen rule",
        take_profit_rule="frozen rule",
        review_events=["ANNUAL_REPORT"],
        max_holding_period_days=180,
        cost_model_version="cost-v1",
        fill_model_version="fill-v1",
        evidence_snapshot_id="evidence-pack:test",
        evidence_ids=["evidence:test"],
        effective_from=_NOW + timedelta(minutes=1),
        broker_execution_allowed=False,
        paper_simulation_allowed=executable,
        ledger_write_allowed=executable,
    )


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (CommitteeVerdict.PAPER_HOLD, TradeProtocolOutcome.WATCH),
        (CommitteeVerdict.REJECT, TradeProtocolOutcome.REJECT),
        (CommitteeVerdict.NEEDS_INFO, TradeProtocolOutcome.NEEDS_INFO),
        (
            CommitteeVerdict.PAPER_ELIGIBLE,
            TradeProtocolOutcome.APPROVE_SIMULATION,
        ),
    ],
)
def test_trade_protocol_exposes_only_public_phase6_outcomes(
    verdict: CommitteeVerdict,
    outcome: TradeProtocolOutcome,
) -> None:
    protocol = _protocol(verdict)
    assert protocol.outcome is outcome
    assert not protocol.broker_execution_allowed


def test_trade_protocol_rejects_broker_or_mismatched_paper_gates() -> None:
    eligible = _protocol(CommitteeVerdict.PAPER_ELIGIBLE)
    with pytest.raises(ValidationError, match="Input should be False"):
        TradeProtocol.model_validate(
            {**eligible.model_dump(mode="python"), "broker_execution_allowed": True}
        )
    with pytest.raises(ValidationError, match="simulation gate"):
        TradeProtocol.model_validate(
            {**eligible.model_dump(mode="python"), "paper_simulation_allowed": False}
        )
    with pytest.raises(ValidationError, match="ledger gate"):
        TradeProtocol.model_validate(
            {**eligible.model_dump(mode="python"), "ledger_write_allowed": False}
        )
