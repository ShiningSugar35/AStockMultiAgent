"""Lazy, read-only research projections on existing canonical stores; no scheduler."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
from astock.schemas.reference_data import (
    CorporateActionObservation,
    CorporateActionStatus,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
)
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


def _verified_action_dates(
    context: QualityServices,
    instrument_id: str,
    *,
    start: date,
    end: date,
    as_of: datetime,
) -> tuple[set[date] | None, dict | None]:
    """Only a COMPLETE official-terms release covering the whole span proves absence.

    Current sync_corporate_actions() deliberately publishes complete=False.
    Thus today's hint/exact-item releases can identify individual actions but
    cannot certify a zero-action period, regardless of record count or recency.
    """
    import pyarrow.parquet as pq

    from astock.investor_orchestration.regime_reference_views import (
        CanonicalRegimeReferenceViews,
    )

    row = context.state.get_market_reference_release(
        ReferenceDatasetKind.CORPORATE_ACTION.value,
        instrument_id,
        as_of=as_of,
    )
    if row is None or row.get("coverage_status") != ReferenceCoverageStatus.COMPLETE.value:
        return None, None
    coverage = json.loads(row["coverage_json"])
    if (
        not isinstance(coverage, dict)
        or coverage.get("reason_codes")
        or not isinstance(coverage.get("record_count"), int)
        or not 0 < coverage["record_count"] <= context.policy.max_bars
    ):
        return None, None
    try:
        first = date.fromisoformat(coverage["requested_start"])
        last = date.fromisoformat(coverage["requested_end"])
    except (TypeError, ValueError, KeyError):
        return None, None
    if first > start or last < end:
        return None, None

    view = CanonicalRegimeReferenceViews(context.state, context.objects, context.parquet_root)
    release = view._release_row(str(row["release_id"]))
    manifest = view.reference._verified_manifest(release)
    if (
        manifest.dataset_kind is not ReferenceDatasetKind.CORPORATE_ACTION
        or manifest.scope_key != instrument_id
        or manifest.available_to_system_at > as_of
        or manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE
        or manifest.coverage.requested_start != first
        or manifest.coverage.requested_end != last
        or manifest.coverage.record_count != coverage["record_count"]
    ):
        raise QualityError("CORPORATE_ACTION_COVERAGE_MISMATCH")

    events: set[date] = set()
    seen: set[str] = set()
    for descriptor in manifest.canonical_files:
        path = (view.parquet.root / descriptor.path).resolve()
        if not path.is_relative_to(view.parquet.root):
            raise QualityError("CORPORATE_ACTION_FILE_OUTSIDE_ROOT")
        for batch in pq.ParquetFile(path).iter_batches(batch_size=256, columns=["record_json"]):
            for raw in batch.column(0).to_pylist():
                payload = json.loads(raw)
                payload.setdefault("created_at", manifest.available_to_system_at)
                item = CorporateActionObservation.model_validate(payload)
                if (
                    item.instrument_id != instrument_id
                    or item.observation_id in seen
                    or item.status is not CorporateActionStatus.TERMS_VERIFIED
                    or item.source_snapshot_id not in manifest.raw_snapshot_ids
                    or item.available_to_system_at > manifest.available_to_system_at
                    or item.ex_date is None
                ):
                    raise QualityError("CORPORATE_ACTION_TERMS_OR_IDENTITY_UNVERIFIED")
                seen.add(item.observation_id)
                if start <= item.ex_date <= end:
                    events.add(item.ex_date)
    if len(seen) != manifest.coverage.record_count:
        raise QualityError("CORPORATE_ACTION_RELEASE_COUNT_MISMATCH")
    return events, {
        "release_id": manifest.release_id,
        "artifact_id": str(release["manifest_artifact_id"]),
        "object_hash": str(release["manifest_object_hash"]),
        "coverage_start": first.isoformat(),
        "coverage_end": last.isoformat(),
        "official_terms_verified": True,
    }


def register_commands(app: Any, services: Any, emit: Callable[[dict], None]) -> None:
    """Root CLI passes its existing factory; isolated tests can supply concrete stores."""

    def context_for(policy: QualityPolicy) -> QualityServices:
        if isinstance(services, QualityServices):
            return services
        from astock.market_data.official_calendar import load_official_trading_calendar

        paths, state, objects = services()
        return QualityServices(
            state,
            objects,
            None,
            load_official_trading_calendar(paths.root / "configs/official_trading_calendar.yaml"),
            policy,
            paths.parquet,
        )

    def run(argument: str, model: Any, operation: Any) -> None:
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
            result = operation(request, context_for(policy))
        except QualityError as exc:
            emit({"status": "ERROR", "code": exc.code})
            raise typer.Exit(2) from None
        except Exception:
            emit({"status": "ERROR", "code": "QUALITY_INPUT_OR_CANONICAL_READ_FAILED"})
            raise typer.Exit(2) from None
        emit(result)

    def debate(request: DebateRequest, context: QualityServices) -> dict:
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

    def outcome(request: OutcomeRequest, context: QualityServices) -> dict:
        from astock.investor_orchestration.canonical_state import normalized_instrument
        from astock.technical.registered import load_daily_release

        receipt, receipt_ref = read_registered(
            context, request.receipt_artifact_id, RecommendationResearchReceipt
        )
        lineage: dict[str, Any] = {"receipt": receipt_ref, "daily_inputs": []}

        def bars(identity: str) -> tuple:
            if not published(receipt):
                return ()
            row = context.state.get_market_reference_release(
                ReferenceDatasetKind.DAILY_UNADJUSTED.value,
                normalized_instrument(identity),
                as_of=request.evaluated_at,
            )
            if row is None:
                return ()
            manifest, values, artifact_id, object_hash = load_daily_release(
                context.state,
                context.objects,
                row["release_id"],
                limit=context.policy.max_bars,
                parquet_root=context.parquet_root,
                max_release_rows=context.policy.max_bars,
            )
            if manifest.available_to_system_at > request.evaluated_at:
                raise QualityError("CANONICAL_VISIBILITY_MISMATCH")
            lineage["daily_inputs"].append({"artifact_id": artifact_id, "object_hash": object_hash})
            return values

        # Only a fully verified, exhaustive corporate-action release can make
        # a directional correctness label admissible. Existing action-sync
        # releases are explicitly PARTIAL, so default to descriptive prices.
        action_dates = None
        if published(receipt):
            shanghai = ZoneInfo("Asia/Shanghai")
            first_day = receipt.as_of.astimezone(shanghai).date()
            last_day = request.evaluated_at.astimezone(shanghai).date()
            if first_day <= last_day:
                action_dates, action_proof = _verified_action_dates(
                    context,
                    normalized_instrument(request.instrument_id),
                    start=first_day,
                    end=last_day,
                    as_of=request.evaluated_at,
                )
                if action_proof is not None:
                    lineage["corporate_actions"] = action_proof
        return evaluate_outcome(
            receipt,
            request.instrument_id,
            request.benchmark_id,
            bars(request.instrument_id),
            bars(request.benchmark_id),
            context.calendar,
            context.policy,
            evaluated_at=request.evaluated_at,
            holding_sessions=request.holding_sessions,
            context_id=request.context_id,
            lineage=lineage,
            corporate_action_dates=action_dates,
        )

    def brief(request: BriefRequest, context: QualityServices) -> dict:
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

    def public_brief(request: BriefRequest, context: QualityServices) -> dict:
        # Produce the draft from registered sources, then pass that exact draft
        # and its signed publication receipt through the existing gateway.
        if request.receipt_artifact_id is None:
            raise QualityError("PUBLICATION_NOT_ADMITTED")
        draft = brief(request, context)
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

    @app.command("research-session-brief-public")
    def public_brief_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, BriefRequest, public_brief)

    @app.command("research-debate-evaluate")
    def debate_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, DebateRequest, debate)

    @app.command("research-outcome-feedback")
    def outcome_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, OutcomeRequest, outcome)

    @app.command("research-session-brief")
    def brief_command(request_json: str = typer.Argument(...)) -> None:
        run(request_json, BriefRequest, brief)
