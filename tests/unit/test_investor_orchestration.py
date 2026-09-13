from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from astock.cli import app as root_cli_app
from astock.investor_orchestration.activation import (
    ActivationGateService,
    shadow_observation,
)
from astock.investor_orchestration.capabilities import (
    CapabilityExecutionResult,
    CapabilityPlanner,
)
from astock.investor_orchestration.gateway import InvestorAnswerGateway
from astock.investor_orchestration.macro import OfficialMacroCaptureService
from astock.investor_orchestration.models import (
    AdmissionStatus,
    CapabilityCoverageReceipt,
    CapabilityRequirement,
    CapabilityRunRecord,
    CapabilityRunStatus,
    ControlledLiveCheck,
    DisclosureLevel,
    InvestorAnswerDraft,
    InvestorRequestEnvelope,
    MarketRegimeFeatureSnapshot,
    RegimeState,
    RequestIntent,
    ScheduledActionPolicy,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
    SideEffectClass,
    SubjectEventKind,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.regime import MarketRegimeService
from astock.investor_orchestration.resolution import AccountResolver, RelativeDateResolver
from astock.investor_orchestration.scenarios import ScenarioContractRunner
from astock.investor_orchestration.schedule_clock import (
    DailyTrackingSchedule,
    StateTradingCalendar,
)
from astock.investor_orchestration.scheduled import (
    ScheduledResearchService,
    policy_from_config,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.investor_orchestration.utils import content_hash

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path: Path) -> InvestorOrchestrationStore:
    result = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    result.initialize()
    return result


def _now() -> datetime:
    return datetime(2026, 9, 7, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _request(
    *,
    request_id: str,
    intent: RequestIntent | str = RequestIntent.RESEARCH,
    side_effect: SideEffectClass = SideEffectClass.READ,
    instruments: tuple[str, ...] = ("600519.XSHG",),
    raw_text: str = "test investor request",
) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope.model_validate(
        {
            "request_id": request_id,
            "question_time": _now(),
            "user_timezone": "Asia/Shanghai",
            "raw_text": raw_text,
            "normalized_intent": intent,
            "side_effect": side_effect,
            "entity_ids": instruments,
            "idempotency_key": f"idempotency:{request_id}",
        }
    )


def _seed_existing_state(store: InvestorOrchestrationStore) -> str:
    """Seed real canonical account/ledger/monitor services, not look-alike tables.

    The recorded clock keeps the economic inputs available before the test
    request. A low-level ledger order has no human-confirmation authority.
    """
    from unittest.mock import patch

    from astock.core.hashing import content_hash as canonical_content_hash
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.external_accounts import ExternalAccountRepository
    from astock.monitoring.repository import ContinuousMonitorRepository
    from astock.paper_trading.ledger import LedgerService
    from astock.schemas import OrderSide
    from astock.schemas.continuous_monitoring import (
        MonitorEvent,
        MonitorEventType,
        MonitorSeverity,
        MonitorSource,
        MonitorTargetEnrollRequest,
        MonitorTargetReason,
    )
    from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
    from astock.schemas.market import Market

    stamp = _now() - timedelta(minutes=1)

    class RecordedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    state = StateStore(store.path)
    objects = ObjectStore(store.path.parent / "objects" / "sha256")
    external = ExternalAccountRepository(state, objects)
    external.create_account(account_id="actual-a", display_name="recorded actual", created_at=stamp)
    external.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id="actual-a",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=stamp,
                available_to_system_at=stamp,
                created_at=stamp,
                market=Market.XSHG,
                symbol="600519",
                side="BUY",
                quantity=100,
                price_cny=Decimal("1500"),
                idempotency_key="actual-recorded-buy",
            )
        ]
    )
    ledger = LedgerService(state)
    with patch("astock.paper_trading.ledger.datetime", RecordedDatetime):
        ledger.initialize_account("paper", 1000000)
        bought = ledger.place_order(
            account_id="paper",
            client_request_id="recorded-filled-order",
            symbol="000001",
            side=OrderSide.BUY,
            qty=200,
            limit_price_fen=1000,
            fee_reserve_fen=0,
            position_identity=(Market.XSHE, "XSHE:000001"),
        )
        ledger.record_fill(
            fill_id="recorded-fill",
            order_id=bought.order_id,
            qty=200,
            price_fen=1000,
            occurred_at=stamp,
        )
        open_order = ledger.place_order(
            account_id="paper",
            client_request_id="recorded-open-order",
            symbol="000001",
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=980,
            fee_reserve_fen=0,
            position_identity=(Market.XSHE, "XSHE:000001"),
        )
    monitor = ContinuousMonitorRepository(state, objects)
    for symbol, market, severity, summary in (
        ("600519", Market.XSHG, MonitorSeverity.CRITICAL, "重大事项需要复核"),
        ("000001", Market.XSHE, MonitorSeverity.MATERIAL, "行业政策发生重要变化"),
    ):
        target = monitor.enroll(
            MonitorTargetEnrollRequest(
                symbol=symbol,
                market=market,
                company_id=symbol,
                display_name=symbol,
                reason=MonitorTargetReason.MANUAL,
                created_at=stamp,
            )
        )
        payload: dict[str, object] = {
            "summary": summary,
            "available_to_system_at": stamp.isoformat(),
        }
        monitor.record_event(
            MonitorEvent(
                event_id=f"event-{symbol}",
                target_id=target.target_id,
                company_id=symbol,
                event_type=MonitorEventType.OFFICIAL_DISCLOSURE,
                severity=severity,
                observed_at=stamp,
                available_at=stamp,
                source=MonitorSource.CNINFO,
                payload=payload,
                payload_hash=canonical_content_hash(payload),
                dedupe_key=f"recorded-event-{symbol}",
                created_at=stamp,
            )
        )
    return open_order.order_id


def test_root_cli_registers_investor_subcommands() -> None:
    result = CliRunner().invoke(root_cli_app, ["investor", "--help"])
    assert result.exit_code == 0, result.output
    assert "audit" in result.output
    assert "preflight" in result.output
    assert "schedule-run" in result.output
    assert "schedule-tick" in result.output


def test_relative_date_and_account_resolution_are_explicit() -> None:
    resolver = RelativeDateResolver()
    yesterday = resolver.resolve(
        "昨天",
        question_time=datetime(2026, 9, 7, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
        user_timezone="Asia/Tokyo",
    )
    assert yesterday.civil_date is not None
    assert yesterday.civil_date.isoformat() == "2026-09-06"
    assert yesterday.exact_timestamp is None
    assert (
        AccountResolver.resolve(
            requested_account_id=None,
            active_account_ids=("only",),
            default_account_id=None,
        )
        == "only"
    )
    with pytest.raises(ValueError, match="multiple active accounts"):
        AccountResolver.resolve(
            requested_account_id=None,
            active_account_ids=("a", "b"),
            default_account_id=None,
        )


def test_store_migration_is_append_only_and_idempotent(
    store: InvestorOrchestrationStore,
) -> None:
    store.initialize()
    service = ResearchSubjectRegistryService(store)
    event = service.add_watchlist(
        "600519.XSHG",
        reason="估值合理但等待更好时点",
        available_at=_now(),
        idempotency_key="watchlist-key",
    )
    replay = service.add_watchlist(
        "600519.XSHG",
        reason="估值合理但等待更好时点",
        available_at=_now(),
        idempotency_key="watchlist-key",
    )
    assert replay == event
    with pytest.raises(ValueError, match="different payload"):
        service.add_watchlist(
            "600519.XSHG",
            reason="same key but changed meaning",
            available_at=_now(),
            idempotency_key="watchlist-key",
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute(
                "UPDATE research_subject_events SET reason='changed' WHERE event_id=?",
                (event.event_id,),
            )
    store.initialize()
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM orchestration_schema_migrations").fetchone()[0]
            == 1
        )
    assert store.audit_integrity()["status"] == "PASS"


def test_schema_migration_checksum_and_transactional_rollback(
    store: InvestorOrchestrationStore,
) -> None:
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.apply_schema_migration(
            "0067_investor_orchestration",
            b"CREATE TABLE should_never_exist(id INTEGER PRIMARY KEY);",
        )
    with pytest.raises(sqlite3.DatabaseError):
        store.apply_schema_migration(
            "0068_broken_probe",
            b"CREATE TABLE migration_atomic_probe(id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;",
        )
    assert store.table_exists("migration_atomic_probe") is False
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM orchestration_schema_migrations "
                "WHERE version='0068_broken_probe'"
            ).fetchone()[0]
            == 0
        )


def test_investor_audit_cli_reports_pass(store: InvestorOrchestrationStore) -> None:
    result = CliRunner().invoke(
        root_cli_app,
        ["investor", "audit", "--database", str(store.path)],
    )
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.output)["status"] == "PASS"


def test_preflight_separates_actual_paper_orders_and_reuses_snapshot(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    service = InvestorSessionPreflightService(store)
    first = service.build(_request(request_id="request-one"))
    second = service.build(_request(request_id="request-two"))

    assert first.context.actual.positions[0].account_id == "actual-a"
    assert first.context.actual.positions[0].instrument_id == "XSHG:600519"
    assert first.context.paper.positions[0].instrument_id == "000001"
    assert "PAPER_POSITION_IDENTITY_UNBOUND" in first.context.paper.warnings
    assert first.context.paper.positions[0].quantity == Decimal("200")
    assert len(first.context.paper.open_orders) == 1
    assert first.context.paper.open_orders[0].quantity == Decimal("100")
    # A raw ledger order is NOT evidence of the separate user-confirmation chain.
    assert first.context.paper.open_orders[0].confirmed is False
    assert first.context.empty_holdings is False
    assert first.regime.available is False
    assert first.regime.reason == "NO_VALID_MARKET_REGIME_SNAPSHOT"
    assert second.built_from_cache is True
    assert first.context.aggregate_revision == second.context.aggregate_revision


def test_account_fact_write_does_not_require_market_regime(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="account-write",
        intent=RequestIntent.ACCOUNT_FACT_WRITE,
        side_effect=SideEffectClass.EA_WRITE,
    )
    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(request, preflight)
    nodes = {node.capability_id: node for node in plan.nodes}

    assert nodes["EXTERNAL_ACCOUNT"].requirement.value == "REQUIRED"
    assert nodes["EXTERNAL_ACCOUNT"].side_effect is SideEffectClass.EA_WRITE
    assert "MARKET_REGIME" not in nodes or nodes["MARKET_REGIME"].requirement.value != "REQUIRED"
    assert nodes["PAPER"].requirement.value == "PROHIBITED"


def test_legacy_holding_decision_is_only_an_input_alias_for_full_research(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="holding-read-migrates",
        intent="HOLDING_DECISION",
        side_effect=SideEffectClass.READ,
    )
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    assert request.metadata["full_research_routed_from"] == "HOLDING_DECISION"
    assert request.metadata["full_research_holding_context"] is True

    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(request, preflight)
    nodes = {node.capability_id: node for node in plan.nodes}
    assert nodes["HOLDING_REVIEW"].requirement is CapabilityRequirement.REQUIRED
    assert "HOLDING_REVIEW" in nodes["FULL_RESEARCH_GATE"].dependencies
    assert nodes["FULL_RESEARCH_GATE"].requirement is CapabilityRequirement.REQUIRED


def test_full_research_may_freeze_provisional_holding_input_without_actual_write(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="holding-provisional-input",
        intent="HOLDING_DECISION",
        side_effect=SideEffectClass.EA_PROVISIONAL,
    )
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(
        request,
        preflight,
        scenario_requirements={"EXTERNAL_ACCOUNT": CapabilityRequirement.REQUIRED},
    )
    nodes = {node.capability_id: node for node in plan.nodes}
    assert nodes["EXTERNAL_ACCOUNT"].side_effect is SideEffectClass.EA_PROVISIONAL
    assert nodes["EXTERNAL_ACCOUNT"].output_schema == "ExternalAccountOperationReceipt"
    assert "PAPER" not in nodes or nodes["PAPER"].requirement is CapabilityRequirement.PROHIBITED


def test_actual_account_write_and_investment_decision_must_be_split(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="holding-account-write",
        intent="HOLDING_DECISION",
        side_effect=SideEffectClass.EA_WRITE,
    )
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    preflight = InvestorSessionPreflightService(store).build(request)
    with pytest.raises(ValueError, match="side-effect permission"):
        CapabilityPlanner().plan(request, preflight)


def test_non_holding_research_cannot_upgrade_to_actual_account_write() -> None:
    from astock.investor_orchestration.capabilities import validate_request_permissions

    with pytest.raises(ValueError, match="side-effect permission"):
        validate_request_permissions(
            _request(
                request_id="research-cannot-write-account",
                intent=RequestIntent.RESEARCH,
                side_effect=SideEffectClass.EA_WRITE,
            )
        )


def test_full_research_decision_requires_complete_research_and_publication_gate(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="buy-decision",
        intent="BUY_DECISION",
    )
    assert request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(request, preflight)
    required = {node.capability_id for node in plan.nodes if node.requirement.value == "REQUIRED"}
    assert {
        "MARKET_REGIME",
        "FULL_MARKET",
        "COMPANY_RESEARCH",
        "FINANCIAL_INTEGRITY",
        "GOVERNANCE",
        "FORECAST_VALUATION",
        "RED_TEAM",
        "COMMITTEE",
        "PORTFOLIO",
        "FULL_RESEARCH_GATE",
        "RESPONSE_GATEWAY",
    }.issubset(required)
    assert plan.nodes[-1].capability_id == "RESPONSE_GATEWAY"


def test_required_capability_promotes_its_hard_dependencies(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="promoted-dependencies",
        intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.READ,
    )
    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(
        request,
        preflight,
        scenario_requirements={"FORECAST_VALUATION": CapabilityRequirement.REQUIRED},
    )
    requirements = {node.capability_id: node.requirement for node in plan.nodes}
    assert requirements["FORECAST_VALUATION"] is CapabilityRequirement.REQUIRED
    assert requirements["COMPANY_RESEARCH"] is CapabilityRequirement.REQUIRED
    assert requirements["FINANCIAL_INTEGRITY"] is CapabilityRequirement.REQUIRED
    assert requirements["CURRENT_MARKET"] is CapabilityRequirement.REQUIRED


def test_required_capability_cannot_complete_without_typed_output(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="missing-required-output",
        intent=RequestIntent.ACCOUNT_FACT_WRITE,
        side_effect=SideEffectClass.EA_WRITE,
    )
    _, _, coverage = InvestorOrchestrationService(store).execute(
        request,
        handlers={"EXTERNAL_ACCOUNT": lambda *_: CapabilityExecutionResult()},
    )
    external = next(
        record for record in coverage.records if record.capability_id == "EXTERNAL_ACCOUNT"
    )
    assert external.status is CapabilityRunStatus.FAILED
    assert external.reason == "output missing for ExternalAccountOperationReceipt"
    assert coverage.coverage_complete is False


def _features(**overrides: Any) -> MarketRegimeFeatureSnapshot:
    values: dict[str, Any] = {
        "feature_snapshot_id": "features-1",
        "as_of": _now(),
        "trend_score": 0.6,
        "breadth_score": 0.5,
        "tail_risk_score": 0.3,
        "liquidity_score": 0.4,
        "valuation_fragility_score": 0.0,
        "earnings_diffusion_score": 0.2,
        "macro_credit_score": 0.1,
        "family_coverage": {
            "trend": 1.0,
            "breadth": 1.0,
            "tail": 1.0,
            "liquidity": 1.0,
            "valuation": 1.0,
            "earnings": 1.0,
            "macro": 1.0,
        },
    }
    values.update(overrides)
    return MarketRegimeFeatureSnapshot.model_validate(values)


def test_regime_distinguishes_healthy_speculative_and_panic(
    store: InvestorOrchestrationStore,
) -> None:
    service = MarketRegimeService(store, PROJECT_ROOT / "configs/market_regime_v2.yaml")
    healthy = service.infer(_features(), persist=False)
    speculative = service.infer(
        _features(
            feature_snapshot_id="features-2",
            breadth_score=-0.2,
            tail_risk_score=-0.2,
        ),
        persist=False,
    )
    panic = service.infer(
        _features(
            feature_snapshot_id="features-3",
            trend_score=-0.8,
            breadth_score=-0.8,
            tail_risk_score=-0.9,
            liquidity_score=-0.9,
        ),
        persist=False,
    )

    assert healthy.baseline_state is RegimeState.HEALTHY_BULL
    assert healthy.selected_state is RegimeState.HEALTHY_BULL
    assert healthy.confidence >= 0.48
    assert speculative.baseline_state is RegimeState.SPECULATIVE_BULL
    assert speculative.selected_state in {RegimeState.SPECULATIVE_BULL, RegimeState.TRANSITION}
    assert panic.selected_state is RegimeState.PANIC
    assert panic.emergency_override is True
    assert abs(sum(healthy.probabilities.values()) - 1.0) < 1e-9


def test_regime_overlay_preserves_user_cap_and_is_more_conservative_in_panic(
    store: InvestorOrchestrationStore,
) -> None:
    service = MarketRegimeService(store, PROJECT_ROOT / "configs/market_regime_v2.yaml")
    healthy = service.infer(_features(), persist=True)
    panic = service.infer(
        _features(
            feature_snapshot_id="panic-features",
            trend_score=-0.9,
            breadth_score=-0.8,
            tail_risk_score=-0.9,
            liquidity_score=-0.9,
        ),
        persist=True,
    )
    profile = {
        "max_total_equity_weight": 0.7,
        "max_single_name_weight": 0.12,
        "minimum_cash_weight": 0.1,
    }
    healthy_overlay = service.overlay(healthy, profile, persist=False)
    panic_overlay = service.overlay(panic, profile, persist=False)

    assert panic_overlay.total_risk_multiplier < healthy_overlay.total_risk_multiplier
    assert panic_overlay.single_name_multiplier < healthy_overlay.single_name_multiplier
    assert panic_overlay.recommendation_cap < healthy_overlay.recommendation_cap
    assert healthy_overlay.user_limit_binding["max_single_name_weight"] == 0.12
    assert healthy_overlay.label_change_only_action_allowed is False


def test_macro_capture_is_raw_first_and_official_host_only(
    store: InvestorOrchestrationStore,
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "objects"
    service = OfficialMacroCaptureService(
        store,
        config_path=PROJECT_ROOT / "configs/macro_current_releases_v1.yaml",
    )
    service.object_store.root = object_root
    spec = service.spec("NBS", "latest-statistical-releases")
    snapshot = service.capture(
        spec,
        recorded_content="国家统计局最新发布内容".encode(),
        recorded_headers={
            "content-type": "text/html; charset=utf-8",
            "last-modified": "Mon, 07 Sep 2026 01:00:00 GMT",
        },
        captured_at=datetime(2026, 9, 7, 2, 0, tzinfo=UTC),
    )
    assert snapshot.raw_object_id.startswith("sha256:")
    assert snapshot.parse_status == "PARTIAL"
    assert snapshot.observations == ()
    from astock.core.object_store import ObjectStore

    assert ObjectStore(object_root).get_bytes(snapshot.raw_object_id.removeprefix("sha256:")) == (
        "国家统计局最新发布内容".encode()
    )

    bad_spec = spec.model_copy(update={"url": "https://example.com/not-official"})
    with pytest.raises(ValueError, match="not approved"):
        service.capture(bad_spec, recorded_content=b"x")


def test_macro_structured_capture_replays_first_and_latest_vintages(
    store: InvestorOrchestrationStore,
    tmp_path: Path,
) -> None:
    fixture_root = PROJECT_ROOT / "tests/fixtures/investor_orchestration/macro"
    service = OfficialMacroCaptureService(
        store,
        config_path=PROJECT_ROOT / "configs/macro_current_releases_v1.yaml",
    )
    service.object_store.root = tmp_path / "objects"
    spec = service.spec("NBS", "manufacturing-pmi-current")
    first_at = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)
    first = service.capture(
        spec,
        recorded_content=(fixture_root / "nbs_pmi_2026_08.html").read_bytes(),
        recorded_headers={
            "content-type": "text/html; charset=utf-8",
            "last-modified": "Mon, 31 Aug 2026 01:00:00 GMT",
        },
        captured_at=first_at,
    )
    assert first.parse_status == "PASS"
    assert {item.series_key for item in first.observations} == {
        "manufacturing_pmi",
        "manufacturing_production_index",
        "manufacturing_new_orders_index",
    }
    pmi_first = next(item for item in first.observations if item.series_key == "manufacturing_pmi")
    assert pmi_first.value == Decimal("49.8")
    assert pmi_first.observation_period == "2026-08"
    assert pmi_first.first_observed is True
    assert pmi_first.first_release is False
    assert pmi_first.first_release_verified is False

    revision_at = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
    revision = service.capture(
        spec,
        recorded_content=(fixture_root / "nbs_pmi_2026_08_revision.html").read_bytes(),
        recorded_headers={
            "content-type": "text/html; charset=utf-8",
            "last-modified": "Wed, 02 Sep 2026 01:00:00 GMT",
        },
        captured_at=revision_at,
    )
    pmi_revision = next(
        item for item in revision.observations if item.series_key == "manufacturing_pmi"
    )
    assert pmi_revision.value == Decimal("49.9")
    assert pmi_revision.first_release is False
    assert pmi_revision.revision.startswith("revision:")

    as_of = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    first_vintage = service.observation_at(
        authority="NBS",
        release_family=spec.release_family,
        series_key="manufacturing_pmi",
        observation_period="2026-08",
        as_of=as_of,
        vintage="FIRST_OBSERVED",
    )
    latest_vintage = service.observation_at(
        authority="NBS",
        release_family=spec.release_family,
        series_key="manufacturing_pmi",
        observation_period="2026-08",
        as_of=as_of,
        vintage="LATEST",
    )
    assert first_vintage is not None and first_vintage.value == Decimal("49.8")
    assert latest_vintage is not None and latest_vintage.value == Decimal("49.9")
    assert (
        service.latest_valid_snapshot(
            authority="NBS",
            release_family=spec.release_family,
            as_of=as_of,
        )
        == revision
    )
    assert (
        service.latest_valid_snapshot(
            authority="NBS",
            release_family=spec.release_family,
            as_of=datetime(2026, 12, 1, tzinfo=UTC),
        )
        is None
    )

    duplicate = service.capture(
        spec,
        recorded_content=(fixture_root / "nbs_pmi_2026_08.html").read_bytes(),
        recorded_headers={"content-type": "text/html; charset=utf-8"},
        captured_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert duplicate.release_id == first.release_id
    assert len(store.macro_releases(authority="NBS", release_family=spec.release_family)) == 2


def test_macro_structured_pboc_and_schema_drift_fail_closed(
    store: InvestorOrchestrationStore,
    tmp_path: Path,
) -> None:
    fixture_root = PROJECT_ROOT / "tests/fixtures/investor_orchestration/macro"
    service = OfficialMacroCaptureService(
        store,
        config_path=PROJECT_ROOT / "configs/macro_current_releases_v1.yaml",
    )
    service.object_store.root = tmp_path / "objects"
    pboc = service.capture(
        service.spec("PBOC", "monetary-credit-social-financing-current"),
        recorded_content=(fixture_root / "pboc_financial_2026_07.html").read_bytes(),
        recorded_headers={
            "content-type": "text/html; charset=utf-8",
            "last-modified": "Fri, 14 Aug 2026 08:30:05 GMT",
        },
        captured_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
    )
    assert pboc.parse_status == "PASS"
    assert {item.series_key: item.value for item in pboc.observations} == {
        "social_financing_stock": Decimal("463.27"),
        "social_financing_stock_yoy": Decimal("7.4"),
        "m2_yoy": Decimal("7.7"),
    }

    nbs_spec = service.spec("NBS", "manufacturing-pmi-current")
    malformed = service.capture(
        nbs_spec,
        recorded_content=(fixture_root / "malformed_release.html").read_bytes(),
        recorded_headers={"content-type": "text/html; charset=utf-8"},
        captured_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert malformed.parse_status == "FAILED"
    assert malformed.observations == ()
    assert any(warning.startswith("OBSERVATION_NOT_FOUND") for warning in malformed.warnings)

    drift_spec = nbs_spec.model_copy(
        update={
            "release_family": "manufacturing-pmi-schema-drift",
            "regex_observations": (
                *nbs_spec.regex_observations,
                {
                    "series_key": "missing_series",
                    "pattern": "template-field-that-does-not-exist=(?P<value>\\d+)",
                },
            ),
        }
    )
    drift = service.capture(
        drift_spec,
        recorded_content=(fixture_root / "nbs_pmi_2026_08.html").read_bytes(),
        recorded_headers={"content-type": "text/html; charset=utf-8"},
        captured_at=datetime(2026, 9, 7, 1, 0, tzinfo=UTC),
    )
    assert drift.parse_status == "PARTIAL"
    assert len(drift.observations) == 3
    assert "OBSERVATION_NOT_FOUND:missing_series" in drift.warnings


def _scheduled_policy() -> Any:
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    return policy_from_config(config)


class _NotificationRecorder:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    def publish(
        self,
        *,
        notification_key: str,
        title: str,
        body: str,
        metadata: Mapping[str, Any],
    ) -> str:
        self.calls.setdefault(
            notification_key,
            {"title": title, "body": body, "metadata": metadata},
        )
        return notification_key


class _PaperReplayRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def replay_confirmed_orders(
        self,
        *,
        run_id: str,
        requested_at: datetime,
        order_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        self.calls.append(
            {
                "run_id": run_id,
                "requested_at": requested_at,
                "order_ids": order_ids,
            }
        )
        return tuple(f"paper-replay:{order_id}" for order_id in order_ids)


def test_state_trading_calendar_fails_closed_without_coverage(
    store: InvestorOrchestrationStore,
) -> None:
    calendar = StateTradingCalendar(store)
    # Canonical official config, not fabricated calendar/session SQL tables.
    assert calendar.is_trading_day(_now().date()) is True
    assert calendar.is_trading_day((_now() - timedelta(days=1)).date()) is False
    with pytest.raises(RuntimeError, match="DATE_NOT_COVERED"):
        calendar.is_trading_day(_now().date().replace(year=2099))


def test_daily_tracking_schedule_uses_injected_trading_calendar() -> None:
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    schedule = DailyTrackingSchedule.from_config(
        config,
        is_trading_day=lambda day: day == _now().date(),
    )
    assert schedule.due(datetime(2026, 9, 7, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))) == (
        (ScheduledWindow.INTRADAY, "2026-09-07:INTRADAY:10:30"),
    )
    assert schedule.due(datetime(2026, 9, 8, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))) == ()


def test_daily_tracking_schedule_has_bounded_catch_up_and_expiry() -> None:
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    schedule = DailyTrackingSchedule.from_config(config, is_trading_day=lambda _: True)
    catch_up = schedule.plan(
        datetime(2026, 9, 7, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        grace_minutes=20,
        catch_up_minutes=180,
        max_catch_up_buckets=1,
    )
    assert catch_up.due == ((ScheduledWindow.PRE_OPEN, "2026-09-07:PRE_OPEN:09:10"),)
    assert catch_up.expired == ()

    post_lunch = schedule.plan(
        datetime(2026, 9, 7, 14, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        grace_minutes=20,
        catch_up_minutes=180,
        max_catch_up_buckets=2,
    )
    assert post_lunch.due == ((ScheduledWindow.INTRADAY, "2026-09-07:INTRADAY:14:00"),)
    assert post_lunch.expired == (
        (ScheduledWindow.PRE_OPEN, "2026-09-07:PRE_OPEN:09:10"),
        (ScheduledWindow.INTRADAY, "2026-09-07:INTRADAY:10:30"),
    )


def test_schedule_tick_cli_runs_due_bucket_and_is_idempotent(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    service = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    policy = _scheduled_policy()
    service.register_policy(policy)
    service.register_binding(
        ScheduledTaskBinding(
            binding_id="tick-three-domain",
            creation_mode="LOCAL_ONLY",
            execution_surface="LOCAL_DAEMON",
            schedule_expression="A_SHARE_PRE_OPEN_INTRADAY_POST_CLOSE",
            timezone="Asia/Shanghai",
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            active=True,
            confirmed_at=_now(),
            consent_hash=content_hash("tick-consent"),
        )
    )
    command = [
        "investor",
        "schedule-tick",
        "tick-three-domain",
        "--at",
        "2026-09-07T10:35:00",
        "--catch-up-minutes",
        "20",
        "--database",
        str(store.path),
    ]
    first = CliRunner().invoke(root_cli_app, command)
    assert first.exit_code == 3, first.output
    first_payload = yaml.safe_load(first.output)
    assert first_payload["status"] == "DEGRADED"
    assert first_payload["due_buckets"] == ["2026-09-07:INTRADAY:10:30"]
    assert first_payload["expired_buckets"] == ["2026-09-07:PRE_OPEN:09:10"]
    assert all(item["economic_write_count"] == 0 for item in first_payload["receipts"])
    assert all(item["outcome"] == "DEGRADED" for item in first_payload["receipts"])

    second = CliRunner().invoke(root_cli_app, command)
    assert second.exit_code == 3, second.output
    second_payload = yaml.safe_load(second.output)
    assert second_payload["status"] == "DEGRADED"
    assert second_payload["due_buckets"] == ["2026-09-07:INTRADAY:10:30"]
    assert second_payload["expired_buckets"] == []
    assert all(item["outcome"] == "DEGRADED" for item in second_payload["receipts"])
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduled_research_runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM scheduled_research_attempts").fetchone()[0]
            == 1
        )
    assert store.completed_schedule_buckets("tick-three-domain") == {"2026-09-07:PRE_OPEN:09:10"}
    checkpoint = store.get_scheduled_checkpoint("tick-three-domain", "2026-09-07:INTRADAY:10:30")
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"


def test_three_domain_scheduled_tracking_is_idempotent_and_advisory(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    subjects = ResearchSubjectRegistryService(store)
    subjects.add_watchlist(
        "600519.XSHG",
        reason="好公司但等待估值和时点",
        available_at=_now(),
        idempotency_key="watch-600519",
    )
    policy = _scheduled_policy()
    assert policy.disclosure_level is DisclosureLevel.MINIMUM
    notifier = _NotificationRecorder()
    service = ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        notification_sink=notifier,
    )
    service.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id="daily-three-domain",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="A_SHARE_PRE_OPEN_INTRADAY_POST_CLOSE",
        timezone="Asia/Shanghai",
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        active=True,
        confirmed_at=_now(),
        consent_hash=content_hash("consent"),
    )
    service.register_binding(binding)
    run_key = content_hash(
        {
            "binding": binding.binding_id,
            "bucket": "2026-09-07:INTRADAY:10:30",
            "policy": policy.policy_hash,
        }
    )
    request = ScheduledRunRequest(
        run_id="run-three-domain",
        binding_id=binding.binding_id,
        schedule_bucket="2026-09-07:INTRADAY:10:30",
        window=ScheduledWindow.INTRADAY,
        domains=tuple(ScheduledDomain),
        requested_at=_now(),
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key=run_key,
    )
    first = service.run(request)
    second = service.run(request)

    assert second == first
    with pytest.raises(ValueError, match="different request"):
        service.run(
            request.model_copy(
                update={
                    "run_id": "run-three-domain-watch-only",
                    "domains": (ScheduledDomain.WATCHLIST,),
                    "idempotency_key": "changed-domain-idempotency",
                }
            )
        )
    assert first.outcome.value == "DEGRADED"
    assert first.economic_write_count == 0
    assert first.watchlist_revision_ids == ()
    assert first.digest_ids == ()
    assert first.paper_replay_ids == ()
    assert first.notification_required is False
    assert len(notifier.calls) == 0
    with store.connect() as connection:
        watch_count = connection.execute(
            "SELECT COUNT(*) FROM watchlist_analysis_revisions"
        ).fetchone()[0]
        run_count = connection.execute("SELECT COUNT(*) FROM scheduled_research_runs").fetchone()[0]
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM scheduled_research_attempts"
        ).fetchone()[0]
    assert watch_count == 0
    assert run_count == 0
    assert attempt_count == 1
    assert store.completed_schedule_buckets(binding.binding_id) == set()
    checkpoint = store.get_scheduled_checkpoint(binding.binding_id, request.schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"


def test_confirmed_paper_replay_passes_only_confirmed_orders(
    store: InvestorOrchestrationStore,
) -> None:
    open_order_id = _seed_existing_state(store)
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    config["domains"]["PAPER_HOLDING"]["action_policy"] = "REPLAY_CONFIRMED_RULES_ONLY"
    config["domains"]["PAPER_HOLDING"]["allow_confirmed_order_replay"] = True
    policy = policy_from_config(config)
    adapter = _PaperReplayRecorder()
    service = ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        paper_replay_adapter=adapter,
    )
    service.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id="paper-confirmed-replay",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="A_SHARE_INTRADAY",
        timezone="Asia/Shanghai",
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        active=True,
        confirmed_at=_now(),
        consent_hash=content_hash("paper-replay-consent"),
    )
    service.register_binding(binding)
    request = ScheduledRunRequest(
        run_id="paper-replay-run",
        binding_id=binding.binding_id,
        schedule_bucket="2026-09-07:INTRADAY:14:00",
        window=ScheduledWindow.INTRADAY,
        domains=(ScheduledDomain.PAPER_HOLDING,),
        requested_at=_now(),
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key="paper-replay-run-key",
    )
    # The real fixture's low-level order is not user-confirmed and must not replay.
    receipt = service.run(request)
    assert adapter.calls == []
    assert receipt.paper_replay_ids == ()
    assert receipt.economic_write_count == 0

    # Exercise the narrow filter with explicit typed unit inputs. This does not
    # certify a real confirmation, replay, fill or business end-to-end scenario.
    preflight = InvestorSessionPreflightService(store).build(
        _request(request_id="replay-filter-unit")
    )
    original = preflight.context.paper.open_orders[0]
    paper = preflight.context.paper.model_copy(
        update={
            "open_orders": (
                original.model_copy(update={"confirmed": True}),
                original.model_copy(update={"order_id": "unconfirmed-unit", "confirmed": False}),
            )
        }
    )
    typed_unit_input = preflight.model_copy(
        update={
            "context": preflight.context.model_copy(update={"paper": paper}),
        }
    )
    reasons: list[str] = []
    replay_ids = service._replay_confirmed_paper_orders(
        policy=policy,
        request=request,
        preflight=typed_unit_input,
        degradation_reasons=reasons,
    )
    assert adapter.calls[0]["order_ids"] == (open_order_id,)
    assert replay_ids == (f"paper-replay:{open_order_id}",)
    assert reasons == []


def test_scheduled_policy_rejects_paper_replay_without_opt_in(
    store: InvestorOrchestrationStore,
) -> None:
    baseline = _scheduled_policy()
    policy = baseline.model_copy(
        update={
            "action_policy_by_domain": {
                **baseline.action_policy_by_domain,
                ScheduledDomain.PAPER_HOLDING: ScheduledActionPolicy.REPLAY_CONFIRMED_RULES_ONLY,
            },
            "confirmed_paper_replay_allowed": False,
        }
    )
    service = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    with pytest.raises(ValueError, match="explicit policy opt-in"):
        service.register_policy(policy)


def test_scheduled_policy_rejects_actual_execution(store: InvestorOrchestrationStore) -> None:
    policy = _scheduled_policy().model_copy(
        update={
            "action_policy_by_domain": {
                **_scheduled_policy().action_policy_by_domain,
                ScheduledDomain.ACTUAL_HOLDING: "REPLAY_CONFIRMED_RULES_ONLY",
            }
        }
    )
    service = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    with pytest.raises(ValueError, match="unsafe action policy"):
        service.register_policy(policy)


def test_gateway_removes_empty_holding_sections_and_internal_metadata(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(request_id="gateway-empty", instruments=())
    preflight = InvestorSessionPreflightService(store).build(request)
    coverage = CapabilityCoverageReceipt(
        receipt_id="coverage-1",
        request_id=request.request_id,
        plan_id="plan-1",
        policy_version="v1",
        records=(
            CapabilityRunRecord(
                capability_id="SESSION_PREFLIGHT",
                status=CapabilityRunStatus.COMPLETED,
            ),
        ),
        required_capability_coverage=1.0,
        prohibited_call_count=0,
        coverage_complete=True,
        receipt_hash="coverage-hash",
    )
    draft = InvestorAnswerDraft(
        request_id=request.request_id,
        conclusion="保持观察",
        reasons=("证据有限",),
        risks=("数据可能变化",),
        actions=("等待新证据",),
        change_conditions=("出现重大公告",),
        actual_holding_section="当前没有正式持仓",
        paper_holding_section="当前没有模拟持仓",
        evidence_as_of=_now(),
        internal_metadata={"provider_error": "must-not-leak"},
    )
    answer = InvestorAnswerGateway().render(
        draft,
        preflight=preflight,
        coverage=coverage,
    )
    assert answer.actual_holding_section is None
    assert answer.paper_holding_section is None
    assert "provider_error" not in answer.model_dump_json()


def test_gateway_replaces_formal_decision_when_required_coverage_is_incomplete(
    store: InvestorOrchestrationStore,
) -> None:
    request = _request(
        request_id="gateway-formal-block",
        intent="BUY_DECISION",
    )
    preflight = InvestorSessionPreflightService(store).build(request)
    coverage = CapabilityCoverageReceipt(
        receipt_id="coverage-formal-block",
        request_id=request.request_id,
        plan_id="plan-formal-block",
        policy_version="v1",
        records=(
            CapabilityRunRecord(
                capability_id="FINANCIAL_INTEGRITY",
                status=CapabilityRunStatus.FAILED,
                reason="missing PIT statement",
            ),
        ),
        required_capability_coverage=0.8,
        prohibited_call_count=0,
        coverage_complete=False,
        unresolved_conflicts=("FINANCIAL_INTEGRITY:missing PIT statement",),
        receipt_hash="coverage-formal-block-hash",
    )
    draft = InvestorAnswerDraft(
        request_id=request.request_id,
        conclusion="立即买入并满仓",
        reasons=("模型看多",),
        risks=("未完成财务核验",),
        actions=("立即下单",),
        change_conditions=("价格变化",),
        evidence_as_of=_now(),
    )
    answer = InvestorAnswerGateway().render(draft, preflight=preflight, coverage=coverage)
    assert answer.degraded is True
    assert "暂不形成" in answer.conclusion
    assert "立即买入" not in answer.conclusion
    assert answer.actions == ("核实关键事实后重新评估。",)
    assert "能力覆盖" not in answer.model_dump_json()


def test_business_scenario_manifest_contains_all_original_ids() -> None:
    manifest = yaml.safe_load(
        (PROJECT_ROOT / "configs/business_scenarios_v1.yaml").read_text(encoding="utf-8")
    )
    expected = [
        1,
        2,
        3,
        *range(11, 15),
        *range(22, 30),
        *range(31, 39),
        *range(41, 49),
        *range(51, 58),
        *range(61, 68),
        *range(71, 78),
        *range(81, 91),
        *range(91, 97),
    ]
    assert manifest["scenario_count"] == 68
    assert [scenario["id"] for scenario in manifest["scenarios"]] == expected
    for scenario in manifest["scenarios"]:
        assert scenario["required"]
        assert scenario["allowed_side_effects"]
        assert set(scenario["required"]).isdisjoint(scenario["prohibited"])


def test_all_68_scenarios_reject_unregistered_success_stub_certification(
    store: InvestorOrchestrationStore,
) -> None:
    from astock.investor_orchestration.capabilities import validate_request_permissions

    MarketRegimeService(
        store,
        PROJECT_ROOT / "configs/market_regime_v2.yaml",
    ).infer(_features(), persist=True)
    orchestration = InvestorOrchestrationService(store)
    runner = ScenarioContractRunner.from_path(
        orchestration,
        PROJECT_ROOT / "configs/business_scenarios_v1.yaml",
    )
    capability_ids = {
        capability_id
        for scenario in runner.manifest.scenarios
        for capability_id in (*scenario.required, *scenario.conditional)
    }

    def success_handler(*_: Any) -> CapabilityExecutionResult:
        return CapabilityExecutionResult(artifact_ids=("fixture-artifact",))

    handlers = {capability_id: success_handler for capability_id in capability_ids}
    assert runner.validate_handlers(handlers) == ()

    for scenario in runner.manifest.scenarios:
        side_effect = scenario.allowed_side_effects[0]
        request = _request(
            request_id=f"scenario-{scenario.id}",
            intent=scenario.intent,
            side_effect=side_effect,
            instruments=("600519.XSHG",),
        )
        draft = InvestorAnswerDraft(
            request_id=request.request_id,
            conclusion="fixture conclusion",
            reasons=("fixture reason",),
            risks=("fixture risk",),
            actions=("fixture action",),
            change_conditions=("fixture condition",),
            evidence_as_of=_now(),
        )
        try:
            validate_request_permissions(request)
        except ValueError as permission_error:
            # Composite account-update/research questions still need separate
            # authorized phases. Refusal is safe, but is NOT scenario completion.
            with pytest.raises(ValueError) as refused:
                runner.run(scenario.id, request, draft, handlers=handlers)
            assert str(refused.value) == str(permission_error)
            continue
        result = runner.run(
            scenario.id,
            request,
            draft,
            handlers=handlers,
        )
        # This is a negative planner/runner contract, not a business E2E run.
        # Non-empty arbitrary IDs cannot authenticate research or formal advice.
        assert result.required_capability_coverage < 1.0
        assert result.prohibited_call_count == 0
        assert result.coverage_complete is False
        assert result.answer.degraded is True
        assert "REQUIRED_CAPABILITY_COVERAGE" in result.failures


def test_preflight_same_revision_is_built_once_under_concurrency(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    service = InvestorSessionPreflightService(store)
    original = service._build_state
    build_count = 0

    def counted_build(*, as_of: datetime, revisions: Mapping[str, str]) -> Any:
        nonlocal build_count
        build_count += 1
        time.sleep(0.02)
        return original(as_of=as_of, revisions=revisions)

    service._build_state = counted_build  # type: ignore[method-assign]
    requests = [_request(request_id=f"concurrent-{index}") for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = tuple(executor.map(service.build, requests))
    assert len(receipts) == 8
    assert build_count == 1
    assert sum(receipt.built_from_cache for receipt in receipts) == 7


def test_preflight_warm_cache_is_fast_on_100_requests(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    service = InvestorSessionPreflightService(store)
    service.build(_request(request_id="warm-0"))
    samples = []
    receipts = []
    for index in range(100):
        started = time.perf_counter()
        receipts.append(service.build(_request(request_id=f"warm-{index + 1}")))
        samples.append(time.perf_counter() - started)
    assert all(receipt.built_from_cache for receipt in receipts)
    # The frozen requirement is per-request p95 <= 2s, not total time for 100 writes.
    assert sorted(samples)[94] <= 2.0


def test_watchlist_auto_enrollment_requires_quality_and_waiting_timing(
    store: InvestorOrchestrationStore,
) -> None:
    service = ResearchSubjectRegistryService(store)
    assert (
        service.enroll_from_analysis(
            "600519.XSHG",
            formal_readiness="REJECT",
            timing_status="WAIT",
            reason="quality failed",
            available_at=_now(),
        )
        is None
    )
    assert (
        service.enroll_from_analysis(
            "600519.XSHG",
            formal_readiness="QUALIFIED",
            timing_status="READY_NOW",
            reason="already actionable",
            available_at=_now(),
        )
        is None
    )
    enrolled = service.enroll_from_analysis(
        "600519.XSHG",
        formal_readiness="QUALIFIED",
        timing_status="TIMING_NOT_READY",
        reason="good target, wait for valuation and price",
        available_at=_now(),
    )
    assert enrolled is not None
    assert enrolled.event_type is SubjectEventKind.WATCHLIST_ADDED


def test_activation_is_fail_closed_until_real_shadow_and_live_evidence(
    store: InvestorOrchestrationStore,
) -> None:
    service = ActivationGateService(
        store,
        config_path=PROJECT_ROOT / "configs/investor_orchestration_activation_v1.yaml",
    )
    assessment = service.assess(
        "market_regime_v2",
        recorded_scenario_count=68,
        owner_approval_id="owner-approval",
        assessed_at=_now(),
    )
    assert assessment.eligible is False
    assert "shadow_observations" in assessment.blockers
    assert "current_macro_controlled_live" in assessment.blockers
    assert service.status("market_regime_v2") is AdmissionStatus.SHADOW_ONLY


def test_activation_and_rollback_never_write_account_or_paper_ledgers(
    store: InvestorOrchestrationStore,
) -> None:
    _seed_existing_state(store)
    service = ActivationGateService(
        store,
        config_path=PROJECT_ROOT / "configs/investor_orchestration_activation_v1.yaml",
    )
    before = store.revision_for_tables(
        (
            "external_account_event",
            "paper_account",
            "journal",
            "ledger_entry",
            "position",
            "order_record",
            "fill",
        )
    )
    for index in range(100):
        service.record_shadow(
            shadow_observation(
                feature_id="market_regime_v2",
                request_or_run_id=f"shadow-{index}",
                observed_at=_now() + timedelta(days=index // 5),
                required_capability_coverage=1.0,
                baseline_action="HOLD",
                candidate_action="HOLD",
            )
        )
    for check_type in ("CURRENT_MACRO", "SCHEDULED_RESEARCH"):
        body = {
            "check_type": check_type,
            "checked_at": _now(),
            "status": "PASS",
            "evidence_ids": (f"evidence-{check_type}",),
            "details": "controlled-live fixture passed",
        }
        service.record_controlled_live(
            ControlledLiveCheck(
                check_id=f"check-{check_type.lower()}",
                **body,  # type: ignore[arg-type]
            )
        )
    assessment = service.assess(
        "market_regime_v2",
        recorded_scenario_count=68,
        owner_approval_id="owner-approval",
        assessed_at=_now(),
    )
    # Synthetic observations/IDs and a caller-supplied count are not accepted
    # live, prospective or owner-approval evidence, regardless of their quantity.
    assert assessment.eligible is False
    assert assessment.recorded_scenario_count == 0
    assert assessment.shadow_observation_count == 0
    with pytest.raises(ValueError, match="activation gates"):
        service.activate(assessment, feature_flags={"market_regime_v2_enabled": True})
    rolled_back = service.rollback("market_regime_v2", reason="rollback drill")
    assert rolled_back.new_status is AdmissionStatus.ROLLED_BACK
    assert rolled_back.ledger_write_count == 0
    after = store.revision_for_tables(
        (
            "external_account_event",
            "paper_account",
            "journal",
            "ledger_entry",
            "position",
            "order_record",
            "fill",
        )
    )
    assert after == before


def test_paper_status_reuses_single_audited_snapshot_in_one_cli_call() -> None:
    ledger_source = (PROJECT_ROOT / "src/astock/paper_trading/ledger.py").read_text(
        encoding="utf-8"
    )
    cli_source = (PROJECT_ROOT / "src/astock/cli.py").read_text(encoding="utf-8")
    assert "paper-status-single-snapshot-v1" in ledger_source
    assert "_consume_prefetched_status(account_id) or self.status" in ledger_source
    assert "paper-status-prime-single-snapshot-v1" in cli_source
    assert ".prime_status_snapshot(" in cli_source
