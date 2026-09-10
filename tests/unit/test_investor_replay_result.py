"""Actual confirmed-paper replay and its read-only public-result audit.

Market responses are explicitly recorded fixtures. Orders, confirmation, canonical
market publication, matching, bar commits, fills and ledger projections are real
project services in an isolated database, not success stubs.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.capabilities import CapabilityExecutionResult
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.scenarios import ScenarioContractRunner
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.market_data.quality import cross_validate_batches
from astock.market_data.storage import CanonicalMarketStore
from astock.paper_trading import LedgerService, PaperReplayService, load_fee_schedule
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    BarRequest,
    Frequency,
    Market,
    MarketBar,
    MarketDataBatch,
    ProviderStatus,
    ReplayExecutionReport,
    ReplayFeeSchedule,
    SourceSnapshot,
    TimestampSemantics,
    VolumeUnit,
)
from tests.unit.test_paper_operations import NOW, _confirmation, _request, _service

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReplayCase:
    state: StateStore
    objects: ObjectStore
    metadata: InvestorOrchestrationStore
    ledger: LedgerService
    engine: PaperReplayService
    market_request: BarRequest
    fees: ReplayFeeSchedule
    report: ReplayExecutionReport
    original_order_id: str


def _recorded_batch(
    state: StateStore, objects: ObjectStore, provider: str, volume_unit: VolumeUnit
) -> MarketDataBatch:
    timestamps = [
        NOW.replace(hour=hour, minute=minute)
        for hour, minute in ((10, 30), (11, 30), (14, 0), (15, 0))
    ]
    request = BarRequest(
        symbol="600519",
        market=Market.XSHG,
        frequency=Frequency.H1,
        requested_start=NOW.replace(hour=0, minute=0),
        requested_end=NOW.replace(hour=23, minute=59),
        adjustment_mode=AdjustmentMode.NONE,
    )
    bars = []
    for timestamp in timestamps:
        values = {
            "provider_id": provider,
            "symbol": request.symbol,
            "market": request.market,
            "frequency": request.frequency,
            "timestamp": timestamp,
            "timestamp_semantics": TimestampSemantics.BAR_END,
            "open": Decimal("10"),
            "high": Decimal("10.02"),
            "low": Decimal("9.98"),
            "close": Decimal("10.01"),
            "volume": Decimal(1000 if volume_unit is VolumeUnit.LOT_100_SHARES else 100000),
            "volume_unit": volume_unit,
            "amount": Decimal("1001000"),
            "amount_unit": AmountUnit.CNY,
            "adjustment_mode": AdjustmentMode.NONE,
        }
        bars.append(MarketBar.model_validate({"observation_id": content_hash(values), **values}))
    raw = objects.put_json([bar.model_dump(mode="json") for bar in bars])
    source_id = f"recorded-investor-replay:{provider}"
    snapshot = SourceSnapshot(
        snapshot_id=f"{source_id}:{raw.sha256}",
        source_id=source_id,
        object_sha256=raw.sha256,
        byte_size=raw.byte_size,
        fetched_at=timestamps[-1] + timedelta(minutes=1),
        available_to_system_at=timestamps[-1] + timedelta(minutes=1),
        source_url="https://example.invalid/recorded-investor-replay",
        mime="application/json",
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    return MarketDataBatch(
        batch_id=content_hash({"provider": provider, "bars": [bar.observation_id for bar in bars]}),
        provider_id=provider,
        request=request,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        actual_start=timestamps[0],
        actual_end=timestamps[-1],
        bar_count=len(bars),
        bars=bars,
        raw_snapshot_id=snapshot.snapshot_id,
        cursor=timestamps[-1].isoformat(),
        provider_latency_ms=1,
        provider_status=ProviderStatus.AVAILABLE,
    )


def make_replay_case(root: Path) -> ReplayCase:
    state = StateStore(root / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(root / "objects" / "sha256")
    operations, ledger = _service(state, objects)
    request = _request()
    operations.execute(request, _confirmation(request))
    order = ledger.open_orders("paper")[0]
    primary = _recorded_batch(state, objects, "eastmoney-5m", VolumeUnit.LOT_100_SHARES)
    secondary = _recorded_batch(state, objects, "sina-5m", VolumeUnit.SHARE)
    canonical = CanonicalMarketStore(root / "parquet", root / "manifests")
    canonical.publish(
        primary,
        cross_validate_batches(primary, secondary),
        source_batch_ids=[primary.batch_id, secondary.batch_id],
    )
    engine = PaperReplayService(ledger, canonical, operations.references)
    fees = load_fee_schedule(ROOT / "configs" / "fee_rules.yaml")
    report = engine.replay(
        account_id="paper",
        request=primary.request,
        requested_cursor=NOW + timedelta(hours=6),
        fee_schedule=fees,
    )
    metadata = InvestorOrchestrationStore(state.path)
    metadata.initialize()
    assert report.fill_ids and report.processed_bars > 0
    assert ledger.get_order(order.order_id).filled_qty == 100
    return ReplayCase(
        state, objects, metadata, ledger, engine, primary.request, fees, report, order.order_id
    )


@pytest.fixture(scope="module")
def replay_case(tmp_path_factory: pytest.TempPathFactory) -> ReplayCase:
    return make_replay_case(tmp_path_factory.mktemp("investor-replay-result"))


def register_report(case: ReplayCase, report: ReplayExecutionReport) -> str:
    reference = case.objects.put_json(report.model_dump(mode="json"))
    artifact_id = f"ReplayExecutionReport:{reference.sha256}"
    with closing(case.state.connect()) as connection:
        hashes = [
            str(row[0])
            for row in connection.execute(
                "SELECT commit_object_hash FROM paper_replay_bar_commit WHERE account_id=?",
                (report.account_id,),
            )
        ]
    if case.state.artifact_record(artifact_id) is None:
        case.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ReplayExecutionReport",
            schema_version=report.schema_version,
            object_hash=reference.sha256,
            input_hashes=sorted(hashes),
        )
    return artifact_id


def request_for_report() -> InvestorRequestEnvelope:
    identity = f"replay-result-{uuid4().hex}"
    return InvestorRequestEnvelope(
        request_id=identity,
        idempotency_key=identity,
        raw_text="查看之前的模拟限价单",
        question_time=datetime.now(UTC),
        user_timezone="Asia/Shanghai",
        normalized_intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.PT_REPLAY,
        account_id="paper",
        entity_ids=("XSHG:600519",),
    )


def publish_replay(case: ReplayCase, report: ReplayExecutionReport):
    artifact_id = register_report(case, report)
    request = request_for_report()
    runner = ScenarioContractRunner.from_path(
        InvestorOrchestrationService(case.metadata), ROOT / "configs" / "business_scenarios_v1.yaml"
    )
    return runner.run(
        96,
        request,
        handlers={
            "PAPER": lambda _request, _preflight: CapabilityExecutionResult(
                artifact_ids=(artifact_id,)
            )
        },
    )


def test_actual_replay_result_reaches_certified_public_answer(replay_case: ReplayCase) -> None:
    result = publish_replay(replay_case, replay_case.report)
    assert result.failures == ()
    assert not result.answer.degraded
    assert result.coverage_complete
    assert "模拟成交" in result.answer.conclusion
    assert any("近似" in item for item in result.answer.risks)


@pytest.mark.parametrize(
    "changes",
    [
        {"processed_bars": 999},
        {"matched_orders": 999},
        {"fill_ids": []},
        {"fill_ids": ["unrecorded-fill"]},
        {"checkpoint": None},
        {"symbol": "000001"},
        {"market": Market.XSHE},
    ],
)
def test_registered_but_false_replay_facts_are_rejected(
    replay_case: ReplayCase, changes: dict[str, Any]
) -> None:
    bad = replay_case.report.model_copy(update=changes)
    identity = register_report(replay_case, bad)
    request = request_for_report()
    service = InvestorOrchestrationService(replay_case.metadata)
    preflight, plan = service.prepare(request)
    node = next(node for node in plan.nodes if node.capability_id == "PAPER")
    with pytest.raises(ValueError):
        RegisteredOutputVerifier(replay_case.metadata).verify(node, (identity,), request, preflight)


def test_replay_retry_has_zero_duplicate_fills_and_can_be_published(
    replay_case: ReplayCase,
) -> None:
    from scripts.benchmark_investor_preflight import economic_digest

    before = economic_digest(replay_case.state)
    repeated = replay_case.engine.replay(
        account_id="paper",
        request=replay_case.market_request,
        requested_cursor=replay_case.report.requested_cursor,
        fee_schedule=replay_case.fees,
    )
    assert repeated.processed_bars == 0 and repeated.fill_ids == []
    assert economic_digest(replay_case.state) == before
    result = publish_replay(replay_case, repeated)
    assert result.failures == () and not result.answer.degraded
    assert "未新增" in result.answer.conclusion
    assert economic_digest(replay_case.state) == before
