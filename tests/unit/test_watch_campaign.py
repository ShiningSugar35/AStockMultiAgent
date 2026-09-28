from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import astock.investor_orchestration.watch_campaign as watch_campaign_module
from astock.investor_orchestration.run_ownership import ScheduledRunInProgress
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.watch_campaign import WatchCampaignService


@pytest.fixture
def campaign(tmp_path: Path) -> WatchCampaignService:
    database = tmp_path / "runtime/state.sqlite"
    store = InvestorOrchestrationStore(database)
    store.initialize()
    service = WatchCampaignService(database, "fixture-campaign")
    service.initialize(["XSHG:603317", "XSHE:002595"])
    return service


def test_prebound_continuation_is_inherited_by_new_round(
    campaign: WatchCampaignService,
) -> None:
    campaign.bind("continuation", "hourly-watcher")
    started = campaign.begin("am", "2026-09-28", "owner")
    assert started.child_task_id == "hourly-watcher"


def test_watchlist_idempotence_and_no_fake_positions(
    campaign: WatchCampaignService,
) -> None:
    before = campaign.subjects.store.subject_events()
    campaign.initialize(["XSHG:603317", "XSHE:002595"])
    assert len(campaign.subjects.store.subject_events()) == len(before)
    assert campaign.members() == ["XSHE:002595", "XSHG:603317"]
    assert not campaign.status()["paper_ledger_write_allowed"]
    assert all(event.lane.value == "WATCHLIST" for event in before)
    with pytest.raises(ValueError, match="differs"):
        campaign.initialize(["XSHG:600315"])


def test_owner_retry_does_not_reset_deadline_and_live_owner_blocks(
    campaign: WatchCampaignService,
) -> None:
    now = datetime.now(UTC)
    first = campaign.begin("pm", "2026-09-28", "owner-1", now=now)
    retry = campaign.begin(
        "pm",
        "2026-09-28",
        "owner-1",
        now=now + timedelta(minutes=34),
    )
    assert retry.deadline_at == first.deadline_at
    assert retry.invocation_started_at == first.invocation_started_at
    with pytest.raises(ScheduledRunInProgress):
        campaign.begin(
            "pm",
            "2026-09-28",
            "owner-2",
            now=now + timedelta(minutes=34),
        )
    next_activation = campaign.begin(
        "pm",
        "2026-09-29",
        "owner-2",
        now=now + timedelta(minutes=35),
    )
    assert next_activation.round_id == first.round_id
    assert next_activation.owner == "owner-2"


def test_partial_child_resumes_same_round_and_checkpoints_stay_bounded(
    campaign: WatchCampaignService,
) -> None:
    first = campaign.begin("pm", "2026-09-28", "one")
    partial = campaign.checkpoint(
        "pm",
        "one",
        {
            "status": "PARTIAL",
            "completed_units": 9,
            "total_units": 10,
            "checkpoint": "9 verified, 1 pending",
            "child_task_id": "opaque-child",
        },
    )
    assert not partial.owner
    resumed = campaign.begin("pm", "2026-09-29", "two")
    assert resumed.round_id == first.round_id
    assert resumed.completed_units == 9
    with pytest.raises(ScheduledRunInProgress):
        campaign.checkpoint("pm", "one", {"status": "COMPLETE"})
    with pytest.raises(ValueError, match="incomplete"):
        campaign.checkpoint("pm", "two", {"status": "COMPLETE"})
    campaign.checkpoint(
        "pm",
        "two",
        {"completed_units": 10, "status": "COMPLETE"},
    )
    duplicate = campaign.begin("pm", "2026-09-28", "three")
    assert duplicate.status == "COMPLETE"
    new_day = campaign.begin("pm", "2026-09-29", "four")
    assert new_day.round_id != first.round_id
    assert len(campaign.load().rounds) == 1


def _official_q3_row(
    instrument_id: str,
    *,
    lineage: str = "CNINFO_EXHAUSTIVE_ENUMERATION",
) -> dict[str, object]:
    return {
        "release_id": f"release-{instrument_id}",
        "instrument_id": instrument_id,
        "status": "CERTIFIED",
        "manifest_schema_version": "financial-source-release-v2",
        "official_lineage_kind": lineage,
        "official_document_id": f"document-{instrument_id}",
        "official_snapshot_id": f"snapshot-{instrument_id}",
    }


def _patch_q3_releases(
    monkeypatch: pytest.MonkeyPatch,
    members: list[str],
    *,
    lineage: str = "CNINFO_EXHAUSTIVE_ENUMERATION",
) -> None:
    by_company = {
        instrument.split(":", 1)[1]: _official_q3_row(instrument, lineage=lineage)
        for instrument in members
    }

    def fake_get(_self, company_id: str, period_end: str, period_type: str):
        assert period_end == "2026-09-30"
        assert period_type == "QUARTERLY"
        return by_company.get(company_id)

    monkeypatch.setattr(
        watch_campaign_module.FinancialSourceReleaseRepository,
        "get",
        fake_get,
    )


def _patch_receipt(
    monkeypatch: pytest.MonkeyPatch,
    members: list[str],
    positions: list[str],
    *,
    formal: bool = True,
    verification_status: str = "PASS",
) -> None:
    receipt = SimpleNamespace(
        candidate_universe=tuple(members),
        portfolio=SimpleNamespace(
            positions=tuple(SimpleNamespace(instrument_id=value) for value in positions)
        ),
        publication=SimpleNamespace(
            status="PUBLISH" if formal else "BLOCKED",
            formal_recommendation_allowed=formal,
        ),
    )

    def fake_load(_self, _receipt_id: str):
        return receipt, {"status": verification_status}

    monkeypatch.setattr(
        watch_campaign_module.FullResearchRecommendationService,
        "load_and_replay",
        fake_load,
    )


def test_finalization_requires_all_official_q3_releases(
    campaign: WatchCampaignService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = campaign.members()
    _patch_q3_releases(monkeypatch, members, lineage="RECORDED_EXACT_ITEM_FIXTURE")
    readiness = campaign.finalization_readiness("RecommendationResearchReceipt:test")
    assert not readiness["q3_ready"]
    assert readiness["missing_q3"] == members
    with pytest.raises(ValueError, match="Q3_RELEASES_INCOMPLETE"):
        campaign.finalize("RecommendationResearchReceipt:test")


def test_finalization_rejects_nonformal_or_unbounded_portfolio(
    campaign: WatchCampaignService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = campaign.members()
    _patch_q3_releases(monkeypatch, members)
    _patch_receipt(monkeypatch, members, [], formal=True)
    readiness = campaign.finalization_readiness("RecommendationResearchReceipt:test")
    assert readiness["q3_ready"]
    assert not readiness["portfolio_ready"]
    with pytest.raises(ValueError, match="FORMAL_PORTFOLIO_NOT_ADMITTED"):
        campaign.finalize("RecommendationResearchReceipt:test")


def test_finalization_transitions_only_with_verified_q3_and_formal_receipt(
    campaign: WatchCampaignService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = campaign.members()
    _patch_q3_releases(monkeypatch, members)
    _patch_receipt(monkeypatch, members, [members[0]])
    receipt_id = "RecommendationResearchReceipt:formal"
    readiness = campaign.finalization_readiness(receipt_id)
    assert readiness["ready"]
    finalized = campaign.finalize(receipt_id)
    assert finalized.lifecycle == "PORTFOLIO_MONITORING"
    assert finalized.final_recommendation_receipt_id == receipt_id
    assert set(finalized.q3_release_ids) == set(members)
    assert campaign.finalize(receipt_id) == finalized
    with pytest.raises(ValueError, match="different receipt"):
        campaign.finalize("RecommendationResearchReceipt:other")


def test_finalization_rejects_more_than_five_positions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runtime/state.sqlite"
    store = InvestorOrchestrationStore(database)
    store.initialize()
    service = WatchCampaignService(database, "six-name-campaign")
    members = [
        "XSHG:600315",
        "XSHG:600521",
        "XSHG:600686",
        "XSHG:600867",
        "XSHG:603087",
        "XSHG:603129",
    ]
    service.initialize(members)
    _patch_q3_releases(monkeypatch, members)
    _patch_receipt(monkeypatch, members, members)
    readiness = service.finalization_readiness("RecommendationResearchReceipt:six")
    assert readiness["q3_ready"]
    assert readiness["position_count"] == 6
    assert not readiness["portfolio_ready"]


def test_missing_database_and_bad_inputs_do_not_create_storage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing canonical"):
        WatchCampaignService(tmp_path / "absent/state.sqlite", "fixture")
    assert not (tmp_path / "absent").exists()
