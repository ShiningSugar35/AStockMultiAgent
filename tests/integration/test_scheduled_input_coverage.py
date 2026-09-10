"""Recorded source adapters to five-family audit; not a trading/live qualification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from astock.core.hashing import content_hash as canonical_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.investor_orchestration import scheduled_input_coverage as coverage
from astock.investor_orchestration.activation import ActivationGateService
from astock.investor_orchestration.macro import MacroReleaseSpec, OfficialMacroCaptureService
from astock.investor_orchestration.models import ScheduledDomain, ScheduledWindow
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.monitoring import news
from astock.schemas import DisclosureExchange, DisclosureSearchRequest
from astock.schemas.institutional_research import MarketPriceAnchor
from astock.schemas.market import Market

AT = datetime(2026, 7, 22, 8, tzinfo=UTC)
START = AT - timedelta(hours=2)
END = AT - timedelta(seconds=1)


class RecordedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz else AT.replace(tzinfo=None)


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    root = tmp_path_factory.mktemp("five-family-recorded")
    store = InvestorOrchestrationStore(root / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(root / "objects" / "sha256")
    from pathlib import Path

    from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews
    from astock.market_data.reference import MarketReferenceService
    from astock.market_data.reference_storage import ReferenceParquetStore

    reference = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(root / "data" / "parquet"),
        Path("tests/fixtures/reference").resolve(),
    )
    report = reference.sync_daily("600519", Market.XSHG, AT.date().replace(day=20), AT.date())
    assert report.release_id
    price_id = CanonicalRegimeReferenceViews(state, objects).daily_locators(
        "XSHG:600519", as_of=AT
    )[-1]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"articles": []}))
    ) as client:
        provider = news.GdeltNewsLeadProvider(objects, state, client=client)
        with patch.object(news, "datetime", RecordedClock):
            company = provider.search_checked(
                names=["recorded-issuer"], symbol="600519", start=START, end=END, max_records=20
            )
            industry = provider.search_checked(
                names=["recorded-industry"], symbol="600519", start=START, end=END, max_records=20
            )
    body = {
        "announcements": [
            {
                "announcementId": "recorded-announcement",
                "adjunctUrl": "2026-07-22/recorded.pdf",
                "announcementTime": int(START.timestamp() * 1000),
                "announcementTitle": "已披露的事项索引",
                "secCode": "600519",
                "secName": "测试公司",
                "orgId": "recorded-org",
            }
        ],
        "totalAnnouncement": 1,
        "totalpages": 1,
        "hasMore": False,
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        provider = CninfoDisclosureProvider(objects, state, client=client)
        with patch("astock.documents.cninfo.datetime", RecordedClock):
            batches = provider.search_all(
                DisclosureSearchRequest(
                    symbol="600519",
                    exchange=DisclosureExchange.SSE,
                    start_date=START.date(),
                    end_date=END.date(),
                )
            )
    ids = []
    for batch in batches:
        source = state.get_snapshot(batch.raw_snapshot_id)
        assert source is not None
        ref = objects.put_json(batch.model_dump(mode="json"))
        identity = f"DisclosureSearchBatch:{batch.batch_id}"
        state.register_artifact(
            artifact_id=identity,
            artifact_type="DisclosureSearchBatch",
            schema_version=batch.schema_version,
            object_hash=ref.sha256,
            input_hashes=[
                source.object_sha256,
                canonical_hash(batch.request.model_dump(mode="json")),
            ],
        )
        ids.append(identity)
    release = OfficialMacroCaptureService(store).capture_checked(
        MacroReleaseSpec(
            authority="NBS",
            release_family="RECORDED_POLICY_FEED",
            url="https://www.stats.gov.cn/recorded-policy",
            parser="regex-v1",
            regex_observations=(
                {
                    "series_key": "recorded",
                    "pattern": r"INDEX=(?P<value>[0-9.]+)",
                    "period": "2026-07",
                    "unit": "index",
                },
            ),
        ),
        recorded_content=b"<html>INDEX=50.1</html>",
        captured_at=AT,
    )
    policy = coverage.ScheduledInputAuditPolicy(
        version="recorded-five-family-v1",
        maximum_price_age_seconds={window: 7200 for window in ScheduledWindow},
        maximum_search_age_seconds=3600,
        maximum_policy_capture_age_seconds=3600,
    )
    service = coverage.ScheduledInputCoverageService(store, policy)
    request = coverage.ScheduledInputAuditRequest(
        run_id="recorded-five-family",
        domain=ScheduledDomain.WATCHLIST,
        window=ScheduledWindow.POST_CLOSE,
        scope=coverage.ScheduledSourceScope(
            instrument_id="XSHG:600519",
            company_query=company.query,
            industry_query=industry.query,
            policy_families=(("NBS", "RECORDED_POLICY_FEED"),),
        ),
        interval_start=START,
        interval_end=END,
        as_of=AT + timedelta(seconds=1),
        market_references=(price_id,),
        company_news_snapshot_ids=(company.snapshot_id,),
        industry_news_snapshot_ids=(industry.snapshot_id,),
        disclosure_batch_artifact_ids=tuple(ids),
        policy_capture_artifact_ids=(release.capture_artifact_id,),
    )
    return service, request


@pytest.mark.parametrize("domain", list(ScheduledDomain))
@pytest.mark.parametrize("window", list(ScheduledWindow))
def test_five_families_are_independently_audited_for_each_domain_window(context, domain, window):
    service, request = context
    selected = request.model_copy(update={"domain": domain, "window": window})
    report = service.build(selected)
    assert report.all_families_checked, report.checks
    assert {item.family for item in report.checks} == set(coverage.ScheduledInputFamily)
    assert all(item.source_revisions for item in report.checks)
    assert report.formal_research_complete is False
    assert report.adverse_news_absence_proven is False


@pytest.mark.parametrize(
    "field",
    [
        "market_references",
        "company_news_snapshot_ids",
        "industry_news_snapshot_ids",
        "disclosure_batch_artifact_ids",
        "policy_capture_artifact_ids",
    ],
)
def test_missing_family_cannot_claim_complete_input_checks(context, field):
    service, request = context
    result = service.build(request.model_copy(update={field: ()}))
    assert not result.all_families_checked
    assert sum(item.status == "MISSING" for item in result.checks) == 1


def test_same_query_capture_cannot_certify_a_different_news_scope(context):
    service, request = context
    result = service.build(
        request.model_copy(update={"industry_news_snapshot_ids": request.company_news_snapshot_ids})
    )
    assert not result.all_families_checked
    assert (
        next(
            item
            for item in result.checks
            if item.family is coverage.ScheduledInputFamily.INDUSTRY_NEWS
        ).status
        == "INVALID"
    )


def test_replayed_registered_report_is_idempotent_and_unchanged(context):
    service, request = context
    first = service.register(request)
    second = service.register(request)
    assert first == second
    assert service.verify_registered(first) == service.build(request)


def test_later_requirement_cannot_advance_the_checked_watermark(context):
    service, request = context
    result = service.build(request.model_copy(update={"interval_end": request.as_of}))
    assert not result.all_families_checked
    assert any(item.reason == "DISCOVERY_INTERVAL_GAP" for item in result.checks)


def test_stale_input_is_not_relabelled_current_by_a_fresh_report(context):
    service, request = context
    later = request.as_of + timedelta(hours=3)
    result = service.build(request.model_copy(update={"as_of": later}))
    assert not result.all_families_checked
    assert any(item.status == "STALE" for item in result.checks)


def test_arbitrary_registered_price_cannot_replace_a_canonical_release(context):
    service, request = context
    result = service.build(
        request.model_copy(update={"market_references": ("DailyBarObservation:invented",)})
    )
    assert not result.all_families_checked
    assert result.checks[0].status == "INVALID"


def test_registered_market_price_anchor_is_a_current_market_check(context):
    service, request = context
    source, bar = service.views.load(request.market_references[0])
    source_id = source["parent_artifact_id"]
    source_hash = source["object_hash"]
    anchor = MarketPriceAnchor(
        price=bar["close"],
        observed_at=bar["session_close_at"],
        available_to_system_at=bar["available_to_system_at"],
        source_artifact_id=source_id,
        source_object_hash=source_hash,
        created_at=request.as_of,
    )
    anchor_ref = service.objects.put_json(anchor.model_dump(mode="json"))
    anchor_id = "MarketPriceAnchor:scheduled-anchor"
    service.state.register_artifact(
        artifact_id=anchor_id,
        artifact_type="MarketPriceAnchor",
        schema_version=anchor.schema_version,
        object_hash=anchor_ref.sha256,
        input_hashes=[source_hash],
    )

    result = service.build(request.model_copy(update={"market_references": (anchor_id,)}))
    market = next(
        item for item in result.checks if item.family is coverage.ScheduledInputFamily.MARKET
    )
    assert market.status == "CHECKED"
    assert market.checked_through == anchor.observed_at
    assert market.source_revisions[source_id] == source_hash


def test_market_price_anchor_with_forged_source_binding_is_invalid(context):
    service, request = context
    anchor = MarketPriceAnchor(
        price=Decimal("10.00"),
        observed_at=AT,
        available_to_system_at=AT,
        source_artifact_id="market-source:missing",
        source_object_hash="f" * 64,
        created_at=AT,
    )
    ref = service.objects.put_json(anchor.model_dump(mode="json"))
    artifact_id = "MarketPriceAnchor:forged-source"
    service.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="MarketPriceAnchor",
        schema_version=anchor.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )

    result = service.build(request.model_copy(update={"market_references": (artifact_id,)}))
    market = next(
        item for item in result.checks if item.family is coverage.ScheduledInputFamily.MARKET
    )
    assert market.status == "INVALID"
    assert not result.all_families_checked


def test_disclosure_total_is_recomputed_from_raw_response(context):
    service, request = context
    from astock.schemas import DisclosureSearchBatch

    old_id = request.disclosure_batch_artifact_ids[0]
    record = service.state.artifact_record(old_id)
    batch = service.verifier.load(old_id, DisclosureSearchBatch)
    forged = batch.model_copy(update={"total_count": 100})
    ref = service.objects.put_json(forged.model_dump(mode="json"))
    new_id = "DisclosureSearchBatch:forged-total"
    service.state.register_artifact(
        artifact_id=new_id,
        artifact_type="DisclosureSearchBatch",
        schema_version=batch.schema_version,
        object_hash=ref.sha256,
        input_hashes=record["input_hashes"],
    )
    result = service.build(request.model_copy(update={"disclosure_batch_artifact_ids": (new_id,)}))
    assert (
        next(
            c for c in result.checks if c.family is coverage.ScheduledInputFamily.DISCLOSURES
        ).status
        == "INVALID"
    )


def test_future_capture_cannot_be_admitted_by_backdating_a_report(context):
    service, request = context
    earlier = request.model_copy(update={"as_of": END, "interval_end": END})
    result = service.build(earlier)
    assert not result.all_families_checked
    assert (
        next(
            c for c in result.checks if c.family is coverage.ScheduledInputFamily.COMPANY_NEWS
        ).status
        == "INVALID"
    )


def test_rehashed_pass_report_cannot_replace_its_source_replay(context):
    service, request = context
    incomplete = service.build(request.model_copy(update={"market_references": ()}))
    forged = incomplete.model_copy(update={"all_families_checked": True})
    from astock.investor_orchestration.utils import content_hash

    body = {
        "request": forged.request,
        "policy": forged.policy,
        "checks": forged.checks,
        "all_families_checked": True,
    }
    forged = forged.model_copy(update={"report_hash": content_hash(body)})
    ref = service.objects.put_json(forged.model_dump(mode="json"))
    identity = f"ScheduledInputCoverageReport:{forged.report_hash}"
    service.state.register_artifact(
        artifact_id=identity,
        artifact_type="ScheduledInputCoverageReport",
        schema_version=forged.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    with pytest.raises(ValueError, match="canonical sources"):
        service.verify_registered(identity)


def test_source_reports_bind_to_the_exact_run_scope_and_empty_runs_are_not_complete(context):
    service, request = context
    artifact_id = service.register(request)
    arguments = dict(
        run_id=request.run_id,
        window=request.window,
        requested_at=request.as_of,
        subjects={request.domain: (request.scope.instrument_id,)},
        report_ids=(artifact_id,),
        interval_start=request.interval_start,
        interval_end=request.interval_end,
    )
    result = service.bind_run(**arguments)
    assert result.all_families_checked
    assert result.expected_subject_count == result.verified_subject_count == 1
    assert result.checked_through == request.interval_end
    for alteration in (
        {"run_id": "another-run"},
        {"subjects": {ScheduledDomain.ACTUAL_HOLDING: (request.scope.instrument_id,)}},
        {"report_ids": (artifact_id, artifact_id)},
        {"interval_start": request.interval_start + timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError):
            service.bind_run(**{**arguments, **alteration})
    missing = service.bind_run(**{**arguments, "report_ids": ()})
    assert not missing.all_families_checked and missing.degradation_reasons
    empty = service.bind_run(**{**arguments, "report_ids": (), "subjects": {}})
    assert not empty.all_families_checked and empty.checked_through is None


@pytest.mark.parametrize("include_sources", [True, False])
def test_scheduled_runtime_consumes_verified_checks_without_writing_economic_facts(
    context,
    include_sources,
):
    import uuid
    from pathlib import Path

    import yaml

    from astock.investor_orchestration.models import (
        MaterialChangeDigest,
        ScheduledRunRequest,
        ScheduledTaskBinding,
    )
    from astock.investor_orchestration.preflight import InvestorSessionPreflightService
    from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
    from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

    audit, source_request = context
    run_id = f"recorded-source-integration-{uuid.uuid4()}"
    source_request = source_request.model_copy(update={"run_id": run_id})
    store = audit.store
    registry = ResearchSubjectRegistryService(store)
    registry.add_watchlist(
        source_request.scope.instrument_id,
        reason="recorded source integration",
        available_at=source_request.interval_start,
        idempotency_key="recorded-watchlist-membership",
    )
    config = yaml.safe_load(
        Path("configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config, source_audit_policy=audit.policy)

    def recorded_analyzer(**kwargs):
        return MaterialChangeDigest(
            digest_id=f"digest-{run_id}",
            run_id=run_id,
            domain=kwargs["domain"],
            instrument_id=kwargs["instrument_id"],
            as_of=kwargs["preflight"].as_of,
            severity="MEDIUM",
            change_summary="来源检查已记录，需要复核新增事项。",
            impact_summary="此次只更新观察依据，不形成交易指令。",
            action="RESEARCH",
            source_artifact_ids=source_request.disclosure_batch_artifact_ids,
        )

    service = ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        analyzer=recorded_analyzer,
        source_audit_policy=audit.policy,
    )
    service.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id=f"binding-{run_id}",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_SOURCE_INTEGRATION",
        timezone="Asia/Shanghai",
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=source_request.interval_start,
        consent_hash="recorded-local-test-consent",
    )
    service.register_binding(binding)
    reports = (audit.register(source_request),) if include_sources else ()
    request = ScheduledRunRequest(
        run_id=run_id,
        binding_id=binding.binding_id,
        schedule_bucket=run_id,
        window=source_request.window,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=source_request.as_of,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key=run_id,
        source_coverage_artifact_ids=reports,
        source_interval_start=source_request.interval_start,
        source_interval_end=source_request.interval_end,
    )
    tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
    )

    def economics():
        with store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in tables
            }

    before = economics()
    receipt = service.run(request)
    assert receipt == service.run(request)
    assert economics() == before
    assert receipt.source_coverage_artifact_ids == reports
    assert receipt.source_coverage_complete is include_sources
    assert receipt.watchlist_revision_ids == ()
    assert receipt.digest_ids == ()
    assert receipt.capability_receipt_id is not None
    certified = audit.verify_registered_run_coverage(receipt.capability_receipt_id)
    assert certified.report_ids == reports
    assert not certified.semantic_coverage_complete
    assert certified.semantic_capability_receipt_id is None
    valid = ActivationGateService(store).evidence.valid_coverage(
        f"scheduled:{receipt.run_id}", receipt.capability_receipt_id
    )
    assert not valid
    if include_sources:
        assert certified.all_families_checked
        assert receipt.source_checked_through == source_request.interval_end
        assert "SEMANTIC_CAPABILITY_COVERAGE_UNAVAILABLE" in receipt.degradation_reasons
    else:
        assert not certified.all_families_checked
        assert receipt.source_checked_through is None and receipt.next_watermark is None
    assert receipt.outcome.value == "DEGRADED"
    assert (
        store.get_scheduled_receipt(
            binding_id=binding.binding_id, schedule_bucket=request.schedule_bucket
        )
        is None
    )
    assert store.completed_schedule_buckets(binding.binding_id) == set()
    attempts = store.scheduled_attempts(binding.binding_id, request.schedule_bucket)
    assert attempts == (receipt,)
    checkpoint = store.get_scheduled_checkpoint(binding.binding_id, request.schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"


def test_three_domain_recorded_controlled_run_and_rollback_are_economically_read_only(context):
    import uuid
    from pathlib import Path

    import yaml

    from astock.external_accounts import ExternalAccountRepository
    from astock.investor_orchestration.models import (
        MaterialChangeDigest,
        ScheduledRunRequest,
        ScheduledTaskBinding,
    )
    from astock.investor_orchestration.preflight import InvestorSessionPreflightService
    from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
    from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
    from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
    from tests.unit.test_paper_operations import (
        NOW as PAPER_NOW,
    )
    from tests.unit.test_paper_operations import (
        _confirmation as paper_confirmation,
    )
    from tests.unit.test_paper_operations import (
        _request as paper_request,
    )
    from tests.unit.test_paper_operations import (
        _service as paper_service,
    )

    audit, base_request = context
    store = audit.store
    state = StateStore(store.path)
    run_id = f"recorded-three-domain-{uuid.uuid4()}"
    instrument_id = base_request.scope.instrument_id

    registry = ResearchSubjectRegistryService(store)
    registry.add_watchlist(
        instrument_id,
        reason="recorded controlled three-domain validation",
        available_at=base_request.interval_start,
        idempotency_key=f"watch-{run_id}",
    )
    external = ExternalAccountRepository(state, audit.objects)
    external.create_account(
        account_id=f"actual-{run_id}",
        display_name="recorded controlled actual account",
        created_at=base_request.interval_start - timedelta(minutes=1),
    )
    external.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id=f"actual-{run_id}",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=base_request.interval_start,
                available_to_system_at=base_request.interval_start,
                market=Market.XSHG,
                symbol="600519",
                side="BUY",
                quantity=100,
                price_cny=Decimal("10"),
                idempotency_key=f"actual-trade-{run_id}",
            )
        ]
    )

    class RecordedPaperClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = PAPER_NOW.astimezone(UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    with patch("astock.paper_trading.ledger.datetime", RecordedPaperClock):
        operation_service, ledger = paper_service(state, audit.objects)
        operation = paper_request()
        operation_report = operation_service.execute(operation, paper_confirmation(operation))
        order = operation_report.result["order"]
        assert isinstance(order, dict)
        ledger.record_fill(
            fill_id=f"controlled-fill-{run_id}",
            order_id=str(order["order_id"]),
            qty=100,
            price_fen=1000,
            occurred_at=PAPER_NOW + timedelta(minutes=3),
        )

    config = yaml.safe_load(
        Path("configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config, source_audit_policy=audit.policy)

    class LocalSink:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def publish(self, *, notification_key, title, body, metadata):
            assert notification_key and title and body and metadata
            self.calls.append(notification_key)
            return f"local-delivery:{notification_key}"

    sink = LocalSink()

    def recorded_analyzer(**kwargs):
        domain = kwargs["domain"]
        severity = "HIGH" if domain is ScheduledDomain.ACTUAL_HOLDING else "MEDIUM"
        action = "RESEARCH" if domain is ScheduledDomain.WATCHLIST else "REVIEW"
        return MaterialChangeDigest(
            digest_id=f"digest-{domain.value}-{run_id}",
            run_id=run_id,
            domain=domain,
            instrument_id=kwargs["instrument_id"],
            as_of=kwargs["preflight"].as_of,
            severity=severity,
            change_summary="已记录来源出现需要复核的新增事项。",
            impact_summary="仅更新研究或持仓复核，不授权真实交易。",
            action=action,
            action_conditions=("复核权威来源与原投资逻辑",),
            source_artifact_ids=base_request.disclosure_batch_artifact_ids,
        )

    scheduled = ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        analyzer=recorded_analyzer,
        notification_sink=sink,
        source_audit_policy=audit.policy,
    )
    scheduled.register_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id=f"binding-{run_id}",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_THREE_DOMAIN_CONTROLLED",
        timezone="Asia/Shanghai",
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=base_request.interval_start,
        consent_hash=f"consent-{run_id}",
    )
    scheduled.register_binding(binding)
    reports = tuple(
        audit.register(base_request.model_copy(update={"run_id": run_id, "domain": domain}))
        for domain in ScheduledDomain
    )
    request = ScheduledRunRequest(
        run_id=run_id,
        binding_id=binding.binding_id,
        schedule_bucket=run_id,
        window=base_request.window,
        domains=tuple(ScheduledDomain),
        requested_at=base_request.as_of,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key=run_id,
        source_coverage_artifact_ids=reports,
        source_interval_start=base_request.interval_start,
        source_interval_end=base_request.interval_end,
    )
    economic_tables = (
        "external_account_event",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
        "position_settlement",
    )

    def economics():
        with store.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                for table in economic_tables
            }

    before = economics()
    receipt = scheduled.run(request)
    assert receipt == scheduled.run(request)
    assert economics() == before
    assert receipt.outcome.value == "DEGRADED"
    assert receipt.economic_write_count == 0
    assert receipt.source_coverage_complete
    assert "SEMANTIC_CAPABILITY_COVERAGE_UNAVAILABLE" in receipt.degradation_reasons
    assert receipt.digest_ids == ()
    assert receipt.watchlist_revision_ids == ()
    assert not receipt.notification_required and len(sink.calls) == 0
    assert receipt.capability_receipt_id is not None
    certified = audit.verify_registered_run_coverage(receipt.capability_receipt_id)
    assert certified.expected_subject_count == certified.verified_subject_count == 3
    assert certified.all_families_checked
    assert not certified.semantic_coverage_complete
    activation = ActivationGateService(store)
    assert not activation.evidence.valid_coverage(
        f"scheduled:{run_id}", receipt.capability_receipt_id
    )

    assessment = activation.assess("scheduled_research", recorded_scenario_count=68)
    assert not assessment.eligible
    assert assessment.shadow_observation_count == 0
    assert "shadow_observations" in assessment.blockers
    assert "owner_approval" in assessment.blockers
    rollback_before = economics()
    rollback = activation.rollback(
        "scheduled_research", reason="recorded controlled rollback drill"
    )
    assert rollback.new_status.value == "ROLLED_BACK"
    assert rollback.ledger_write_count == 0
    assert not any(rollback.feature_flags.values())
    assert economics() == rollback_before
