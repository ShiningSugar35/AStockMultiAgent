from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from astock.investor_orchestration.canonical_state import (
    CanonicalStateProjectionReader,
    aware_time,
    normalized_instrument,
)
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    LaneSnapshot,
    PortfolioLane,
    RegimeAvailability,
    UnifiedPortfolioContext,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now

# Preserve the import surface, not the unsafe table-name guessing implementation.
ExistingStateProjectionReader = CanonicalStateProjectionReader


@dataclass(frozen=True)
class _CachedPreflightState:
    context: UnifiedPortfolioContext
    regime: RegimeAvailability
    freshness: str
    coverage_warnings: tuple[str, ...]
    material_holding_change_present: bool
    valid_until: datetime | None = None


class InvestorSessionPreflightService:
    def __init__(
        self,
        store: InvestorOrchestrationStore,
        reader: CanonicalStateProjectionReader | None = None,
    ) -> None:
        self.store = store
        self.reader = reader or CanonicalStateProjectionReader(store)
        self._cache_lock = threading.Lock()
        self._state_cache: dict[str, _CachedPreflightState] = {}

    def build(
        self,
        request: InvestorRequestEnvelope,
        *,
        force_refresh: bool = False,
    ) -> InvestorSessionPreflightReceipt:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if request.decision_time is not None:
            from astock.investor_orchestration.decision_freeze import DecisionFreezeService

            DecisionFreezeService(self.store).verify(request)
        as_of = aware_time(request.evidence_cutoff)
        if as_of > utc_now():
            raise ValueError("preflight cannot certify a future request time")
        # The revision vector and all economic/metadata reads share one SQLite
        # snapshot. Receipt persistence happens only after this read transaction.
        with self._cache_lock, self.store.transaction(read_only=True):
            revisions = self.reader.revision_vector(
                {
                    "subjects": ("research_subject_events",),
                    "regime": ("market_regime_snapshots_v2",),
                }
            )
            cache_key = content_hash(revisions)
            cached_state = None if force_refresh else self._state_cache.get(cache_key)
            if cached_state is not None and (
                as_of < cached_state.context.as_of
                or (cached_state.valid_until is not None and as_of >= cached_state.valid_until)
            ):
                cached_state = None
            if cached_state is None:
                cached_state = self._build_state(as_of=as_of, revisions=revisions)
                self._state_cache[cache_key] = cached_state
                while len(self._state_cache) > 64:
                    self._state_cache.pop(next(iter(self._state_cache)))
                built_from_cache = False
            else:
                built_from_cache = True
        state = self._state_for_request(cached_state, as_of)
        receipt_body = {
            "request_id": request.request_id,
            "normalized_intent": request.normalized_intent,
            "as_of": as_of,
            "source_revision_vector": revisions,
            "context": state.context,
            "regime": state.regime,
            "freshness": state.freshness,
            "coverage_warnings": state.coverage_warnings,
            "material_holding_change_present": state.material_holding_change_present,
        }
        receipt_hash = content_hash(receipt_body)
        receipt = InvestorSessionPreflightReceipt(
            receipt_id=f"preflight-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
            receipt_hash=receipt_hash,
            built_from_cache=built_from_cache,
            **receipt_body,
        )
        persisted = self.store.save_or_get_preflight(receipt)
        return persisted.model_copy(update={"built_from_cache": built_from_cache}, deep=True)

    def _build_state(
        self,
        *,
        as_of: datetime,
        revisions: Mapping[str, str],
    ) -> _CachedPreflightState:
        health_ok, health_reason = self.reader.bounded_health_check()
        actual = self.reader.lane_snapshot(
            PortfolioLane.ACTUAL,
            revisions["actual"],
            health_ok=health_ok,
            health_reason=health_reason,
            as_of=as_of,
        )
        paper = self.reader.lane_snapshot(
            PortfolioLane.PAPER,
            revisions["paper"],
            health_ok=health_ok,
            health_reason=health_reason,
            as_of=as_of,
        )
        events = self.reader.material_events(revisions["monitor"], as_of=as_of)
        subject_events = tuple(
            event
            for event in self.store.subject_events()
            if aware_time(event.available_at) <= as_of
        )
        researched = sorted(
            {
                event.instrument_id
                for event in subject_events
                if event.event_type.value in {"RESEARCHED", "RECOMMENDED"}
            }
        )
        recommended = sorted(
            {
                event.instrument_id
                for event in subject_events
                if event.event_type.value == "RECOMMENDED"
            }
        )
        context = UnifiedPortfolioContext(
            as_of=as_of,
            actual=actual,
            paper=paper,
            material_events=events,
            researched_instruments=tuple(researched),
            recommended_instruments=tuple(recommended),
            economic_duplicate_warnings=self._economic_duplicate_warnings(actual, paper),
            aggregate_revision=content_hash(revisions),
        )
        snapshot = self.store.latest_valid_regime(as_of)
        if snapshot is None or not (
            snapshot.as_of <= as_of and snapshot.valid_from <= as_of < snapshot.expires_at
        ):
            regime = RegimeAvailability(available=False, reason="NO_VALID_MARKET_REGIME_SNAPSHOT")
        else:
            regime = RegimeAvailability(
                snapshot_id=snapshot.snapshot_id,
                state=snapshot.selected_state,
                valid_from=snapshot.valid_from,
                expires_at=snapshot.expires_at,
                age_seconds=max(0, int((as_of - snapshot.as_of).total_seconds())),
                available=True,
            )
        warnings = tuple(actual.warnings + paper.warnings)
        held = {
            normalized_instrument(item.instrument_id)
            for item in (*actual.positions, *paper.positions)
        }
        high_events = tuple(
            event
            for event in events
            if event.severity in {"HIGH", "CRITICAL"}
            and (event.instrument_id is None or normalized_instrument(event.instrument_id) in held)
        )
        healthy = health_ok and actual.audit_status == "PASS" and paper.audit_status == "PASS"
        return _CachedPreflightState(
            context=context,
            regime=regime,
            freshness="FRESH" if healthy else "DEGRADED",
            coverage_warnings=warnings,
            material_holding_change_present=bool(high_events and held),
            valid_until=self.reader.next_visibility_change(as_of),
        )

    @staticmethod
    def _state_for_request(state: _CachedPreflightState, as_of: datetime) -> _CachedPreflightState:
        # Detached nested values keep caller-side dict mutation out of the cache.
        context = state.context.model_copy(update={"as_of": as_of}, deep=True)
        regime = state.regime.model_copy(deep=True)
        if regime.available and regime.age_seconds is not None:
            elapsed = int((as_of - state.context.as_of).total_seconds())
            regime = regime.model_copy(update={"age_seconds": max(0, regime.age_seconds + elapsed)})
        return _CachedPreflightState(
            context=context,
            regime=regime,
            freshness=state.freshness,
            coverage_warnings=state.coverage_warnings,
            material_holding_change_present=state.material_holding_change_present,
            valid_until=state.valid_until,
        )

    @staticmethod
    def _economic_duplicate_warnings(actual: LaneSnapshot, paper: LaneSnapshot) -> tuple[str, ...]:
        actual_keys = {
            (normalized_instrument(item.instrument_id), item.quantity) for item in actual.positions
        }
        paper_keys = {
            (normalized_instrument(item.instrument_id), item.quantity) for item in paper.positions
        }
        duplicates = sorted(actual_keys & paper_keys, key=lambda item: (item[0], item[1]))
        return tuple(
            f"SIMILAR_CROSS_LANE_EXPOSURE:{instrument}:{quantity}"
            for instrument, quantity in duplicates
        )
