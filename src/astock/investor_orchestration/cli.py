from __future__ import annotations

# ruff: noqa: B008
import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
import yaml

from astock.investor_orchestration.capabilities import CapabilityPlanner
from astock.investor_orchestration.macro import OfficialMacroCaptureService
from astock.investor_orchestration.models import (
    DatePrecision,
    InvestorRequestEnvelope,
    MarketRegimeFeatureSnapshot,
    PortfolioLane,
    RequestIntent,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
    SideEffectClass,
)
from astock.investor_orchestration.paper_replay import CanonicalConfirmedPaperReplayAdapter
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.public_cli import register_public_commands
from astock.investor_orchestration.regime import MarketRegimeService
from astock.investor_orchestration.schedule_clock import (
    DailyTrackingSchedule,
    StateTradingCalendar,
)
from astock.investor_orchestration.scheduled import (
    ScheduledResearchService,
    SQLiteNotificationOutbox,
    policy_from_config,
)
from astock.investor_orchestration.source_audit_cli import register_source_audit_commands
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.investor_orchestration.utils import content_hash, utc_now

app = typer.Typer(
    name="investor",
    help="Unified investor preflight, watchlist, regime and scheduled research tools.",
    no_args_is_help=True,
)
register_public_commands(app)
register_source_audit_commands(app)


def _store(database: Path | None) -> InvestorOrchestrationStore:
    store = InvestorOrchestrationStore(database)
    store.initialize()
    return store


def _dump(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _aware_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def _decimal(value: str, *, parameter: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{parameter} must be a decimal number") from exc


def _scheduled_service(store: InvestorOrchestrationStore) -> ScheduledResearchService:
    return ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        notification_sink=SQLiteNotificationOutbox(store),
        paper_replay_adapter=CanonicalConfirmedPaperReplayAdapter.from_store(store),
    )


def _scheduled_request(
    binding: ScheduledTaskBinding,
    *,
    window: ScheduledWindow,
    schedule_bucket: str,
    requested_at: datetime,
    domains: tuple[ScheduledDomain, ...],
) -> ScheduledRunRequest:
    key = content_hash(
        {
            "binding_id": binding.binding_id,
            "schedule_bucket": schedule_bucket,
            "window": window,
            "domains": tuple(sorted(domain.value for domain in domains)),
            "policy_hash": binding.policy_hash,
        }
    )
    return ScheduledRunRequest(
        run_id=f"scheduled-{uuid.uuid5(uuid.NAMESPACE_URL, key)}",
        binding_id=binding.binding_id,
        schedule_bucket=schedule_bucket,
        window=window,
        domains=domains,
        requested_at=requested_at,
        source_revision_set={},
        policy_hash=binding.policy_hash,
        idempotency_key=key,
    )


@app.command("init")
def initialize(
    database: Path | None = typer.Option(None, help="SQLite state database path."),
) -> None:
    store = _store(database)
    _dump(store.export_summary())


@app.command("status")
def status(
    database: Path | None = typer.Option(None, help="SQLite state database path."),
) -> None:
    _dump(_store(database).export_summary())


@app.command("audit")
def audit(
    database: Path | None = typer.Option(None, help="SQLite state database path."),
) -> None:
    result = _store(database).audit_integrity()
    _dump(result)
    if result["status"] != "PASS":
        raise typer.Exit(code=1)


@app.command("preflight")
def preflight(
    text: str = typer.Argument(..., help="Original investor request text."),
    intent: RequestIntent = typer.Option(RequestIntent.RESEARCH),
    side_effect: SideEffectClass = typer.Option(SideEffectClass.READ),
    instrument: list[str] | None = typer.Option(None, "--instrument"),
    account_id: str | None = typer.Option(None),
    user_timezone: str = typer.Option("Asia/Shanghai"),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    request_time = _aware_now(user_timezone)
    body = {
        "question_time": request_time,
        "raw_text": text,
        "intent": intent,
        "side_effect": side_effect,
        "entity_ids": tuple(instrument or ()),
        "account_id": account_id,
    }
    request_hash = content_hash(body)
    request = InvestorRequestEnvelope(
        request_id=f"request-{uuid.uuid5(uuid.NAMESPACE_URL, request_hash)}",
        question_time=request_time,
        user_timezone=user_timezone,
        raw_text=text,
        normalized_intent=intent,
        side_effect=side_effect,
        entity_ids=tuple(instrument or ()),
        account_id=account_id,
        idempotency_key=request_hash,
    )
    receipt = InvestorSessionPreflightService(store).build(request)
    _dump(receipt)


@app.command("capability-plan")
def capability_plan(
    text: str = typer.Argument(...),
    intent: RequestIntent = typer.Option(RequestIntent.RESEARCH),
    side_effect: SideEffectClass = typer.Option(SideEffectClass.READ),
    scenario_id: int | None = typer.Option(None),
    instrument: list[str] | None = typer.Option(None, "--instrument"),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    now = _aware_now("Asia/Shanghai")
    request_hash = content_hash(
        {
            "text": text,
            "intent": intent,
            "side_effect": side_effect,
            "instrument": instrument or [],
            "at": now,
        }
    )
    request = InvestorRequestEnvelope(
        request_id=f"request-{uuid.uuid5(uuid.NAMESPACE_URL, request_hash)}",
        question_time=now,
        user_timezone="Asia/Shanghai",
        raw_text=text,
        normalized_intent=intent,
        side_effect=side_effect,
        entity_ids=tuple(instrument or ()),
        idempotency_key=request_hash,
    )
    receipt = InvestorSessionPreflightService(store).build(request)
    requirements = (
        None
        if scenario_id is None
        else CapabilityPlanner.scenario_requirements(
            "configs/business_scenarios_v1.yaml", scenario_id
        )
    )
    _dump(CapabilityPlanner(store=store).plan(request, receipt, scenario_requirements=requirements))


@app.command("watch-add")
def watch_add(
    instrument_id: str,
    reason: str = typer.Option(..., help="Why the target is attractive but not ready."),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    event = ResearchSubjectRegistryService(store).add_watchlist(
        instrument_id,
        reason=reason,
        available_at=utc_now(),
        idempotency_key=content_hash(
            {"instrument": instrument_id, "reason": reason, "action": "watch-add"}
        ),
    )
    _dump(event)


@app.command("watch-remove")
def watch_remove(
    instrument_id: str,
    reason: str = typer.Option(...),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    event = ResearchSubjectRegistryService(store).remove_watchlist(
        instrument_id,
        reason=reason,
        available_at=utc_now(),
        idempotency_key=content_hash(
            {"instrument": instrument_id, "reason": reason, "action": "watch-remove"}
        ),
    )
    _dump(event)


@app.command("watch-list")
def watch_list(database: Path | None = typer.Option(None)) -> None:
    store = _store(database)
    _dump(
        [
            event.model_dump(mode="json")
            for event in ResearchSubjectRegistryService(store).current_watchlist()
        ]
    )


@app.command("provisional-position")
def provisional_position(
    instrument_id: str,
    quantity: str,
    source_text: str = typer.Option(...),
    account_id: str = typer.Option("default"),
    lane: PortfolioLane = typer.Option(PortfolioLane.ACTUAL),
    precision: DatePrecision = typer.Option(DatePrecision.DATE_ONLY),
    asserted_at: datetime | None = typer.Option(None),
    cost_low: str | None = typer.Option(None),
    cost_high: str | None = typer.Option(None),
    cost_source_artifact: str | None = typer.Option(None),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    at = asserted_at or _aware_now("Asia/Shanghai")
    if at.tzinfo is None:
        at = at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    parsed_quantity = _decimal(quantity, parameter="quantity")
    key = content_hash(
        {
            "account": account_id,
            "lane": lane,
            "instrument": instrument_id,
            "quantity": parsed_quantity,
            "asserted_at": at,
            "source": source_text,
        }
    )
    service = ResearchSubjectRegistryService(store)
    assertion = service.record_provisional_position(
        account_id=account_id,
        lane=lane,
        instrument_id=instrument_id,
        quantity=parsed_quantity,
        asserted_at=at,
        date_precision=precision,
        source_text=source_text,
        idempotency_key=key,
    )
    output: dict[str, object] = {"assertion": assertion.model_dump(mode="json")}
    if cost_low is not None or cost_high is not None:
        if cost_low is None or cost_high is None or cost_source_artifact is None:
            raise typer.BadParameter(
                "cost-low, cost-high and cost-source-artifact are required together"
            )
        estimate = service.attach_estimated_cost(
            assertion,
            low=_decimal(cost_low, parameter="cost-low"),
            high=_decimal(cost_high, parameter="cost-high"),
            method="user-specified or PIT market range; not ledger truth",
            as_of=at,
            source_artifact_id=cost_source_artifact,
        )
        output["estimated_cost"] = estimate.model_dump(mode="json")
    _dump(output)


@app.command("regime-evaluate")
def regime_evaluate(
    feature_file: Path = typer.Argument(..., exists=True, readable=True),
    profile_file: Path | None = typer.Option(None, exists=True, readable=True),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    features = MarketRegimeFeatureSnapshot.model_validate(
        yaml.safe_load(feature_file.read_text(encoding="utf-8"))
    )
    service = MarketRegimeService(store)
    previous = store.latest_valid_regime(features.as_of)
    snapshot = service.infer(features, previous=previous)
    profile = (
        None if profile_file is None else yaml.safe_load(profile_file.read_text(encoding="utf-8"))
    )
    overlay = service.overlay(snapshot, profile)
    _dump(
        {
            "snapshot": snapshot.model_dump(mode="json"),
            "overlay": overlay.model_dump(mode="json"),
        }
    )


@app.command("macro-capture")
def macro_capture(
    authority: str,
    release_family: str,
    live: bool = typer.Option(False, help="Explicitly call the official endpoint."),
    recorded_file: Path | None = typer.Option(None, exists=True, readable=True),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    service = OfficialMacroCaptureService(store)
    spec = service.spec(authority.upper(), release_family)
    if live and recorded_file is not None:
        raise typer.BadParameter("choose live or recorded-file, not both")
    if not live and recorded_file is None:
        raise typer.BadParameter("recorded-file is required unless --live is explicit")
    snapshot = service.capture(
        spec,
        live=live,
        recorded_content=(None if recorded_file is None else recorded_file.read_bytes()),
    )
    _dump(snapshot)


@app.command("schedule-policy-init")
def schedule_policy_init(
    config_path: Path = typer.Option(
        Path("configs/scheduled_investor_tracking_v1.yaml"),
        exists=True,
        readable=True,
    ),
    database: Path | None = typer.Option(None),
) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    store = _store(database)
    policy = policy_from_config(config)
    ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
    ).register_policy(policy)
    _dump(policy)


@app.command("schedule-preview")
def schedule_preview(
    config_path: Path = typer.Option(
        Path("configs/scheduled_investor_tracking_v1.yaml"),
        exists=True,
        readable=True,
    ),
) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    policy = policy_from_config(config)
    _dump(
        {
            "policy": policy.model_dump(mode="json"),
            "windows": config["windows"],
            "domains": config["domains"],
            "creation_guidance": (
                "Use LOCAL_ONLY/LOCAL_DAEMON for deterministic local execution, "
                "or create a native ChatGPT task and bind its opaque id."
            ),
            "actual_holding_execution": False,
            "paper_default": "PROPOSE_ONLY",
        }
    )


@app.command("schedule-bind")
def schedule_bind(
    binding_id: str,
    schedule_expression: str,
    execution_surface: str = typer.Option("LOCAL_DAEMON"),
    creation_mode: str = typer.Option("LOCAL_ONLY"),
    platform_task_id: str | None = typer.Option(None),
    consent_hash: str | None = typer.Option(None),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    policy = store.get_scheduled_policy("scheduled-investor-tracking")
    if policy is None:
        raise typer.BadParameter("initialize scheduled policy first")
    binding = ScheduledTaskBinding(
        binding_id=binding_id,
        platform_task_id=platform_task_id,
        creation_mode=creation_mode,  # type: ignore[arg-type]
        execution_surface=execution_surface,  # type: ignore[arg-type]
        schedule_expression=schedule_expression,
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        active=True,
        confirmed_at=utc_now(),
        consent_hash=consent_hash,
    )
    ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
    ).register_binding(binding)
    _dump(binding)


@app.command("schedule-run")
def schedule_run(
    binding_id: str,
    window: ScheduledWindow,
    domain: list[ScheduledDomain] | None = typer.Option(None, "--domain"),
    schedule_bucket: str | None = typer.Option(None),
    database: Path | None = typer.Option(None),
) -> None:
    store = _store(database)
    binding = store.get_binding(binding_id)
    if binding is None:
        raise typer.BadParameter("unknown binding")
    now = _aware_now(binding.timezone)
    bucket = schedule_bucket or f"{now.date().isoformat()}:{window.value}"
    selected_domains = tuple(domain or list(ScheduledDomain))
    request = _scheduled_request(
        binding,
        window=window,
        schedule_bucket=bucket,
        requested_at=now,
        domains=selected_domains,
    )
    receipt = _scheduled_service(store).run(request)
    _dump(receipt)
    if receipt.outcome.value in {"BLOCKED", "FAILED", "DEGRADED"}:
        raise typer.Exit(code=3)


@app.command("schedule-tick")
def schedule_tick(
    binding_id: str,
    config_path: Path = typer.Option(
        Path("configs/scheduled_investor_tracking_v1.yaml"),
        exists=True,
        readable=True,
    ),
    at: datetime | None = typer.Option(None, "--at"),
    grace_minutes: int = typer.Option(20, min=0, max=120),
    catch_up_minutes: int = typer.Option(180, min=0, max=1440),
    max_catch_up_buckets: int = typer.Option(2, min=0, max=16),
    database: Path | None = typer.Option(None),
) -> None:
    """Run due trading-session checks and persist expired missed-run receipts."""
    store = _store(database)
    binding = store.get_binding(binding_id)
    if binding is None or not binding.active:
        raise typer.BadParameter("unknown or inactive binding")
    policy = store.get_scheduled_policy(binding.policy_id)
    if policy is None or policy.policy_hash != binding.policy_hash:
        raise typer.BadParameter("binding does not match the active scheduled policy")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    configured_policy = policy_from_config(config)
    if configured_policy.policy_hash != binding.policy_hash:
        raise typer.BadParameter("config policy hash does not match the binding")
    now = at or _aware_now(binding.timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(binding.timezone))
    schedule = DailyTrackingSchedule.from_config(
        config,
        is_trading_day=StateTradingCalendar(store).is_trading_day,
    )
    completed = store.completed_schedule_buckets(binding_id)
    try:
        plan = schedule.plan(
            now,
            last_completed_buckets=completed,
            grace_minutes=grace_minutes,
            catch_up_minutes=catch_up_minutes,
            max_catch_up_buckets=max_catch_up_buckets,
        )
    except RuntimeError as exc:
        _dump(
            {
                "binding_id": binding_id,
                "at": now.isoformat(),
                "status": "BLOCKED",
                "reason": str(exc),
                "receipts": [],
            }
        )
        raise typer.Exit(code=2) from exc

    service = _scheduled_service(store)
    domains = tuple(policy.domains)
    expired_receipts = [
        service.block(
            _scheduled_request(
                binding,
                window=window,
                schedule_bucket=bucket,
                requested_at=now,
                domains=domains,
            ),
            "MISSED_RUN_EXPIRED",
        )
        for window, bucket in plan.expired
    ]
    receipts = [
        service.run(
            _scheduled_request(
                binding,
                window=window,
                schedule_bucket=bucket,
                requested_at=now,
                domains=domains,
            )
        )
        for window, bucket in plan.due
    ]
    blocked = any(receipt.outcome.value in {"BLOCKED", "FAILED"} for receipt in receipts)
    degraded = any(receipt.outcome.value == "DEGRADED" for receipt in receipts)
    status = (
        "NON_TRADING_DAY"
        if not plan.trading_day
        else "BLOCKED"
        if blocked
        else "DEGRADED"
        if degraded or expired_receipts
        else "COMPLETED"
        if receipts
        else "NOOP"
    )
    _dump(
        {
            "binding_id": binding_id,
            "at": now.isoformat(),
            "status": status,
            "due_buckets": [bucket for _, bucket in plan.due],
            "expired_buckets": [bucket for _, bucket in plan.expired],
            "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
            "expired_receipts": [receipt.model_dump(mode="json") for receipt in expired_receipts],
            "actual_execution_allowed": False,
        }
    )
    if status in {"BLOCKED", "DEGRADED"}:
        raise typer.Exit(code=3)
