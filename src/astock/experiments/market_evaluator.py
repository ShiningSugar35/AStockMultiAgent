"""Trusted daily-close research simulator, with no paper/account writes.

The host admits a registered, exhaustive canonical release, never a caller's
return stream. Daily close executions are a conservative model, not queue fills.
Only lagged raw-price momentum and cash are supported; corporate actions that
cannot be accounted for exactly are rejected rather than silently adjusted.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.experiments.validation import (
    Budget,
    Evidence,
    Frozen,
    Plan,
    Query,
    RegisteredEvidenceEvaluator,
    Row,
    digest,
)
from astock.schemas.market import MarketBar
from astock.schemas.paper import CorporateActionEvent, OrderSide, ReplayFeeSchedule

VERSION = "canonical-daily-close-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class Session(Frozen):
    bar: MarketBar
    benchmark: MarketBar
    suspended: bool
    # Exact exchange limits, including ST/board/action changes; never inferred.
    limit_up: Decimal = Field(gt=0)
    limit_down: Decimal = Field(gt=0)


class FrozenMarketRelease(Frozen):
    schema_version: Literal["strategy-canonical-market-v1"] = "strategy-canonical-market-v1"
    symbol: str
    benchmark_id: str
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    sessions: tuple[Session, ...] = Field(min_length=2)
    corporate_actions: tuple[CorporateActionEvent, ...]
    corporate_action_coverage: Literal["TERMS_VERIFIED_EXHAUSTIVE"]
    calendar_coverage: Literal["EXCHANGE_SESSIONS_EXHAUSTIVE"]
    fees: tuple[ReplayFeeSchedule, ...] = Field(min_length=1)
    initial_cash_fen: int = Field(gt=0)
    slippage_bps: Decimal = Field(ge=0, le=1000)
    maximum_participation: Decimal = Field(gt=0, le=1)


class Parameters(Frozen):
    lookback: int = Field(ge=1, le=1000, strict=True)
    shares: int = Field(gt=0, strict=True)


def _day(bar: MarketBar) -> date:
    return bar.timestamp.astimezone(SHANGHAI).date()


def _fen(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _fees(notional: int, sell: bool, schedule: ReplayFeeSchedule) -> int:
    from astock.paper_trading.replay import PaperReplayService

    return sum(
        PaperReplayService._fees(OrderSide.SELL if sell else OrderSide.BUY, notional, schedule)
    )


class CanonicalMarketEvaluator:
    """Host-only constructor: registry/object readers are trusted capabilities.

    The entire input is hashed and copied at admission. Strategies receive no
    callback or access to the release; their only signal uses closes before t.
    Windows start flat and end marked to market, with no forced fictional sale.
    """

    def __init__(self, state, objects, artifact_id: str, plan: Plan):
        reader = RegisteredEvidenceEvaluator(state, objects, lambda q, b: artifact_id, plan.policy)
        release = reader._read(artifact_id, "StrategyCanonicalMarketRelease", FrozenMarketRelease)
        self._release_json = release.model_dump_json()
        self._plan_json = plan.model_dump_json()
        self.artifact_id = artifact_id
        self.state, self.objects = state, objects
        if len(release.source_snapshot_ids) > plan.policy.max_bars:
            raise ValueError("source reference budget exceeded")
        source_bytes = 0
        for snapshot_id in dict.fromkeys(release.source_snapshot_ids):
            snapshot = state.get_snapshot(snapshot_id)
            if snapshot is None or snapshot.fetch_status != "SUCCEEDED":
                raise ValueError("canonical source snapshot unavailable")
            source_bytes += snapshot.byte_size
            if source_bytes > plan.policy.max_artifact_bytes:
                raise ValueError("source snapshot byte budget exceeded")
            raw = objects.get_bytes(snapshot.object_sha256)
            if (
                len(raw) > plan.policy.max_artifact_bytes
                or len(raw) != snapshot.byte_size
                or sha256_bytes(raw) != snapshot.object_sha256
            ):
                raise ValueError("canonical source snapshot integrity mismatch")
        if digest(release) != plan.request.input_release_hash:
            raise ValueError("canonical release hash mismatch")
        if (
            plan.request.executor_version != VERSION
            or release.benchmark_id != plan.request.benchmark_id
        ):
            raise ValueError("canonical executor/benchmark mismatch")
        if tuple(s.bar.timestamp for s in release.sessions) != plan.request.timestamps:
            raise ValueError("canonical session coverage mismatch")
        if len(release.sessions) > plan.policy.max_bars:
            raise ValueError("canonical bar budget exceeded")
        last_day = None
        stock_market = release.sessions[0].bar.market
        for session in release.sessions:
            bar, benchmark = session.bar, session.benchmark
            day = _day(bar)
            if last_day is not None and day <= last_day:
                raise ValueError("daily sessions must have unique increasing exchange dates")
            last_day = day
            if (
                bar.symbol != release.symbol
                or benchmark.symbol != release.benchmark_id
                or bar.market not in {"XSHG", "XSHE", "BJSE"}
                or bar.market != stock_market
                or benchmark.market != "INDEX"
                or bar.timestamp != benchmark.timestamp
            ):
                raise ValueError("canonical security/benchmark identity mismatch")
            for item in (bar, benchmark):
                if (
                    item.frequency != "1d"
                    or item.adjustment_mode != "NONE"
                    or item.timestamp_semantics != "BAR_END"
                    or item.volume_unit != "SHARE"
                    or not 0 < item.low <= min(item.open, item.close)
                    or not max(item.open, item.close) <= item.high
                    or (item.market != "INDEX" and any(
                        v * 100 != (v * 100).to_integral_value()
                        for v in (item.open, item.high, item.low, item.close)
                    ))
                ):
                    raise ValueError("invalid canonical raw daily OHLC/units")
            if not session.limit_down <= bar.low <= bar.high <= session.limit_up:
                raise ValueError("bar outside verified exchange limits")
            if not session.suspended and bar.volume <= 0:
                raise ValueError("missing trading volume")
            if not any(
                s.effective_from <= day and bar.market in s.applicable_markets for s in release.fees
            ):
                raise ValueError("applicable fee schedule unavailable")
        effective = [s.effective_from for s in release.fees]
        if effective != sorted(set(effective)) or effective[0] > _day(release.sessions[0].bar):
            raise ValueError("fee coverage missing or ambiguous")
        for candidate in plan.request.candidates:
            if candidate.strategy_version != "lagged-momentum-v1":
                raise ValueError("strategy version not admitted")
            p = Parameters.model_validate(candidate.parameters)
            if p.shares % 100:
                raise ValueError("A-share buy quantity requires 100-share lots")
        if len(release.corporate_actions) > plan.policy.max_bars:
            raise ValueError("corporate action budget exceeded")
        session_days = {_day(s.bar) for s in release.sessions}
        for action in release.corporate_actions:
            if (
                action.symbol != release.symbol
                or not action.source_id
                or not action.rule_version
                or action.event_type != "CASH_DIVIDEND"
                or action.cash_fen is None
                or action.cash_fen < 0
                or action.record_date is None
                or action.pay_date != action.ex_date
                or date.fromisoformat(action.record_date) >= date.fromisoformat(action.ex_date)
            ):
                raise ValueError("unsupported or unverified corporate action terms")
            for value in (action.record_date, action.ex_date):
                event_day = date.fromisoformat(value)
                if (
                    min(session_days) <= event_day <= max(session_days)
                    and event_day not in session_days
                ):
                    raise ValueError("corporate action session coverage unavailable")
        if len({a.event_id for a in release.corporate_actions}) != len(release.corporate_actions):
            raise ValueError("duplicate corporate action")

    def evaluate(self, query: Query, budget: Budget) -> Evidence:
        budget.check()
        if query.phase == "DIAGNOSTIC":
            raise ValueError("canonical evaluator requires a planned train/OOS window")
        plan = Plan.model_validate_json(self._plan_json)
        release = FrozenMarketRelease.model_validate_json(self._release_json)
        if (
            query.plan_hash != digest(plan)
            or query.executor_version != VERSION
            or query.input_release_hash != digest(release)
            or query.benchmark_id != release.benchmark_id
            or digest(query.candidate) not in {digest(c) for c in plan.request.candidates}
            or query.window.stop > len(release.sessions)
        ):
            raise ValueError("evaluation query binding mismatch")
        expected = (
            plan.final_holdout
            if query.phase == "FINAL_HOLDOUT"
            else plan.final_train
            if query.phase == "FINAL_TRAIN"
            else next(
                (
                    getattr(f, "train" if query.phase == "TRAIN" else "test")
                    for f in plan.folds
                    if f.number == query.fold_number
                ),
                None,
            )
        )
        if query.phase != "DIAGNOSTIC" and query.window != expected:
            raise ValueError("evaluation window mismatch")
        if query.phase in {"OOS", "FINAL_HOLDOUT"} and not query.selection_hash:
            raise ValueError("selection must be sealed before OOS")
        if query.phase in {"OOS", "FINAL_HOLDOUT"}:
            claim_id = "StrategySelectionStudyRun:" + digest(
                {"study_id": plan.request.study_id, "run_id": plan.request.run_id}
            )
            record = self.state.artifact_record(claim_id)
            if record is None or record["type"] != "StrategySelectionStudyRun":
                raise ValueError("registered selection seal unavailable")
            raw = self.objects.get_bytes(record["object_hash"])
            if (
                len(raw) > plan.policy.max_artifact_bytes
                or sha256_bytes(raw) != record["object_hash"]
            ):
                raise ValueError("selection object hash mismatch")
            from astock.experiments.validation import Commitment

            commitment = Commitment.model_validate(json.loads(raw)["commitment"])
            if commitment.plan_hash != digest(plan) or not any(
                digest(s) == query.selection_hash
                and s.phase == query.phase
                and s.fold_number == query.fold_number
                and s.candidate == query.candidate
                for s in commitment.selections
            ):
                raise ValueError("registered selection binding mismatch")
        parameters = Parameters.model_validate(query.candidate.parameters)
        cash = Decimal(release.initial_cash_fen)
        shares = 0
        bought = None
        previous = None
        record_positions = {}
        rows = []
        executions = []
        actions_by_day = {}
        for action in release.corporate_actions:
            actions_by_day.setdefault(date.fromisoformat(action.ex_date), []).append(action)
        # Local decimal context makes repeatability independent of caller settings.
        with localcontext() as ctx:
            ctx.prec = 34
            for i in range(query.window.start, query.window.stop):
                budget.check()
                session = release.sessions[i]
                bar = session.bar
                day = _day(bar)
                mark = bar.close * 100
                start_nav = cash + shares * (previous if previous is not None else mark)
                dividend = 0
                for action in actions_by_day.get(day, []):
                    record = date.fromisoformat(action.record_date)
                    # A flat independent window has no pre-window entitlement.
                    entitled = record_positions.get(record, 0)
                    dividend += entitled * action.cash_fen
                cash += dividend
                target = 0
                if i - query.window.start > parameters.lookback:
                    if (
                        release.sessions[i - 1].bar.close
                        > release.sessions[i - 1 - parameters.lookback].bar.close
                    ):
                        target = parameters.shares
                delta = target - shares
                fee = slip = turnover = Decimal(0)
                if (
                    delta
                    and not session.suspended
                    and abs(delta) <= bar.volume * release.maximum_participation
                    and (
                        delta > 0
                        and bar.close < session.limit_up
                        or delta < 0
                        and bar.close > session.limit_down
                        and bought != day
                    )
                ):
                    schedules = [
                        s
                        for s in release.fees
                        if s.effective_from <= day and bar.market in s.applicable_markets
                    ]
                    if not schedules:
                        raise ValueError("applicable fee schedule unavailable")
                    execution = _fen(
                        mark * (1 + (1 if delta > 0 else -1) * release.slippage_bps / 10000)
                    )
                    if execution <= 0:
                        raise ValueError("invalid slipped execution")
                    if session.limit_down * 100 <= execution <= session.limit_up * 100:
                        notional = abs(delta) * execution
                        cost = _fees(notional, delta < 0, schedules[-1])
                        if delta < 0 or cash >= notional + cost:
                            fee = Decimal(cost)
                            slip = abs(delta) * abs(execution - mark)
                            turnover = abs(delta) * mark
                            cash -= delta * execution + cost
                            shares += delta
                            if delta > 0:
                                bought = day
                            executions.append(
                                {
                                    "timestamp": bar.timestamp.isoformat(),
                                    "quantity_delta": delta,
                                    "price_fen": execution,
                                    "fees_fen": cost,
                                    "fee_rule": schedules[-1].rule_version,
                                }
                            )
                end_nav = cash + shares * mark
                benchmark_return = (
                    Decimal(0)
                    if i == query.window.start
                    else (session.benchmark.close / release.sessions[i - 1].benchmark.close - 1)
                )
                rows.append(
                    Row(
                        period_end=bar.timestamp,
                        gross_return=(end_nav - start_nav + fee + slip) / start_nav,
                        fee_return=fee / start_nav,
                        slippage_return=slip / start_nav,
                        turnover=turnover / start_nav,
                        benchmark_return=benchmark_return,
                    )
                )
                previous = mark
                record_positions[day] = shares
        execution = {
            "schema_version": "strategy-daily-execution-v1",
            "query_hash": digest(query),
            "input_release_hash": digest(release),
            "fills": executions,
            "ending_cash_fen": str(cash),
            "ending_shares": shares,
            "rows": [r.model_dump(mode="json") for r in rows],
        }
        raw = canonical_json_bytes(execution)
        if len(raw) > plan.policy.max_artifact_bytes:
            raise ValueError("execution artifact budget exceeded")
        obj = self.objects.put_bytes(raw)
        execution_id = "StrategyDailyExecution:" + obj.sha256
        self.state.register_artifact(
            artifact_id=execution_id,
            artifact_type="StrategyDailyExecution",
            schema_version="strategy-daily-execution-v1",
            object_hash=obj.sha256,
            input_hashes=[query.input_release_hash, digest(query)],
        )
        return Evidence(
            query_hash=digest(query),
            producer_version=VERSION,
            input_release_hash=digest(release),
            source_snapshot_ids=release.source_snapshot_ids,
            execution_artifact_ids=(execution_id,),
            rows=tuple(rows),
            accounting_basis="PERIOD_START_NAV_GROSS_MINUS_FEES_MINUS_SLIPPAGE",
            boundary_policy="INDEPENDENT_WINDOW_NO_UNACCOUNTED_CARRY",
        )
