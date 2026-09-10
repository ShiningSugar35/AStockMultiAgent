from __future__ import annotations

import importlib.util
import json
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from astock.cli import app as root_cli_app
from astock.core.hashing import content_hash
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.external_accounts import ExternalAccountRepository
from astock.investor_orchestration.activation import ActivationGateService
from astock.investor_orchestration.capabilities import CapabilityExecutionResult
from astock.investor_orchestration.macro import MacroReleaseSpec, OfficialMacroCaptureService
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    MaterialChangeDigest,
    RequestIntent,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledSemanticSubjectResult,
    ScheduledTaskBinding,
    ScheduledWindow,
    SideEffectClass,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import (
    ScheduledResearchService,
    build_scheduled_run_request,
    policy_from_config,
)
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditRequest,
    ScheduledInputCoverageService,
    ScheduledSourceScope,
    load_input_audit_policy,
)
from astock.investor_orchestration.scheduled_semantic_worker import (
    ScheduledSemanticSubmission,
    ScheduledSemanticWorkInProgress,
    ScheduledSemanticWorkService,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.investor_orchestration.utils import content_hash as scheduled_content_hash
from astock.monitoring.news import GdeltNewsLeadProvider
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.paper_trading import LedgerService, PaperOperationService, load_fee_schedule
from astock.paper_trading.operation import PaperInstrumentTradingFacts
from astock.schemas import DisclosureExchange, DisclosureSearchRequest
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
from tests.integration.test_business_scenarios_real_e2e import (
    COMPANY,
    RecordedBank,
    _build_recorded_bank,
)
from tests.unit.test_paper_operations import (
    _SERVICE_SECURITY,
    _ReferenceFixture,
)
from tests.unit.test_paper_operations import (
    NOW as PAPER_NOW,
)
from tests.unit.test_paper_operations import (
    _confirmation as _paper_confirmation,
)
from tests.unit.test_paper_operations import (
    _request as _paper_request,
)
from tests.unit.test_scheduled_paper_replay_adapter import (
    _adapter as _paper_replay_adapter,
)
from tests.unit.test_scheduled_paper_replay_adapter import (
    _ReplayRecorder,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ThreeDomainPaperReference(_ReferenceFixture):
    def trading_classification(
        self,
        instrument: Any,
        *,
        visible_at: datetime,
    ) -> PaperInstrumentTradingFacts:
        if instrument.symbol != COMPANY:
            return super().trading_classification(instrument, visible_at=visible_at)
        return PaperInstrumentTradingFacts(
            board="MAIN",
            risk_status="NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id="recorded-three-domain-main-board",
        )


def _controlled_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "scheduled_controlled_live_test",
        PROJECT_ROOT / "scripts" / "run_investor_orchestration_controlled_live.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_material_monitor_event(
    bank: RecordedBank,
    *,
    run_id: str,
    available_at: datetime,
) -> None:
    repository = ContinuousMonitorRepository(bank.state, bank.objects)
    target = repository.enroll(
        MonitorTargetEnrollRequest(
            symbol=COMPANY,
            market=Market.XSHG,
            company_id=COMPANY,
            display_name="recorded controlled target",
            reason=MonitorTargetReason.MANUAL,
            created_at=available_at - timedelta(minutes=1),
        )
    )
    payload: dict[str, object] = {"summary": "公司公告出现重大变化，需要复核原投资逻辑。"}
    event = MonitorEvent(
        event_id=f"monitor-event:{run_id}",
        target_id=target.target_id,
        company_id=COMPANY,
        event_type=MonitorEventType.OFFICIAL_DISCLOSURE,
        severity=MonitorSeverity.MATERIAL,
        observed_at=available_at - timedelta(seconds=1),
        available_at=available_at,
        source=MonitorSource.CNINFO,
        source_ref=f"recorded:{run_id}",
        payload=payload,
        payload_hash=content_hash(payload),
        dedupe_key=content_hash({"run_id": run_id, "kind": "material-disclosure"}),
        affected_modules=[],
        requires_research=False,
        created_at=available_at,
    )
    stored, inserted = repository.record_event(event)
    assert inserted and stored == event


def _handler_artifact(
    bank: RecordedBank,
    capability_id: str,
    request: InvestorRequestEnvelope,
) -> str:
    handler = cast(
        Callable[[Any, Any], CapabilityExecutionResult],
        bank.handlers[capability_id],
    )
    result = handler(request, None)
    if len(result.artifact_ids) != 1:
        raise AssertionError(f"recorded {capability_id} handler did not return one artifact")
    return result.artifact_ids[0]


def _canonical_market_anchor(bank: RecordedBank, *, symbol: str = COMPANY) -> str:
    """Use the real recorded reference/raw/Parquet path, not a self-certified price.

    This dual-gate test runs outside market hours too: it audits prior-close
    prices under PRE_OPEN, leaving the production intraday freshness unchanged.
    """
    from zoneinfo import ZoneInfo

    from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews
    from astock.market_data.reference import MarketReferenceService
    from astock.market_data.reference_storage import ReferenceParquetStore
    from astock.schemas.institutional_research import MarketPriceAnchor

    root = bank.state.path.parent
    fixtures = root / "scheduled-reference-fixtures"
    shutil.copytree(PROJECT_ROOT / "tests/fixtures/reference", fixtures)
    at = datetime.now(UTC)
    day = at.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    days = []
    while len(days) < 3:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    path = fixtures / "baostock/market_daily_unadjusted.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["request_started_at"] = (at - timedelta(seconds=1)).isoformat()
    raw["request_finished_at"] = at.isoformat()
    for row, session in zip(raw["rows"], days, strict=True):
        row[0] = session.isoformat()
        row[1] = f"sh.{symbol}"
    path.write_text(json.dumps(raw), encoding="utf-8")
    reference = MarketReferenceService(
        bank.state, bank.objects, ReferenceParquetStore(root / "data/parquet"), fixtures
    )
    report = reference.sync_daily(symbol, Market.XSHG, days[0], days[-1])
    assert report.release_id
    views = CanonicalRegimeReferenceViews(bank.state, bank.objects)
    at = datetime.now(UTC)
    locators = views.daily_locators(f"XSHG:{symbol}", as_of=at)
    source, bar = views.load(locators[-1])
    anchor = MarketPriceAnchor(
        price=bar["close"],
        observed_at=bar["session_close_at"],
        available_to_system_at=bar["available_to_system_at"],
        created_at=at,
        source_artifact_id=source["parent_artifact_id"],
        source_object_hash=source["object_hash"],
    )
    obj = bank.objects.put_json(anchor.model_dump(mode="json"))
    identity = f"MarketPriceAnchor:canonical-scheduled:{obj.sha256}"
    bank.state.register_artifact(
        artifact_id=identity,
        artifact_type="MarketPriceAnchor",
        schema_version=anchor.schema_version,
        object_hash=obj.sha256,
        input_hashes=[anchor.source_object_hash],
    )
    return identity


def _semantic_monitor_receipt(
    bank: RecordedBank,
    run_id: str,
    *,
    question_time: datetime | None = None,
    market_artifact_id: str | None = None,
    symbol: str = COMPANY,
):
    instrument_id = f"XSHG:{symbol}"
    request = InvestorRequestEnvelope(
        request_id=f"scheduled:{run_id}",
        question_time=question_time or bank.question_time,
        user_timezone="Asia/Shanghai",
        market_timezone="Asia/Shanghai",
        raw_text="scheduled semantic coverage integration",
        normalized_intent=RequestIntent.MONITOR,
        side_effect=SideEffectClass.META,
        entity_ids=(instrument_id,),
        idempotency_key=f"semantic:{run_id}",
    )
    current_market_id = market_artifact_id or _handler_artifact(bank, "CURRENT_MARKET", request)
    event_research_id = _handler_artifact(bank, "EVENT_RESEARCH", request)
    _, plan, receipt = InvestorOrchestrationService(bank.store).execute_registered_inputs(
        request,
        {
            "CURRENT_MARKET": (current_market_id,),
            "EVENT_RESEARCH": (event_research_id,),
        },
    )
    assert receipt.coverage_complete
    assert receipt.required_capability_coverage == 1.0
    assert receipt.prohibited_call_count == 0
    assert {node.capability_id for node in plan.nodes if node.requirement.value == "REQUIRED"}
    return request, receipt


def _five_family_source_report(
    bank: RecordedBank,
    *,
    run_id: str,
    market_artifact_id: str,
    symbol: str = COMPANY,
):
    interval_end = datetime.now(UTC) - timedelta(seconds=2)
    interval_start = interval_end - timedelta(hours=2)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"articles": []}))
    ) as client:
        news = GdeltNewsLeadProvider(bank.objects, bank.state, client=client)
        company = news.search_checked(
            names=["semantic recorded issuer"],
            symbol=symbol,
            start=interval_start,
            end=interval_end,
            max_records=20,
        )
        industry = news.search_checked(
            names=["semantic recorded liquor industry"],
            symbol=symbol,
            start=interval_start,
            end=interval_end,
            max_records=20,
        )

    disclosure_body = {
        "announcements": [
            {
                "announcementId": f"semantic-announcement-{run_id}",
                "adjunctUrl": "2026-09-08/semantic.pdf",
                "announcementTime": int(interval_start.timestamp() * 1000),
                "announcementTitle": "语义定时验收公告",
                "secCode": symbol,
                "secName": "测试公司",
                "orgId": "semantic-org",
            }
        ],
        "totalAnnouncement": 1,
        "totalpages": 1,
        "hasMore": False,
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=disclosure_body))
    ) as client:
        disclosure = CninfoDisclosureProvider(bank.objects, bank.state, client=client)
        batches = disclosure.search_all(
            DisclosureSearchRequest(
                symbol=symbol,
                exchange=DisclosureExchange.SSE,
                start_date=interval_start.date(),
                end_date=interval_end.date(),
            )
        )
    disclosure_ids = []
    for batch in batches:
        source = bank.state.get_snapshot(batch.raw_snapshot_id)
        assert source is not None
        ref = bank.objects.put_json(batch.model_dump(mode="json"))
        artifact_id = f"DisclosureSearchBatch:{batch.batch_id}"
        bank.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="DisclosureSearchBatch",
            schema_version=batch.schema_version,
            object_hash=ref.sha256,
            input_hashes=[
                source.object_sha256,
                content_hash(batch.request.model_dump(mode="json")),
            ],
        )
        disclosure_ids.append(artifact_id)

    policy_capture = OfficialMacroCaptureService(bank.store).capture_checked(
        MacroReleaseSpec(
            authority="NBS",
            release_family="RECORDED_POLICY_FEED",
            url="https://www.stats.gov.cn/semantic-policy",
            parser="regex-v1",
            regex_observations=(
                {
                    "series_key": "semantic-policy-index",
                    "pattern": r"INDEX=(?P<value>[0-9.]+)",
                    "period": "2026-09",
                    "unit": "index",
                },
            ),
        ),
        recorded_content=b"<html>INDEX=50.1</html>",
        captured_at=datetime.now(UTC),
    )
    requested_at = datetime.now(UTC)
    audit = ScheduledInputCoverageService(bank.store, load_input_audit_policy())
    request = ScheduledInputAuditRequest(
        run_id=run_id,
        domain=ScheduledDomain.WATCHLIST,
        window=ScheduledWindow.PRE_OPEN,
        scope=ScheduledSourceScope(
            instrument_id=f"XSHG:{symbol}",
            company_query=company.query,
            industry_query=industry.query,
            policy_families=(("NBS", "RECORDED_POLICY_FEED"),),
        ),
        interval_start=interval_start,
        interval_end=interval_end,
        as_of=requested_at,
        market_references=(market_artifact_id,),
        company_news_snapshot_ids=(company.snapshot_id,),
        industry_news_snapshot_ids=(industry.snapshot_id,),
        disclosure_batch_artifact_ids=tuple(disclosure_ids),
        policy_capture_artifact_ids=(policy_capture.capture_artifact_id,),
    )
    report_id = audit.register(request)
    report = audit.verify_registered(report_id)
    assert report.all_families_checked, report.checks
    return audit, request, report_id


def test_scheduled_semantic_receipt_binds_real_monitor_coverage_to_exact_subject(
    tmp_path: Path,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "semantic-monitor-positive"
    request, semantic = _semantic_monitor_receipt(bank, run_id)
    audit = ScheduledInputCoverageService(bank.store, load_input_audit_policy())
    envelope = audit.register_run_coverage(
        run_id=run_id,
        window=ScheduledWindow.PRE_OPEN,
        requested_at=request.question_time,
        subjects={ScheduledDomain.WATCHLIST: request.entity_ids},
        report_ids=(),
        semantic_capability_receipt_id=semantic.receipt_id,
    )

    assert envelope.semantic_coverage_complete
    assert envelope.semantic_capability_receipt_id == semantic.receipt_id
    assert not envelope.all_families_checked
    assert envelope.degradation_reasons == (
        f"SOURCE_CHECK_MISSING:WATCHLIST:{request.entity_ids[0]}",
    )
    assert audit.verify_registered_run_coverage(envelope.receipt_id) == envelope


def test_scheduled_semantic_receipt_cannot_cover_another_subject(tmp_path: Path) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "semantic-monitor-wrong-subject"
    request, semantic = _semantic_monitor_receipt(bank, run_id)
    audit = ScheduledInputCoverageService(bank.store, load_input_audit_policy())

    with pytest.raises(ValueError, match="authenticated MONITOR coverage"):
        audit.register_run_coverage(
            run_id=run_id,
            window=ScheduledWindow.PRE_OPEN,
            requested_at=request.question_time,
            subjects={ScheduledDomain.WATCHLIST: ("XSHE:000001",)},
            report_ids=(),
            semantic_capability_receipt_id=semantic.receipt_id,
        )


def test_source_and_semantic_gates_together_produce_authentic_scheduled_coverage(
    tmp_path: Path,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "semantic-monitor-full-double-gate"
    market_artifact_id = _canonical_market_anchor(bank)
    audit, source_request, report_id = _five_family_source_report(
        bank,
        run_id=run_id,
        market_artifact_id=market_artifact_id,
    )
    semantic_request, semantic = _semantic_monitor_receipt(
        bank,
        run_id,
        question_time=source_request.as_of,
        market_artifact_id=market_artifact_id,
    )
    envelope = audit.register_run_coverage(
        run_id=run_id,
        window=source_request.window,
        requested_at=source_request.as_of,
        subjects={ScheduledDomain.WATCHLIST: semantic_request.entity_ids},
        report_ids=(report_id,),
        interval_start=source_request.interval_start,
        interval_end=source_request.interval_end,
        semantic_capability_receipt_id=semantic.receipt_id,
    )

    assert envelope.all_families_checked
    assert envelope.semantic_coverage_complete
    assert envelope.degradation_reasons == ()
    assert envelope.expected_subject_count == envelope.verified_subject_count == 1
    assert ActivationGateService(bank.store).evidence.valid_coverage(
        f"scheduled:{run_id}", envelope.receipt_id
    )


def test_scheduled_semantic_preflight_rejects_monitor_drift_after_research(
    tmp_path: Path,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "semantic-monitor-drift"
    semantic_request, semantic = _semantic_monitor_receipt(bank, run_id)
    _seed_material_monitor_event(
        bank,
        run_id=f"{run_id}:after-semantic",
        available_at=datetime.now(UTC),
    )
    request = ScheduledRunRequest(
        run_id=run_id,
        binding_id="drift-binding",
        schedule_bucket=f"{run_id}:PRE_OPEN",
        window=ScheduledWindow.PRE_OPEN,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=semantic_request.question_time,
        source_revision_set={},
        policy_hash="drift-policy",
        idempotency_key=f"drift:{run_id}",
        semantic_capability_receipt_id=semantic.receipt_id,
    )
    service = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
    )

    with pytest.raises(ValueError, match="monitor"):
        service._preflight_for_run(semantic_request, request)


def test_controlled_live_consumes_prepared_double_gate_without_enabling_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "prepared-controlled-double-gate"
    market_artifact_id = _canonical_market_anchor(bank)
    _audit, source_request, report_id = _five_family_source_report(
        bank,
        run_id=run_id,
        market_artifact_id=market_artifact_id,
    )
    _seed_material_monitor_event(
        bank,
        run_id=run_id,
        available_at=source_request.interval_end,
    )
    instrument_id = f"XSHG:{COMPANY}"
    ResearchSubjectRegistryService(bank.store).add_watchlist(
        instrument_id,
        reason="prepared controlled scheduled validation",
        available_at=source_request.interval_start,
        idempotency_key=f"watchlist:{run_id}",
    )
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "scheduled_investor_tracking_v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    policy = policy_from_config(config)
    scheduler = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
    )
    scheduler.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id=f"binding:{run_id}",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="PREPARED_CONTROLLED_RECORDED",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=source_request.interval_start,
        consent_hash=content_hash(f"consent:{run_id}"),
    )
    scheduler.register_binding(binding)
    wake_at = source_request.interval_end
    unprepared = ScheduledRunRequest(
        run_id=run_id,
        binding_id=binding.binding_id,
        schedule_bucket=f"{run_id}:INTRADAY",
        window=source_request.window,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=wake_at,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key=f"prepared:{run_id}",
    )
    degraded = scheduler.run(unprepared)
    assert degraded.outcome.value == "DEGRADED"
    assert degraded.preflight_receipt_id is None
    assert degraded.degradation_reasons == ("RESEARCH_INPUTS_PENDING",)
    assert bank.store.get_preflight_for_request(f"scheduled:{run_id}") is None
    assert degraded.digest_ids == () and degraded.watchlist_revision_ids == ()
    assert bank.store.completed_schedule_buckets(binding.binding_id) == set()
    assert (
        bank.store.get_scheduled_receipt(
            binding_id=binding.binding_id, schedule_bucket=unprepared.schedule_bucket
        )
        is None
    )
    semantic_request, semantic = _semantic_monitor_receipt(
        bank,
        run_id,
        question_time=source_request.as_of,
        market_artifact_id=market_artifact_id,
    )
    assert semantic_request.entity_ids == (instrument_id,)
    digest_body = {
        "run_id": run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": source_request.as_of,
        "severity": "HIGH",
        "change_summary": "语义研究确认公告变化需要重新评估观察名单结论。",
        "impact_summary": "只更新研究判断，不形成真实或模拟新交易。",
        "action": "RESEARCH",
        "action_conditions": ("复核公告原文和原投资逻辑",),
        "actual_execution_allowed": False,
        "source_artifact_ids": (report_id,),
    }
    digest_hash = scheduled_content_hash(digest_body)
    digest = MaterialChangeDigest(
        digest_id=f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}",
        **digest_body,
    )
    digest_artifact_id = f"MaterialChangeDigest:{digest_hash}"
    result_body = {
        "run_id": run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": source_request.as_of,
        "material_change": True,
        "digest_artifact_id": digest_artifact_id,
        "evidence_artifact_ids": (report_id,),
        "actual_execution_allowed": False,
    }
    result_hash = scheduled_content_hash(
        {
            "schema_version": "scheduled-semantic-subject-result-v1",
            **result_body,
        }
    )
    semantic_result = ScheduledSemanticSubjectResult(
        result_id=f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}",
        result_hash=result_hash,
        **result_body,
    )
    semantic_result_id = scheduler.register_semantic_result(semantic_result, digest=digest)
    prepared = unprepared.model_copy(
        update={
            "requested_at": source_request.as_of,
            "source_coverage_artifact_ids": (report_id,),
            "source_interval_start": source_request.interval_start,
            "source_interval_end": source_request.interval_end,
            "semantic_capability_receipt_id": semantic.receipt_id,
            "semantic_result_artifact_ids": (semantic_result_id,),
        }
    )
    assert scheduler.stage(prepared) == prepared
    checkpoint = bank.store.get_scheduled_checkpoint(binding.binding_id, unprepared.schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "READY"
    assert bank.store.completed_schedule_buckets(binding.binding_id) == set()
    request_file = tmp_path / "prepared-scheduled-request.json"
    request_file.write_text(prepared.model_dump_json(indent=2), encoding="utf-8")

    economic_tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
    )

    def economics():
        with bank.store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in economic_tables
            }

    before = economics()
    module = _controlled_module()
    monkeypatch.setattr(
        "sys.argv",
        [
            "controlled-live",
            "--database",
            str(bank.store.path),
            "--scheduled-request-file",
            str(request_file),
        ],
    )
    module.main()
    payload = json.loads(capsys.readouterr().out)
    assert economics() == before
    statuses = {item["check_type"]: item["status"] for item in payload["checks"]}
    assert statuses == {
        "CURRENT_MACRO": "NOT_RUN",
        "SCHEDULED_RESEARCH_RECORDED": "PASS",
        "SCHEDULED_RESEARCH": "BLOCKED",
    }
    recorded_check = next(
        item
        for item in payload["checks"]
        if item["check_type"] == "SCHEDULED_RESEARCH_RECORDED"
    )
    assert len(recorded_check["evidence_ids"]) == 1
    assert recorded_check["evidence_ids"][0].startswith("ScheduledCapabilityCoverageReceipt:")
    controlled_check = next(
        item for item in payload["checks"] if item["check_type"] == "SCHEDULED_RESEARCH"
    )
    assert controlled_check["evidence_ids"] == []
    assert "NOT_CERTIFIED" in controlled_check["details"]
    assert payload["feature_activation_changed"] is False


def test_ordinary_schedule_run_recovers_staged_source_and_semantic_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "scheduled_investor_tracking_v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    policy = policy_from_config(config)
    scheduler = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
    )
    scheduler.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id="binding:ordinary-stage-recovery",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_ORDINARY_STAGE_RECOVERY",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=datetime.now(UTC) - timedelta(hours=1),
        consent_hash=scheduled_content_hash("ordinary-stage-recovery-consent"),
    )
    scheduler.register_binding(binding)
    schedule_bucket = "2026-09-09:PRE_OPEN:ordinary-stage-recovery"
    seed = build_scheduled_run_request(
        binding,
        window=ScheduledWindow.PRE_OPEN,
        schedule_bucket=schedule_bucket,
        requested_at=datetime.now(UTC),
        domains=(ScheduledDomain.WATCHLIST,),
    )
    market_artifact_id = _canonical_market_anchor(bank)
    _audit, source_request, report_id = _five_family_source_report(
        bank,
        run_id=seed.run_id,
        market_artifact_id=market_artifact_id,
    )
    scheduled_at = source_request.as_of
    monkeypatch.setattr(
        "astock.investor_orchestration.cli._aware_now",
        lambda _timezone: scheduled_at,
    )
    _seed_material_monitor_event(
        bank,
        run_id=seed.run_id,
        available_at=source_request.interval_end,
    )
    instrument_id = f"XSHG:{COMPANY}"
    ResearchSubjectRegistryService(bank.store).add_watchlist(
        instrument_id,
        reason="ordinary staged schedule-run recovery",
        available_at=source_request.interval_start,
        idempotency_key=f"watchlist:{seed.run_id}",
    )
    command = [
        "investor",
        "schedule-run",
        binding.binding_id,
        ScheduledWindow.PRE_OPEN.value,
        "--domain",
        ScheduledDomain.WATCHLIST.value,
        "--schedule-bucket",
        schedule_bucket,
        "--database",
        str(bank.store.path),
    ]
    runner = CliRunner()
    first = runner.invoke(root_cli_app, command)
    assert first.exit_code == 3, first.output
    first_payload = json.loads(first.stdout)
    assert first_payload["outcome"] == "DEGRADED"
    assert first_payload["digest_ids"] == []
    assert first_payload["economic_write_count"] == 0
    assert bank.store.completed_schedule_buckets(binding.binding_id) == set()
    assert (
        bank.store.get_scheduled_receipt(
            binding_id=binding.binding_id,
            schedule_bucket=schedule_bucket,
        )
        is None
    )
    checkpoint = bank.store.get_scheduled_checkpoint(binding.binding_id, schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"
    pending = ScheduledRunRequest.model_validate(checkpoint["request"])
    assert pending.run_id == seed.run_id
    assert pending.requested_at == scheduled_at

    semantic_request, semantic = _semantic_monitor_receipt(
        bank,
        pending.run_id,
        question_time=pending.requested_at,
        market_artifact_id=market_artifact_id,
    )
    assert semantic_request.entity_ids == (instrument_id,)
    digest_body = {
        "run_id": pending.run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": pending.requested_at,
        "severity": "HIGH",
        "change_summary": "普通定时入口已取得完整来源与语义结果，需要复核观察名单结论。",
        "impact_summary": "只更新研究判断，不产生任何真实或模拟新交易。",
        "action": "RESEARCH",
        "action_conditions": ("复核冻结来源与原投资逻辑",),
        "actual_execution_allowed": False,
        "source_artifact_ids": (report_id,),
    }
    digest_hash = scheduled_content_hash(digest_body)
    digest = MaterialChangeDigest(
        digest_id=f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}",
        **digest_body,
    )
    digest_artifact_id = f"MaterialChangeDigest:{digest_hash}"
    result_body = {
        "run_id": pending.run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": pending.requested_at,
        "material_change": True,
        "digest_artifact_id": digest_artifact_id,
        "evidence_artifact_ids": (report_id,),
        "actual_execution_allowed": False,
    }
    result_hash = scheduled_content_hash(
        {
            "schema_version": "scheduled-semantic-subject-result-v1",
            **result_body,
        }
    )
    semantic_result = ScheduledSemanticSubjectResult(
        result_id=f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}",
        result_hash=result_hash,
        **result_body,
    )
    semantic_result_id = scheduler.register_semantic_result(semantic_result, digest=digest)
    prepared = pending.model_copy(
        update={
            "source_coverage_artifact_ids": (report_id,),
            "source_interval_start": source_request.interval_start,
            "source_interval_end": source_request.interval_end,
            "semantic_capability_receipt_id": semantic.receipt_id,
            "semantic_result_artifact_ids": (semantic_result_id,),
        }
    )
    prepared_file = tmp_path / "ordinary-prepared-run.json"
    prepared_file.write_text(prepared.model_dump_json(indent=2), encoding="utf-8")
    stage = runner.invoke(
        root_cli_app,
        [
            "investor",
            "schedule-run-stage",
            str(prepared_file),
            "--database",
            str(bank.store.path),
        ],
    )
    assert stage.exit_code == 0, stage.output
    stage_payload = json.loads(stage.stdout)
    assert stage_payload["checkpoint_status"] == "READY"

    economic_tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
    )

    def economics() -> dict[str, list[tuple[object, ...]]]:
        with bank.store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in economic_tables
            }

    before = economics()
    recovered = runner.invoke(root_cli_app, command)
    assert recovered.exit_code == 0, recovered.output
    recovered_payload = json.loads(recovered.stdout)
    assert recovered_payload["outcome"] == "MATERIAL_CHANGE"
    assert recovered_payload["source_coverage_complete"] is True
    assert recovered_payload["degradation_reasons"] == []
    assert len(recovered_payload["digest_ids"]) == 1
    assert len(recovered_payload["watchlist_revision_ids"]) == 1
    assert recovered_payload["economic_write_count"] == 0
    assert economics() == before
    assert bank.store.completed_schedule_buckets(binding.binding_id) == {schedule_bucket}

    repeated = runner.invoke(root_cli_app, command)
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout) == recovered_payload
    assert economics() == before


def test_semantic_worker_lease_claim_submit_and_final_run_are_single_owner(
    tmp_path: Path,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    run_id = "semantic-worker-single-owner"
    market_artifact_id = _canonical_market_anchor(bank)
    _audit, source_request, report_id = _five_family_source_report(
        bank,
        run_id=run_id,
        market_artifact_id=market_artifact_id,
    )
    instrument_id = f"XSHG:{COMPANY}"
    ResearchSubjectRegistryService(bank.store).add_watchlist(
        instrument_id,
        reason="semantic worker single-owner integration",
        available_at=source_request.interval_start,
        idempotency_key=f"watchlist:{run_id}",
    )
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "scheduled_investor_tracking_v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    policy = policy_from_config(config)
    scheduler = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
    )
    scheduler.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id=f"binding:{run_id}",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_SEMANTIC_WORKER",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=source_request.interval_start,
        consent_hash=scheduled_content_hash(f"consent:{run_id}"),
    )
    scheduler.register_binding(binding)
    bucket = f"{run_id}:PRE_OPEN"
    source_prepared = ScheduledRunRequest(
        run_id=run_id,
        binding_id=binding.binding_id,
        schedule_bucket=bucket,
        window=source_request.window,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=source_request.as_of,
        source_revision_set={report_id: scheduled_content_hash(report_id)},
        policy_hash=policy.policy_hash,
        idempotency_key=f"semantic-worker:{run_id}",
        source_coverage_artifact_ids=(report_id,),
        source_interval_start=source_request.interval_start,
        source_interval_end=source_request.interval_end,
    )
    assert scheduler.stage(source_prepared) == source_prepared

    worker = ScheduledSemanticWorkService(bank.store)
    work = worker.claim(
        binding.binding_id,
        bucket,
        owner_id="semantic-worker-a",
        lease_seconds=300,
    )
    assert work.scheduled_request == source_prepared
    assert work.investor_request.request_id == f"scheduled:{run_id}"
    assert work.investor_request.entity_ids == (instrument_id,)
    assert work.actual_execution_allowed is False
    assert len(work.subjects) == 1
    assert not hasattr(work.subjects[0].context.context, "actual")
    assert not hasattr(work.subjects[0].context.context, "paper")
    with pytest.raises(ScheduledSemanticWorkInProgress, match="already claimed"):
        ScheduledSemanticWorkService(bank.store).claim(
            binding.binding_id,
            bucket,
            owner_id="semantic-worker-b",
            lease_seconds=300,
        )

    event_artifact_id = _handler_artifact(bank, "EVENT_RESEARCH", work.investor_request)
    capability_artifacts = {
        "CURRENT_MARKET": (market_artifact_id,),
        "EVENT_RESEARCH": (event_artifact_id,),
    }
    digest_body = {
        "run_id": run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": source_request.as_of,
        "severity": "HIGH",
        "change_summary": "唯一语义worker确认重大变化，需要复核观察名单结论。",
        "impact_summary": "只更新研究判断，不产生真实或模拟新交易。",
        "action": "RESEARCH",
        "action_conditions": ("复核冻结来源与原投资逻辑",),
        "actual_execution_allowed": False,
        "source_artifact_ids": (report_id,),
    }
    digest_hash = scheduled_content_hash(digest_body)
    digest = MaterialChangeDigest(
        digest_id=f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}",
        **digest_body,
    )
    digest_artifact_id = f"MaterialChangeDigest:{digest_hash}"
    result_body = {
        "run_id": run_id,
        "domain": ScheduledDomain.WATCHLIST,
        "instrument_id": instrument_id,
        "as_of": source_request.as_of,
        "material_change": True,
        "digest_artifact_id": digest_artifact_id,
        "evidence_artifact_ids": (report_id,),
        "actual_execution_allowed": False,
    }
    result_hash = scheduled_content_hash(
        {
            "schema_version": "scheduled-semantic-subject-result-v1",
            **result_body,
        }
    )
    semantic_result = ScheduledSemanticSubjectResult(
        result_id=f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}",
        result_hash=result_hash,
        **result_body,
    )
    wrong_owner = ScheduledSemanticSubmission(
        owner_id="semantic-worker-b",
        capability_artifacts=capability_artifacts,
        results=(semantic_result,),
        digests=(digest,),
    )
    with pytest.raises(ScheduledSemanticWorkInProgress, match="current lease"):
        worker.submit(binding.binding_id, bucket, wrong_owner)

    submission = wrong_owner.model_copy(update={"owner_id": "semantic-worker-a"})
    staged = worker.submit(binding.binding_id, bucket, submission)
    assert staged.semantic_capability_receipt_id is not None
    assert len(staged.semantic_result_artifact_ids) == 1
    assert worker.release(binding.binding_id, bucket, owner_id="semantic-worker-a") is False

    economic_tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
    )

    def economics() -> dict[str, list[tuple[object, ...]]]:
        with bank.store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in economic_tables
            }

    before = economics()
    receipt = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
    ).run(staged)
    assert receipt.outcome.value == "MATERIAL_CHANGE"
    assert receipt.source_coverage_complete
    assert receipt.degradation_reasons == ()
    assert len(receipt.digest_ids) == 1
    assert len(receipt.watchlist_revision_ids) == 1
    assert receipt.economic_write_count == 0
    assert economics() == before
    assert scheduler.run(staged) == receipt


def test_three_domain_scheduled_run_keeps_actual_advisory_and_replays_only_confirmed_paper(
    tmp_path: Path,
) -> None:
    bank = _build_recorded_bank(tmp_path)
    scheduled_symbol = COMPANY
    actual_at = datetime.now(UTC) - timedelta(minutes=2)
    actual_accounts = ExternalAccountRepository(bank.state, bank.objects)
    actual_accounts.create_account(
        account_id="actual-scheduled",
        display_name="scheduled actual",
        created_at=actual_at,
    )
    actual_accounts.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id="actual-scheduled",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=actual_at,
                available_to_system_at=actual_at,
                created_at=actual_at,
                market=Market.XSHG,
                symbol=scheduled_symbol,
                side="BUY",
                quantity=100,
                price_cny=Decimal("10"),
                idempotency_key="scheduled-actual-position",
            )
        ]
    )

    ledger = LedgerService(bank.state, bank.objects)
    ledger.initialize_account("paper", 2_000_000)
    paper = PaperOperationService(
        bank.state,
        bank.objects,
        ledger,
        _ThreeDomainPaperReference(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: PAPER_NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    filled_request = _paper_request(symbol=scheduled_symbol, limit_price_fen=1000)
    paper.execute(filled_request, _paper_confirmation(filled_request))
    filled_order = ledger.open_orders("paper")[0]
    ledger.record_fill(
        fill_id="scheduled-three-domain-position-fill",
        order_id=filled_order.order_id,
        qty=100,
        price_fen=1000,
        occurred_at=PAPER_NOW + timedelta(minutes=2),
    )
    open_request = _paper_request(symbol=scheduled_symbol, limit_price_fen=900)
    paper.execute(open_request, _paper_confirmation(open_request))
    open_order = ledger.open_orders("paper")[0]
    assert open_order.limit_price_fen == 900

    config = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "scheduled_investor_tracking_v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    policy = policy_from_config(config)
    replay_engine = _ReplayRecorder()
    scheduler = ScheduledResearchService(
        bank.store,
        InvestorSessionPreflightService(bank.store),
        paper_replay_adapter=_paper_replay_adapter(bank.state, bank.objects, replay_engine),
    )
    scheduler.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id="binding:three-domain-scheduled",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_THREE_DOMAIN",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=actual_at,
        consent_hash=scheduled_content_hash("three-domain-consent"),
    )
    scheduler.register_binding(binding)
    bucket = "2026-09-09:PRE_OPEN:three-domain"
    seed = build_scheduled_run_request(
        binding,
        window=ScheduledWindow.PRE_OPEN,
        schedule_bucket=bucket,
        requested_at=datetime.now(UTC),
        domains=(
            ScheduledDomain.WATCHLIST,
            ScheduledDomain.PAPER_HOLDING,
            ScheduledDomain.ACTUAL_HOLDING,
        ),
    )
    market_artifact_id = _canonical_market_anchor(bank, symbol=scheduled_symbol)
    audit, source_request, watch_report_id = _five_family_source_report(
        bank,
        run_id=seed.run_id,
        market_artifact_id=market_artifact_id,
        symbol=scheduled_symbol,
    )
    report_ids = {ScheduledDomain.WATCHLIST: watch_report_id}
    for domain in (ScheduledDomain.PAPER_HOLDING, ScheduledDomain.ACTUAL_HOLDING):
        domain_request = source_request.model_copy(update={"domain": domain})
        artifact_id = audit.register(domain_request)
        assert audit.verify_registered(artifact_id).all_families_checked
        report_ids[domain] = artifact_id

    instrument_id = f"XSHG:{scheduled_symbol}"
    ResearchSubjectRegistryService(bank.store).add_watchlist(
        instrument_id,
        reason="three-domain scheduled integration",
        available_at=source_request.interval_start,
        idempotency_key=f"watchlist:{seed.run_id}",
    )
    semantic_request, semantic = _semantic_monitor_receipt(
        bank,
        seed.run_id,
        question_time=source_request.as_of,
        market_artifact_id=market_artifact_id,
        symbol=scheduled_symbol,
    )
    assert semantic_request.entity_ids == (instrument_id,)

    def register_semantic(
        domain: ScheduledDomain,
        *,
        severity: str,
        action: str,
        change_summary: str,
    ) -> str:
        report_id = report_ids[domain]
        digest_body = {
            "run_id": seed.run_id,
            "domain": domain,
            "instrument_id": instrument_id,
            "as_of": source_request.as_of,
            "severity": severity,
            "change_summary": change_summary,
            "impact_summary": "只做研究复核；正式账户不执行交易，模拟账户仅回放既有确认订单。",
            "action": action,
            "action_conditions": ("等待下一份正式证据再评估",),
            "actual_execution_allowed": False,
            "source_artifact_ids": (report_id,),
        }
        digest_hash = scheduled_content_hash(digest_body)
        digest = MaterialChangeDigest(
            digest_id=f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}",
            **digest_body,
        )
        digest_artifact_id = f"MaterialChangeDigest:{digest_hash}"
        result_body = {
            "run_id": seed.run_id,
            "domain": domain,
            "instrument_id": instrument_id,
            "as_of": source_request.as_of,
            "material_change": True,
            "digest_artifact_id": digest_artifact_id,
            "evidence_artifact_ids": (report_id,),
            "actual_execution_allowed": False,
        }
        result_hash = scheduled_content_hash(
            {
                "schema_version": "scheduled-semantic-subject-result-v1",
                **result_body,
            }
        )
        result = ScheduledSemanticSubjectResult(
            result_id=f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}",
            result_hash=result_hash,
            **result_body,
        )
        return scheduler.register_semantic_result(result, digest=digest)

    semantic_result_ids = (
        register_semantic(
            ScheduledDomain.WATCHLIST,
            severity="HIGH",
            action="RESEARCH",
            change_summary="观察名单出现需要补充研究的重大变化。",
        ),
        register_semantic(
            ScheduledDomain.PAPER_HOLDING,
            severity="CRITICAL",
            action="TRIM_REVIEW",
            change_summary="模拟持仓出现需要复核的风险变化。",
        ),
        register_semantic(
            ScheduledDomain.ACTUAL_HOLDING,
            severity="HIGH",
            action="EXIT_REVIEW",
            change_summary="正式持仓出现需要用户复核的风险变化。",
        ),
    )
    prepared = build_scheduled_run_request(
        binding,
        window=source_request.window,
        schedule_bucket=bucket,
        requested_at=source_request.as_of,
        domains=(
            ScheduledDomain.WATCHLIST,
            ScheduledDomain.PAPER_HOLDING,
            ScheduledDomain.ACTUAL_HOLDING,
        ),
    ).model_copy(
        update={
            "source_coverage_artifact_ids": tuple(report_ids.values()),
            "source_interval_start": source_request.interval_start,
            "source_interval_end": source_request.interval_end,
            "semantic_capability_receipt_id": semantic.receipt_id,
            "semantic_result_artifact_ids": semantic_result_ids,
        }
    )

    economic_tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
    )

    def economics() -> dict[str, list[tuple[object, ...]]]:
        with bank.store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in economic_tables
            }

    before = economics()
    receipt = scheduler.run(prepared)
    assert receipt.outcome.value == "MATERIAL_CHANGE"
    assert receipt.source_coverage_complete
    assert receipt.degradation_reasons == ()
    assert len(receipt.digest_ids) == 3
    assert len(receipt.watchlist_revision_ids) == 1
    assert len(receipt.paper_replay_ids) == 1
    assert receipt.notification_required
    assert receipt.economic_write_count == 0
    assert economics() == before
    assert len(replay_engine.calls) == 1
    assert replay_engine.calls[0]["account_id"] == "paper"
    replay_request = replay_engine.calls[0]["request"]
    assert getattr(replay_request, "symbol", None) == scheduled_symbol
    assert len(scheduler.outbox.list_pending()) == 1
    pending_notification = scheduler.outbox.list_pending()[0]
    assert pending_notification["status"] == "PENDING"
    assert "正式持仓" in pending_notification["body"]
    assert bank.store.completed_schedule_buckets(binding.binding_id) == {bucket}

    repeated = scheduler.run(prepared)
    assert repeated == receipt
    assert len(replay_engine.calls) == 1
    assert economics() == before
