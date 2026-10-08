from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import astock.investor_orchestration.watch_campaign as watch_campaign_module
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


def test_owner_retry_reuses_lease_and_live_owner_is_a_normal_noop(
    campaign: WatchCampaignService,
) -> None:
    now = datetime.now(UTC)
    first = campaign.begin("pm", "2026-09-28", "owner-1", now=now)
    assert first.lease_acquired
    assert first.lease_disposition == "ACQUIRED"
    assert first.lease_generation == 1
    retry = campaign.begin(
        "pm",
        "2026-09-28",
        "owner-1",
        now=now + timedelta(minutes=34),
    )
    assert retry.deadline_at == first.deadline_at
    assert retry.invocation_started_at == first.invocation_started_at
    assert retry.lease_generation == first.lease_generation
    assert retry.lease_disposition == "REUSED"
    blocked = campaign.begin(
        "pm",
        "2026-09-28",
        "owner-2",
        now=now + timedelta(minutes=34),
    )
    assert not blocked.lease_acquired
    assert blocked.lease_disposition == "LIVE_OWNER"
    assert blocked.owner == "owner-1"
    assert blocked.lease_generation == first.lease_generation
    next_activation = campaign.begin(
        "pm",
        "2026-09-29",
        "owner-2",
        now=now + timedelta(minutes=35),
    )
    assert next_activation.round_id == first.round_id
    assert next_activation.owner == "owner-2"
    assert next_activation.lease_acquired
    assert next_activation.lease_generation == first.lease_generation + 1


def test_checkpoint_requires_complete_explicit_lease_token(
    campaign: WatchCampaignService,
) -> None:
    started = campaign.begin("am", "2026-09-28", "owner")
    with pytest.raises(ValueError, match="supplied together"):
        campaign.checkpoint(
            "am",
            "owner",
            {"checkpoint": "not applied"},
            round_id=started.round_id,
        )
    with pytest.raises(ValueError, match="supplied together"):
        campaign.checkpoint(
            "am",
            "owner",
            {"checkpoint": "not applied"},
            lease_generation=started.lease_generation,
        )


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
        round_id=first.round_id,
        lease_generation=first.lease_generation,
    )
    assert partial.checkpoint_applied
    assert not partial.owner
    resumed = campaign.begin("pm", "2026-09-29", "two")
    assert resumed.round_id == first.round_id
    assert resumed.status == "PENDING"
    assert resumed.completed_units == 9
    assert resumed.lease_generation == first.lease_generation + 1
    heartbeat = campaign.checkpoint(
        "pm",
        "diagnostic-owner-can-change",
        {},
        round_id=resumed.round_id,
        lease_generation=resumed.lease_generation,
    )
    assert heartbeat.checkpoint_applied
    assert heartbeat.status == "PENDING"
    assert heartbeat.owner == "two"
    stale = campaign.checkpoint(
        "pm",
        "one",
        {"status": "COMPLETE"},
        round_id=first.round_id,
        lease_generation=first.lease_generation,
    )
    assert not stale.checkpoint_applied
    assert stale.checkpoint_disposition == "STALE_LEASE"
    assert stale.owner == "two"
    assert stale.completed_units == 9
    legacy_stale = campaign.checkpoint(
        "pm",
        "one",
        {"checkpoint": "legacy stale must not overwrite"},
    )
    assert not legacy_stale.checkpoint_applied
    assert legacy_stale.checkpoint_disposition == "STALE_LEASE"
    assert legacy_stale.checkpoint == "9 verified, 1 pending"
    with pytest.raises(ValueError, match="incomplete"):
        campaign.checkpoint(
            "pm",
            "wrapper-owner-name-can-change",
            {"status": "COMPLETE"},
            round_id=resumed.round_id,
            lease_generation=resumed.lease_generation,
        )
    completed = campaign.checkpoint(
        "pm",
        "wrapper-owner-name-can-change",
        {"completed_units": 10, "status": "COMPLETE"},
        round_id=resumed.round_id,
        lease_generation=resumed.lease_generation,
    )
    assert completed.checkpoint_applied
    released_stale = campaign.checkpoint(
        "pm",
        "two",
        {"checkpoint": "must not overwrite after release"},
        round_id=resumed.round_id,
        lease_generation=resumed.lease_generation,
    )
    assert not released_stale.checkpoint_applied
    assert released_stale.checkpoint == completed.checkpoint
    duplicate = campaign.begin("pm", "2026-09-28", "three")
    assert duplicate.status == "COMPLETE"
    assert not duplicate.lease_acquired
    assert duplicate.lease_disposition == "ALREADY_COMPLETE"
    new_day = campaign.begin("pm", "2026-09-29", "four")
    assert new_day.round_id != first.round_id
    assert new_day.lease_generation == completed.lease_generation + 1
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


def test_expired_activation_cannot_reacquire_by_reusing_owner(
    campaign: WatchCampaignService,
) -> None:
    started = campaign.begin("pm", "2026-10-02", "same-activation")
    expired = campaign.begin(
        "pm", "2026-10-02", "same-activation", now=started.deadline_at
    )
    assert not expired.lease_acquired
    assert expired.lease_disposition == "EXPIRED_LEASE"
    assert expired.deadline_at == started.deadline_at
    assert expired.lease_generation == started.lease_generation
    assert campaign.load().rounds["pm"].owner == "same-activation"


@pytest.mark.parametrize("explicit", [False, True])
def test_checkpoint_expiry_is_fenced_without_a_successor(
    campaign: WatchCampaignService,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    started = campaign.begin("pm", "2026-10-02", "expired-activation")
    before = campaign.load().model_dump(mode="json")
    monkeypatch.setattr(
        watch_campaign_module, "datetime", SimpleNamespace(now=lambda _tz: started.deadline_at)
    )
    result = campaign.checkpoint(
        "pm",
        "expired-activation",
        {"checkpoint": "must not overwrite after deadline"},
        round_id=started.round_id if explicit else None,
        lease_generation=started.lease_generation if explicit else None,
    )
    assert not result.checkpoint_applied
    assert result.checkpoint_disposition == "STALE_LEASE"
    assert campaign.load().model_dump(mode="json") == before


def test_missing_old_report_does_not_block_research_checkpoint_or_release(
    campaign: WatchCampaignService,
) -> None:
    report = campaign.database.parent / "latest-pm.md"
    report.write_text("fixture report", encoding="utf-8")
    started = campaign.begin("pm", "2026-10-02", "research-owner")
    campaign.checkpoint(
        "pm", "research-owner", {"report_file": str(report)}
    )
    report.unlink()  # Only a fixture projection, never a production report.
    saved = campaign.checkpoint(
        "pm",
        "research-owner",
        {
            "checkpoint": "Evidence retained; report projection must be regenerated.",
            "completed_units": 2,
            "total_units": 3,
            "status": "PARTIAL",
        },
        round_id=started.round_id,
        lease_generation=started.lease_generation,
    )
    assert saved.checkpoint_applied
    assert saved.completed_units == 2
    assert saved.total_units == 3
    assert saved.report_file == str(report)
    assert not saved.owner
    resumed = campaign.begin("pm", "2026-10-02", "successor")
    assert resumed.round_id == started.round_id
    assert resumed.checkpoint == saved.checkpoint
    assert resumed.submit_status == "NOT_SUBMITTED"


def test_new_report_reference_still_requires_existing_project_file(
    campaign: WatchCampaignService,
) -> None:
    campaign.begin("pm", "2026-10-02", "owner")
    before = campaign.load().model_dump(mode="json")
    with pytest.raises(ValueError, match="existing project-local"):
        campaign.checkpoint(
            "pm", "owner", {"report_file": str(campaign.database.parent / "missing.md")}
        )
    assert campaign.load().model_dump(mode="json") == before
