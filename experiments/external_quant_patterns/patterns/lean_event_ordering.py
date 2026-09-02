"""LEAN event ordering pattern benchmark.

Simulates LEAN's event-driven backtest ordering (handle_bar → match → settle → accounting)
and compares it against AStockMultiAgent's stricter event pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from experiments.external_quant_patterns.fixtures.sample_data import MockEvent


@dataclass(frozen=True)
class EventOrderingEntry:
    """Single event in an ordering chain."""
    event_id: str
    event_type: str
    sequence_index: int
    timestamp: str
    symbol: str
    side: str
    object_hash: str


@dataclass(frozen=True)
class EventOrderingReport:
    """Report from an event ordering comparison."""
    entries: tuple[EventOrderingEntry, ...]
    total_events: int
    ordering_name: str
    ordering_description: str
    violates_astock_strict_ordering: bool
    object_hash: str


@dataclass(frozen=True)
class OrderingComparisonResult:
    """Quantitative comparison of two event ordering approaches."""
    lean_events: int
    astock_events: int
    lean_ordering: str
    astock_ordering: str
    lean_violates_astock: bool
    overlap_description: str
    marginal_value: str
    recommendation: str


def _hash_content(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def build_lean_event_entries(events: list[MockEvent]) -> list[EventOrderingEntry]:
    """Build modeled external-order entries from synthetic events."""
    entries: list[EventOrderingEntry] = []
    for idx, event in enumerate(events):
        entries.append(EventOrderingEntry(
            event_id=event.event_id,
            event_type=event.event_type,
            sequence_index=idx,
            timestamp=event.timestamp,
            symbol=event.symbol,
            side=event.side,
            object_hash=_hash_content(f"lean-{event.event_id}"),
        ))
    return entries


def build_lean_ordering_report(events: list[MockEvent]) -> EventOrderingReport:
    """Aggregate modeled external events into an ordering report."""
    entries = build_lean_event_entries(events)
    return EventOrderingReport(
        entries=tuple(entries),
        total_events=len(entries),
        ordering_name="MODELED_EXTERNAL_EVENT_CHAIN",
        ordering_description="handle_bar → match → settle → accounting",
        violates_astock_strict_ordering=False,
        object_hash=_hash_content("lean-ordering-report"),
    )


def build_astock_ordering_entries(events: list[MockEvent]) -> list[EventOrderingEntry]:
    """Build AStockMultiAgent-style ordering entries from mock events."""
    entries: list[EventOrderingEntry] = []
    for idx, event in enumerate(events):
        entries.append(EventOrderingEntry(
            event_id=event.event_id,
            event_type=event.event_type,
            sequence_index=idx,
            timestamp=event.timestamp,
            symbol=event.symbol,
            side=event.side,
            object_hash=_hash_content(f"astock-{event.event_id}"),
        ))
    return entries


def build_astock_ordering_report(events: list[MockEvent]) -> EventOrderingReport:
    """Aggregate AStockMultiAgent-style events into an ordering report."""
    entries = build_astock_ordering_entries(events)
    return EventOrderingReport(
        entries=tuple(entries),
        total_events=len(entries),
        ordering_name="AStockMultiAgent",
        ordering_description=(
            "CLASSIFICATION → COMMITTEE_PROTOCOL → CLASSIFIED_PROTOCOL → "
            "EXECUTION_PREPARE → EXECUTION_CONFIRM → LEDGER_FILL"
        ),
        violates_astock_strict_ordering=False,
        object_hash=_hash_content("astock-ordering-report"),
    )


def _check_lean_violates_astock(
    lean_events: list[MockEvent],
    astock_events: list[MockEvent],
) -> bool:
    """Check whether the modeled fixture omits required AStock governance stages."""
    lean_types = [e.event_type for e in lean_events]
    astock_types = [e.event_type for e in astock_events]

    lean_has_classification = "CLASSIFICATION" in lean_types
    lean_has_committee = "COMMITTEE_PROTOCOL" in lean_types
    lean_has_ledger_last = lean_types[-1] == "LEDGER_FILL" if lean_types else False

    astock_has_classification = "CLASSIFICATION" in astock_types
    astock_has_committee = "COMMITTEE_PROTOCOL" in astock_types
    astock_has_ledger_last = astock_types[-1] == "LEDGER_FILL" if astock_types else False

    if astock_has_classification and not lean_has_classification:
        return True
    if astock_has_committee and not lean_has_committee:
        return True
    if astock_has_ledger_last and not lean_has_ledger_last:
        return True
    return False


def compare_event_orderings(
    lean_events: list[MockEvent],
    astock_events: list[MockEvent],
) -> OrderingComparisonResult:
    """Compare the modeled external ordering fixture with AStock ordering."""
    lean_violates = _check_lean_violates_astock(lean_events, astock_events)

    lean_types = [e.event_type for e in lean_events]
    astock_types = [e.event_type for e in astock_events]

    return OrderingComparisonResult(
        lean_events=len(lean_events),
        astock_events=len(astock_events),
        lean_ordering=" → ".join(lean_types),
        astock_ordering=" → ".join(astock_types),
        lean_violates_astock=lean_violates,
        overlap_description=(
            "The modeled external chain contains market/order/fill/settlement stages, "
            "while the AStock fixture also requires classification, committee protocol, "
            "confirmation, and ledger stages."
        ),
        marginal_value=(
            "This simplified modeled chain omits governance stages required by AStock, "
            "so the fixture provides no evidence for replacing the current ordering "
            "contract. This result is not a quality comparison of the full LEAN engine. "
            "Selected robustness ideas can be evaluated separately under model-risk governance."
        ),
        recommendation="REJECT_ORDERING",
    )
