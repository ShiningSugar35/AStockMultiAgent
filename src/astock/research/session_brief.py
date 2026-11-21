"""On-demand Chinese brief projection; no scheduler, delivery or approval."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from astock.core.errors import StorageError
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.market_data.official_calendar import OfficialTradingCalendarConfig
from astock.research.debate_evaluation import QualityError, QualityPolicy, aware, digest, sealed
from astock.research.outcome_feedback import SHANGHAI, decision_completed_at, published
from astock.schemas.full_research import NewsEventResearchPack, RecommendationResearchReceipt
from astock.schemas.market import Market
from astock.schemas.technical import TechnicalIndicatorSnapshot


def market_period(
    now: datetime, exchange: Market, calendar: OfficialTradingCalendarConfig, policy: QualityPolicy
) -> str:
    local = aware(now).astimezone(SHANGHAI)
    dates = calendar.open_dates(exchange, local.date(), local.date())
    if dates is None:
        return "CALENDAR_UNKNOWN"
    if not dates:
        return "CLOSED"
    clock = local.time().replace(tzinfo=None)
    morning, lunch, afternoon, close = (time.fromisoformat(x) for x in policy.session_boundaries)
    if clock < morning:
        return "PRE_MARKET"
    if morning <= clock < lunch or afternoon <= clock < close:
        return "INTRADAY"
    if lunch <= clock < afternoon:
        return "MIDDAY_BREAK"
    return "POST_MARKET"


def build_session_brief(
    *,
    instrument_id: str,
    account_id: str | None,
    context_id: str,
    now: datetime,
    calendar: OfficialTradingCalendarConfig,
    policy: QualityPolicy,
    receipt: RecommendationResearchReceipt | None = None,
    previous_receipt: RecommendationResearchReceipt | None = None,
    technical: TechnicalIndicatorSnapshot | None = None,
    events: NewsEventResearchPack | None = None,
    previous_fingerprint: str | None = None,
    lineage: dict | None = None,
) -> dict:
    now = aware(now)
    instrument_id = normalized_instrument(instrument_id)
    if not context_id:
        raise QualityError("INVALID_BRIEF_CONTEXT")
    try:
        exchange = Market(instrument_id.split(":", 1)[0])
    except ValueError:
        raise QualityError("SECURITY_MISMATCH") from None
    if exchange is Market.INDEX:
        raise QualityError("SECURITY_MISMATCH")
    period = market_period(now, exchange, calendar, policy)
    local_day = now.astimezone(SHANGHAI).date()
    labels = {
        "PRE_MARKET": "盘前研究简报",
        "INTRADAY": "盘中研究简报",
        "MIDDAY_BREAK": "午间研究简报",
        "POST_MARKET": "盘后研究简报",
        "CLOSED": "休市研究简报",
        "CALENDAR_UNKNOWN": "研究简报：交易日历尚未核实",
    }

    def view(item, *, historical: bool = False):
        if item is None:
            return None
        if item.request_contract.account_id != account_id:
            raise QualityError("ACCOUNT_MISMATCH")
        if instrument_id not in {normalized_instrument(x) for x in item.candidate_universe}:
            raise QualityError("SECURITY_MISMATCH")
        completed = decision_completed_at(item)
        if completed > now or (
            not historical and now - completed > timedelta(hours=policy.receipt_max_age_hours)
        ):
            return None
        if not published(item):
            return None
        for source in item.source_manifest:
            clocks = [source.available_to_system_at, source.captured_at]
            clocks.extend(
                x
                for x in (source.observed_at, source.release_at, source.published_at)
                if x is not None
            )
            if max(clocks) > now:
                raise QualityError("RECEIPT_FUTURE_SOURCE")
        position_ids = {normalized_instrument(x.instrument_id) for x in item.portfolio.positions}
        holding = next(
            (
                x
                for x in item.holding_reviews
                if normalized_instrument(x.instrument_id) == instrument_id
            ),
            None,
        )
        if holding is not None:
            action = {
                "HOLD": "继续持有",
                "ADD": "满足条件后考虑加仓",
                "TRIM": "按条件减仓",
                "EXIT": "按条件退出",
                "REVIEW": "重新评估",
            }[holding.recommended_action]
        elif instrument_id in position_ids:
            action = "满足原入场条件后考虑配置"
        else:
            action = "继续观察"
        execution = next(
            (
                x
                for x in item.execution_plans
                if normalized_instrument(x.instrument_id) == instrument_id
            ),
            None,
        )
        narrative = next(
            (
                x
                for x in item.candidate_narratives
                if normalized_instrument(x.instrument_id) == instrument_id
            ),
            None,
        )
        conditions = {
            "execution": None
            if execution is None
            else execution.model_dump(
                mode="json",
                include={
                    "buy_range_low",
                    "buy_range_high",
                    "maximum_acceptable_price",
                    "add_conditions",
                    "reduce_exit_conditions",
                    "thesis_invalidation_conditions",
                    "time_stop_condition",
                    "valuation_exit_condition",
                    "event_exit_conditions",
                },
            ),
            "holding": None
            if holding is None
            else holding.model_dump(
                mode="json",
                include={
                    "preconditions",
                    "reversal_conditions",
                    "next_review_conditions",
                    "target_weight_lower",
                    "target_weight_mid",
                    "target_weight_upper",
                },
            ),
            "narrative": None
            if narrative is None
            else narrative.model_dump(
                mode="json",
                exclude={
                    "created_at",
                    "schema_version",
                    "instrument_id",
                    "evidence_ids",
                },
            ),
        }
        return {
            "conditions": conditions,
            "receipt_id": item.receipt_id,
            "receipt_hash": item.receipt_hash,
            "receipt_content_hash": digest(item.model_dump(mode="json")),
            "completion_boundary": completed.isoformat(),
            "action": action,
        }

    current = view(receipt)
    previous = view(previous_receipt, historical=True)
    if (
        current is not None
        and previous is not None
        and datetime.fromisoformat(previous["completion_boundary"])
        > datetime.fromisoformat(current["completion_boundary"])
    ):
        raise QualityError("PREVIOUS_RECEIPT_ORDER")
    technical_fact = None
    if technical is not None:
        if technical.instrument_id != instrument_id:
            raise QualityError("SECURITY_MISMATCH")
        lookback_start = max(
            technical.latest_session_date, local_day - timedelta(days=policy.max_calendar_days)
        )
        sessions = calendar.open_dates(exchange, lookback_start, local_day)
        close_clock = time.fromisoformat(policy.session_boundaries[-1])
        closed_sessions = (
            []
            if sessions is None
            else [day for day in sessions if datetime.combine(day, close_clock, SHANGHAI) <= now]
        )
        latest_expected = closed_sessions[-1] if closed_sessions else None
        if (
            technical.as_of <= now
            and latest_expected == technical.latest_session_date
            and now - technical.as_of <= timedelta(hours=policy.receipt_max_age_hours)
        ):
            technical_fact = {
                "snapshot_id": technical.snapshot_id,
                "content_hash": digest(technical.model_dump(mode="json")),
                "rsi": technical.rsi,
                "atr": technical.atr,
                "breakout_state": technical.breakout_state,
                "caution_codes": list(technical.caution_codes),
            }
    event_facts = []
    event_binding = None
    if events is not None:
        if normalized_instrument(events.instrument_id) != instrument_id:
            raise QualityError("SECURITY_MISMATCH")
        if len(events.events) > policy.max_events:
            raise QualityError("INPUT_LIMIT")
        if events.as_of <= now:
            event_binding = digest(events.model_dump(mode="json"))
            seen = set()
            for event in events.events:
                if (
                    event.instrument_id is not None
                    and normalized_instrument(event.instrument_id) != instrument_id
                ):
                    raise QualityError("SECURITY_MISMATCH")
                if len(event.summary) > policy.max_text_chars:
                    raise QualityError("INPUT_LIMIT")
                if event.event_timestamp <= now and event.event_id not in seen:
                    seen.add(event.event_id)
                    event_facts.append(
                        {
                            "source_id": event.source_id,
                            "event_id": event.event_id,
                            "summary": event.summary,
                        }
                    )
    action = str(current["action"]) if current is not None else ""
    old_action = str(previous["action"]) if previous is not None else ""
    action_changed = current is not None and previous is not None and action != old_action
    conditions_changed = (
        current is not None
        and previous is not None
        and current["conditions"] != previous["conditions"]
    )
    conclusion_changed = action_changed or conditions_changed
    facts = {
        "policy_hash": digest(policy.model_dump(mode="json")),
        "account_scope_hash": digest({"account_id": account_id}),
        "instrument_id": instrument_id,
        "local_session_date": local_day.isoformat(),
        "market_period": period,
        "calendar_version": calendar.schema_version,
        "calendar_year": None,
        "current": current,
        "previous": previous,
        "technical": technical_fact,
        "events": event_facts,
        "event_version": event_binding,
    }
    year = calendar.years.get(local_day.year)
    if year is not None and exchange in year.sources:
        facts["calendar_year"] = {
            "verification_boundary": year.verified_at.isoformat(),
            "publication_boundary": year.published_at.isoformat(),
            "source_url": year.sources[exchange].url,
            "closures": [
                {"start": x.start.isoformat(), "end": x.end.isoformat()} for x in year.closures
            ],
        }
    semantic = dict(facts)
    for key in ("current", "previous"):
        value = facts[key]
        semantic[key] = (
            None
            if value is None
            else {
                "action": value["action"],
                "conditions": value["conditions"],
            }
        )
    # The citation/hash still lives in full source_facts. A recalculation time
    # is not a change to the underlying closed K-line or displayed indicators.
    if technical_fact is not None and technical is not None:
        semantic["technical"] = {
            "latest_session_date": technical.latest_session_date.isoformat(),
            "rsi": technical_fact["rsi"],
            "atr": technical_fact["atr"],
            "breakout_state": technical_fact["breakout_state"],
            "caution_codes": technical_fact["caution_codes"],
        }
    fingerprint = digest(semantic)
    lines = [labels[period], instrument_id]
    if current is not None:
        lines.append("已发布研究观点：" + action + "。")
        lines.append("入场、退出及失效条件仍以原研究为准。")
    else:
        lines.append("当前没有可沿用的正式投资结论。")
    if action_changed:
        lines.append("研究观点变化：由“" + old_action + "”调整为“" + action + "”。")
    elif conditions_changed:
        lines.append("操作方向未变，但入场、退出或论据条件已经调整，需核对原研究。")
    elif current is not None and previous is not None:
        lines.append("前后操作方向与已核实的研究条件未变。")
    if technical_fact is not None and technical is not None:
        state_labels = {
            "INSUFFICIENT_HISTORY": "技术历史不足",
            "NO_TRADE": "该日无成交",
            "NO_BREAKOUT": "尚未突破参考区间",
            "PRICE_ONLY": "价格突破尚缺成交量确认",
            "VOLUME_CONFIRMED": "价格与成交量同时满足突破观察条件",
            "CORPORATE_ACTION_REVIEW": "公司行动可能影响技术比较，需复核",
        }
        lines.append("技术观察：" + state_labels[technical.breakout_state] + "。")
        if technical.rsi is not None:
            lines.append("相对强弱指标为" + format(technical.rsi, ".2f") + "。")
        if technical.atr is not None:
            lines.append("平均真实波幅为" + format(technical.atr, ".2f") + "。")
        if technical.caution_codes:
            lines.append("技术数据存在需继续核对的条件。")
        lines.append("技术观察仅描述市场事实，不能单独形成买卖或仓位建议。")
    if event_facts:
        # External prose is data, not instructions. Never paste arbitrary summaries
        # into an investor action surface; the existing event-research workflow owns
        # semantic verification and the final ResponseGateway publication.
        lines.append(
            "新增"
            + str(len(event_facts))
            + "项事件线索，需结合已核实原文判断；不据此新增买卖指令。"
        )
    text = "\n".join(lines)
    # Draft-only reuse of the established public editor/audit. This does not
    # invoke, emulate or claim the gateway's authenticated publication route.
    from astock.research.presentation import audit_public_answer, normalize_public_text
    from astock.research.public_polish import polish_public_text

    text = normalize_public_text(polish_public_text(text, request_text="").text)
    audit = audit_public_answer(text, source_text=text, request_text="")
    if not audit.safe_to_send:
        text = "研究简报正文尚需完成公开展示核验。"
    return sealed(
        "session-brief",
        {
            "context_id": context_id,
            "instrument_id": instrument_id,
            "projection_boundary": now.isoformat(),
            "semantic_fingerprint": fingerprint,
            "changed": fingerprint != previous_fingerprint,
            "conclusion_changed": conclusion_changed,
            "conditions_changed": conditions_changed,
            "market_period": period,
            "text": text,
            "delivery_status": "DRAFT",
            "public_approved": False,
            "public_audit_passed": audit.safe_to_send,
            "source_facts": facts,
            "lineage": lineage or {},
            "scheduler_created": False,
            "notification_sent": False,
        },
    )


def render_session_brief(
    draft: dict,
    receipt: RecommendationResearchReceipt | None,
    *,
    instrument_id: str,
) -> dict:
    """Opt-in local display through the existing ResponseGateway; never deliver.

    The caller must build *draft* from registered canonical inputs in this process.
    A draft, arbitrary CLI flag or positive editorial audit cannot grant a
    recommendation independently of the signed publication receipt.
    """
    current = draft.get("source_facts", {}).get("current")
    subject = normalized_instrument(instrument_id)
    if (
        receipt is None
        or draft.get("result_id") != "session-brief:" + digest(
            {key: value for key, value in draft.items() if key != "result_id"}
        )
        or not published(receipt)
        or draft.get("source_facts", {}).get("account_scope_hash") != digest(
            {"account_id": receipt.request_contract.account_id}
        )
        or draft.get("instrument_id") != subject
        or draft.get("source_facts", {}).get("instrument_id") != subject
        or subject not in {normalized_instrument(x) for x in receipt.candidate_universe}
        or not isinstance(current, dict)
        or current.get("receipt_id") != receipt.receipt_id
        or current.get("receipt_hash") != receipt.receipt_hash
        or current.get("receipt_content_hash") != digest(receipt.model_dump(mode="json"))
        or current.get("action") is None
        or draft.get("public_audit_passed") is not True
    ):
        return {
            "status": "NOT_AUTHORIZED_FOR_DISPLAY",
            "public_approved": False,
            "delivery_status": "DRAFT",
            "notification_sent": False,
            "draft_result_id": draft.get("result_id"),
        }
    from astock.research.presentation import ResponseGateway
    from astock.schemas.presentation import (
        ConclusionStrength,
        ResearchNarrativeBundle,
        ResponseChannel,
        ResponseDetail,
        ResponseMode,
        ResponseTaskType,
    )

    text = str(draft.get("text", ""))
    if not text or len(text) > 4000:
        raise QualityError("PUBLIC_BRIEF_LENGTH_OUT_OF_BOUNDS")
    gateway = ResponseGateway()
    context = gateway.context(
        "展示已获正式准入的研究简报",
        task_type=ResponseTaskType.FORMAL_REPORT,
        channel=ResponseChannel.REPORT,
        requested_detail=ResponseDetail.STANDARD,
        explicit_mode=ResponseMode.REPORT,
    )
    report = gateway.render(
        context,
        narrative=ResearchNarrativeBundle(
            subject=subject,
            task_type=ResponseTaskType.FORMAL_REPORT,
            headline=text,
            conclusion_strength=ConclusionStrength.UNSPECIFIED,
            data_as_of=receipt.as_of,
        ),
    )
    if report.safe_fallback_used or not report.audit.safe_to_send:
        return {
            "status": "PUBLIC_GATEWAY_REJECTED",
            "public_approved": False,
            "delivery_status": "DRAFT",
            "notification_sent": False,
            "draft_result_id": draft.get("result_id"),
        }
    return {
        "status": "PUBLIC_PRESENTATION_READY",
        "public_approved": True,
        "public_audit_passed": report.audit.safe_to_send,
        "presentation_text": report.text,
        "text": report.text,
        "receipt_id": receipt.receipt_id,
        "receipt_hash": receipt.receipt_hash,
        "source_receipt_id": receipt.receipt_id,
        "draft_result_id": draft.get("result_id"),
        "display_audit": report.audit.model_dump(mode="json"),
        "delivery_status": "LOCAL_ONLY",
        "notification_sent": False,
    }


class SessionBriefReuseService:
    """Local scheduled/monitor projection over bounded registered history only."""

    def __init__(self, context):
        self.context = context

    def project_monitor_target(self, target, *, now):
        # Monitor targets have security identity but no account identity. Never
        # infer an account from an order/position reason or reuse private advice.
        reasons = {str(reason) for reason in target.reasons}
        if reasons & {"PAPER_POSITION", "OPEN_PAPER_ORDER"}:
            return {
                "status": "MONITOR_BRIEF_ACCOUNT_BINDING_REQUIRED",
                "delivery_status": "DRAFT",
                "public_approved": False,
            }
        if not reasons & {"ANALYZED", "RECOMMENDED", "CATALYST"}:
            return {
                "status": "MONITOR_BRIEF_RESEARCH_SCOPE_REQUIRED",
                "delivery_status": "DRAFT",
                "public_approved": False,
            }
        return self.project(
            instrument_id=f"{target.market.value}:{target.symbol}",
            account_id=None,
            context_id=f"continuous-monitor:{target.target_id}",
            now=now,
        )

    def project(self, *, instrument_id, account_id, context_id, now):
        from astock.research.quality_cli import read_registered

        subject = normalized_instrument(instrument_id)
        key = digest({"context": context_id, "account": account_id, "subject": subject})
        checkpoint = self.context.state.get_checkpoint("session-brief-reuse", key)
        receipts = []
        receipt_read_gaps = []
        for record in self.context.state.recent_artifact_records(
            "RecommendationResearchReceipt", limit=64
        ):
            try:
                item, lineage = read_registered(
                    self.context, record["artifact_id"], RecommendationResearchReceipt
                )
            except (ValueError, OSError, StorageError):
                receipt_read_gaps.append("RECEIPT_UNAVAILABLE")
                continue
            if (
                item.request_contract.account_id == account_id
                and subject in {normalized_instrument(x) for x in item.candidate_universe}
                and decision_completed_at(item) <= aware(now)
                and published(item)
            ):
                receipts.append((item, lineage))
        receipts.sort(key=lambda pair: decision_completed_at(pair[0]), reverse=True)
        current = receipts[0] if receipts else (None, None)
        previous = receipts[1] if len(receipts) > 1 else (None, None)
        optional_input_gaps = []

        def latest(model):
            candidates = []
            for record in self.context.state.recent_artifact_records(model.__name__, limit=64):
                try:
                    item, binding = read_registered(self.context, record["artifact_id"], model)
                except (ValueError, OSError, StorageError):
                    optional_input_gaps.append({"type": model.__name__, "reason": "UNAVAILABLE"})
                    continue
                if (
                    normalized_instrument(item.instrument_id) == subject
                    and item.as_of <= aware(now)
                ):
                    candidates.append((item, binding))
            if not candidates:
                return None, None
            candidates.sort(key=lambda pair: (pair[0].as_of, pair[1]["object_hash"]), reverse=True)
            newest = candidates[0]
            if any(
                item.as_of == newest[0].as_of and binding["object_hash"] != newest[1]["object_hash"]
                for item, binding in candidates[1:]
            ):
                optional_input_gaps.append({"type": model.__name__, "reason": "SAME_TIME_CONFLICT"})
                return None, None
            return newest

        technical, technical_binding = latest(TechnicalIndicatorSnapshot)
        events, events_binding = latest(NewsEventResearchPack)
        draft = build_session_brief(
            instrument_id=subject,
            account_id=account_id,
            context_id=context_id,
            now=now,
            calendar=self.context.calendar,
            policy=self.context.policy,
            receipt=current[0],
            previous_receipt=previous[0],
            previous_fingerprint=(checkpoint["cursor"].get("fingerprint") if checkpoint else None),
            technical=technical,
            events=events,
            lineage={
                "receipt": current[1], "previous_receipt": previous[1],
                "technical": technical_binding, "events": events_binding,
                "optional_input_gaps": optional_input_gaps,
                "receipt_read_gaps": sorted(set(receipt_read_gaps)),
            },
        )
        # Never promote or enqueue this draft. The existing authenticated public
        # route alone owns display authorization; scheduled consent is insufficient.
        if checkpoint and not draft["changed"]:
            return {
                "status": "UNCHANGED",
                "artifact_id": checkpoint["cursor"]["artifact_id"],
                "delivery_status": "DRAFT",
                "public_approved": False,
            }
        ref = self.context.objects.put_json(draft)
        artifact_id = "SessionBrief:" + draft["result_id"]
        self.context.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="SessionBrief",
            schema_version=draft["schema_version"],
            object_hash=ref.sha256,
            input_hashes=[
                x["object_hash"]
                for x in (current[1], previous[1], technical_binding, events_binding) if x
            ],
        )
        self.context.state.set_checkpoint(
            scope_type="session-brief-reuse",
            scope_key=key,
            cursor={"fingerprint": draft["semantic_fingerprint"], "artifact_id": artifact_id},
            status="DRAFT",
            object_hash=ref.sha256,
        )
        return {
            "status": "DRAFT",
            "artifact_id": artifact_id,
            "delivery_status": "DRAFT",
            "public_approved": False,
        }
