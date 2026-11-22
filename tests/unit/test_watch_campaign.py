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


def test_owner_lease_is_explicit_and_independent_of_host_policy(
    campaign: WatchCampaignService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_host_policy(*_args: object, **_kwargs: object) -> None:
        pytest.fail("owner leases must not be derived from host handoff reserves")

    monkeypatch.setattr(
        "astock.investor_orchestration.host_continuation.ChatInvocationPolicy.load",
        reject_host_policy,
    )
    now = datetime.now(UTC)
    first = campaign.begin(
        "am", "2026-09-28", "owner-1", now=now, lease_seconds=90,
    )
    assert first.deadline_at == now + timedelta(seconds=90)
    retry = campaign.begin(
        "am", "2026-09-28", "owner-1", now=now + timedelta(seconds=89),
        lease_seconds=7200,
    )
    assert retry.lease_disposition == "REUSED"
    assert retry.deadline_at == first.deadline_at
    successor = campaign.begin(
        "am", "2026-09-28", "owner-2", now=first.deadline_at, lease_seconds=120,
    )
    assert successor.lease_acquired
    assert successor.round_id == first.round_id
    assert successor.lease_generation == first.lease_generation + 1
    assert successor.deadline_at == first.deadline_at + timedelta(seconds=120)


def test_owner_lease_does_not_create_a_host_deadline_in_campaign_status(
    campaign: WatchCampaignService,
) -> None:
    started = campaign.begin(
        "pm", "2026-09-28", "active-owner",
        now=datetime.now(UTC) - timedelta(hours=1), lease_seconds=7200,
    )
    assert started.deadline_at > datetime.now(UTC)
    budgets = campaign.status()["budgets"]
    assert isinstance(budgets, dict)
    budget = budgets["pm"]
    assert isinstance(budget, dict)
    assert budget["elapsed_seconds"] >= 3600
    assert not budget["activation_deadline_enforced"]
    assert budget["remaining_invocation_seconds"] is None
    assert not budget["invocation_stop_required"]
    assert not budget["continuation_required"]
    assert not budget["scheduled_creation_required"]
    assert campaign.load().rounds["pm"].deadline_at == started.deadline_at


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
    wrong_owner = campaign.checkpoint(
        "pm",
        "diagnostic-owner-can-change",
        {},
        round_id=resumed.round_id,
        lease_generation=resumed.lease_generation,
    )
    assert not wrong_owner.checkpoint_applied
    assert wrong_owner.checkpoint_disposition == "STALE_LEASE"
    assert wrong_owner.owner == resumed.owner
    heartbeat = campaign.checkpoint(
        "pm",
        resumed.owner,
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
            resumed.owner,
            {"status": "COMPLETE"},
            round_id=resumed.round_id,
            lease_generation=resumed.lease_generation,
        )
    completed = campaign.checkpoint(
        "pm",
        resumed.owner,
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
    # Portfolio policy tests isolate the comprehensive financial source verifier.
    # Corrupt/missing canonical evidence has separate negative coverage below.
    monkeypatch.setattr(
        WatchCampaignService, "_verified_q3_release",
        lambda self, instrument, row: row["release_id"]
        if self._official_q3_release(instrument, row) else None,
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
    assert resumed.report_file == saved.report_file


def test_campaign_round_keeps_strict_metadata_contract(
    campaign: WatchCampaignService,
) -> None:
    from astock.investor_orchestration.watch_campaign import CampaignRound

    existing = campaign.begin("pm", "2026-10-02", "owner")
    invalid = {**existing.model_dump(mode="python"), "unexpected_extension": "not_admitted"}
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        CampaignRound.model_validate(invalid)
    with pytest.raises(ValueError, match="unsupported fields"):
        campaign.checkpoint("pm", "owner", {"unexpected_extension": "not_admitted"})


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


def test_typed_row_without_verified_evidence_is_not_a_release(campaign, monkeypatch):
    monkeypatch.setattr(
        watch_campaign_module.FinancialSourceReleaseRepository, "get",
        lambda self, company, period, period_type: _official_q3_row("XSHG:" + company),
    )
    monkeypatch.setattr(campaign, "_financial_service", lambda: SimpleNamespace(
        read_certified_release=lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("official snapshot corrupt"))))
    assert campaign.finalization_readiness()["q3_ready"] is False


def test_recovery_projects_existing_members_and_never_reinitializes(campaign):
    before = campaign.load().model_dump(mode="json")
    events = len(campaign.subjects.store.subject_events())
    plan = campaign.recovery_plan()
    assert plan["members"] == campaign.members()
    assert len(plan["requests"]) == 2
    assert all(campaign.campaign_id in item["request_id"] for item in plan["requests"])
    assert campaign.load().model_dump(mode="json") == before
    assert len(campaign.subjects.store.subject_events()) == events


def test_delta_recovery_only_fetches_missing_member_and_releases_same_generation(
    campaign, monkeypatch,
):
    members = campaign.members()
    calls = []
    monkeypatch.setattr(campaign, "_verified_q3_release",
                        lambda instrument, row: "known" if instrument == members[0] else None)
    def sync(company, market, period, period_type, **kwargs):
        calls.append(company)
        return SimpleNamespace(model_dump=lambda **kwargs: {"status": "UNAVAILABLE"})
    monkeypatch.setattr(campaign, "_financial_service", lambda: SimpleNamespace(sync=sync))
    before = len(campaign.subjects.store.subject_events())
    current = campaign.begin("pm", "2026-11-21", "recovery-owner")
    result = campaign.recover_missing_q3(
        slot="pm", owner="recovery-owner", round_id=current.round_id,
        lease_generation=current.lease_generation,
    )
    assert calls == [members[1].split(":")[1]]
    assert result["attempted"] == 1 and not result["readiness"]["ready"]
    assert campaign.load().rounds["pm"].round_id == current.round_id
    assert not campaign.load().rounds["pm"].owner
    assert len(campaign.subjects.store.subject_events()) == before
    with pytest.raises(ValueError, match="stale"):
        campaign.recover_missing_q3(
            slot="pm", owner="recovery-owner", round_id=current.round_id,
            lease_generation=current.lease_generation,
        )
    assert len(calls) == 1


def test_delta_transport_ends_with_owner_lease_and_cannot_spend_on_next_member(
    campaign, monkeypatch,
):
    from astock.core.request_budget import current_request_budget

    current = campaign.begin("pm", "2026-11-21", "short-owner", lease_seconds=10)
    clock = [current.invocation_started_at]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr("astock.investor_orchestration.watch_campaign.datetime", Clock)
    monkeypatch.setattr(campaign, "_verified_q3_release", lambda instrument, row: None)
    calls = []
    def sync(company, *args, **kwargs):
        budget = current_request_budget()
        assert budget is not None and budget.deadline_at == current.deadline_at
        calls.append(company)
        clock[0] = current.deadline_at + timedelta(seconds=1)
        return SimpleNamespace(model_dump=lambda **kwargs: {"status": "UNAVAILABLE"})
    monkeypatch.setattr(campaign, "_financial_service", lambda: SimpleNamespace(sync=sync))
    result = campaign.recover_missing_q3(
        slot="pm", owner="short-owner", round_id=current.round_id,
        lease_generation=current.lease_generation, elapsed_budget_seconds=180,
    )
    assert len(calls) == 1 and not result["checkpoint"]["checkpoint_applied"]
    successor = campaign.begin("pm", "2026-11-21", "new-owner")
    assert successor.lease_acquired and successor.lease_generation == current.lease_generation + 1
    assert successor.round_id == current.round_id



def test_recovery_rotates_unresolved_members_across_owned_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One persistently failing first tranche must not starve later companies."""
    database = tmp_path / "runtime/state.sqlite"
    InvestorOrchestrationStore(database).initialize()
    service = WatchCampaignService(database, "rotation-campaign")
    original = [
        "XSHE:002001", "XSHE:002475", "XSHE:002595",
        "XSHE:002897", "XSHE:002901",
    ]
    service.initialize(original)
    members = service.members()
    monkeypatch.setattr(service, "_verified_q3_release", lambda instrument, row: None)
    calls: list[str] = []

    def sync(company: str, *args: object, **kwargs: object) -> SimpleNamespace:
        calls.append(company)
        remaining[0] -= 1
        return SimpleNamespace(model_dump=lambda **options: {"status": "NEEDS_INFO"})

    monkeypatch.setattr(service, "_financial_service", lambda: SimpleNamespace(sync=sync))
    # Test exhaustion of the existing transport budget, not an arbitrary member cap.
    remaining = [0]
    monkeypatch.setattr(
        watch_campaign_module.RequestExecutionBudget,
        "remaining_attempts", lambda self: remaining[0],
    )
    expected = [members[:2], members[2:4], members[4:], members[:2]]
    prior_generation = 0
    for index, group in enumerate(expected):
        remaining[0] = len(group)
        current = service.begin("pm", "2026-11-22", f"owner-{index}")
        assert current.lease_acquired
        assert current.lease_generation == prior_generation + 1
        prior_generation = current.lease_generation
        offset = len(calls)
        result = service.recover_missing_q3(
            slot="pm", owner=f"owner-{index}", round_id=current.round_id,
            lease_generation=current.lease_generation,
        )
        assert calls[offset:] == [member.split(":", 1)[1] for member in group]
        assert result["attempted"] == len(group)
        checkpoint = result["checkpoint"]
        assert isinstance(checkpoint, dict) and checkpoint["checkpoint_applied"] is True
        assert service.load().rounds["pm"].status == "PARTIAL"


def test_recovery_progress_ignores_stale_or_foreign_members() -> None:
    missing = ["XSHE:002001", "XSHG:600315", "XSHE:002475"]
    selection, previous = watch_campaign_module._pending_recovery_batch(
        missing,
        '{"schema_version":"campaign-q3-delta-v1","attempted_members":'
        '["XSHE:002001","XSHE:002001","OTHER:000001"]}',
    )
    assert selection == ["XSHG:600315", "XSHE:002475"]
    assert previous == "XSHE:002001"
    assert watch_campaign_module._pending_recovery_batch(
        missing, "invalid historical free-text checkpoint",
    ) == (missing, None)
    assert watch_campaign_module._pending_recovery_batch(
        ["XSHE:002001", "XSHE:002475", "XSHG:600315"],
        '{"schema_version":"campaign-q3-delta-v2","last_attempted_member":"XSHE:002475"}',
    ) == (["XSHG:600315", "XSHE:002001", "XSHE:002475"], "XSHE:002475")


def test_explicit_generation_also_requires_owning_identity(campaign):
    current = campaign.begin("am", "2026-11-21", "owner")
    result = campaign.checkpoint(
        "am", "other-owner", {"checkpoint": "must not write"},
        round_id=current.round_id, lease_generation=current.lease_generation,
    )
    assert not result.checkpoint_applied


def test_financial_source_initializes_once_per_campaign_service(
    campaign: WatchCampaignService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness of an N-member campaign must not reload provider configs N times."""
    import astock.financial_sources as financial_sources

    created: list[object] = []

    def fake_financial_service(*_args: object) -> object:
        instance = object()
        created.append(instance)
        return instance

    monkeypatch.setattr(financial_sources, "FinancialSourceService", fake_financial_service)
    first = campaign._financial_service()
    assert campaign._financial_service() is first
    separate_invocation = WatchCampaignService(campaign.database, campaign.campaign_id)
    assert separate_invocation._financial_service() is not first
    assert len(created) == 2


def test_recovery_does_not_cap_missing_members_at_eighteen_or_fifty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every eligible company is attempted while the actual lease/budget remains valid."""
    database = tmp_path / "runtime/state.sqlite"
    InvestorOrchestrationStore(database).initialize()
    service = WatchCampaignService(database, "unbounded-q3-campaign")
    members = [f"XSHE:{100000 + index:06d}" for index in range(65)]
    service.initialize(members)
    monkeypatch.setattr(service, "_verified_q3_release", lambda instrument, row: None)
    attempts: list[str] = []

    def sync(company: str, *args: object, **kwargs: object) -> SimpleNamespace:
        attempts.append(company)
        return SimpleNamespace(model_dump=lambda **options: {"status": "NEEDS_INFO"})

    monkeypatch.setattr(service, "_financial_service", lambda: SimpleNamespace(sync=sync))
    lease = service.begin("pm", "2026-11-22", "all-missing-owner")
    result = service.recover_missing_q3(
        slot="pm", owner="all-missing-owner", round_id=lease.round_id,
        lease_generation=lease.lease_generation,
    )
    assert result["attempted"] == len(members)
    assert attempts == [item.split(":", 1)[1] for item in members]
    checkpoint = result["checkpoint"]
    readiness = result["readiness"]
    assert isinstance(checkpoint, dict) and checkpoint["checkpoint_applied"] is True
    assert isinstance(readiness, dict) and readiness["missing_q3"] == members
    assert len(service.load().rounds["pm"].checkpoint) < 12000


def test_historical_maximum_members_cli_flag_does_not_limit_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Previously saved scheduler commands must remain executable without a cap."""
    import json
    import sys

    observed: dict[str, object] = {}

    class FakeCampaign:
        def __init__(self, database: Path, campaign_id: str) -> None:
            observed["campaign"] = campaign_id

        def recover_missing_q3(self, **kwargs: object) -> dict[str, int]:
            observed.update(kwargs)
            return {"attempted": 65}

    monkeypatch.setattr(watch_campaign_module, "WatchCampaignService", FakeCampaign)
    monkeypatch.setattr(sys, "argv", [
        "watch_campaign.py", "recover-q3", "--slot", "pm", "--owner", "cli-owner",
        "--round-id", "round-1", "--lease-generation", "1",
        "--maximum-members", "3",
    ])
    watch_campaign_module.main()
    assert observed["campaign"] == "ten-stock-q3-2026"
    assert "maximum_members" not in observed
    assert json.loads(capsys.readouterr().out)["attempted"] == 65
