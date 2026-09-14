"""Proactive entry-watch membership from sealed research, without trading authority."""

from __future__ import annotations

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.investor_orchestration.models import PortfolioLane, SubjectEventKind
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.schemas.entry_quality import EntryQualityState
from astock.schemas.full_research import RecommendationResearchReceipt

_WAIT_STATES = {
    EntryQualityState.EXTENDED: "价格相对近期区间偏高，等待更合适的入场条件",
    EntryQualityState.FALLING_KNIFE_RISK: "下跌尚未稳定，跟踪经营假设及企稳条件",
    EntryQualityState.INSUFFICIENT_HISTORY: "可核实的价格历史不足，暂作观察",
}
_REMOVALS = {
    SubjectEventKind.WATCHLIST_REMOVED,
    SubjectEventKind.MONITOR_REMOVED,
    SubjectEventKind.REJECTED,
    SubjectEventKind.EXITED,
}


def enroll_waiting_entries(
    receipt: RecommendationResearchReceipt,
    registry: ResearchSubjectRegistryService,
    state: StateStore,
    objects: ObjectStore,
) -> tuple[str, ...]:
    """Called only after canonical sealing; verify registration and preserve user opt-outs."""
    if not receipt.publication.formal_recommendation_allowed:
        return ()
    record = state.artifact_record(receipt.receipt_id)
    if record is None or record["type"] != "RecommendationResearchReceipt":
        raise ValueError("waiting-entry enrollment requires a registered research receipt")
    digest = str(record["object_hash"])
    if not objects.verify(digest):
        raise ValueError("waiting-entry research object is unavailable")
    stored = RecommendationResearchReceipt.model_validate_json(objects.get_bytes(digest))
    if stored.model_dump(mode="json") != receipt.model_dump(mode="json"):
        raise ValueError("waiting-entry research identity differs from the frozen receipt")
    # Build one membership history snapshot, rather than reading the store for every candidate.
    previous = registry.store.subject_events()
    watch = {normalized_instrument(item.instrument_id) for item in registry.current_watchlist()}
    enrolled: list[str] = []
    for candidate in receipt.candidate_rankings:
        entry_state = candidate.entry_quality_state
        reason = _WAIT_STATES.get(entry_state) if entry_state is not None else None
        if not candidate.eligible or reason is None:
            continue
        instrument = normalized_instrument(candidate.instrument_id)
        if instrument in watch:
            continue
        blocked = any(
            normalized_instrument(event.instrument_id) == instrument
            and event.event_type in _REMOVALS
            and (event.lane is PortfolioLane.WATCHLIST or event.available_at >= receipt.as_of)
            for event in previous
        )
        if blocked:
            continue
        event = registry.enroll_from_analysis(
            instrument,
            formal_readiness="QUALIFIED",
            timing_status="ENTRY_NOT_READY",
            reason=reason,
            request_id=receipt.request_id,
            artifact_id=receipt.receipt_id,
            available_at=receipt.as_of,
        )
        if event is not None:
            enrolled.append(event.instrument_id)
            watch.add(instrument)
    return tuple(enrolled)
