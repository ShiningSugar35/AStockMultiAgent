"""Read-only recommendation reference performance; never execution accounting."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.market_data.official_calendar import OfficialTradingCalendarConfig
from astock.research.debate_evaluation import QualityError, QualityPolicy, aware, digest, sealed
from astock.schemas.full_research import RecommendationResearchReceipt
from astock.schemas.market import Market
from astock.schemas.reference_data import DailyBarObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")


def decision_completed_at(receipt: RecommendationResearchReceipt) -> datetime:
    return max([receipt.as_of, *(x.completed_at for x in receipt.dag.executions)])


def published(receipt: RecommendationResearchReceipt) -> bool:
    return bool(
        receipt.publication.formal_recommendation_allowed
        and receipt.publication.status in {"PUBLISH", "CONDITIONAL_ONLY"}
        and receipt.dag.publication_ready
        and any(
            x.node.value == "PUBLICATION_GATE" and x.status.value == "PASS"
            for x in receipt.dag.executions
        )
    )


def evaluate_outcome(
    receipt: RecommendationResearchReceipt,
    instrument_id: str,
    benchmark_id: str,
    stock_bars: tuple[DailyBarObservation, ...],
    benchmark_bars: tuple[DailyBarObservation, ...],
    calendar: OfficialTradingCalendarConfig,
    policy: QualityPolicy,
    *,
    evaluated_at: datetime,
    holding_sessions: int,
    context_id: str,
    lineage: dict | None = None,
    corporate_action_dates: set[date] | None = None,
) -> dict:
    """Entry is a strictly later exchange opening; horizon counts exchange sessions.

    Metrics are unadjusted gross PRICE returns, excluding dividends and fees.
    They describe a reference trajectory, not a filled conditional strategy.
    """
    evaluated_at = aware(evaluated_at)
    instrument_id = normalized_instrument(instrument_id)
    benchmark_id = normalized_instrument(benchmark_id)
    if not context_id or holding_sessions not in policy.holding_sessions:
        raise QualityError("INVALID_OUTCOME_REQUEST")
    if len(stock_bars) > policy.max_bars or len(benchmark_bars) > policy.max_bars:
        raise QualityError("INPUT_LIMIT")
    completed = decision_completed_at(receipt)
    base = {
        "context_id": context_id,
        "receipt_id": receipt.receipt_id,
        "receipt_hash": receipt.receipt_hash,
        "receipt_input_hash": digest(receipt.model_dump(mode="json")),
        "instrument_id": instrument_id,
        "benchmark_id": benchmark_id,
        "decision_completion_boundary": completed.isoformat(),
        "evaluation_boundary": evaluated_at.isoformat(),
        "holding_sessions": holding_sessions,
        "policy": policy.model_dump(mode="json"),
        "performance_kind": "RECOMMENDATION_REFERENCE_GROSS_UNADJUSTED_PRICE",
        "actual_trade_profit": None,
        "net_return": None,
        "condition_execution": "NOT_MODELED",
        "stop_target_execution": "NOT_MODELED",
        "ledger_write_allowed": False,
        "lineage": lineage or {},
    }

    def finish(status: str, **fields) -> dict:
        return sealed("outcome-feedback", {**base, "status": status, **fields})

    if completed > evaluated_at:
        return finish("DECISION_NOT_YET_COMPLETE")
    if not published(receipt):
        return finish("UNPUBLISHED")
    holding = next(
        (
            x
            for x in receipt.holding_reviews
            if normalized_instrument(x.instrument_id) == instrument_id
        ),
        None,
    )
    if holding is not None and holding.recommended_action not in {"HOLD", "ADD"}:
        return finish(
            "NON_LONG_ACTION_NOT_EVALUATED", recommendation_action=holding.recommended_action
        )
    base["recommendation_action"] = (
        holding.recommended_action if holding is not None else "NEW_LONG"
    )
    rankings = {normalized_instrument(x.instrument_id): x for x in receipt.candidate_rankings}
    positions = {normalized_instrument(x.instrument_id) for x in receipt.portfolio.positions}
    if (
        instrument_id not in positions
        or instrument_id not in rankings
        or not rankings[instrument_id].eligible
    ):
        return finish("NOT_A_PUBLISHED_LONG_RECOMMENDATION")
    if receipt.publication.status == "CONDITIONAL_ONLY":
        return finish("CONDITIONAL_ENTRY_NOT_EVALUATED")
    if instrument_id == benchmark_id:
        raise QualityError("BENCHMARK_IDENTITY_MISMATCH")
    try:
        exchange = Market(instrument_id.split(":", 1)[0])
    except ValueError:
        raise QualityError("SECURITY_MISMATCH") from None
    if exchange is Market.INDEX:
        raise QualityError("SECURITY_MISMATCH")
    start = completed.astimezone(SHANGHAI).date()
    today = evaluated_at.astimezone(SHANGHAI).date()
    if (today - start).days > policy.max_calendar_days:
        return finish("EVALUATION_WINDOW_LIMIT")
    dates = calendar.open_dates(exchange, start, today)
    if dates is None:
        return finish("CALENDAR_UNKNOWN")
    # Session time is shared with the brief consumer and comes from one policy.
    opening = time.fromisoformat(policy.session_boundaries[0])
    eligible_dates = [d for d in dates if datetime.combine(d, opening, SHANGHAI) > completed]
    if len(eligible_dates) < holding_sessions:
        return finish("IMMATURE")
    window = eligible_dates[:holding_sessions]
    close_time = time.fromisoformat(policy.session_boundaries[-1])
    if datetime.combine(window[-1], close_time, SHANGHAI) > evaluated_at:
        return finish("IMMATURE")

    def select(bars, identity):
        selected = {}
        window_set = set(window)
        for bar in bars:
            if bar.instrument_id != identity:
                raise QualityError("SECURITY_MISMATCH")
            if (
                bar.session_date not in window_set
                or max(bar.session_close_at, bar.available_to_system_at) > evaluated_at
            ):
                continue
            previous = selected.get(bar.session_date)
            if previous is not None and previous.model_dump(mode="json") != bar.model_dump(
                mode="json"
            ):
                raise QualityError("BAR_CONFLICT")
            selected[bar.session_date] = bar
        return selected

    stock = select(stock_bars, instrument_id)
    benchmark = select(benchmark_bars, benchmark_id)
    base["calendar_dates"] = [d.isoformat() for d in window]
    base["calendar_version"] = calendar.schema_version
    base["calendar_sources"] = [
        {
            "year": year,
            "published_boundary": item.published_at.isoformat(),
            "verification_boundary": item.verified_at.isoformat(),
            "source_url": item.sources[exchange].url,
        }
        for year, item in calendar.years.items()
        if start.year <= year <= window[-1].year
    ]
    base["visible_bar_inputs"] = [
        b.model_dump(mode="json")
        for d in window
        for b in (stock.get(d), benchmark.get(d))
        if b is not None
    ]
    missing_stock = [d.isoformat() for d in window if d not in stock]
    missing_benchmark = [d.isoformat() for d in window if d not in benchmark]
    if missing_stock:
        return finish("MISSING_BARS", missing_session_dates=missing_stock)
    if missing_benchmark:
        return finish("MISSING_BENCHMARK", missing_session_dates=missing_benchmark)
    if any(stock[d].volume == 0 for d in window):
        return finish("SUSPENDED_OR_NO_TRADE")
    entry = stock[window[0]].open
    exit_price = stock[window[-1]].close
    gross = exit_price / entry - Decimal("1")
    benchmark_return = benchmark[window[-1]].close / benchmark[window[0]].open - Decimal("1")
    adverse = min(Decimal("0"), min(stock[d].low / entry - Decimal("1") for d in window))
    # Company-action coverage must originate in a verified canonical release.
    # Unknown coverage or any in-window action makes raw prices unsuitable for a
    # directional correctness label; only the descriptive price path remains.
    price_comparability = (
        "UNKNOWN"
        if corporate_action_dates is None
        else (
            "ACTION_IN_WINDOW"
            if any(day in corporate_action_dates for day in window)
            else "VERIFIED_NO_ACTION"
        )
    )
    return finish(
        "MATURE" if price_comparability == "VERIFIED_NO_ACTION" else "PRICE_ONLY_UNQUALIFIED",
        price_comparability=price_comparability,
        entry_reference_time=datetime.combine(window[0], opening, SHANGHAI).isoformat(),
        exit_reference_time=datetime.combine(window[-1], close_time, SHANGHAI).isoformat(),
        entry_reference_price=str(entry),
        exit_reference_price=str(exit_price),
        gross_price_return=str(gross),
        benchmark_gross_price_return=str(benchmark_return),
        benchmark_relative_return=str(gross - benchmark_return),
        maximum_adverse_excursion=str(adverse),
        direction_correct=(gross > 0 if price_comparability == "VERIFIED_NO_ACTION" else None),
        direction="LONG",
        interpretation=(
            "建议参考价格表现，非实际成交盈亏；未计费用和分红。"
            if price_comparability == "VERIFIED_NO_ACTION"
            else "公司行动影响或覆盖状态未核实，不对价格方向判断正确性打分。"
        ),
    )


def summarize_feedback(
    results: tuple[dict, ...], policy: QualityPolicy, *, context_id: str
) -> dict:
    """Only structurally intact mature results from this evaluator are eligible.

    Registration/authentication belongs to the caller; this does not admit an
    arbitrary JSON report as evidence or change production parameters.
    """
    if not context_id or len(results) > policy.max_feedback_samples:
        raise QualityError("INPUT_LIMIT")
    selected = {}
    for result in results:
        body = {k: v for k, v in result.items() if k != "result_id"}
        if result.get("result_id") != "outcome-feedback:" + digest(body):
            raise QualityError("FEEDBACK_INTEGRITY_MISMATCH")
        if (
            result.get("status") != "MATURE"
            or result.get("price_comparability") != "VERIFIED_NO_ACTION"
            or result.get("direction_correct") is None
        ):
            continue
        key = (
            result["receipt_id"],
            result["receipt_hash"],
            result["instrument_id"],
            result["benchmark_id"],
            result["holding_sessions"],
        )
        old = selected.get(key)
        if old is not None and old["result_id"] != result["result_id"]:
            # Reassessment at another wall-clock instant is not a new economic
            # observation. Compare decision-relevant price and provenance, not
            # evaluation_boundary or generated result IDs.
            decisive = (
                "receipt_input_hash",
                "price_comparability",
                "calendar_dates",
                "calendar_version",
                "calendar_sources",
                "visible_bar_inputs",
                "entry_reference_time",
                "exit_reference_time",
                "entry_reference_price",
                "exit_reference_price",
                "gross_price_return",
                "benchmark_gross_price_return",
                "benchmark_relative_return",
                "maximum_adverse_excursion",
                "direction",
                "direction_correct",
                "lineage",
            )
            if any(old.get(field) != result.get(field) for field in decisive):
                raise QualityError("FEEDBACK_SAMPLE_CONFLICT")
            selected[key] = min((old, result), key=lambda item: item["result_id"])
        else:
            selected[key] = result
    samples = list(selected.values())
    count = len(samples)
    mean = (
        None
        if not count
        else str(
            sum((Decimal(x["benchmark_relative_return"]) for x in samples), Decimal("0")) / count
        )
    )
    return sealed(
        "outcome-experience",
        {
            "context_id": context_id,
            "policy_version": policy.policy_version,
            "policy_hash": digest(policy.model_dump(mode="json")),
            "sample_ids": [x["result_id"] for x in samples],
            "mature_sample_count": count,
            "direction_correct_count": sum(x["direction_correct"] for x in samples),
            "mean_benchmark_relative_price_return": mean,
            "production_weight_changes": False,
            "historical_decision_changes": False,
        },
    )
