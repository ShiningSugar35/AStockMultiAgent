from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astock.market_data.official_calendar import (
    OfficialCalendarClosure,
    OfficialCalendarSource,
    OfficialCalendarYear,
    OfficialTradingCalendarConfig,
    OfficialTradingCalendarPolicy,
)
from astock.research.debate_evaluation import load_quality_policy
from astock.research.session_brief import build_session_brief, market_period
from astock.schemas.market import Market

ROOT = Path(__file__).resolve().parents[2]


def setup():
    from datetime import date

    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    stamp = datetime(2025, 12, 1, tzinfo=UTC)
    source = OfficialCalendarSource("exchange", "holiday", "https://www.sse.com.cn/notice")
    year = OfficialCalendarYear(
        2026,
        stamp,
        stamp,
        {Market.XSHG: source},
        (OfficialCalendarClosure("holiday", date(2026, 10, 1), date(2026, 10, 7)),),
    )
    calendar = OfficialTradingCalendarConfig(
        "official-trading-calendar-v1",
        "CN_A_SHARE",
        OfficialTradingCalendarPolicy(
            "LOCAL_OFFICIAL_FIRST", True, True, True, "SEARCH_OFFICIAL_THEN_API_FALLBACK"
        ),
        {2026: year},
    )
    return policy, calendar


def test_periods_holiday_unknown_year_and_timezones():
    policy, calendar = setup()

    def period(value):
        return market_period(datetime.fromisoformat(value), Market.XSHG, calendar, policy)

    assert period("2026-11-02T01:00:00+00:00") == "PRE_MARKET"
    assert period("2026-11-02T01:30:00+00:00") == "INTRADAY"
    assert period("2026-11-02T03:30:00+00:00") == "MIDDAY_BREAK"
    assert period("2026-11-02T05:00:00+00:00") == "INTRADAY"
    assert period("2026-11-02T07:00:00+00:00") == "POST_MARKET"
    assert period("2026-10-01T10:00:00+08:00") == "CLOSED"
    assert period("2027-01-04T10:00:00+08:00") == "CALENDAR_UNKNOWN"
    assert period("2026-11-01T17:30:00-08:00") == period("2026-11-02T09:30:00+08:00")


def test_repeated_facts_stable_draft_and_no_unauthenticated_recommendation():
    policy, calendar = setup()
    args: dict[str, Any] = dict(
        instrument_id="XSHG:600000",
        account_id=None,
        context_id="brief",
        calendar=calendar,
        policy=policy,
    )
    first = build_session_brief(now=datetime.fromisoformat("2026-11-02T10:00:00+08:00"), **args)
    second = build_session_brief(
        now=datetime.fromisoformat("2026-11-02T10:01:00+08:00"),
        previous_fingerprint=first["semantic_fingerprint"],
        **args,
    )
    assert first["semantic_fingerprint"] == second["semantic_fingerprint"]
    assert second["changed"] is False
    assert second["delivery_status"] == "DRAFT"
    assert second["public_approved"] is False
    assert second["source_facts"]["current"] is None
    assert "考虑配置" not in second["text"]
    after = build_session_brief(
        now=datetime.fromisoformat("2026-11-02T15:01:00+08:00"),
        previous_fingerprint=first["semantic_fingerprint"],
        **args,
    )
    assert after["changed"] is True


def test_registered_public_brief_never_promotes_an_unlicensed_draft():
    from datetime import timedelta

    from astock.investor_orchestration.full_research import FullResearchRecommendationService
    from astock.research.session_brief import render_session_brief
    from tests.unit.test_full_research_recommendation import NOW, _build_receipt, _request

    receipt = _build_receipt(
        FullResearchRecommendationService(),
        _request("分析600001能否买入", intent="FULL_RESEARCH_RECOMMENDATION"),
    )
    policy, calendar = setup()
    draft = build_session_brief(
        instrument_id="XSHG:600001",
        account_id=receipt.request_contract.account_id,
        context_id="formal-brief-boundary",
        now=NOW + timedelta(minutes=1),
        calendar=calendar,
        policy=policy,
        receipt=receipt,
    )
    assert draft["public_approved"] is False
    display = render_session_brief(draft, receipt, instrument_id="XSHG:600001")
    assert display["status"] == "PUBLIC_PRESENTATION_READY"
    assert display["public_approved"] is True
    assert display["notification_sent"] is False
    assert display["receipt_id"] == receipt.receipt_id
    assert "已发布研究观点" in display["presentation_text"]

    # Content integrity and account binding survive a valid unrelated receipt.
    from astock.research.debate_evaluation import digest

    rewritten = {**draft, "text": "a different recommendation"}
    assert not render_session_brief(
        rewritten, receipt, instrument_id="XSHG:600001"
    )["public_approved"]
    wrong_account = {
        **draft,
        "source_facts": {
            **draft["source_facts"], "account_scope_hash": digest({"account_id": "foreign"}),
        },
    }
    wrong_account["result_id"] = "session-brief:" + digest(
        {key: value for key, value in wrong_account.items() if key != "result_id"}
    )
    assert not render_session_brief(
        wrong_account, receipt, instrument_id="XSHG:600001"
    )["public_approved"]

    # A valid receipt must not publish the same draft under another security.
    mismatch = render_session_brief(draft, receipt, instrument_id="XSHG:600002")
    assert mismatch["status"] == "NOT_AUTHORIZED_FOR_DISPLAY"
    assert mismatch["public_approved"] is False
    wrong_subject = {**draft, "instrument_id": "XSHG:600002"}
    assert (
        render_session_brief(wrong_subject, receipt, instrument_id="XSHG:600001")["public_approved"]
        is False
    )

    # A different or unsigned receipt cannot take over a verified brief.
    forged = receipt.model_copy(update={"receipt_hash": "f" * 64})
    rejected = render_session_brief(draft, forged, instrument_id="XSHG:600001")
    assert rejected["status"] == "NOT_AUTHORIZED_FOR_DISPLAY"
    assert rejected["public_approved"] is False

    # Editorial safety alone is never a Publication Gate.
    blank = build_session_brief(
        instrument_id="XSHG:600001",
        account_id=None,
        context_id="unpublished-brief",
        now=NOW + timedelta(minutes=1),
        calendar=calendar,
        policy=policy,
    )
    assert (
        render_session_brief(blank, receipt, instrument_id="XSHG:600001")["public_approved"]
        is False
    )


def test_scheduled_monitor_draft_reuse_is_idempotent_and_account_security_bound(tmp_path):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.quality_cli import QualityServices
    from astock.research.session_brief import SessionBriefReuseService

    policy, calendar = setup()
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    reuse = SessionBriefReuseService(
        QualityServices(
            state,
            ObjectStore(tmp_path / "objects"),
            None,
            calendar,
            policy,
        )
    )
    args = dict(
        instrument_id="XSHG:600000",
        account_id="actual-a",
        context_id="monitor-existing-session",
        now=datetime(2026, 11, 2, 8, tzinfo=UTC),
    )
    first = reuse.project(**args)
    assert first["status"] == "DRAFT"
    assert first["public_approved"] is False
    second = reuse.project(**args)
    assert second["status"] == "UNCHANGED"
    assert second["artifact_id"] == first["artifact_id"]
    assert reuse.project(**{**args, "account_id": "paper-a"})["artifact_id"] != first["artifact_id"]
    assert (
        reuse.project(**{**args, "instrument_id": "XSHG:600001"})["artifact_id"]
        != first["artifact_id"]
    )


def test_scheduled_brief_requires_canonical_reader(tmp_path):
    from types import SimpleNamespace

    from astock.investor_orchestration.scheduled import ScheduledResearchService

    service = object.__new__(ScheduledResearchService)
    from typing import cast

    service.store = cast(Any, SimpleNamespace(path=tmp_path / "state.sqlite"))
    service.preflight_service = cast(Any, SimpleNamespace(reader=SimpleNamespace()))
    result = service._reuse_session_briefs(None, None, {}, None)
    assert result == [{"status": "BRIEF_CANONICAL_READER_REQUIRED", "public_approved": False}]


def test_existing_scheduled_monitor_path_reuses_drafts_without_delivery(tmp_path):
    from types import SimpleNamespace
    from typing import cast

    from astock.core.state import StateStore
    from astock.investor_orchestration.canonical_state import CanonicalStateProjectionReader
    from astock.investor_orchestration.models import ScheduledDomain
    from astock.investor_orchestration.scheduled import ScheduledResearchService
    from astock.investor_orchestration.store import InvestorOrchestrationStore

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = object.__new__(ScheduledResearchService)
    service.store = store
    service.preflight_service = cast(
        Any, SimpleNamespace(reader=CanonicalStateProjectionReader(store))
    )
    request = SimpleNamespace(
        binding_id="existing-binding", requested_at=datetime(2026, 11, 2, 8, tzinfo=UTC)
    )

    def lane(account):
        return SimpleNamespace(
            positions=(SimpleNamespace(account_id=account, instrument_id="XSHG:600000"),)
        )

    preflight = SimpleNamespace(context=SimpleNamespace(actual=lane("actual"), paper=lane("paper")))
    subjects = {
        ScheduledDomain.ACTUAL_HOLDING: ("XSHG:600000",),
        ScheduledDomain.PAPER_HOLDING: ("XSHG:600000",),
    }
    policy = SimpleNamespace(max_subjects_per_run=10)
    first = service._reuse_session_briefs(request, preflight, subjects, policy)
    assert [x["status"] for x in first] == ["DRAFT", "DRAFT"]
    assert first[0]["artifact_id"] != first[1]["artifact_id"]
    second = service._reuse_session_briefs(request, preflight, subjects, policy)
    assert all(x["status"] == "UNCHANGED" and not x["public_approved"] for x in second)
    assert len(StateStore(store.path).recent_artifact_records("SessionBrief")) == 2
    preflight.context.actual.positions = ()
    blocked = service._reuse_session_briefs(request, preflight, subjects, policy)
    assert blocked[0]["status"] == "BRIEF_ACCOUNT_SCOPE_UNAVAILABLE"


def test_monitor_private_target_without_account_fails_closed():
    from types import SimpleNamespace

    from astock.research.session_brief import SessionBriefReuseService

    reuse = SessionBriefReuseService(None)
    for reason in ("PAPER_POSITION", "OPEN_PAPER_ORDER"):
        result = reuse.project_monitor_target(
            SimpleNamespace(reasons=[reason]), now=datetime(2026, 11, 2, 8, tzinfo=UTC)
        )
        assert result["status"] == "MONITOR_BRIEF_ACCOUNT_BINDING_REQUIRED"
        assert result["public_approved"] is False
    result = reuse.project_monitor_target(
        SimpleNamespace(reasons=["MANUAL"]), now=datetime(2026, 11, 2, 8, tzinfo=UTC)
    )
    assert result["status"] == "MONITOR_BRIEF_RESEARCH_SCOPE_REQUIRED"


def test_optional_missing_objects_do_not_block_receipt_projection(tmp_path):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.quality_cli import QualityServices
    from astock.research.session_brief import SessionBriefReuseService

    policy, calendar = setup()
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(tmp_path / "objects")
    state.register_artifact(
        artifact_id="missing-optional", artifact_type="TechnicalIndicatorSnapshot",
        schema_version="technical-indicator-snapshot-v1", object_hash="f" * 64, input_hashes=[],
    )
    reuse = SessionBriefReuseService(QualityServices(state, objects, None, calendar, policy))
    result = reuse.project(
        instrument_id="XSHG:600000", account_id=None, context_id="optional-not-a-gate",
        now=datetime(2026, 11, 2, 8, tzinfo=UTC),
    )
    assert result["status"] == "DRAFT" and not result["public_approved"]
    record = state.artifact_record(result["artifact_id"])
    assert record is not None
    import json

    draft = json.loads(objects.get_bytes(record["object_hash"]))
    assert draft["lineage"]["optional_input_gaps"] == [
        {"type": "TechnicalIndicatorSnapshot", "reason": "UNAVAILABLE"}
    ]
