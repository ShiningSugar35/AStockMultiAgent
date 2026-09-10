from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from astock.investor_orchestration.canonical_state import aware_time, normalized_instrument
from astock.investor_orchestration.models import (
    DatePrecision,
    EstimatedCostBasisRange,
    PortfolioLane,
    ProvisionalPositionAssertion,
    ResearchSubjectEvent,
    SubjectEventKind,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


class ResearchSubjectRegistryService:
    def __init__(self, store: InvestorOrchestrationStore) -> None:
        self.store = store

    def append(
        self,
        *,
        instrument_id: str,
        event_type: SubjectEventKind,
        lane: PortfolioLane = PortfolioLane.RESEARCH,
        request_id: str | None = None,
        artifact_id: str | None = None,
        reason: str | None = None,
        available_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> ResearchSubjectEvent:
        at = available_at or utc_now()
        key_body = {
            "instrument_id": instrument_id,
            "event_type": event_type,
            "lane": lane,
            "request_id": request_id,
            "artifact_id": artifact_id,
            "reason": reason,
            "available_at": at,
            "metadata": metadata or {},
        }
        key = idempotency_key or content_hash(key_body)
        event = ResearchSubjectEvent(
            event_id=f"subject-{uuid.uuid5(uuid.NAMESPACE_URL, key)}",
            instrument_id=instrument_id,
            event_type=event_type,
            lane=lane,
            available_at=at,
            request_id=request_id,
            artifact_id=artifact_id,
            reason=reason,
            idempotency_key=key,
            metadata=dict(metadata or {}),
        )
        return self.store.append_subject_event(event)

    def add_watchlist(
        self,
        instrument_id: str,
        *,
        reason: str,
        request_id: str | None = None,
        artifact_id: str | None = None,
        available_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> ResearchSubjectEvent:
        return self.append(
            instrument_id=instrument_id,
            event_type=SubjectEventKind.WATCHLIST_ADDED,
            lane=PortfolioLane.WATCHLIST,
            request_id=request_id,
            artifact_id=artifact_id,
            reason=reason,
            available_at=available_at,
            idempotency_key=idempotency_key,
        )

    def enroll_from_analysis(
        self,
        instrument_id: str,
        *,
        formal_readiness: str,
        timing_status: str,
        reason: str,
        request_id: str | None = None,
        artifact_id: str | None = None,
        available_at: datetime | None = None,
    ) -> ResearchSubjectEvent | None:
        """Enroll only qualified targets whose timing is explicitly not ready."""

        qualified = {
            "WATCH",
            "APPROVE_SIMULATION",
            "RESEARCH_READY",
            "QUALIFIED",
        }
        waiting = {
            "WAIT",
            "ATTRACTIVE_WAIT",
            "TIMING_NOT_READY",
            "ENTRY_NOT_READY",
        }
        if formal_readiness.upper() not in qualified or timing_status.upper() not in waiting:
            return None
        return self.add_watchlist(
            instrument_id,
            reason=reason,
            request_id=request_id,
            artifact_id=artifact_id,
            available_at=available_at,
            idempotency_key=content_hash(
                {
                    "instrument_id": instrument_id,
                    "formal_readiness": formal_readiness.upper(),
                    "timing_status": timing_status.upper(),
                    "artifact_id": artifact_id,
                }
            ),
        )

    def remove_watchlist(
        self,
        instrument_id: str,
        *,
        reason: str,
        request_id: str | None = None,
        available_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> ResearchSubjectEvent:
        return self.append(
            instrument_id=instrument_id,
            event_type=SubjectEventKind.WATCHLIST_REMOVED,
            lane=PortfolioLane.WATCHLIST,
            request_id=request_id,
            reason=reason,
            available_at=available_at,
            idempotency_key=idempotency_key,
        )

    def current_watchlist(
        self, *, as_of: datetime | None = None
    ) -> tuple[ResearchSubjectEvent, ...]:
        """Project membership independently of research and economic account events.

        Normalize explicit market aliases only for identity comparison; preserve
        the original event and its immutable provenance in the returned view.
        """
        cutoff = aware_time(as_of or utc_now())
        adding = {
            SubjectEventKind.WATCHLIST_ADDED,
            SubjectEventKind.WATCHLIST_UPDATED,
            SubjectEventKind.MONITOR_ENROLLED,
        }
        removing = {
            SubjectEventKind.WATCHLIST_REMOVED,
            SubjectEventKind.MONITOR_REMOVED,
            SubjectEventKind.REJECTED,
            SubjectEventKind.EXITED,
        }
        events = self.store.subject_events(
            event_types=tuple(sorted(kind.value for kind in adding | removing))
        )
        latest: dict[str, ResearchSubjectEvent] = {}
        for event in sorted(events, key=lambda item: aware_time(item.available_at)):
            if event.lane is not PortfolioLane.WATCHLIST or aware_time(event.available_at) > cutoff:
                continue
            latest[normalized_instrument(event.instrument_id)] = event
        return tuple(latest[key] for key in sorted(latest) if latest[key].event_type in adding)

    def record_provisional_position(
        self,
        *,
        account_id: str,
        lane: PortfolioLane,
        instrument_id: str,
        quantity: Decimal,
        asserted_at: datetime,
        date_precision: DatePrecision,
        source_text: str,
        idempotency_key: str,
    ) -> ProvisionalPositionAssertion:
        assertion = ProvisionalPositionAssertion(
            assertion_id=f"provisional-{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)}",
            account_id=account_id,
            lane=lane,
            instrument_id=instrument_id,
            quantity=quantity,
            asserted_at=asserted_at,
            date_precision=date_precision,
            source_text=source_text,
            idempotency_key=idempotency_key,
        )
        return self.store.save_provisional_position(assertion)

    def attach_estimated_cost(
        self,
        assertion: ProvisionalPositionAssertion,
        *,
        low: Decimal,
        high: Decimal,
        method: str,
        as_of: datetime,
        source_artifact_id: str,
    ) -> EstimatedCostBasisRange:
        body = {
            "assertion_id": assertion.assertion_id,
            "low": low,
            "high": high,
            "method": method,
            "as_of": as_of,
            "source_artifact_id": source_artifact_id,
        }
        estimate = EstimatedCostBasisRange(
            estimate_id=f"estimated-cost-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
            assertion_id=assertion.assertion_id,
            low=low,
            high=high,
            method=method,
            as_of=as_of,
            source_artifact_id=source_artifact_id,
        )
        self.store.save_estimated_cost(estimate)
        return estimate
