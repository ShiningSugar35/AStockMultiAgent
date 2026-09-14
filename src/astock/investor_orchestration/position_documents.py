"""Recoverable human-readable projections, never an economic ledger."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from astock.core.atomic import atomic_write_text
from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import aware_time, normalized_instrument
from astock.investor_orchestration.models import InvestorSessionPreflightReceipt
from astock.investor_orchestration.run_ownership import (
    ScheduledRunInProgress,
    schedule_run_ownership,
)
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.investor_orchestration.utils import content_hash


class PositionDocumentProjector:
    """Optional, bounded document I/O over already-certified account snapshots.

    The local state is a reconstructable projection cache. Unknown source coverage
    never means liquidation. A write-ahead document bundle and the existing OS
    ownership helper protect interrupted/concurrent activations without holding a
    database write transaction or adding an economic state store.
    """

    def __init__(self, project_root: Path, subjects: ResearchSubjectRegistryService) -> None:
        self.root = project_root / "user_state" / "position_tracking"
        self.subjects = subjects
        self._research_snapshot_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._subject_event_cache: tuple[Any, ...] | None = None

    def update(self, preflight: InvestorSessionPreflightReceipt) -> dict[str, object]:
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.root / ".projection.json"
        try:
            with schedule_run_ownership(state_path, "position-documents", "current"):
                return self._update_locked(preflight)
        except ScheduledRunInProgress:
            return {"changed": False, "deferred": True, "reason": "DOCUMENT_UPDATE_IN_PROGRESS"}

    def _update_locked(self, preflight: InvestorSessionPreflightReceipt) -> dict[str, object]:
        self._recover_pending()
        state_path = self.root / ".projection.json"
        previous = self._load(state_path)
        previous_at = previous.get("as_of")
        if isinstance(previous_at, str) and datetime.fromisoformat(previous_at) > preflight.as_of:
            return {"changed": False, "deferred": True, "reason": "OLDER_OBSERVATION"}
        from astock.investor_orchestration.position_trade_history import (
            apply_trade_history,
            position_trade_history,
        )

        self._research_snapshot_cache.clear()
        self._subject_event_cache = None
        active = self._active(preflight, previous)
        store = getattr(self.subjects, "store", None)
        try:
            histories = position_trade_history(store, as_of=preflight.as_of) if store else {}
        except (OSError, ValueError, sqlite3.Error, StorageError):
            histories = {}
        active, closed = apply_trade_history(
            active,
            previous,
            histories,
            as_of=preflight.as_of,
            checked_accounts={
                (lane.lane.value, account)
                for lane in (preflight.context.actual, preflight.context.paper)
                if lane.audit_status == "PASS"
                for account in lane.account_ids
            },
            entry_snapshot=lambda instrument, price, snapshot_at, price_kind: self._entry_snapshot(
                instrument,
                preflight,
                observed_price=price,
                snapshot_at=snapshot_at,
                price_kind=price_kind,
            ),
        )
        watch = [
            {
                "instrument_id": event.instrument_id,
                "reason": event.reason,
                "since": event.available_at.isoformat(),
            }
            for event in self.subjects.current_watchlist(as_of=preflight.as_of)
        ]
        watch.sort(key=lambda item: (str(item["instrument_id"]), str(item["since"])))
        plan_text = self._monitor_plan_text(preflight, active)
        fingerprint = content_hash(
            {
                "active": self._material(active),
                "watch": watch,
                "monitor_plan": plan_text,
                "closed": closed,
            }
        )
        if previous.get("fingerprint") == fingerprint:
            # A missing or interrupted readable file can still be repaired from
            # the committed generation; a normal no-change activation writes zero bytes.
            self._publish(previous)
            return {"changed": False, "fingerprint": fingerprint}
        state = {
            "version": 2,
            "fingerprint": fingerprint,
            "active": active,
            "watch": watch,
            "closed": closed,
            "as_of": preflight.as_of.isoformat(),
            "monitor_plan": plan_text,
        }
        self._commit(state)
        return {"changed": True, "fingerprint": fingerprint}

    @staticmethod
    def _material(active: dict[str, Any]) -> dict[str, Any]:
        return {
            key: {
                name: value
                for name, value in item.items()
                if name not in {"source_revision", "last_seen_at"}
            }
            for key, item in active.items()
        }

    def _active(
        self, preflight: InvestorSessionPreflightReceipt, previous: dict[str, Any]
    ) -> dict[str, Any]:
        old = previous.get("active", {})
        result: dict[str, Any] = {}
        for lane in (preflight.context.actual, preflight.context.paper):
            prior_lane = {
                key: value for key, value in old.items() if value["lane"] == lane.lane.value
            }
            if lane.audit_status != "PASS":
                for key, value in prior_lane.items():
                    result[key] = {**value, "coverage": "UNAVAILABLE"}
                continue
            # A missing account identity is not proof its positions were sold.
            for key, value in prior_lane.items():
                if value["account_id"] not in lane.account_ids:
                    result[key] = {**value, "coverage": "UNAVAILABLE"}
            for item in lane.positions:
                if not item.quantity.is_finite() or item.quantity <= 0:
                    raise ValueError("position projection requires a finite positive quantity")
                key = f"{item.lane.value}:{item.account_id}:{item.instrument_id}"
                prior = old.get(key, {})
                events = {
                    event.event_id: {
                        "summary": event.summary,
                        "available_at": event.available_at.isoformat(),
                    }
                    for event in preflight.context.material_events
                    if event.available_at <= preflight.as_of
                    and event.instrument_id in {None, item.instrument_id}
                }
                known_events = {**prior.get("current_events", {}), **events}
                known_events = dict(
                    sorted(
                        known_events.items(), key=lambda pair: (pair[1]["available_at"], pair[0])
                    )[-16:]
                )
                value = item.market_value
                if value is not None and not value.is_finite():
                    raise ValueError("position valuation must be finite")
                cost = str(item.average_cost) if item.average_cost is not None else None
                observed_price = str(value / item.quantity) if value is not None else None
                result[key] = {
                    "lane": item.lane.value,
                    "account_id": item.account_id,
                    "instrument_id": item.instrument_id,
                    "quantity": str(item.quantity),
                    "average_cost": cost,
                    "cost_status": item.cost_status,
                    "market_value": str(value) if value is not None else None,
                    "price_from_valuation": observed_price,
                    "first_seen_at": prior.get("first_seen_at", preflight.as_of.isoformat()),
                    # Historical compatibility: entry_price was a cost anchor,
                    # not an observed market quote. Label it truthfully below.
                    "entry_price": prior.get("entry_price", cost),
                    "entry_snapshot": prior.get("entry_snapshot")
                    or self._entry_snapshot(
                        item.instrument_id,
                        preflight,
                        observed_price=observed_price,
                    ),
                    "current_research": self._formal_research_snapshot(
                        item.instrument_id, preflight.as_of
                    )
                    or prior.get("current_research", {}),
                    "current_market_regime": (
                        preflight.regime.state.value
                        if preflight.regime.available and preflight.regime.state
                        else prior.get("current_market_regime")
                    ),
                    "current_events": known_events,
                    "coverage": "CHECKED",
                    "source_revision": item.source_revision,
                }
        return result

    def _monitor_plan_text(
        self, preflight: InvestorSessionPreflightReceipt, active: dict[str, Any]
    ) -> str:
        from astock.investor_orchestration.proactive_monitoring import ProactiveMonitorPlanner

        config_path = (
            Path(__file__).resolve().parents[3] / "configs/scheduled_investor_tracking_v1.yaml"
        )
        try:
            planner = ProactiveMonitorPlanner(self.subjects, config_path)
            plan = planner.plan(
                preflight,
                retained_subjects=tuple(item["instrument_id"] for item in active.values()),
            )
            if plan is None:
                return "# 主动跟踪计划\n\n当前没有需要跟踪的持仓或正式观察标的。"
            return (
                "# 主动跟踪计划\n\n跟踪对象："
                + "、".join(plan.subjects)
                + "。\n\n建议窗口："
                + "、".join(plan.windows)
                + "。\n\n"
                + plan.prompt
                + "\n\n单次检查分批进行，不会因批次上限遗漏持仓。"
                + "\n\n本页只记录跟踪安排；任务是否启用及实际运行结果，以单独核验记录为准。"
            )
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            return "# 主动跟踪计划\n\n本次未能更新跟踪安排；保留既有持仓，后续激活时重试。"

    def _entry_snapshot(
        self,
        instrument_id: str,
        preflight: InvestorSessionPreflightReceipt,
        *,
        observed_price: str | None,
        snapshot_at: datetime | None = None,
        price_kind: str = "VALUATION",
    ) -> dict[str, object]:
        cutoff = aware_time(snapshot_at or preflight.as_of)
        preflight_at = aware_time(preflight.as_of)
        if cutoff > preflight_at:
            raise ValueError("entry snapshot cannot use a future cutoff")
        research = self._formal_research_snapshot(instrument_id, cutoff)
        same_observation = cutoff == preflight_at
        if research:
            research_status = "RESEARCHED"
        elif same_observation and instrument_id in preflight.context.recommended_instruments:
            research_status = "RECOMMENDED"
        elif same_observation and instrument_id in preflight.context.researched_instruments:
            research_status = "RESEARCHED"
        else:
            research_status = "UNAVAILABLE"
        snapshot: dict[str, object] = {
            "first_observed_price": observed_price,
            "first_observed_price_as_of": cutoff.isoformat() if observed_price else None,
            "first_observed_price_source": price_kind,
            "market_regime": (
                preflight.regime.state.value
                if same_observation and preflight.regime.available and preflight.regime.state
                else None
            ),
            "research_status": research_status,
            "material_events": [
                event.summary
                for event in preflight.context.material_events
                if event.available_at <= cutoff and event.instrument_id in {None, instrument_id}
            ][:5],
            "note": "按可靠成交时点或首次可核实观察时点冻结；不把后来信息倒填进历史。",
        }
        if research:
            snapshot["formal_research"] = research
        return snapshot

    def _formal_research_snapshot(self, instrument_id: str, as_of: datetime) -> dict[str, object]:
        """Project an already-sealed research receipt; never call a provider or model here."""

        cache_key = (normalized_instrument(instrument_id), as_of.isoformat())
        if cache_key in self._research_snapshot_cache:
            return deepcopy(self._research_snapshot_cache[cache_key])
        store = getattr(self.subjects, "store", None)
        if store is None:
            self._research_snapshot_cache[cache_key] = {}
            return {}
        try:
            if self._subject_event_cache is None:
                self._subject_event_cache = tuple(store.subject_events())

            def same_instrument(event: Any) -> bool:
                try:
                    return normalized_instrument(str(event.instrument_id)) == cache_key[0]
                except (AttributeError, TypeError, ValueError):
                    return False

            candidates = sorted(
                (
                    event
                    for event in self._subject_event_cache
                    if event.artifact_id
                    and aware_time(event.available_at) <= as_of
                    and same_instrument(event)
                ),
                key=lambda event: (aware_time(event.available_at), event.event_id),
                reverse=True,
            )
            state = StateStore(store.path)
            objects = ObjectStore(store.path.parent / "objects" / "sha256")
            seen: set[str] = set()
            for event in candidates[:32]:
                artifact_id = str(event.artifact_id)
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                record = state.artifact_record(artifact_id)
                if record is None or record.get("type") != "RecommendationResearchReceipt":
                    continue
                raw = json.loads(objects.get_bytes(str(record["object_hash"])))
                compact = self._compact_research_snapshot(raw, instrument_id, as_of)
                if compact:
                    self._research_snapshot_cache[cache_key] = compact
                    return deepcopy(compact)
        except (
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            TypeError,
            StorageError,
            sqlite3.Error,
        ):
            pass
        self._research_snapshot_cache[cache_key] = {}
        return {}

    @staticmethod
    def _compact_research_snapshot(
        payload: object,
        instrument_id: str,
        cutoff: datetime,
    ) -> dict[str, object]:
        """Read compact public-safe facts from one already-sealed recommendation receipt."""

        if not isinstance(payload, dict):
            return {}
        receipt_as_of = payload.get("as_of")
        if not isinstance(receipt_as_of, str):
            return {}
        try:
            at = datetime.fromisoformat(receipt_as_of)
            target = normalized_instrument(instrument_id)
        except ValueError:
            return {}
        if at.tzinfo is None or at.utcoffset() is None or at > cutoff:
            return {}

        def matching(items: object, *, id_key: str = "instrument_id") -> dict[str, Any] | None:
            if not isinstance(items, list):
                return None
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get(id_key), str):
                    continue
                try:
                    if normalized_instrument(str(item[id_key])) == target:
                        return item
                except ValueError:
                    continue
            return None

        narrative = matching(payload.get("candidate_narratives"))
        fundamental = matching(payload.get("fundamentals"))
        financial = matching(payload.get("financial_quality"))
        governance = matching(payload.get("governance"))
        valuation = matching(payload.get("valuations"))
        ranking = matching(payload.get("candidate_rankings"))
        if not any((narrative, fundamental, financial, governance, valuation, ranking)):
            return {}

        result: dict[str, object] = {"as_of": receipt_as_of}
        if narrative is not None:
            thesis = narrative.get("investment_thesis")
            why_now = narrative.get("why_now")
            if isinstance(thesis, str) and thesis.strip():
                result["investment_thesis"] = thesis.strip()
            if isinstance(why_now, str) and why_now.strip():
                result["why_now"] = why_now.strip()
        if fundamental is not None:
            years = fundamental.get("analyzed_complete_years")
            quarters = fundamental.get("analyzed_quarters")
            ttm = fundamental.get("ttm_reconstructed")
            if isinstance(years, int) and isinstance(quarters, int) and isinstance(ttm, bool):
                result["fundamental"] = f"已复核{years}个完整年度和{quarters}个季度" + (
                    "，并重构最近十二个月数据" if ttm else ""
                )
        industry_id = ranking.get("industry_id") if ranking else None
        industries = payload.get("industries")
        if isinstance(industry_id, str) and isinstance(industries, list):
            for industry in industries:
                if not isinstance(industry, dict) or industry.get("industry_id") != industry_id:
                    continue
                phase = industry.get("cycle_phase")
                competition = industry.get("competitive_intensity")
                parts = [industry_id]
                if isinstance(phase, str) and phase:
                    parts.append(f"周期位置：{phase}")
                if isinstance(competition, str) and competition:
                    parts.append(f"竞争强度：{competition}")
                risks = industry.get("industry_risks")
                if isinstance(risks, list) and risks and isinstance(risks[0], str):
                    parts.append(f"主要行业风险：{risks[0]}")
                result["industry"] = "；".join(parts)
                break
        macro = payload.get("macro")
        if isinstance(macro, dict):
            labels = (
                ("macro_regime", "宏观"),
                ("liquidity_regime", "流动性"),
                ("risk_appetite", "风险偏好"),
                ("policy_bias", "政策取向"),
            )
            parts = [
                f"{label}：{macro[key]}"
                for key, label in labels
                if isinstance(macro.get(key), str) and macro[key]
            ]
            if parts:
                result["macro"] = "；".join(parts)
        if financial is not None:
            parts = []
            score = financial.get("accounting_quality_score")
            opinion = financial.get("audit_opinion")
            if score is not None:
                parts.append(f"会计质量评分{score}/100")
            if isinstance(opinion, str) and opinion:
                parts.append(f"审计意见：{opinion}")
            veto = financial.get("critical_veto")
            if veto is True:
                reasons = financial.get("critical_veto_reasons")
                if isinstance(reasons, list) and reasons and isinstance(reasons[0], str):
                    parts.append(f"存在重大否决项：{reasons[0]}")
            else:
                flags = financial.get("red_flags")
                if isinstance(flags, list) and flags and isinstance(flags[0], str):
                    parts.append(f"主要关注：{flags[0]}")
            if parts:
                result["financial_audit"] = "；".join(parts)
        if governance is not None:
            parts = []
            score = governance.get("governance_score")
            stability = governance.get("management_stability")
            if score is not None:
                parts.append(f"治理评分{score}/100")
            if isinstance(stability, str) and stability:
                parts.append(f"管理层稳定性：{stability}")
            flags = (
                governance.get("critical_veto_reasons")
                if governance.get("critical_veto") is True
                else governance.get("red_flags")
            )
            if isinstance(flags, list) and flags and isinstance(flags[0], str):
                prefix = (
                    "重大治理否决："
                    if governance.get("critical_veto") is True
                    else "主要治理关注："
                )
                parts.append(prefix + flags[0])
            if parts:
                result["governance"] = "；".join(parts)
        if valuation is not None and valuation.get("current_price") is not None:
            result["research_price"] = str(valuation["current_price"])
        return result

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            if any(path.parent.glob("*.md")):
                raise ValueError("missing projection cache; existing documents retained")
            return {}
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("projection cache exceeds bounded document size")
        value = json.loads(path.read_text(encoding="utf-8"))
        return PositionDocumentProjector._validate_state(value)

    @staticmethod
    def _validate_state(value: object, *, ready_to_publish: bool = False) -> dict[str, Any]:
        """Validate only consumed fields; retain unknown additive fields and legacy caches."""
        if not isinstance(value, dict):
            raise ValueError("projection cache must contain an object")
        for key in ("active", "closed"):
            if not isinstance(value.get(key), dict) or any(
                not isinstance(item, dict) for item in value[key].values()
            ):
                raise ValueError("invalid projection cache; existing documents retained")
        as_of = value.get("as_of")
        if as_of is not None:
            if not isinstance(as_of, str):
                raise ValueError("invalid projection timestamp")
            moment = datetime.fromisoformat(as_of)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("projection timestamp must be timezone-aware")
        if ready_to_publish and (as_of is None or not isinstance(value.get("monitor_plan"), str)):
            raise ValueError("document generation is incomplete")
        watch = value.get("watch")
        if not isinstance(watch, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("instrument_id"), str)
            or (item.get("reason") is not None and not isinstance(item["reason"], str))
            for item in watch
        ):
            raise ValueError("invalid watch projection")
        for item in value["active"].values():
            if any(
                not isinstance(item.get(key), str)
                for key in ("lane", "account_id", "instrument_id", "quantity", "first_seen_at")
            ):
                raise ValueError("invalid active projection identity")
            if "average_cost" not in item:
                raise ValueError("active projection omitted its cost availability")
            snapshot = item.get("entry_snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("invalid entry snapshot")
            status = snapshot.get("research_status")
            if status is not None and not isinstance(status, str):
                raise ValueError("invalid research status")
            observed_price = snapshot.get("first_observed_price")
            if observed_price is not None and not isinstance(observed_price, str):
                raise ValueError("invalid first observed price")
            observed_source = snapshot.get("first_observed_price_source")
            if observed_source is not None and observed_source not in {
                "VALUATION",
                "TRADE",
                "TRANSFER",
            }:
                raise ValueError("invalid first observed price source")
            observed_at = snapshot.get("first_observed_price_as_of")
            if observed_at is not None:
                if not isinstance(observed_at, str):
                    raise ValueError("invalid first observed price timestamp")
                observed_moment = datetime.fromisoformat(observed_at)
                if observed_moment.tzinfo is None or observed_moment.utcoffset() is None:
                    raise ValueError("first observed price timestamp must be timezone-aware")
            formal = snapshot.get("formal_research")
            if formal is not None:
                if not isinstance(formal, dict):
                    raise ValueError("invalid formal research snapshot")
                research_at = formal.get("as_of")
                if not isinstance(research_at, str):
                    raise ValueError("formal research snapshot omitted its timestamp")
                research_moment = datetime.fromisoformat(research_at)
                if research_moment.tzinfo is None or research_moment.utcoffset() is None:
                    raise ValueError("formal research timestamp must be timezone-aware")
                for field in (
                    "investment_thesis",
                    "why_now",
                    "fundamental",
                    "industry",
                    "macro",
                    "financial_audit",
                    "governance",
                    "research_price",
                ):
                    if formal.get(field) is not None and not isinstance(formal[field], str):
                        raise ValueError("formal research snapshot contains an invalid field")
            events = snapshot.get("material_events", [])
            if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
                raise ValueError("invalid entry event summaries")
            current = item.get("current_events", {})
            if not isinstance(current, dict):
                raise ValueError("invalid current event summaries")
            for event in current.values():
                if (
                    not isinstance(event, dict)
                    or not isinstance(event.get("summary"), str)
                    or not isinstance(event.get("available_at"), str)
                ):
                    raise ValueError("invalid current event summary")
                available = datetime.fromisoformat(event["available_at"])
                if available.tzinfo is None or available.utcoffset() is None:
                    raise ValueError("event availability must be timezone-aware")
            if item.get("market_value") is not None and "price_from_valuation" not in item:
                raise ValueError("valuation projection is incomplete")
        return value

    def _commit(self, state: dict[str, Any]) -> None:
        # Commit intent first, publish all readable views, then move the pointer.
        # The same state can be replayed after interruption at every write boundary.
        pending = self.root / ".projection.pending.json"
        self._write(
            pending,
            json.dumps(
                {"state": state, "hash": content_hash(state)}, ensure_ascii=False, sort_keys=True
            ),
        )
        self._publish(state)
        self._write(
            self.root / ".projection.json",
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        )
        pending.unlink(missing_ok=True)

    def _recover_pending(self) -> None:
        pending = self.root / ".projection.pending.json"
        if not pending.exists():
            return
        if pending.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("pending document generation is too large")
        envelope = json.loads(pending.read_text(encoding="utf-8"))
        state = envelope.get("state") if isinstance(envelope, dict) else None
        if not isinstance(state, dict) or envelope.get("hash") != content_hash(state):
            raise ValueError("pending document generation failed integrity check")
        self._publish(state)
        self._write(
            self.root / ".projection.json",
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        )
        pending.unlink(missing_ok=True)

    def _publish(self, state: dict[str, Any]) -> None:
        self._validate_state(state, ready_to_publish=True)
        documents = {
            "当前持仓.md": self._render_active(state, state["as_of"]),
            "已结束交易.md": self._render_closed(state["closed"]),
            "主动跟踪计划.md": state["monitor_plan"],
        }
        for name, text in documents.items():
            self._write(self.root / name, text)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        content = text.rstrip() + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return
        try:
            atomic_write_text(path, content)
        except StorageError as exc:
            # Preflight's existing optional-document fallback accepts OSError.
            raise OSError("optional position document write failed") from exc

    @staticmethod
    def _render_active(state: dict[str, Any], as_of: str) -> str:
        lines = ["# 当前持仓与观察", "", f"> 最近内容更新：{as_of}", ""]
        for item in state["active"].values():
            lane = "实盘" if item["lane"] == "ACTUAL" else "模拟盘"
            lines += [
                f"## {item['instrument_id']} · {lane}",
                f"持有 {item['quantity']} 股；单位成本 {item['average_cost'] or '未知'} 元；"
                f"首次记录 {item['first_seen_at']}。",
            ]
            if item.get("coverage") == "UNAVAILABLE":
                lines.append("本次未能完整核实该账户，暂保留上次记录；不视为清仓。")
            if item.get("market_value") is not None:
                lines.append(
                    f"已记录持仓市值 {item['market_value']} 元；按该估值折算每股 "
                    f"{item['price_from_valuation']} 元，时效以原始估值记录为准。"
                )
            else:
                lines.append("当前报价尚未核实；不把成本当作最新市价。")
            if item.get("entry_trade_time"):
                label = "实际首次买入" if item.get("entry_time_kind") == "TRADE" else "转入记录"
                lines.append(
                    f"{label}：{item['entry_trade_time']}；"
                    f"记录价格 {item.get('entry_trade_price') or '未知'} 元。"
                )
            snapshot = item["entry_snapshot"]
            if snapshot.get("first_observed_price"):
                first_price = snapshot["first_observed_price"]
                first_price_at = snapshot["first_observed_price_as_of"]
                if snapshot.get("first_observed_price_source") == "TRADE":
                    lines.append(
                        f"建仓快照按实际买入成交时点 {first_price_at} 冻结，"
                        f"成交价每股 {first_price} 元；后续价格和研究不回填这份快照。"
                    )
                else:
                    lines.append(
                        f"首次记录时按持仓估值折算每股 {first_price} 元（{first_price_at}）；"
                        "这只是当时可核实的观察价，不冒充实际成交价。"
                    )
            else:
                lines.append("首次记录时没有独立可核实的市价；单位成本仍只作为成本事实保存。")
            research = {"RECOMMENDED": "已完成推荐研究", "RESEARCHED": "已有研究记录"}
            lines.append(
                "首次快照："
                + research.get(snapshot.get("research_status"), "尚无可核实的完整研究摘要")
                + "。"
            )
            formal = snapshot.get("formal_research")
            if isinstance(formal, dict):
                lines.append(f"冻结研究时间：{formal.get('as_of', '未知')}。")
                if formal.get("research_price") is not None:
                    lines.append(
                        f"当次正式研究使用的价格为 {formal['research_price']} 元；"
                        "该价格对应研究时点，不替代实际成交记录。"
                    )
                labels = (
                    ("investment_thesis", "投资逻辑"),
                    ("why_now", "时点判断"),
                    ("fundamental", "基本面"),
                    ("industry", "行业"),
                    ("macro", "宏观"),
                    ("financial_audit", "财务审计"),
                    ("governance", "治理"),
                )
                for field, label in labels:
                    value = formal.get(field)
                    if isinstance(value, str) and value:
                        lines.append(f"{label}：{value}。")
            current_research = item.get("current_research")
            if (
                isinstance(current_research, dict)
                and current_research
                and current_research != formal
            ):
                lines.append(f"最新复核（{current_research.get('as_of', '时间未核实')}）：")
                for field, label in (
                    ("investment_thesis", "投资逻辑"),
                    ("why_now", "时点判断"),
                    ("fundamental", "基本面"),
                    ("industry", "行业"),
                    ("macro", "宏观"),
                    ("financial_audit", "财务审计"),
                    ("governance", "治理"),
                ):
                    text = current_research.get(field)
                    if isinstance(text, str) and text:
                        lines.append(f"{label}：{text}。")
            if snapshot.get("material_events"):
                lines.append("首次关注事项：" + "；".join(snapshot["material_events"]) + "。")
            if item.get("current_events"):
                lines.append(
                    "后续记录事项："
                    + "；".join(event["summary"] for event in item["current_events"].values())
                    + "。"
                )
            lines.append("")
        if state["watch"]:
            lines += ["## 待入场观察", ""] + [
                f"{item['instrument_id']}：{item['reason'] or '等待更合适的进入条件'}。"
                for item in state["watch"]
            ]
        if len(lines) == 4:
            lines.append("当前没有已记录的持仓或待入场观察标的。")
        return "\n".join(lines)

    @staticmethod
    def _render_closed(closed: dict[str, Any]) -> str:
        lines = ["# 已结束交易", ""]
        for item in closed.values():
            lines += [
                f"## {item.get('instrument_id', '未知标的')}",
                f"首次记录：{item.get('first_seen_at', '未知')}；"
                f"确认已无持仓的核对时间：{item.get('closed_at', '未知')}。",
                f"实际进入记录时间：{item.get('entry_trade_time', '尚缺可核实的成交记录')}。",
                f"实际退出记录时间：{item.get('exit_trade_time', '尚缺可核实的成交记录')}。",
                "首次记录与核对时间只用于跟踪；没有成交凭证时，不把它们冒充实际买卖时间。",
                f"收益：{item.get('realized_return', '尚未核实')}。",
                "",
            ]
        if len(lines) == 2:
            lines.append("暂无已结束交易。")
        return "\n".join(lines)
