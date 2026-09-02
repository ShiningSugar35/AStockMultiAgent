from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from pydantic import HttpUrl

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.local_portfolio import LocalPortfolioService
from astock.paper_trading import LedgerService
from astock.portfolio.decision import PortfolioDecisionService
from astock.portfolio.service import PortfolioService, _History
from astock.schemas import OrderSide
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.knowledge import PositionAction
from astock.schemas.market import InstrumentType, Market, SourceClass
from astock.schemas.portfolio import (
    PortfolioAllocationMethod,
    PortfolioAllocationProposal,
    PortfolioAnalysisReport,
    PortfolioAnalysisRequest,
    PortfolioAnalysisStatus,
    PortfolioAssetRisk,
    PortfolioConstructionReport,
    PortfolioHoldingInput,
    PortfolioRiskMetrics,
)
from astock.schemas.portfolio_decision import (
    DeclaredTradeValidationStatus,
    ETFCategory,
    ETFMarketPriceSighting,
    ETFNavSighting,
    ETFPremiumDiscountRequest,
    ETFProductProfile,
    ETFResearchMetricsRequest,
    FundProductProfile,
    HedgeClassification,
    HedgeEffectivenessReport,
    HedgeEffectivenessRequest,
    HedgeInstrumentCandidate,
    IndexProductProfile,
    InstrumentTradingUnitRule,
    PortfolioComplementScreenRequest,
    PortfolioImplementationCostInput,
    PortfolioIntentProfile,
    PortfolioRiskObjective,
    PortfolioTransitionRequest,
    ProductConstituent,
    ProductConstituentSnapshot,
    ProductCoverageStatus,
    ProductDataQuality,
    SettlementCycle,
    UserDeclaredTradeCapture,
)
from astock.schemas.reference_data import InstrumentRecord
from astock.schemas.research_seeds import (
    ResearchSeed,
    ResearchSeedOrigin,
    ResearchSeedReport,
    ResearchSeedStatus,
    ResearchUniverseCoverageStatus,
)
from astock.schemas.source_access import OfficialWebDocumentCapture

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


class _Verifier:
    def resolve_instrument(self, symbol: str, *, visible_at: datetime):
        del visible_at
        market = Market.XSHG
        return (
            InstrumentRecord(
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                name=f"fixture-{symbol}",
                instrument_type=InstrumentType.STOCK,
                tradable=True,
                status_date=date(2026, 8, 31),
                is_st=False,
                source_snapshot_id="fixture:instrument",
                available_to_system_at=AS_OF - timedelta(days=1),
                created_at=AS_OF,
            ),
            "fixture-instruments",
        )


class _PortfolioStub:
    def __init__(self, history_object_hash: str = "a" * 64) -> None:
        self.verifier = _Verifier()
        self.history_object_hash = history_object_hash
        self.committee_rules = SimpleNamespace(
            max_total_exposure=Decimal("0.80"),
            max_single_position=Decimal("0.10"),
            max_industry_exposure=Decimal("0.25"),
            max_abs_correlation=Decimal("0.80"),
            max_portfolio_drawdown=Decimal("0.20"),
        )

    def _history(
        self,
        company_id: str,
        market: Market,
        *,
        as_of: datetime,
        lookback_sessions: int,
        minimum_sessions: int,
        live: bool,
        allow_live_capture: bool,
    ) -> _History:
        del minimum_sessions, live, allow_live_capture
        points = max(lookback_sessions + 1, 61)
        start = date(2026, 5, 1)
        price = 10.0
        closes = {start.isoformat(): price}
        for index in range(1, points):
            if company_id == "600001":
                daily_return = 0.004 if index % 3 else -0.003
            elif company_id == "000300":
                daily_return = 0.003 if index % 4 else -0.002
            else:
                daily_return = 0.005 if index % 2 else -0.002
            price *= 1.0 + daily_return
            closes[(start + timedelta(days=index)).isoformat()] = price
        return _History(
            company_id=company_id,
            market=market,
            closes_by_date=closes,
            research_closes_by_date=closes,
            latest_close_fen=1000,
            release_id=f"daily-{company_id}",
            release_object_hash=self.history_object_hash,
            cutoff_at=as_of,
        )

    _align = staticmethod(PortfolioService._align)


def _state(tmp_path: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state, ObjectStore(tmp_path / "objects")


def _service(tmp_path: Path, *, portfolio: Any | None = None) -> PortfolioDecisionService:
    state, objects = _state(tmp_path)
    return PortfolioDecisionService(
        state,
        objects,
        LocalPortfolioService(tmp_path, state),
        cast(PortfolioService, portfolio or _PortfolioStub()),
        PROJECT_ROOT,
    )


def _register_model(
    state: StateStore,
    objects: ObjectStore,
    artifact_id: str,
    artifact_type: str,
    model: Any,
    *,
    input_hashes: list[str] | None = None,
) -> str:
    ref = objects.put_json(model.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version=model.schema_version,
        object_hash=ref.sha256,
        input_hashes=input_hashes or [],
    )
    return ref.sha256


def _register_official_capture(
    service: PortfolioDecisionService,
    artifact_id: str,
    *,
    observed_at: datetime | None = None,
) -> str:
    observed = observed_at or (AS_OF - timedelta(days=1))
    suffix = artifact_id.split(":", 1)[-1]
    raw_ref = service.objects.put_json(
        {"fixture": "official-web-document", "artifact_id": artifact_id}
    )
    capture = OfficialWebDocumentCapture(
        capture_id=suffix,
        requested_capability="PORTFOLIO_DECISION_FIXTURE",
        source_id="sse-official",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        document_id=f"document:{suffix}",
        snapshot_id=f"snapshot:{suffix}",
        admission_snapshot_id=f"admission:{suffix}",
        pit_id=f"pit:{suffix}",
        source_url=HttpUrl("https://www.sse.com.cn/assortment/fund/etf/question/"),
        object_sha256=raw_ref.sha256,
        observed_at=observed,
        created_at=observed,
    )
    ref = service.objects.put_json(capture.model_dump(mode="json"))
    service.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="OfficialWebDocumentCapture",
        schema_version=capture.schema_version,
        object_hash=ref.sha256,
        input_hashes=[raw_ref.sha256],
    )
    return ref.sha256


def test_declared_external_trade_is_exactly_once_and_restores_position(tmp_path: Path) -> None:
    service = _service(tmp_path)
    capture = UserDeclaredTradeCapture(
        raw_statement="2026-08-20 以 50 元买入 600519 共 300 股",
        declared_at=AS_OF,
        market=Market.XSHG,
        symbol="600519",
        side="BUY",
        quantity=300,
        price_cny=Decimal("50"),
        occurred_at=datetime(2026, 8, 20, 2, 0, tzinfo=UTC),
        created_at=AS_OF,
    )
    first = service.import_declared_trade(capture)
    second = service.import_declared_trade(capture)

    assert first.status is DeclaredTradeValidationStatus.READY
    assert second.status is DeclaredTradeValidationStatus.DUPLICATE
    assert first.trade_id == second.trade_id
    local = service.local_portfolio.status()
    assert local["trade_count"] == 1
    assert local["position_count"] == 1
    external_events = service.external_accounts.list_events("default")
    assert len(external_events) == 1
    assert external_events[0].event_type.value == "TRADE"
    assert external_events[0].note.startswith("user-declared:")
    position = cast(list[dict[str, object]], local["positions"])[0]
    assert position["quantity"] == 300
    assert position["average_cost_cny"] == "50.0000"
    snapshot = service.snapshot_local_portfolio(as_of=AS_OF)
    assert snapshot.cash_known is False
    assert snapshot.positions[0].quantity == 300
    assert snapshot.positions[0].average_cost_cny == Decimal("50.0000")
    assert snapshot.positions[0].opened_at == capture.occurred_at
    snapshot_artifact = service.state.artifact_record(
        f"UserPortfolioSnapshot:{snapshot.snapshot_id}"
    )
    assert snapshot_artifact is not None
    assert len(snapshot_artifact["input_hashes"]) == 1
    assert service.audit(f"UserPortfolioSnapshot:{snapshot.snapshot_id}")["status"] == "PASS"
    assert service.audit(f"ExternalTradeImportReceipt:{second.receipt_id}")["status"] == "PASS"


def test_canonical_external_event_survives_legacy_projection_failure_and_retry_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    capture = UserDeclaredTradeCapture(
        raw_statement="2026-08-20 以 50 元买入 600519 共 300 股",
        declared_at=AS_OF,
        market=Market.XSHG,
        symbol="600519",
        side="BUY",
        quantity=300,
        price_cny=Decimal("50"),
        occurred_at=datetime(2026, 8, 20, 2, 0, tzinfo=UTC),
        created_at=AS_OF,
    )
    original = service.local_portfolio.record_validated_external_trade

    def fail_projection(*args, **kwargs):
        del args, kwargs
        raise ValueError("projection write failed")

    monkeypatch.setattr(service.local_portfolio, "record_validated_external_trade", fail_projection)
    first = service.import_declared_trade(capture)
    assert first.status is DeclaredTradeValidationStatus.READY
    assert first.reason_codes == ["LEGACY_LOCAL_PROJECTION_REFRESH_FAILED"]
    assert len(service.external_accounts.list_events("default")) == 1
    assert service.local_portfolio.status()["trade_count"] == 0

    monkeypatch.setattr(service.local_portfolio, "record_validated_external_trade", original)
    recovered = service.import_declared_trade(capture)
    assert recovered.status is DeclaredTradeValidationStatus.DUPLICATE
    assert recovered.trade_id == first.trade_id
    assert recovered.reason_codes == []
    assert len(service.external_accounts.list_events("default")) == 1
    assert service.local_portfolio.status()["trade_count"] == 1


def test_local_snapshot_uses_external_account_authority_over_stale_markdown(tmp_path: Path) -> None:
    service = _service(tmp_path)
    event_time = AS_OF - timedelta(hours=1)
    service.local_portfolio.record_trade(
        market="XSHG",
        symbol="600519",
        side="BUY",
        quantity=300,
        price_cny="1",
        source="IMPORT",
        occurred_at=event_time,
    )
    service.external_accounts.create_account(
        account_id="default",
        display_name="默认外部账户",
        created_at=event_time,
    )
    service.external_accounts.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id="default",
                event_type=ExternalAccountEventType.CASH_DEPOSIT,
                occurred_at=event_time,
                available_to_system_at=event_time,
                amount_cny=Decimal("10000"),
                idempotency_key="snapshot-cash",
                created_at=event_time,
            ),
            ExternalAccountEventDraft(
                account_id="default",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=event_time,
                available_to_system_at=event_time,
                market=Market.XSHG,
                symbol="600519",
                side="BUY",
                quantity=100,
                price_cny=Decimal("50"),
                idempotency_key="snapshot-buy",
                created_at=event_time,
            ),
        ]
    )

    snapshot = service.snapshot_local_portfolio(as_of=AS_OF)
    assert snapshot.source == "EXTERNAL_ACCOUNT_DEFAULT"
    assert snapshot.trade_count == 1
    assert snapshot.cash_known is True
    assert snapshot.cash_cny == Decimal("5000")
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].quantity == 100
    assert snapshot.positions[0].average_cost_cny == Decimal("50")


def test_declared_trade_outside_listed_period_is_rejected(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    original = service.portfolio.verifier.resolve_instrument

    def late_listing(symbol: str, *, visible_at: datetime):
        instrument, release_id = original(symbol, visible_at=visible_at)
        return instrument.model_copy(update={"listing_date": date(2026, 8, 21)}), release_id

    monkeypatch.setattr(service.portfolio.verifier, "resolve_instrument", late_listing)
    receipt = service.import_declared_trade(
        UserDeclaredTradeCapture(
            raw_statement="2026-08-20 买入 600519 100 股，价格 50 元",
            declared_at=AS_OF,
            market=Market.XSHG,
            symbol="600519",
            side="BUY",
            quantity=100,
            price_cny=Decimal("50"),
            occurred_at=datetime(2026, 8, 20, 2, 0, tzinfo=UTC),
            created_at=AS_OF,
        )
    )
    assert receipt.status is DeclaredTradeValidationStatus.CONFLICT
    assert receipt.reason_codes == ["TRADE_OCCURRED_OUTSIDE_LISTED_PERIOD"]
    assert service.local_portfolio.status()["trade_count"] == 0


def test_incomplete_declared_trade_does_not_create_position(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.import_declared_trade(
        UserDeclaredTradeCapture(
            raw_statement="我买了 600519",
            declared_at=AS_OF,
            market=Market.XSHG,
            symbol="600519",
            side="BUY",
            created_at=AS_OF,
        )
    )
    assert receipt.status is DeclaredTradeValidationStatus.NEEDS_INFO
    assert set(receipt.reason_codes) == {
        "MISSING_OCCURRED_AT",
        "MISSING_PRICE_CNY",
        "MISSING_QUANTITY",
    }
    assert service.local_portfolio.status()["trade_count"] == 0


def test_external_trade_conflicting_with_paper_fill_is_blocked(tmp_path: Path) -> None:
    service = _service(tmp_path)
    occurred_at = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    ledger = LedgerService(service.state)
    ledger.initialize_account("paper", 1_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="portfolio-decision-paper-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=5_000,
        fee_reserve_fen=0,
    )
    ledger.record_fill(
        fill_id="portfolio-decision-paper-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=5_000,
        occurred_at=occurred_at,
    )
    receipt = service.import_declared_trade(
        UserDeclaredTradeCapture(
            raw_statement="这笔模拟成交也是我的真实成交",
            declared_at=AS_OF,
            market=Market.XSHG,
            symbol="600519",
            side="BUY",
            quantity=100,
            price_cny=Decimal("50"),
            occurred_at=occurred_at,
            created_at=AS_OF,
        )
    )
    assert receipt.status is DeclaredTradeValidationStatus.CONFLICT
    assert service.external_accounts.list_events("default") == []
    assert service.local_portfolio.status()["trade_count"] == 0


def test_formal_hedge_claim_requires_frozen_provenance() -> None:
    with pytest.raises(ValueError, match="requires frozen provenance"):
        HedgeInstrumentCandidate(
            instrument_id="XSHG:510300",
            market=Market.XSHG,
            symbol="510300",
            instrument_type=InstrumentType.ETF,
            classification=HedgeClassification.EXPLICIT_HEDGE,
            targeted_risk_codes=["MARKET_BETA"],
            expected_risk_reduction_fraction=0.2,
            created_at=AS_OF,
        )
    diversification = HedgeInstrumentCandidate(
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        instrument_type=InstrumentType.ETF,
        classification=HedgeClassification.DIVERSIFICATION,
        created_at=AS_OF,
    )
    assert diversification.classification is HedgeClassification.DIVERSIFICATION


def test_etf_profile_research_registration_is_independent_of_execution_switch(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source_hash = _register_official_capture(service, "OfficialWebDocumentCapture:etf-profile")
    rule = InstrumentTradingUnitRule(
        instrument_id="XSHG:510300",
        instrument_type=InstrumentType.ETF,
        buy_lot_size=100,
        sell_lot_size=100,
        tick_size_cny=Decimal("0.001"),
        settlement_cycle=SettlementCycle.T1,
        effective_from=date(2026, 4, 24),
        source_urls=["https://www.sse.com.cn/assortment/fund/etf/question/"],
        created_at=AS_OF,
    )
    profile = ETFProductProfile(
        profile_id="xshg-510300-v1",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        name="fixture ETF",
        category=ETFCategory.EQUITY,
        tracking_target="CSI 300",
        trading_rule=rule,
        paper_replay_supported=False,
        official_source_artifact_ids=["OfficialWebDocumentCapture:etf-profile"],
        official_source_object_hashes=[source_hash],
        available_to_system_at=AS_OF,
        created_at=AS_OF,
    )
    assert service.register_etf_profile(profile) == profile
    assert service.audit("ETFProductProfile:xshg-510300-v1")["status"] == "PASS"
    paper_profile = profile.model_copy(
        update={"profile_id": "paper", "paper_replay_supported": True}
    )
    assert service.register_etf_profile(paper_profile) == paper_profile
    assert service.audit("ETFProductProfile:paper")["status"] == "PASS"


def test_fund_index_and_constituents_freeze_identity_pit_quality_and_provenance(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    fund_source = _register_official_capture(service, "OfficialWebDocumentCapture:fund-profile")
    index_source = _register_official_capture(service, "OfficialWebDocumentCapture:index-profile")
    constituent_source = _register_official_capture(
        service, "OfficialWebDocumentCapture:index-constituents"
    )
    facts_as_of = AS_OF - timedelta(days=1)

    fund = FundProductProfile(
        profile_id="fund-000001-v1",
        instrument_id="FUND:000001",
        fund_code="000001",
        name="fixture fund",
        fund_category="INDEX_FUND",
        manager_name="fixture manager",
        tracking_target="CSI 300",
        tracking_benchmark_market=Market.INDEX,
        tracking_benchmark_symbol="000300",
        management_fee_bps=50,
        custody_fee_bps=10,
        total_expense_ratio_bps=60,
        total_net_asset_cny=Decimal("123456789.01"),
        total_outstanding_shares=123_000_000,
        facts_as_of=facts_as_of,
        available_to_system_at=AS_OF,
        quality_status=ProductDataQuality.PARTIAL,
        quality_warning_codes=["FUND_SIZE_SOURCE_LAG"],
        official_source_artifact_ids=["OfficialWebDocumentCapture:fund-profile"],
        official_source_object_hashes=[fund_source],
        created_at=AS_OF,
    )
    assert service.register_fund_profile(fund) == fund
    assert service.audit("FundProductProfile:fund-000001-v1")["status"] == "PASS"
    with pytest.raises(ValueError, match="VERIFIED fund profile"):
        FundProductProfile.model_validate(
            fund.model_copy(
                update={
                    "quality_status": ProductDataQuality.VERIFIED,
                    "quality_warning_codes": [],
                    "total_outstanding_shares": None,
                }
            ).model_dump(mode="python")
        )

    index = IndexProductProfile(
        profile_id="csi300-v1",
        instrument_id="INDEX:000300",
        symbol="000300",
        name="fixture CSI 300",
        index_provider="fixture official provider",
        methodology_name="fixture methodology",
        base_date=date(2004, 12, 31),
        base_value=Decimal("1000"),
        facts_as_of=facts_as_of,
        available_to_system_at=AS_OF,
        quality_status=ProductDataQuality.VERIFIED,
        official_source_artifact_ids=["OfficialWebDocumentCapture:index-profile"],
        official_source_object_hashes=[index_source],
        created_at=AS_OF,
    )
    assert service.register_index_profile(index) == index
    assert service.audit("IndexProductProfile:csi300-v1")["status"] == "PASS"

    snapshot = ProductConstituentSnapshot(
        snapshot_id="csi300-constituents-v1",
        product_artifact_id="IndexProductProfile:csi300-v1",
        product_instrument_id="INDEX:000300",
        product_type="INDEX",
        as_of=facts_as_of,
        available_to_system_at=AS_OF,
        coverage_status=ProductCoverageStatus.COMPLETE,
        constituents=[
            ProductConstituent(
                instrument_id="XSHE:000001",
                market=Market.XSHE,
                symbol="000001",
                weight=Decimal("0.4"),
            ),
            ProductConstituent(
                instrument_id="XSHG:600519",
                market=Market.XSHG,
                symbol="600519",
                weight=Decimal("0.6"),
            ),
        ],
        official_source_artifact_ids=["OfficialWebDocumentCapture:index-constituents"],
        official_source_object_hashes=[constituent_source],
        created_at=AS_OF,
    )
    assert service.register_product_constituents(snapshot) == snapshot
    assert service.audit("ProductConstituentSnapshot:csi300-constituents-v1")["status"] == "PASS"
    with pytest.raises(ValueError, match="approximately 100%"):
        snapshot.model_copy(
            update={
                "snapshot_id": "bad-coverage",
                "constituents": [snapshot.constituents[0]],
            }
        ).model_validate(
            snapshot.model_copy(
                update={
                    "snapshot_id": "bad-coverage",
                    "constituents": [snapshot.constituents[0]],
                }
            ).model_dump(mode="python")
        )


def test_etf_premium_discount_requires_nav_inav_and_market_price_with_pit(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    profile_source = _register_official_capture(
        service, "OfficialWebDocumentCapture:etf-premium-profile"
    )
    rule = InstrumentTradingUnitRule(
        instrument_id="XSHG:510300",
        instrument_type=InstrumentType.ETF,
        buy_lot_size=100,
        sell_lot_size=100,
        tick_size_cny=Decimal("0.001"),
        settlement_cycle=SettlementCycle.T1,
        effective_from=date(2026, 4, 24),
        source_urls=["https://www.sse.com.cn/assortment/fund/etf/question/"],
        created_at=AS_OF,
    )
    profile = ETFProductProfile(
        profile_id="premium-etf-profile",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        name="fixture premium ETF",
        category=ETFCategory.EQUITY,
        tracking_target="CSI 300",
        tracking_benchmark_market=Market.INDEX,
        tracking_benchmark_symbol="000300",
        management_fee_bps=15,
        custody_fee_bps=5,
        total_expense_ratio_bps=20,
        total_net_asset_cny=Decimal("1000000000"),
        total_outstanding_shares=900_000_000,
        trading_rule=rule,
        facts_as_of=AS_OF - timedelta(days=1),
        quality_status=ProductDataQuality.VERIFIED,
        official_source_artifact_ids=["OfficialWebDocumentCapture:etf-premium-profile"],
        official_source_object_hashes=[profile_source],
        available_to_system_at=AS_OF - timedelta(hours=3),
        created_at=AS_OF,
    )
    service.register_etf_profile(profile)
    with pytest.raises(ValueError, match="VERIFIED ETF profile"):
        ETFProductProfile.model_validate(
            profile.model_copy(update={"total_outstanding_shares": None}).model_dump(mode="python")
        )

    valuation_at = AS_OF - timedelta(hours=2)
    available_at = AS_OF - timedelta(hours=1)
    nav_source = _register_official_capture(
        service,
        "OfficialWebDocumentCapture:etf-nav",
        observed_at=available_at,
    )
    inav_source = _register_official_capture(
        service,
        "OfficialWebDocumentCapture:etf-inav",
        observed_at=available_at,
    )
    price_source = _register_official_capture(
        service,
        "OfficialWebDocumentCapture:etf-market-price",
        observed_at=available_at,
    )
    nav = ETFNavSighting(
        sighting_id="nav-1",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        sighting_type="NAV",
        value_cny=Decimal("1.00"),
        as_of=valuation_at - timedelta(hours=4),
        available_to_system_at=available_at,
        official_source_artifact_ids=["OfficialWebDocumentCapture:etf-nav"],
        official_source_object_hashes=[nav_source],
        created_at=AS_OF,
    )
    inav = ETFNavSighting(
        sighting_id="inav-1",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        sighting_type="INAV",
        value_cny=Decimal("1.01"),
        as_of=valuation_at,
        available_to_system_at=available_at,
        official_source_artifact_ids=["OfficialWebDocumentCapture:etf-inav"],
        official_source_object_hashes=[inav_source],
        created_at=AS_OF,
    )
    market_price = ETFMarketPriceSighting(
        sighting_id="price-1",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        price_cny=Decimal("1.02"),
        as_of=valuation_at,
        available_to_system_at=available_at,
        source_artifact_ids=["OfficialWebDocumentCapture:etf-market-price"],
        source_object_hashes=[price_source],
        created_at=AS_OF,
    )
    service.register_etf_nav_sighting(nav)
    service.register_etf_nav_sighting(inav)
    service.register_etf_market_price_sighting(market_price)

    request = ETFPremiumDiscountRequest(
        profile_artifact_id="ETFProductProfile:premium-etf-profile",
        as_of=AS_OF,
        nav_sighting_artifact_id="ETFNavSighting:nav-1",
        inav_sighting_artifact_id="ETFNavSighting:inav-1",
        market_price_sighting_artifact_id="ETFMarketPriceSighting:price-1",
        created_at=AS_OF,
    )
    valuation = service.evaluate_etf_premium_discount(request)
    assert valuation.premium_discount_basis == "INAV"
    assert valuation.premium_discount_rate == pytest.approx(
        float(Decimal("1.02") / Decimal("1.01") - 1)
    )
    assert valuation.market_to_nav_rate == pytest.approx(0.02)
    assert (
        service.audit(f"ETFPremiumDiscountValuation:{valuation.valuation_id}")["status"] == "PASS"
    )

    with pytest.raises(ValueError):
        service.evaluate_etf_premium_discount(
            request.model_copy(update={"nav_sighting_artifact_id": "ETFNavSighting:missing-nav"})
        )
    with pytest.raises(ValueError):
        service.evaluate_etf_premium_discount(
            request.model_copy(update={"inav_sighting_artifact_id": "ETFNavSighting:missing-inav"})
        )
    with pytest.raises(ValueError, match="distinct NAV and iNAV"):
        service.evaluate_etf_premium_discount(
            request.model_copy(update={"nav_sighting_artifact_id": "ETFNavSighting:inav-1"})
        )


def _transition_fixture(
    tmp_path: Path,
) -> tuple[PortfolioDecisionService, PortfolioTransitionRequest]:
    state, objects = _state(tmp_path)
    history_ref = objects.put_json({"fixture": "portfolio-history"})
    portfolio = _PortfolioStub(history_ref.sha256)
    service = PortfolioDecisionService(
        state,
        objects,
        LocalPortfolioService(tmp_path, state),
        cast(PortfolioService, portfolio),
        PROJECT_ROOT,
    )
    analysis_request = PortfolioAnalysisRequest(
        portfolio_id="portfolio:test",
        as_of=AS_OF,
        holdings=[
            PortfolioHoldingInput(company_id="600000", market=Market.XSHG, weight=0.02),
            PortfolioHoldingInput(company_id="600001", market=Market.XSHG, weight=0.02),
        ],
        lookback_sessions=60,
        minimum_common_sessions=40,
        live=False,
        created_at=AS_OF,
    )
    request_id = "PortfolioAnalysisRequest:fixture"
    request_hash = _register_model(
        state, objects, request_id, "PortfolioAnalysisRequest", analysis_request
    )
    metrics = PortfolioRiskMetrics(
        invested_weight=0.04,
        cash_weight=0.96,
        constant_weight_historical_annualized_return=0.01,
        annualized_volatility=0.05,
        annualized_downside_deviation=0.03,
        beta_to_benchmark=0.10,
        annualized_tracking_error=0.04,
        max_drawdown=0.05,
        historical_var_95=0.01,
        historical_cvar_95=0.02,
        historical_cdar_95=0.03,
        concentration_hhi=0.5,
        effective_number_of_positions=2.0,
        max_abs_pair_correlation=0.3,
        top_risk_contribution_fraction=0.5,
        industry_exposures={"UNVERIFIED": 0.04},
        created_at=AS_OF,
    )
    assets = [
        PortfolioAssetRisk(
            company_id=symbol,
            market=Market.XSHG,
            weight=0.02,
            latest_close_fen=1000,
            observation_count=60,
            annualized_volatility=0.1,
            beta_to_benchmark=0.1,
            risk_contribution_fraction=0.5,
            max_abs_pair_correlation=0.3,
            daily_release_id=f"daily-{symbol}",
            daily_release_object_hash="a" * 64,
            created_at=AS_OF,
        )
        for symbol in ("600000", "600001")
    ]
    current = PortfolioAnalysisReport(
        report_id="portfolio-analysis:fixture",
        portfolio_id="portfolio:test",
        as_of=AS_OF,
        data_cutoff_at=AS_OF,
        status=PortfolioAnalysisStatus.READY,
        common_session_count=60,
        assets=assets,
        metrics=metrics,
        benchmark_release_id="daily-000300",
        benchmark_release_object_hash="a" * 64,
        warning_codes=[],
        hard_breach_codes=[],
        source_artifact_ids=[request_id],
        source_object_hashes=[request_hash],
        created_at=AS_OF,
    )
    _register_model(
        state,
        objects,
        "PortfolioAnalysisReport:fixture",
        "PortfolioAnalysisReport",
        current,
        input_hashes=[request_hash],
    )
    proposals = [
        PortfolioAllocationProposal(
            method=method,
            weights={"600000": 0.10, "600001": 0.10},
            cash_weight=0.80,
            ex_ante_annualized_volatility=0.08,
            concentration_hhi=0.5,
            max_single_weight=0.10,
            max_group_weight=0.20,
            binding_constraint_codes=[],
            model_risk_codes=[],
            created_at=AS_OF,
        )
        for method in PortfolioAllocationMethod
    ]
    construction = PortfolioConstructionReport(
        report_id="portfolio-construction:fixture",
        portfolio_id="portfolio:test",
        as_of=AS_OF,
        data_cutoff_at=AS_OF,
        status=PortfolioAnalysisStatus.READY,
        proposals=proposals,
        admitted_company_ids=["600000", "600001"],
        rejected_company_ids=[],
        common_session_count=60,
        warning_codes=[],
        source_artifact_ids=[],
        source_object_hashes=[],
        created_at=AS_OF,
    )
    _register_model(
        state,
        objects,
        "PortfolioConstructionReport:fixture",
        "PortfolioConstructionReport",
        construction,
    )
    transition_request = PortfolioTransitionRequest(
        current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
        target_construction_artifact_id="PortfolioConstructionReport:fixture",
        intent=PortfolioIntentProfile(
            portfolio_id="portfolio:test",
            as_of=AS_OF,
            anchor_company_id="600000",
            risk_objectives=[],
            constraints_complete=True,
            created_at=AS_OF,
        ),
        selected_method=PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED,
        portfolio_nav_fen=1_000_000,
        current_quantities={"600000": 20, "600001": 20},
        created_at=AS_OF,
    )
    return service, transition_request


def test_portfolio_transition_generates_target_bands_and_keeps_no_trade_logic_deterministic(
    tmp_path: Path,
) -> None:
    service, request = _transition_fixture(tmp_path)
    report = service.transition(request)
    assert report.paper_ledger_write_allowed is False
    assert report.broker_execution_allowed is False
    assert report.current.weights == {"600000": 0.02, "600001": 0.02}
    assert report.target.weights == {"600000": 0.1, "600001": 0.1}
    assert report.anchor_only is not None
    assert report.anchor_only.weights["600000"] == 0.1
    assert report.estimated_turnover_weight == pytest.approx(0.08)
    assert {item.action for item in report.target_bands} == {PositionAction.ADD}
    for band in report.target_bands:
        assert band.target_weight_upper <= 0.10
        assert band.estimated_trade_quantity_min is None
        assert band.estimated_trade_quantity_max is None
        assert band.target_quantity_min is None
        assert band.target_quantity_max is None
    assert any(
        item.startswith("TRADING_UNIT_EXACT_TRANSITION_UNAVAILABLE")
        for item in report.warning_codes
    )
    second = service.transition(request)
    assert second.report_id == report.report_id
    assert service.audit(f"PortfolioTransitionReport:{report.report_id}")["status"] == "PASS"


def test_no_trade_band_keeps_small_weight_drift_as_hold(tmp_path: Path) -> None:
    service, _ = _transition_fixture(tmp_path)
    assert service._band_action(0.091, 0.10, 0.08, 0.12) is PositionAction.HOLD
    assert service._band_action(0.05, 0.10, 0.08, 0.12) is PositionAction.ADD
    assert service._band_action(0.14, 0.10, 0.08, 0.12) is PositionAction.TRIM


def test_explicit_hedge_remains_blocked_by_long_only_toolkit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = HedgeEffectivenessReport(
        report_id="explicit-fixture",
        current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
        instrument_id="XSHG:510300",
        targeted_risk=PortfolioRiskObjective.REDUCE_MARKET_BETA,
        hedge_weight=0.10,
        classification=HedgeClassification.EXPLICIT_HEDGE,
        baseline_risk_value=1.0,
        hedged_risk_value=0.8,
        gross_risk_reduction_fraction=0.2,
        estimated_round_trip_cost_bps=10,
        cost_verified=True,
        cost_acceptable=True,
        normal_correlation=-0.5,
        stress_correlation=-0.3,
        common_session_count=60,
        basis_risk_codes=[],
        source_artifact_ids=[],
        source_object_hashes=[],
        created_at=AS_OF,
    )
    ref = service.objects.put_json(report.model_dump(mode="json"))
    artifact_id = f"HedgeEffectivenessReport:{report.report_id}"
    service.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="HedgeEffectivenessReport",
        schema_version=report.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    candidate = HedgeInstrumentCandidate(
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        instrument_type=InstrumentType.ETF,
        classification=HedgeClassification.EXPLICIT_HEDGE,
        targeted_risk_codes=["REDUCE_MARKET_BETA"],
        expected_risk_reduction_fraction=0.19,
        source_artifact_ids=[artifact_id],
        source_object_hashes=[ref.sha256],
        created_at=AS_OF,
    )
    with pytest.raises(ValueError, match="explicit hedge is not admitted"):
        service._verify_hedge_candidate(candidate)


class _HedgePortfolioStub(_PortfolioStub):
    def _history(
        self,
        company_id: str,
        market: Market,
        *,
        as_of: datetime,
        lookback_sessions: int,
        minimum_sessions: int,
        live: bool,
        allow_live_capture: bool,
    ) -> _History:
        del minimum_sessions, live, allow_live_capture
        count = max(lookback_sessions, 60)
        if company_id == "510300":
            returns = [
                -(0.008 + 0.0002 * (index % 5))
                if index % 2 == 0
                else (0.007 + 0.0001 * (index % 7))
                for index in range(count)
            ]
        else:
            returns = [
                (0.010 + 0.0003 * (index % 5))
                if index % 2 == 0
                else -(0.009 + 0.0002 * (index % 7))
                for index in range(count)
            ]
        price = 100.0
        closes = {date(2026, 5, 1).isoformat(): price}
        for index, value in enumerate(returns, start=1):
            price *= 1.0 + value
            closes[(date(2026, 5, 1) + timedelta(days=index)).isoformat()] = price
        return _History(
            company_id=company_id,
            market=market,
            closes_by_date=closes,
            research_closes_by_date=closes,
            latest_close_fen=int(round(price * 100)),
            release_id=f"daily-{company_id}",
            release_object_hash=self.history_object_hash,
            cutoff_at=as_of,
            average_daily_amount_cny=500_000_000.0,
        )


def test_hedge_effectiveness_requires_verified_cost_before_natural_hedge(tmp_path: Path) -> None:
    base_service, _ = _transition_fixture(tmp_path)
    hedge_portfolio = _HedgePortfolioStub(
        base_service.objects.put_json({"fixture": "hedge-history"}).sha256
    )
    service = PortfolioDecisionService(
        base_service.state,
        base_service.objects,
        base_service.local_portfolio,
        cast(PortfolioService, hedge_portfolio),
        PROJECT_ROOT,
    )
    source_hash = _register_official_capture(service, "OfficialWebDocumentCapture:hedge-mechanism")
    etf_rule = InstrumentTradingUnitRule(
        instrument_id="XSHG:510300",
        instrument_type=InstrumentType.ETF,
        buy_lot_size=100,
        sell_lot_size=100,
        tick_size_cny=Decimal("0.001"),
        settlement_cycle=SettlementCycle.T1,
        effective_from=date(2026, 4, 24),
        source_urls=["https://www.sse.com.cn/assortment/fund/etf/question/"],
        created_at=AS_OF,
    )
    profile = ETFProductProfile(
        profile_id="hedge-etf-profile",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        name="fixture hedge ETF",
        category=ETFCategory.EQUITY,
        tracking_target="fixture inverse economic exposure",
        tracking_benchmark_market=Market.INDEX,
        tracking_benchmark_symbol="000300",
        management_fee_bps=15,
        custody_fee_bps=5,
        total_expense_ratio_bps=20,
        trading_rule=etf_rule,
        official_source_artifact_ids=["OfficialWebDocumentCapture:hedge-mechanism"],
        official_source_object_hashes=[source_hash],
        available_to_system_at=AS_OF,
        created_at=AS_OF,
    )
    service.register_etf_profile(profile)
    metrics = service.evaluate_etf_metrics(
        ETFResearchMetricsRequest(
            profile_artifact_id="ETFProductProfile:hedge-etf-profile",
            as_of=AS_OF,
            lookback_sessions=60,
            minimum_sessions=40,
            created_at=AS_OF,
        )
    )
    metrics_artifact_id = f"ETFResearchMetrics:{metrics.metrics_id}"
    assert metrics.average_daily_amount_cny == 500_000_000.0
    assert metrics.tracking_error_annualized is not None
    assert metrics.total_expense_ratio_bps == 20
    assert "ETF_PREMIUM_DISCOUNT_UNAVAILABLE_NO_NAV_SERIES" in metrics.warning_codes
    assert service.audit(metrics_artifact_id)["status"] == "PASS"
    common: dict[str, Any] = dict(
        current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        instrument_type=InstrumentType.ETF,
        hedge_weight=0.04,
        targeted_risk="REDUCE_VOLATILITY",
        etf_profile_artifact_id="ETFProductProfile:hedge-etf-profile",
        etf_metrics_artifact_id=metrics_artifact_id,
        mechanism_source_artifact_ids=["OfficialWebDocumentCapture:hedge-mechanism"],
        mechanism_source_object_hashes=[source_hash],
        created_at=AS_OF,
    )
    without_cost = service.evaluate_hedge(HedgeEffectivenessRequest(**common))
    assert without_cost.classification is HedgeClassification.DIVERSIFICATION
    assert "IMPLEMENTATION_COST_UNVERIFIED" in without_cost.basis_risk_codes
    cost = PortfolioImplementationCostInput(
        instrument_id="XSHG:510300",
        estimated_round_trip_cost_bps=10,
        source_artifact_ids=["OfficialWebDocumentCapture:hedge-mechanism"],
        source_object_hashes=[source_hash],
        verified=True,
        created_at=AS_OF,
    )
    with_cost = service.evaluate_hedge(
        HedgeEffectivenessRequest(**common, implementation_cost=cost)
    )
    assert with_cost.classification is HedgeClassification.NATURAL_HEDGE
    assert with_cost.gross_risk_reduction_fraction > 0.05
    assert with_cost.stress_correlation < 0
    assert "IMPLEMENTATION_COST_WITHIN_FORMAL_HEDGE_LIMIT" in with_cost.basis_risk_codes
    assert "HEDGE_TEST_IS_GROSS_OVERLAY_NOT_SELF_FINANCING" in with_cost.basis_risk_codes
    assert any(
        item.startswith("PortfolioDecisionPolicy:") for item in with_cost.source_artifact_ids
    )
    high_cost = cost.model_copy(update={"estimated_round_trip_cost_bps": 100})
    expensive = service.evaluate_hedge(
        HedgeEffectivenessRequest(**common, implementation_cost=high_cost)
    )
    assert expensive.classification is HedgeClassification.DIVERSIFICATION
    assert expensive.cost_verified is True
    assert expensive.cost_acceptable is False
    assert "IMPLEMENTATION_COST_ABOVE_FORMAL_HEDGE_LIMIT" in expensive.basis_risk_codes
    report_artifact_id = f"HedgeEffectivenessReport:{with_cost.report_id}"
    assert service.audit(report_artifact_id)["status"] == "PASS"
    report_record = service.state.artifact_record(report_artifact_id)
    assert report_record is not None
    candidate = HedgeInstrumentCandidate(
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        instrument_type=InstrumentType.ETF,
        classification=HedgeClassification.NATURAL_HEDGE,
        targeted_risk_codes=["REDUCE_VOLATILITY"],
        expected_risk_reduction_fraction=with_cost.gross_risk_reduction_fraction,
        normal_correlation=with_cost.normal_correlation,
        stress_correlation=with_cost.stress_correlation,
        estimated_cost_bps=10,
        source_artifact_ids=[report_artifact_id],
        source_object_hashes=[str(report_record["object_hash"])],
        created_at=AS_OF,
    )
    service._verify_hedge_candidate(candidate)


def test_undefined_stress_correlation_is_not_treated_as_valid_low_correlation() -> None:
    value, valid = PortfolioDecisionService._correlation_with_status(
        np.asarray([-0.01, -0.01, -0.01, -0.01, -0.01]),
        np.asarray([0.01, 0.01, 0.01, 0.01, 0.01]),
    )
    assert value == 0.0
    assert valid is False


class _ComplementPortfolioStub(_PortfolioStub):
    def _history(
        self,
        company_id: str,
        market: Market,
        *,
        as_of: datetime,
        lookback_sessions: int,
        minimum_sessions: int,
        live: bool,
        allow_live_capture: bool,
    ) -> _History:
        del minimum_sessions, live, allow_live_capture
        count = max(lookback_sessions, 60)
        inverse = company_id == "600002"
        price = 100.0
        closes = {date(2026, 5, 1).isoformat(): price}
        for index in range(1, count + 1):
            base = 0.008 + 0.0003 * (index % 5)
            sign = 1.0 if index % 2 == 0 else -1.0
            value = -sign * base if inverse else sign * base
            price *= 1.0 + value
            closes[(date(2026, 5, 1) + timedelta(days=index)).isoformat()] = price
        return _History(
            company_id=company_id,
            market=market,
            closes_by_date=closes,
            research_closes_by_date=closes,
            latest_close_fen=int(round(price * 100)),
            release_id=f"daily-{company_id}",
            release_object_hash=self.history_object_hash,
            cutoff_at=as_of,
        )


def test_complement_screen_uses_research_seed_lineage_without_granting_weight_authority(
    tmp_path: Path, monkeypatch
) -> None:
    base_service, _ = _transition_fixture(tmp_path)
    history_hash = base_service.objects.put_json({"fixture": "complement-history"}).sha256
    service = PortfolioDecisionService(
        base_service.state,
        base_service.objects,
        base_service.local_portfolio,
        cast(PortfolioService, _ComplementPortfolioStub(history_hash)),
        PROJECT_ROOT,
    )
    seeds = [
        ResearchSeed(
            seed_id=f"seed:{symbol}",
            company_id=symbol,
            market=Market.XSHG,
            name=f"fixture-{symbol}",
            origins=[ResearchSeedOrigin.MARKET],
            research_priority_score=0.8,
            market_liquidity_score=0.8,
            reason_codes=["MARKET_SCREEN"],
            source_snapshot_ids=[],
            created_at=AS_OF,
        )
        for symbol in ("600002", "600003")
    ]
    report = ResearchSeedReport(
        report_id="seed-report:complement",
        as_of=AS_OF,
        data_cutoff_at=AS_OF,
        status=ResearchSeedStatus.READY,
        profiles=[],
        seeds=seeds,
        source_snapshot_ids=[],
        source_object_hashes=[],
        warning_codes=[],
        market_coverage_ratios={Market.XSHG: 1.0},
        universe_coverage_status=ResearchUniverseCoverageStatus.PARTIAL,
        formal_full_market_coverage_allowed=False,
        market_seed_count=2,
        expert_seed_count=0,
        existing_candidate_seed_count=0,
        created_at=AS_OF,
    )
    seed_artifact_id = "ResearchSeedReport:complement"
    _register_model(
        service.state,
        service.objects,
        seed_artifact_id,
        "ResearchSeedReport",
        report,
    )
    screened = service.screen_complements(
        PortfolioComplementScreenRequest(
            current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
            research_seed_report_artifact_id=seed_artifact_id,
            objective=PortfolioRiskObjective.DIVERSIFY,
            max_candidates=2,
            created_at=AS_OF,
        )
    )
    assert [item.company_id for item in screened.candidates] == ["600002", "600003"]
    assert screened.candidates[0].prefilter_score > screened.candidates[1].prefilter_score
    assert screened.recommendation_allowed is False
    assert screened.portfolio_weight_allowed is False
    assert screened.universe_coverage_complete is False
    assert "RESEARCH_SEED_UNIVERSE_COVERAGE_NOT_FORMAL_FULL_MARKET" in screened.warning_codes
    artifact_id = f"PortfolioComplementScreenReport:{screened.report_id}"
    assert service.audit(artifact_id)["status"] == "PASS"

    original_history = service.portfolio._history

    def partial_history(company_id: str, market: Market, **kwargs: Any):
        if company_id == "600003":
            raise ValueError("synthetic candidate history gap")
        return original_history(company_id, market, **kwargs)

    monkeypatch.setattr(service.portfolio, "_history", partial_history)
    partial = service.screen_complements(
        PortfolioComplementScreenRequest(
            current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
            research_seed_report_artifact_id=seed_artifact_id,
            objective=PortfolioRiskObjective.DIVERSIFY,
            max_candidates=2,
            created_at=AS_OF,
        )
    )
    assert [item.company_id for item in partial.candidates] == ["600002"]
    assert "COMPLEMENT_HISTORY_UNAVAILABLE:600003" in partial.warning_codes


def test_transition_refuses_unverifiable_explicit_industry_limit(tmp_path: Path) -> None:
    service, request = _transition_fixture(tmp_path)
    constrained = request.model_copy(
        update={"intent": request.intent.model_copy(update={"max_industry_exposure": 0.20})}
    )
    with pytest.raises(ValueError, match="verified industry taxonomy"):
        service.transition(constrained)


def test_target_transition_rejects_risk_limit_breach(tmp_path: Path) -> None:
    service, request = _transition_fixture(tmp_path)
    strict = request.model_copy(
        update={"intent": request.intent.model_copy(update={"max_market_beta": 0.0001})}
    )
    with pytest.raises(ValueError, match="market-beta constraint"):
        service.transition(strict)


def test_etf_profile_rejects_official_capture_observed_after_profile_boundary(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    future_hash = _register_official_capture(
        service,
        "OfficialWebDocumentCapture:future-etf-profile",
        observed_at=AS_OF + timedelta(hours=1),
    )
    rule = InstrumentTradingUnitRule(
        instrument_id="XSHG:510300",
        instrument_type=InstrumentType.ETF,
        buy_lot_size=100,
        sell_lot_size=100,
        tick_size_cny=Decimal("0.001"),
        settlement_cycle=SettlementCycle.T1,
        effective_from=date(2026, 4, 24),
        source_urls=["https://www.sse.com.cn/assortment/fund/etf/question/"],
        created_at=AS_OF,
    )
    profile = ETFProductProfile(
        profile_id="future-source-profile",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        name="fixture ETF",
        category=ETFCategory.EQUITY,
        tracking_target="CSI 300",
        trading_rule=rule,
        official_source_artifact_ids=["OfficialWebDocumentCapture:future-etf-profile"],
        official_source_object_hashes=[future_hash],
        available_to_system_at=AS_OF,
        created_at=AS_OF,
    )
    with pytest.raises(ValueError, match="not visible at the requested as_of"):
        service.register_etf_profile(profile)


def test_hedge_evaluation_rejects_etf_profile_created_after_portfolio_as_of(
    tmp_path: Path,
) -> None:
    base_service, _ = _transition_fixture(tmp_path)
    history_hash = base_service.objects.put_json({"fixture": "future-profile-history"}).sha256
    service = PortfolioDecisionService(
        base_service.state,
        base_service.objects,
        base_service.local_portfolio,
        cast(PortfolioService, _HedgePortfolioStub(history_hash)),
        PROJECT_ROOT,
    )
    source_hash = _register_official_capture(
        service, "OfficialWebDocumentCapture:future-visible-product"
    )
    future_at = AS_OF + timedelta(hours=1)
    rule = InstrumentTradingUnitRule(
        instrument_id="XSHG:510300",
        instrument_type=InstrumentType.ETF,
        buy_lot_size=100,
        sell_lot_size=100,
        tick_size_cny=Decimal("0.001"),
        settlement_cycle=SettlementCycle.T1,
        effective_from=date(2026, 4, 24),
        source_urls=["https://www.sse.com.cn/assortment/fund/etf/question/"],
        created_at=AS_OF,
    )
    profile = ETFProductProfile(
        profile_id="future-visible-profile",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        name="fixture future ETF",
        category=ETFCategory.EQUITY,
        tracking_target="CSI 300",
        trading_rule=rule,
        official_source_artifact_ids=["OfficialWebDocumentCapture:future-visible-product"],
        official_source_object_hashes=[source_hash],
        available_to_system_at=future_at,
        created_at=future_at,
    )
    service.register_etf_profile(profile)
    with pytest.raises(ValueError, match="was not visible at the portfolio as_of"):
        service.evaluate_hedge(
            HedgeEffectivenessRequest(
                current_analysis_artifact_id="PortfolioAnalysisReport:fixture",
                instrument_id="XSHG:510300",
                market=Market.XSHG,
                symbol="510300",
                instrument_type=InstrumentType.ETF,
                hedge_weight=0.04,
                targeted_risk=PortfolioRiskObjective.REDUCE_VOLATILITY,
                etf_profile_artifact_id="ETFProductProfile:future-visible-profile",
                etf_metrics_artifact_id="ETFResearchMetrics:unreachable",
                created_at=AS_OF,
            )
        )
