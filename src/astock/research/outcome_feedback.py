"""Read-only recommendation reference performance; never execution accounting."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.market_data.official_calendar import OfficialTradingCalendarConfig
from astock.research.debate_evaluation import QualityError, QualityPolicy, aware, digest, sealed
from astock.schemas.full_research import RecommendationResearchReceipt
from astock.schemas.institutional_research import (
    DriverTree,
    ForecastEventBuildRequest,
    ForecastEventDefinition,
    ForecastEventProbability,
    ForecastPack,
)
from astock.schemas.market import Market
from astock.schemas.reference_data import DailyBarObservation

if TYPE_CHECKING:
    from astock.financial_sources.service import FinancialSourceService, VerifiedFinancialRelease

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
        "account_scope_hash": digest({"account_id": receipt.request_contract.account_id}),
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
    path = evaluate_reference_price_path(
        completed, instrument_id, benchmark_id, stock_bars, benchmark_bars, calendar, policy,
        evaluated_at=evaluated_at, holding_sessions=holding_sessions,
        corporate_action_dates=corporate_action_dates,
    )
    status = path.pop("status")
    if "price_comparability" in path:
        path["interpretation"] = (
            "建议参考价格表现，非实际成交盈亏；未计费用和分红。"
            if path["price_comparability"] == "VERIFIED_NO_ACTION"
            else "公司行动影响或覆盖状态未核实，不对价格方向判断正确性打分。"
        )
    return finish(status, **path)


def evaluate_reference_price_path(
    signal_at: datetime,
    instrument_id: str,
    benchmark_id: str,
    stock_bars: tuple[DailyBarObservation, ...],
    benchmark_bars: tuple[DailyBarObservation, ...],
    calendar: OfficialTradingCalendarConfig,
    policy: QualityPolicy,
    *,
    evaluated_at: datetime,
    holding_sessions: int,
    corporate_action_dates: set[date] | None = None,
) -> dict:
    """One dated, unadjusted reference path shared by research diagnostics.

    This calculates prices after a frozen signal, without creating a recommendation
    receipt, a fill, or an account position. Unknown actions never qualify an IC label.
    """
    completed, evaluated_at = aware(signal_at), aware(evaluated_at)
    instrument_id = normalized_instrument(instrument_id)
    benchmark_id = normalized_instrument(benchmark_id)
    if holding_sessions not in policy.holding_sessions:
        raise QualityError("INVALID_OUTCOME_REQUEST")
    if max(len(stock_bars), len(benchmark_bars)) > policy.max_bars:
        raise QualityError("INPUT_LIMIT")
    base: dict = {}

    def finish(status: str, **fields) -> dict:
        return {**base, "status": status, **fields}

    if completed > evaluated_at:
        return finish("SIGNAL_NOT_YET_AVAILABLE")
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
    observations = list(selected.values())
    # Rewording/reissuing a recommendation in the same security/session is not
    # another independent success. Horizons and benchmarks are separate analyses.
    analyses = {
        (x["benchmark_id"], x["holding_sessions"], x["performance_kind"])
        for x in observations
    }
    if len(analyses) > 1:
        raise QualityError("FEEDBACK_COMPARABILITY_MISMATCH")
    clusters = {}
    for result in observations:
        key = (
            result["instrument_id"], result.get("entry_reference_time"),
            result.get("account_scope_hash"),
        )
        current = clusters.get(key)
        if current is not None and any(
            current.get(field) != result.get(field)
            for field in (
                "entry_reference_price", "exit_reference_price", "gross_price_return",
                "benchmark_gross_price_return", "benchmark_relative_return",
                "maximum_adverse_excursion", "direction", "direction_correct",
            )
        ):
            raise QualityError("FEEDBACK_ECONOMIC_CLUSTER_CONFLICT")
        if current is None or result["result_id"] < current["result_id"]:
            clusters[key] = result
    samples = list(clusters.values())
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
            "economic_cluster_count": count,
            "distinct_receipt_observations": len(observations),
            "independence_assumed": False,
            "performance_kind": "RECOMMENDATION_REFERENCE_GROSS_UNADJUSTED_PRICE",
            "direction_correct_count": sum(x["direction_correct"] for x in samples),
            "mean_benchmark_relative_price_return": mean,
            "production_weight_changes": False,
            "historical_decision_changes": False,
        },
    )

def _realized_financial_metrics(release, company_id: str, period_end: date) -> dict:
    """Normalize verified annual statement amounts and ratios once."""
    from astock.financial_sources.certification import financial_amount_cny
    from astock.schemas.financial import FinancialFieldCode, FinancialPeriodType

    values = {}
    for fact in release.facts:
        if (
            fact.company_id != company_id or fact.period_end != period_end
            or fact.period_type is not FinancialPeriodType.ANNUAL
        ):
            raise QualityError("FORECAST_REALIZATION_SCOPE_MISMATCH")
        if fact.field_code not in {
            FinancialFieldCode.REVENUE, FinancialFieldCode.EBIT,
            FinancialFieldCode.OPERATING_COST, FinancialFieldCode.NET_PROFIT_INCOME,
        }:
            continue
        number = financial_amount_cny(fact.reported_value, fact.unit)
        if fact.field_code in values and values[fact.field_code][0] != number:
            raise QualityError("FORECAST_REALIZATION_FACT_CONFLICT")
        values[fact.field_code] = (number, fact)
    actuals = {}
    for name, field in (
        ("REVENUE", FinancialFieldCode.REVENUE),
        ("EBIT", FinancialFieldCode.EBIT),
        ("NET_INCOME", FinancialFieldCode.NET_PROFIT_INCOME),
    ):
        if field in values:
            actuals[name] = (values[field][0], [values[field][1].fact_id], "CNY")
    revenue = values.get(FinancialFieldCode.REVENUE)
    if revenue and revenue[0] != 0:
        for name, numerator, subtract in (
            ("OPERATING_MARGIN", FinancialFieldCode.EBIT, False),
            ("GROSS_MARGIN", FinancialFieldCode.OPERATING_COST, True),
        ):
            if numerator in values:
                ratio = values[numerator][0] / revenue[0]
                actuals[name] = (
                    1 - ratio if subtract else ratio,
                    [revenue[1].fact_id, values[numerator][1].fact_id], "RATIO",
                )
    return actuals


def _forecast_metric_predictions(
    forecast, *, metric: str, unit: str, declared_unit: str | None, period_end: date
) -> dict:
    """Select three comparable scenario values without guessing missing units."""
    from astock.financial_sources.certification import financial_amount_cny
    from astock.schemas.financial import FinancialUnit

    predictions = {}
    for scenario in forecast.scenarios:
        periods = [p for p in scenario.periods if p.period_end == period_end]
        if len(periods) != 1:
            continue
        period = periods[0]
        value = period.metrics.get(metric)
        if value is None and metric in {"REVENUE", "EBIT", "OPERATING_MARGIN"}:
            value = getattr(period, metric.lower(), None)
        if value is not None:
            if unit == "CNY":
                try:
                    value = financial_amount_cny(value, FinancialUnit(declared_unit))
                except (ValueError, TypeError):
                    continue
            elif declared_unit != "RATIO":
                continue
            predictions[scenario.scenario] = value
    return predictions


def evaluate_forecast_realization(
    forecast, tree, release, *, period_end: date, evaluated_at: datetime, context_id: str
) -> dict:
    """Score frozen financial forecasts against an already verified release.

    The public caller must obtain release through read_certified_release; raw
    vendor rows and caller-supplied 'certified' flags are not accepted by that path.
    Scenario ranges are scenarios, not calibrated confidence intervals.
    """
    from astock.schemas.financial import FinancialPeriodType
    from astock.schemas.financial_sources import FinancialStatementScope
    from astock.schemas.institutional_research import ForecastScenario

    evaluated_at = aware(evaluated_at)
    if not context_id or forecast.company_id != tree.company_id:
        raise QualityError("FORECAST_IDENTITY_MISMATCH")
    base = {
        "context_id": context_id, "forecast_id": forecast.forecast_id,
        "forecast_hash": digest(forecast.model_dump(mode="json")),
        "company_id": forecast.company_id, "period_end": period_end.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "performance_kind": "FROZEN_FINANCIAL_FORECAST_ACCURACY",
        "calibrated_probability": False, "production_weight_changes": False,
        "historical_decision_changes": False,
    }

    def finish(status, **values):
        return sealed("forecast-realization", {**base, "status": status, **values})

    if evaluated_at < forecast.as_of:
        raise QualityError("FORECAST_EVALUATION_PRECEDES_FORECAST")
    if forecast.as_of.date() >= period_end:
        return finish("NOT_EVALUABLE_RETROSPECTIVE_FORECAST")
    if evaluated_at.date() <= period_end or release is None:
        return finish("IMMATURE_OFFICIAL_RELEASE_PENDING")
    manifest = release.manifest
    if (
        manifest.company_id != forecast.company_id or manifest.period_end != period_end
        or manifest.period_type is not FinancialPeriodType.ANNUAL
        or manifest.available_to_system_at > evaluated_at
    ):
        raise QualityError("FORECAST_REALIZATION_SCOPE_MISMATCH")
    if manifest.available_to_system_at <= forecast.as_of:
        return finish("NOT_EVALUABLE_ALREADY_KNOWN_REALIZATION")
    if any(
        item.statement_scope is not FinancialStatementScope.CONSOLIDATED
        for item in release.observations
    ):
        return finish("NOT_EVALUABLE_FINANCIAL_SCOPE")
    actuals = _realized_financial_metrics(release, forecast.company_id, period_end)
    units = {node.node_id: node.unit.upper() for node in tree.draft.nodes}
    results = []
    for metric, (actual, fact_ids, unit) in actuals.items():
        declared_unit = units.get(tree.draft.output_bindings.get(metric, ""))
        if metric == "EBIT" and declared_unit is None:
            declared_unit = units.get(tree.draft.output_bindings.get("REVENUE", ""))
        predictions = _forecast_metric_predictions(
            forecast, metric=metric, unit=unit, declared_unit=declared_unit, period_end=period_end,
        )
        if set(predictions) != set(ForecastScenario):
            continue
        point = predictions[ForecastScenario.BASE]
        lower, upper = min(predictions.values()), max(predictions.values())
        results.append({
            "metric": metric, "unit": unit, "actual": str(actual),
            "base_forecast": str(point), "error": str(point - actual),
            "absolute_error": str(abs(point - actual)),
            "relative_error": str((point - actual) / abs(actual)) if actual else None,
            "scenario_low": str(lower), "scenario_high": str(upper),
            "inside_scenario_range": lower <= actual <= upper,
            "realized_fact_ids": sorted(fact_ids),
            "economic_cluster": digest({
                "company": forecast.company_id, "period": period_end.isoformat(), "metric": metric,
            }),
        })
    return finish(
        "MATURE" if results else "NOT_EVALUABLE_NO_COMPARABLE_METRIC",
        metrics=results, release_id=manifest.release_id,
        release_available_at=manifest.available_to_system_at.isoformat(),
        source_object_hash=digest(manifest.model_dump(mode="json")),
    )


def forecast_revision_impact(
    forecast, tree, *, changed_claim_ids, invalidated_evidence_ids
) -> dict:
    """Project premise changes onto the existing graph; do not rewrite old artifacts."""
    from astock.research.fundamental_analytics import affected_driver_nodes

    if forecast.company_id != tree.company_id:
        raise QualityError("FORECAST_IDENTITY_MISMATCH")
    claims, evidence = set(changed_claim_ids), set(invalidated_evidence_ids)
    affected = {}
    for scenario in forecast.scenarios:
        inputs = {
            item.node_id for item in scenario.driver_inputs
            if claims.intersection(item.claim_ids) or evidence.intersection(
                [*item.evidence_ids, *(
                    item.verification.counter_evidence_ids if item.verification is not None else []
                )]
            )
        }
        descendants = affected_driver_nodes(tree, inputs)
        if descendants:
            affected[scenario.scenario.value] = {
                "changed_inputs": sorted(inputs), "affected_nodes": descendants,
                "affected_metrics": sorted(
                    name for name, node in tree.draft.output_bindings.items() if node in descendants
                ),
            }
    return sealed("forecast-revision-impact", {
        "forecast_id": forecast.forecast_id,
        "forecast_hash": digest(forecast.model_dump(mode="json")),
        "changed_claim_ids": sorted(claims), "invalidated_evidence_ids": sorted(evidence),
        "affected_scenarios": affected, "downstream_recalculation_required": bool(affected),
        "historical_artifacts_mutated": False,
        "lifecycle_input": {
            "changed_claim_ids": sorted(claims), "invalidated_evidence_ids": sorted(evidence),
        },
    })


_EVENT_METRIC_UNITS = {
    "REVENUE": "CNY", "EBIT": "CNY", "NET_INCOME": "CNY",
    "OPERATING_MARGIN": "RATIO", "GROSS_MARGIN": "RATIO",
}


def forecast_event_identity(
    request: ForecastEventBuildRequest, company_id: str, forecast_hash: str, tree_hash: str,
) -> str:
    """Version the judgement, never substitute its eventual observation."""
    from astock.core.hashing import content_hash

    return "forecast-event:" + content_hash({
        "request": request.model_dump(mode="json"), "company_id": company_id,
        "forecast_object_hash": forecast_hash, "driver_tree_object_hash": tree_hash,
    })


def validate_forecast_event_contract(
    forecast: ForecastPack, tree: DriverTree, definition: ForecastEventDefinition,
) -> None:
    """Share the parent/period/unit contract between freeze and historical reads."""
    from astock.schemas.institutional_research import ForecastScenario, InstitutionalArtifactStatus

    if (
        forecast.company_id != tree.company_id or forecast.as_of != tree.as_of
        or forecast.status is not InstitutionalArtifactStatus.READY
        or any(
            sum(p.period_end == definition.period_end for p in scenario.periods) != 1
            for scenario in forecast.scenarios
        )
    ):
        raise QualityError("EVENT_FORECAST_BASIS_MISMATCH")
    metric = definition.metric or ""
    unit = _EVENT_METRIC_UNITS.get(metric)
    if definition.kind != "FINANCIAL_THRESHOLD" or unit is None:
        return
    if definition.unit != unit:
        raise QualityError("EVENT_METRIC_UNIT_MISMATCH")
    units = {node.node_id: node.unit.upper() for node in tree.draft.nodes}
    binding = tree.draft.output_bindings.get(metric, "")
    if definition.metric == "EBIT" and not binding:
        binding = tree.draft.output_bindings.get("REVENUE", "")
    predictions = _forecast_metric_predictions(
        forecast, metric=metric, unit=unit, declared_unit=units.get(binding),
        period_end=definition.period_end,
    )
    if set(predictions) != set(ForecastScenario) or any(
        not value.is_finite() for value in predictions.values()
    ):
        raise QualityError("EVENT_FORECAST_METRIC_UNBOUND")


def _read_event_artifact[
    EventSource: (ForecastEventProbability, ForecastPack, DriverTree),
](
    state: StateStore, objects: ObjectStore, identity: str,
    model: type[EventSource], max_bytes: int,
) -> tuple[EventSource, dict[str, Any]]:
    from astock.core.artifact_reading import decode_registered_artifact

    record = state.artifact_record(identity)
    if record is None or record["type"] != model.__name__:
        raise QualityError("EVENT_REGISTERED_SOURCE_MISSING")
    try:
        if objects.path_for(record["object_hash"]).stat().st_size > max_bytes:
            raise QualityError("EVENT_SOURCE_BYTES_INVALID")
        raw = objects.get_bytes(record["object_hash"])
    except (OSError, StorageError):
        raise QualityError("EVENT_SOURCE_BYTES_UNAVAILABLE") from None
    if len(raw) > max_bytes:
        raise QualityError("EVENT_SOURCE_BYTES_INVALID")
    value = decode_registered_artifact(raw, model, registry_schema_version=record["schema_version"])
    if sorted(record.get("input_hashes") or ()) != value.source_object_hashes:
        raise QualityError("EVENT_SOURCE_INPUT_HASH_DRIFT")
    return value, record


def load_forecast_event_basis(
    state: StateStore, objects: ObjectStore, forecast_id: str, *, max_bytes: int = 16777216,
) -> tuple[ForecastPack, DriverTree, str, str]:
    """Load the exact registered forecast and its driver graph without network or writes."""
    forecast, forecast_record = _read_event_artifact(
        state, objects, forecast_id, ForecastPack, max_bytes,
    )
    tree, tree_record = _read_event_artifact(
        state, objects, forecast.driver_tree_artifact_id, DriverTree, max_bytes,
    )
    tree_hash = tree_record["object_hash"]
    if (
        forecast_id != f"ForecastPack:{forecast.forecast_id}"
        or forecast.driver_tree_artifact_id != f"DriverTree:{tree.tree_id}"
        or forecast.driver_tree_object_hash != tree_hash
        or forecast.driver_tree_artifact_id not in forecast.source_artifact_ids
        or tree_hash not in forecast.source_object_hashes
    ):
        raise QualityError("EVENT_REGISTERED_SOURCE_BINDING_DRIFT")
    return forecast, tree, forecast_record["object_hash"], tree_hash


def load_forecast_event(
    state: StateStore, objects: ObjectStore, artifact_id: str, *, max_bytes: int = 16777216,
) -> tuple[ForecastEventProbability, dict[str, Any]]:
    """Read immutable registered predictions, including superseded historical parents."""
    event, record = _read_event_artifact(
        state, objects, artifact_id, ForecastEventProbability, max_bytes,
    )
    forecast, tree, forecast_hash, tree_hash = load_forecast_event_basis(
        state, objects, event.forecast_artifact_id, max_bytes=max_bytes,
    )
    request = ForecastEventBuildRequest(
        forecast_artifact_id=event.forecast_artifact_id, definition=event.definition,
        probability=event.probability, probability_basis=event.probability_basis,
        source_group=event.source_group,
    )
    identity = forecast_event_identity(request, forecast.company_id, forecast_hash, tree_hash)
    if (
        artifact_id != f"ForecastEventProbability:{identity}" or event.event_id != identity
        or event.company_id != forecast.company_id
        or event.forecast_object_hash != forecast_hash or event.driver_tree_object_hash != tree_hash
        or forecast.driver_tree_artifact_id != event.driver_tree_artifact_id
    ):
        raise QualityError("EVENT_REGISTERED_SOURCE_BINDING_DRIFT")
    if (
        forecast.as_of > event.frozen_at
        or event.definition.period_end <= event.frozen_at.astimezone(SHANGHAI).date()
    ):
        raise QualityError("EVENT_NOT_FROZEN_BEFORE_PERIOD_END")
    validate_forecast_event_contract(forecast, tree, event.definition)
    return event, {
        "artifact_id": artifact_id, "object_hash": record["object_hash"],
        "forecast_artifact_id": event.forecast_artifact_id,
        "forecast_object_hash": event.forecast_object_hash,
        "driver_tree_artifact_id": event.driver_tree_artifact_id,
        "driver_tree_object_hash": event.driver_tree_object_hash,
    }


def _event_economic_keys(event: ForecastEventProbability) -> tuple[str, str]:
    definition = event.definition
    cluster = {
        "company_id": event.company_id, "market": definition.market.value,
        "period_end": definition.period_end.isoformat(),
    }
    predicate = {
        **cluster, "kind": definition.kind, "metric": definition.metric,
        "comparison": definition.comparison, "unit": definition.unit,
        "threshold": (
            str(definition.threshold.normalize()) if definition.threshold is not None else None
        ),
    }
    return digest(predicate), digest(cluster)


def _financial_event_result(
    event: ForecastEventProbability, release: VerifiedFinancialRelease | None, state: StateStore, *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """The only labels here are calculated from the canonical certified reader."""
    from astock.schemas.financial import FinancialPeriodType
    from astock.schemas.financial_sources import FinancialStatementScope

    definition = event.definition
    if definition.kind != "FINANCIAL_THRESHOLD":
        return {"status": "NOT_EVALUABLE_EVENT_KIND"}
    if definition.metric not in _EVENT_METRIC_UNITS:
        return {"status": "NOT_EVALUABLE_METRIC"}
    if evaluated_at.astimezone(SHANGHAI).date() <= definition.period_end or release is None:
        return {"status": "IMMATURE_OFFICIAL_RELEASE_PENDING"}
    manifest = release.manifest
    if (
        manifest.company_id != event.company_id or manifest.market is not definition.market
        or manifest.period_end != definition.period_end
        or manifest.period_type is not FinancialPeriodType.ANNUAL
        or manifest.available_to_system_at > evaluated_at
    ):
        raise QualityError("EVENT_REALIZATION_IDENTITY_MISMATCH")
    if manifest.available_to_system_at <= event.frozen_at:
        return {"status": "NOT_EVALUABLE_ALREADY_KNOWN_REALIZATION"}
    if not release.observations or any(
        x.statement_scope is not FinancialStatementScope.CONSOLIDATED for x in release.observations
    ):
        return {"status": "NOT_EVALUABLE_FINANCIAL_SCOPE"}
    actuals = _realized_financial_metrics(release, event.company_id, definition.period_end)
    if definition.metric not in actuals:
        return {"status": "NOT_EVALUABLE_METRIC_ABSENT"}
    actual, facts, unit = actuals[definition.metric]
    if (
        definition.unit != unit or definition.threshold is None
        or definition.comparison is None or not actual.is_finite()
    ):
        raise QualityError("EVENT_REALIZATION_UNIT_MISMATCH")
    truth = {
        "GT": actual > definition.threshold, "GE": actual >= definition.threshold,
        "LT": actual < definition.threshold, "LE": actual <= definition.threshold,
    }[definition.comparison]
    identity = f"financial-source:{manifest.release_id}"
    record = state.artifact_record(identity)
    if record is None:
        raise QualityError("EVENT_REALIZATION_REGISTRY_MISSING")
    return {
        "status": "MATURE", "actual": str(actual), "unit": unit, "occurred": truth,
        "brier_score": str((event.probability - Decimal(int(truth))) ** 2),
        "realized_fact_ids": sorted(facts), "release_artifact_id": identity,
        "release_object_hash": record["object_hash"],
        "release_available_at": manifest.available_to_system_at.isoformat(),
    }


def _select_event_versions(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """The earliest frozen version is fixed before inspecting its realized label."""
    groups: dict[str, list[dict]] = {}
    for result in results:
        groups.setdefault(result["economic_event_key"], []).append(result)
    selected, exclusions = [], []
    for key, versions in sorted(groups.items()):
        versions.sort(key=lambda item: (item["frozen_at"], item["event_artifact_id"]))
        earliest = versions[0]["frozen_at"]
        first = [x for x in versions if x["frozen_at"] == earliest]
        if len({Decimal(x["probability"]) for x in first}) != 1:
            exclusions.append({
                "economic_event_key": key, "code": "SIMULTANEOUS_PROBABILITY_CONFLICT",
                "event_artifact_ids": [x["event_artifact_id"] for x in versions],
            })
            continue
        selected.append(first[0])
        if len(versions) > 1:
            exclusions.append({
                "economic_event_key": key, "code": "LATER_OR_DUPLICATE_JUDGEMENT",
                "event_artifact_ids": [x["event_artifact_id"] for x in versions[1:]],
            })
    return selected, exclusions


def _event_reliability(results: list[dict], *, bin_count: int) -> dict:
    mature = [x for x in results if x["status"] == "MATURE"]
    clusters: dict[str, int] = {}
    for row in mature:
        key = row["economic_cluster"]
        clusters[key] = clusters.get(key, 0) + 1
    bins: list[list[tuple[dict, Decimal]]] = [[] for _ in range(bin_count)]
    brier = Decimal(0)
    for row in mature:
        probability = Decimal(row["probability"])
        weight = Decimal(1) / clusters[row["economic_cluster"]]
        brier += weight * Decimal(row["brier_score"])
        index = min(int(probability * bin_count), bin_count - 1)
        bins[index].append((row, weight))
    reliability, error = [], Decimal(0)
    for index, rows in enumerate(bins):
        mass = sum((weight for _, weight in rows), Decimal(0))
        mean = sum((Decimal(row["probability"]) * w for row, w in rows), Decimal(0))
        rate = sum((Decimal(int(row["occurred"])) * w for row, w in rows), Decimal(0))
        if mass:
            mean, rate = mean / mass, rate / mass
            error += mass * abs(mean - rate)
        reliability.append({
            "lower": str(Decimal(index) / bin_count),
            "upper": str(Decimal(index + 1) / bin_count), "upper_inclusive": index == bin_count - 1,
            "event_count": len(rows),
            "economic_cluster_count": len({row["economic_cluster"] for row, _ in rows}),
            "cluster_weight": str(mass),
            "mean_probability": str(mean) if mass else None,
            "observed_frequency": str(rate) if mass else None,
        })
    count = len(clusters)
    return {
        "status": "CALIBRATION_DIAGNOSTIC" if count >= 30 else "INSUFFICIENT_ECONOMIC_CLUSTERS",
        "mature_event_count": len(mature), "economic_cluster_count": count,
        "minimum_diagnostic_clusters": 30, "calibrated_probability": False,
        "independence_assumed": False, "weighting": "EQUAL_COMPANY_PERIOD_CLUSTER",
        "mean_brier_score": str(brier / count) if count else None,
        "binned_absolute_calibration_error": str(error / count) if count else None,
        "reliability_bins": reliability,
    }


def evaluate_forecast_events(
    state: StateStore, objects: ObjectStore, source: FinancialSourceService,
    event_artifact_ids: tuple[str, ...], policy: QualityPolicy, *,
    evaluated_at: datetime, context_id: str, bin_count: int = 5,
) -> dict:
    """Read-only labels and reliability diagnostics; never accept caller truth."""
    from astock.schemas.financial import FinancialPeriodType

    evaluated_at = aware(evaluated_at)
    if (
        not context_id or not event_artifact_ids
        or len(event_artifact_ids) > policy.max_feedback_samples or not 2 <= bin_count <= 20
    ):
        raise QualityError("INPUT_LIMIT")
    loaded = [
        load_forecast_event(state, objects, identity, max_bytes=policy.max_artifact_bytes)
        for identity in sorted(set(event_artifact_ids))
    ]
    cache: dict[tuple[str, date], VerifiedFinancialRelease | None] = {}
    results: list[dict[str, Any]] = []
    for event, lineage in loaded:
        if event.frozen_at > evaluated_at:
            raise QualityError("EVENT_EVALUATION_PRECEDES_FREEZE")
        definition = event.definition
        key = (event.company_id, definition.period_end)
        supported = (
            definition.kind == "FINANCIAL_THRESHOLD" and definition.metric in _EVENT_METRIC_UNITS
        )
        if supported and evaluated_at.astimezone(SHANGHAI).date() > definition.period_end:
            if key not in cache:
                cache[key] = source.read_certified_release(
                    event.company_id, definition.period_end, FinancialPeriodType.ANNUAL,
                    as_of=evaluated_at,
                )
        event_key, cluster = _event_economic_keys(event)
        results.append({
            "event_artifact_id": lineage["artifact_id"],
            "event_object_hash": lineage["object_hash"],
            "company_id": event.company_id, "definition": definition.model_dump(mode="json"),
            "frozen_at": event.frozen_at.isoformat(), "probability": str(event.probability),
            "probability_basis": event.probability_basis, "source_group": event.source_group,
            "economic_event_key": event_key, "economic_cluster": cluster, "lineage": lineage,
            **_financial_event_result(event, cache.get(key), state, evaluated_at=evaluated_at),
        })
    selected, exclusions = _select_event_versions(results)
    return sealed("forecast-event-outcomes", {
        "context_id": context_id, "evaluated_at": evaluated_at.isoformat(),
        "policy_version": policy.policy_version,
        "policy_hash": digest(policy.model_dump(mode="json")),
        "input_count": len(event_artifact_ids), "distinct_prediction_count": len(results),
        "selected_economic_event_count": len(selected), "deduplication": exclusions,
        "selection_policy": "EARLIEST_FROZEN_JUDGEMENT_PER_ECONOMIC_EVENT",
        "events": results, "diagnostic": _event_reliability(selected, bin_count=bin_count),
        "persistent_writes": False, "production_weight_changes": False,
        "publication_authority_changes": False, "historical_decision_changes": False,
    })
