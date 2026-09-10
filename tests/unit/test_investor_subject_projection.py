"""Watchlist membership is a separate, time-bounded event projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from astock.investor_orchestration.models import PortfolioLane, ScheduledDomain, SubjectEventKind
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService


@pytest.fixture
def registry(tmp_path: Path) -> ResearchSubjectRegistryService:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    return ResearchSubjectRegistryService(store)


@pytest.mark.parametrize(
    "kind",
    [
        SubjectEventKind.MENTIONED,
        SubjectEventKind.RESEARCHED,
        SubjectEventKind.HELD_ACTUAL,
        SubjectEventKind.EXITED,
    ],
)
def test_unrelated_subject_events_cannot_remove_watchlist_membership(
    registry: ResearchSubjectRegistryService,
    kind: SubjectEventKind,
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=5)
    added = registry.add_watchlist("600519.XSHG", reason="recorded membership", available_at=at)
    registry.append(
        instrument_id="600519.XSHG",
        event_type=kind,
        lane=PortfolioLane.ACTUAL,
        available_at=at + timedelta(seconds=1),
    )
    assert registry.current_watchlist() == (added,)


def test_membership_removal_resolves_explicit_market_aliases(
    registry: ResearchSubjectRegistryService,
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=5)
    registry.add_watchlist("600519.XSHG", reason="recorded add", available_at=at)
    registry.remove_watchlist(
        "XSHG:600519", reason="recorded removal", available_at=at + timedelta(seconds=1)
    )
    assert registry.current_watchlist() == ()


def test_watchlist_projection_can_reconstruct_before_a_later_removal(
    registry: ResearchSubjectRegistryService,
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=5)
    added = registry.add_watchlist("600519.XSHG", reason="recorded add", available_at=at)
    registry.remove_watchlist(
        "600519.XSHG", reason="recorded removal", available_at=at + timedelta(minutes=1)
    )
    assert registry.current_watchlist(as_of=at + timedelta(seconds=30)) == (added,)
    assert registry.current_watchlist() == ()


def test_scheduled_subjects_use_the_same_time_bounded_membership_projection(
    registry: ResearchSubjectRegistryService,
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=5)
    added = registry.add_watchlist("600519.XSHG", reason="recorded add", available_at=at)
    registry.append(
        instrument_id=added.instrument_id,
        event_type=SubjectEventKind.MENTIONED,
        available_at=at + timedelta(seconds=5),
    )
    registry.remove_watchlist(
        added.instrument_id, reason="recorded later removal", available_at=at + timedelta(minutes=1)
    )
    preflight = SimpleNamespace(
        as_of=at + timedelta(seconds=30),
        context=SimpleNamespace(
            paper=SimpleNamespace(positions=()), actual=SimpleNamespace(positions=())
        ),
    )
    service = ScheduledResearchService(
        registry.store, InvestorSessionPreflightService(registry.store)
    )
    subjects = service._subjects_by_domain(preflight)
    assert subjects[ScheduledDomain.WATCHLIST] == (added.instrument_id,)
    assert subjects[ScheduledDomain.ACTUAL_HOLDING] == ()
    assert subjects[ScheduledDomain.PAPER_HOLDING] == ()
