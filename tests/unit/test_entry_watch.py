from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.entry_watch import enroll_waiting_entries
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.schemas.entry_quality import EntryQualityState
from tests.unit.test_full_research_recommendation import _build_receipt, _request


def _fixture(tmp_path: Path, *, waiting: bool = False):
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = FullResearchRecommendationService(store=store)
    import tests.unit.test_full_research_recommendation as fixtures

    original = fixtures._candidate

    def candidate(*args, **kwargs):
        value = original(*args, **kwargs)
        return value.model_copy(update={"entry_quality_state": EntryQualityState.EXTENDED})

    with pytest.MonkeyPatch.context() as patch:
        if waiting:
            patch.setattr(fixtures, "_candidate", candidate)
        receipt = _build_receipt(service, _request("推荐现在可以买的股票"))
    return store, service, receipt


def test_sealing_formal_research_calls_waiting_entry_enrollment(
    tmp_path: Path, monkeypatch
) -> None:
    import astock.investor_orchestration.entry_watch as module

    observed = []
    original = module.enroll_waiting_entries

    def capture(*args):
        observed.append(args[0].receipt_id)
        return original(*args)

    monkeypatch.setattr(module, "enroll_waiting_entries", capture)
    _, _, receipt = _fixture(tmp_path)
    assert observed == [receipt.receipt_id]


def test_qualified_waiting_entry_enrolls_without_user_prompt_and_deduplicates(
    tmp_path: Path,
) -> None:
    store, _, revised = _fixture(tmp_path, waiting=True)
    registry = ResearchSubjectRegistryService(store)
    events = registry.current_watchlist()
    assert any("入场条件" in (item.reason or "") for item in registry.store.subject_events())
    assert events
    count = len(registry.store.subject_events())
    assert (
        enroll_waiting_entries(
            revised,
            registry,
            StateStore(store.path),
            ObjectStore(store.path.parent / "objects" / "sha256"),
        )
        == ()
    )
    assert len(registry.store.subject_events()) == count


def test_old_receipt_cannot_readd_explicitly_removed_entry_watch(tmp_path: Path) -> None:
    store, service, revised = _fixture(tmp_path, waiting=True)
    registry = ResearchSubjectRegistryService(store)
    instrument = revised.candidate_rankings[0].instrument_id
    registry.remove_watchlist(
        instrument, reason="用户不再关注", available_at=revised.as_of + timedelta(minutes=1)
    )
    added = enroll_waiting_entries(
        revised,
        registry,
        StateStore(store.path),
        ObjectStore(store.path.parent / "objects" / "sha256"),
    )
    assert added == ()
    assert not registry.current_watchlist()


def test_unregistered_waiting_receipt_is_not_admitted(tmp_path: Path) -> None:
    store, _, receipt = _fixture(tmp_path)
    invented = receipt.model_copy(update={"receipt_id": "unregistered"})
    with pytest.raises(ValueError, match="registered"):
        enroll_waiting_entries(
            invented,
            ResearchSubjectRegistryService(store),
            StateStore(store.path),
            ObjectStore(store.path.parent / "objects"),
        )


def test_ineligible_research_never_enrolls_waiting_candidates(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = FullResearchRecommendationService(store=store)
    receipt = _build_receipt(service, _request("推荐现在可以买的股票"), financial_veto=True)
    assert all(not item.eligible for item in receipt.candidate_rankings)
    assert not ResearchSubjectRegistryService(store).current_watchlist()
