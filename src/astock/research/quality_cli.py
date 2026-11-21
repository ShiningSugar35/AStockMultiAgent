"""Lazy research projections and explicit premise invalidation on canonical stores."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import typer
from pydantic import AwareDatetime, Field

from astock.core.artifact_reading import decode_registered_artifact
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.official_calendar import OfficialTradingCalendarConfig
from astock.research.debate_evaluation import (
    CrossResponse,
    DataModel,
    DebateProposal,
    QualityError,
    QualityPolicy,
    compile_debate,
    load_quality_policy,
)
from astock.research.outcome_feedback import evaluate_outcome, published
from astock.research.session_brief import build_session_brief, render_session_brief
from astock.schemas.full_research import (
    CurrentSourceSnapshot,
    NewsEventResearchPack,
    RecommendationResearchReceipt,
)
from astock.schemas.institutional_research import ForecastEventBuildRequest
from astock.schemas.research_team import IndependentBearCase, IndependentBullCase
from astock.schemas.technical import TechnicalIndicatorSnapshot

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class QualityServices:
    state: StateStore
    objects: ObjectStore
    views: Any
    calendar: OfficialTradingCalendarConfig
    policy: QualityPolicy
    parquet_root: Path | None = None


class DebateRequest(DataModel):
    bull_artifact_id: str = Field(min_length=1)
    bear_artifact_id: str = Field(min_length=1)
    evidence_artifact_id: str = Field(min_length=1)
    proposal: DebateProposal
    responses: tuple[CrossResponse, ...] = ()


class OutcomeRequest(DataModel):
    receipt_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    benchmark_id: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    holding_sessions: int
    context_id: str = Field(min_length=1)


class ForecastOutcomeRequest(DataModel):
    forecast_artifact_id: str = Field(min_length=1)
    period_end: date
    evaluated_at: AwareDatetime
    context_id: str = Field(min_length=1)


class ForecastEventOutcomeRequest(DataModel):
    event_artifact_ids: tuple[str, ...] = Field(min_length=1)
    evaluated_at: AwareDatetime
    context_id: str = Field(min_length=1)
    bin_count: int = Field(default=5, ge=2, le=20, strict=True)


class ForecastRevisionRequest(DataModel):
    forecast_artifact_id: str = Field(min_length=1)
    changed_claim_ids: tuple[str, ...] = ()
    invalidated_evidence_ids: tuple[str, ...] = ()


class ForecastInvalidationRequest(ForecastRevisionRequest):
    invalidated_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=1000)


class BriefRequest(DataModel):
    instrument_id: str = Field(min_length=1)
    account_id: str | None = None
    context_id: str = Field(min_length=1)
    now: AwareDatetime
    receipt_artifact_id: str | None = None
    previous_receipt_artifact_id: str | None = None
    technical_artifact_id: str | None = None
    events_artifact_id: str | None = None
    previous_fingerprint: str | None = None


def read_registered(context: QualityServices, artifact_id: str, model: Any) -> tuple[Any, dict]:
    record = context.state.artifact_record(artifact_id)
    if record is None or record["type"] != model.__name__:
        raise QualityError("REGISTERED_ARTIFACT_TYPE_MISMATCH")
    raw = context.objects.get_bytes(record["object_hash"])
    if len(raw) > context.policy.max_artifact_bytes:
        raise QualityError("INPUT_LIMIT")
    value = decode_registered_artifact(raw, model, registry_schema_version=record["schema_version"])
    return value, {
        "artifact_id": artifact_id,
        "object_hash": record["object_hash"],
        "schema_version": record["schema_version"],
        "input_hashes": tuple(record.get("input_hashes") or ()),
    }


def _evaluate_debate(request: DebateRequest, context: QualityServices) -> dict:
    bull, bull_ref = read_registered(context, request.bull_artifact_id, IndependentBullCase)
    bear, bear_ref = read_registered(context, request.bear_artifact_id, IndependentBearCase)
    evidence, evidence_ref = read_registered(
        context, request.evidence_artifact_id, CurrentSourceSnapshot
    )
    # Research-role outputs must both depend on this *exact registered*
    # evidence snapshot. Matching dates or proposal citations alone cannot
    # establish that a snapshot belongs to the same company/request.
    source_hash = evidence_ref["object_hash"]
    if any(source_hash not in role["input_hashes"] for role in (bull_ref, bear_ref)):
        raise QualityError("RESEARCH_EVIDENCE_LINEAGE_MISMATCH")
    result = compile_debate(
        bull,
        bear,
        evidence,
        request.proposal,
        context.policy,
        responses=request.responses,
    )
    return {
        "result": result,
        "registered_inputs": [bull_ref, bear_ref, evidence_ref],
        "persistent_writes": False,
    }

def _evaluate_outcome(request: OutcomeRequest, context: QualityServices) -> dict:
    from astock.investor_orchestration.canonical_state import normalized_instrument
    from astock.research.reference_outcomes import load_reference_price_inputs

    receipt, receipt_ref = read_registered(
        context, request.receipt_artifact_id, RecommendationResearchReceipt
    )
    stock, benchmark, action_dates = (), (), None
    lineage: dict[str, Any] = {"daily_inputs": []}
    if published(receipt):
        stock, benchmark, action_dates, lineage = load_reference_price_inputs(
            context.state, context.objects, context.parquet_root,
            instrument_id=normalized_instrument(request.instrument_id),
            benchmark_id=normalized_instrument(request.benchmark_id),
            signal_at=receipt.as_of, evaluated_at=request.evaluated_at,
            max_bars=context.policy.max_bars,
        )
    lineage["receipt"] = receipt_ref
    return evaluate_outcome(
        receipt, request.instrument_id, request.benchmark_id, stock, benchmark,
        context.calendar, context.policy, evaluated_at=request.evaluated_at,
        holding_sessions=request.holding_sessions, context_id=request.context_id,
        lineage=lineage, corporate_action_dates=action_dates,
    )

def _build_brief(request: BriefRequest, context: QualityServices) -> dict:
    lineage: dict[str, Any] = {}

    def optional(identity: str | None, model: Any, key: str) -> Any:
        if identity is None:
            return None
        value, binding = read_registered(context, identity, model)
        lineage[key] = binding
        return value

    current_receipt = optional(
        request.receipt_artifact_id, RecommendationResearchReceipt, "receipt"
    )
    draft = build_session_brief(
        instrument_id=request.instrument_id,
        account_id=request.account_id,
        context_id=request.context_id,
        now=request.now,
        calendar=context.calendar,
        policy=context.policy,
        receipt=current_receipt,
        previous_receipt=optional(
            request.previous_receipt_artifact_id,
            RecommendationResearchReceipt,
            "previous_receipt",
        ),
        technical=optional(
            request.technical_artifact_id, TechnicalIndicatorSnapshot, "technical"
        ),
        events=optional(request.events_artifact_id, NewsEventResearchPack, "events"),
        previous_fingerprint=request.previous_fingerprint,
        lineage=lineage,
    )
    return draft

def _render_public_brief(request: BriefRequest, context: QualityServices) -> dict:
    # Produce the draft from registered sources, then pass that exact draft
    # and its signed publication receipt through the existing gateway.
    if request.receipt_artifact_id is None:
        raise QualityError("PUBLICATION_NOT_ADMITTED")
    draft = _build_brief(request, context)
    receipt, _ = read_registered(
        context, request.receipt_artifact_id, RecommendationResearchReceipt
    )
    result = render_session_brief(
        draft, receipt, instrument_id=request.instrument_id
    )
    if result["public_approved"] is not True:
        raise QualityError("PUBLICATION_PRESENTATION_AUDIT_FAILED")
    return {
        **result,
        "source_facts_hash": draft["semantic_fingerprint"],
        "lineage": draft["lineage"],
        "scheduler_created": False,
        "paper_ledger_write_allowed": False,
    }

def _forecast_inputs(request, context):
    from astock.schemas.institutional_research import DriverTree, ForecastPack

    forecast, _ = read_registered(context, request.forecast_artifact_id, ForecastPack)
    tree, binding = read_registered(context, forecast.driver_tree_artifact_id, DriverTree)
    if binding["object_hash"] != forecast.driver_tree_object_hash:
        raise QualityError("FORECAST_DRIVER_LINEAGE_DRIFTED")
    return forecast, tree

def _forecast_outcome(request, context):
    from astock.financial_sources.service import FinancialSourceService
    from astock.financial_sources.storage import FinancialSourceParquetStore
    from astock.research.outcome_feedback import evaluate_forecast_realization
    from astock.schemas.financial import FinancialPeriodType

    if context.parquet_root is None:
        raise QualityError("CANONICAL_FINANCIAL_STORAGE_REQUIRED")
    forecast, tree = _forecast_inputs(request, context)
    source = FinancialSourceService(
        context.state, context.objects,
        FinancialSourceParquetStore(context.parquet_root / "financial_sources"), ROOT,
    )
    release = source.read_certified_release(
        forecast.company_id, request.period_end, FinancialPeriodType.ANNUAL,
        as_of=request.evaluated_at,
    )
    return evaluate_forecast_realization(
        forecast, tree, release, period_end=request.period_end,
        evaluated_at=request.evaluated_at, context_id=request.context_id,
    )

def _forecast_revision(request, context):
    from astock.research.outcome_feedback import forecast_revision_impact

    forecast, tree = _forecast_inputs(request, context)
    return forecast_revision_impact(
        forecast, tree, changed_claim_ids=request.changed_claim_ids,
        invalidated_evidence_ids=request.invalidated_evidence_ids,
    )

def _forecast_invalidate(request, context):
    from astock.research.institutional import InstitutionalResearchService

    return InstitutionalResearchService(context.state, context.objects).invalidate_forecast(
        request.forecast_artifact_id, changed_claim_ids=list(request.changed_claim_ids),
        invalidated_evidence_ids=list(request.invalidated_evidence_ids),
        invalidated_at=request.invalidated_at, reason=request.reason,
    )


def _event_freeze(request: ForecastEventBuildRequest, context: QualityServices) -> dict:
    from astock.research.institutional import InstitutionalResearchService

    event = InstitutionalResearchService(context.state, context.objects).freeze_forecast_event(
        request,
    )
    identity = f"ForecastEventProbability:{event.event_id}"
    record = context.state.artifact_record(identity)
    if record is None:
        raise QualityError("EVENT_FREEZE_REGISTRATION_FAILED")
    return {
        "status": "FROZEN", "artifact_id": identity, "object_hash": record["object_hash"],
        "event": event.model_dump(mode="json"), "publication_authority_changes": False,
    }


def _event_outcomes(request: ForecastEventOutcomeRequest, context: QualityServices) -> dict:
    from astock.financial_sources.service import FinancialSourceService
    from astock.financial_sources.storage import FinancialSourceParquetStore
    from astock.research.outcome_feedback import evaluate_forecast_events

    if context.parquet_root is None:
        raise QualityError("CANONICAL_FINANCIAL_STORAGE_REQUIRED")
    source = FinancialSourceService(
        context.state, context.objects,
        FinancialSourceParquetStore(context.parquet_root / "financial_sources"), ROOT,
    )
    return evaluate_forecast_events(
        context.state, context.objects, source, request.event_artifact_ids, context.policy,
        evaluated_at=request.evaluated_at, context_id=request.context_id,
        bin_count=request.bin_count,
    )


def register_commands(app: Any, services: Any, emit: Callable[[dict], None]) -> None:
    """Root CLI passes its existing factory; isolated tests can supply concrete stores."""

    def context_for(policy: QualityPolicy, *, read_only: bool = False) -> QualityServices:
        if isinstance(services, QualityServices):
            return services
        from astock.market_data.official_calendar import load_official_trading_calendar

        if read_only:
            from astock.settings import ProjectPaths

            paths = ProjectPaths.discover()
            state = StateStore(paths.state_db, paths.root / "migrations", read_only=True)
            objects = ObjectStore(paths.objects)
        else:
            paths, state, objects = services()
        return QualityServices(
            state,
            objects,
            None,
            load_official_trading_calendar(paths.root / "configs/official_trading_calendar.yaml"),
            policy,
            paths.parquet,
        )

    def run(
        argument: str, model: Any, operation: Any, *, read_only: bool = False,
    ) -> None:
        try:
            policy = (
                services.policy
                if isinstance(services, QualityServices)
                else load_quality_policy(ROOT / "configs/research_quality.yaml")
            )
            if argument.lstrip().startswith("{"):
                raw = argument.encode("utf-8")
            else:
                with Path(argument).open("rb") as handle:
                    raw = handle.read(policy.max_request_bytes + 1)
            if len(raw) > policy.max_request_bytes:
                raise QualityError("INPUT_LIMIT")
            request = model.model_validate_json(raw)
            result = operation(request, context_for(policy, read_only=read_only))
        except QualityError as exc:
            emit({"status": "ERROR", "code": exc.code})
            raise typer.Exit(2) from None
        except Exception:
            emit({"status": "ERROR", "code": "QUALITY_INPUT_OR_CANONICAL_READ_FAILED"})
            raise typer.Exit(2) from None
        emit(result)

    @app.command("research-event-freeze")
    def event_freeze_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, ForecastEventBuildRequest, _event_freeze)

    @app.command("research-event-outcomes")
    def event_outcomes_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, ForecastEventOutcomeRequest, _event_outcomes, read_only=True)

    @app.command("research-forecast-invalidate")
    def forecast_invalidate_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, ForecastInvalidationRequest, _forecast_invalidate)

    @app.command("research-forecast-outcome")
    def forecast_outcome_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, ForecastOutcomeRequest, _forecast_outcome)

    @app.command("research-forecast-revision-impact")
    def forecast_revision_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, ForecastRevisionRequest, _forecast_revision)

    @app.command("research-session-brief-public")
    def public_brief_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, BriefRequest, _render_public_brief)

    @app.command("research-debate-evaluate")
    def debate_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, DebateRequest, _evaluate_debate)

    @app.command("research-outcome-feedback")
    def outcome_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, OutcomeRequest, _evaluate_outcome)

    @app.command("research-session-brief")
    def brief_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, BriefRequest, _build_brief)
