from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pymupdf

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentPageRepository, DocumentRepository, PdfParseService
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    AvailabilityBasis,
    BarRequest,
    DocumentType,
    EvidenceGrade,
    FactStatus,
    FinancialAuditRequest,
    FinancialDurationSemantics,
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
    Frequency,
    Market,
    MarketBar,
    MarketDataBatch,
    PointInTimeStatus,
    ProviderStatus,
    SourceDocument,
    SourceSnapshot,
    TimestampSemantics,
    VolumeUnit,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

FINANCIAL_GOLDEN_VALUES: dict[FinancialFieldCode, Decimal] = {
    FinancialFieldCode.TOTAL_ASSETS: Decimal("1000"),
    FinancialFieldCode.TOTAL_LIABILITIES: Decimal("600"),
    FinancialFieldCode.TOTAL_EQUITY: Decimal("400"),
    FinancialFieldCode.CASH_BEGINNING: Decimal("100"),
    FinancialFieldCode.NET_CASH_OPERATING: Decimal("200"),
    FinancialFieldCode.NET_CASH_INVESTING: Decimal("-80"),
    FinancialFieldCode.NET_CASH_FINANCING: Decimal("30"),
    FinancialFieldCode.EXCHANGE_EFFECT: Decimal("0"),
    FinancialFieldCode.CASH_ENDING: Decimal("250"),
    FinancialFieldCode.NET_PROFIT_INCOME: Decimal("100"),
    FinancialFieldCode.NET_PROFIT_CASH_FLOW: Decimal("100"),
    FinancialFieldCode.REVENUE: Decimal("1000"),
    FinancialFieldCode.OPERATING_COST: Decimal("600"),
    FinancialFieldCode.ACCOUNTS_RECEIVABLE: Decimal("100"),
    FinancialFieldCode.INVENTORY: Decimal("120"),
    FinancialFieldCode.PREPAYMENTS: Decimal("20"),
    FinancialFieldCode.OTHER_RECEIVABLES: Decimal("10"),
}

_FINANCIAL_STATEMENTS: dict[FinancialFieldCode, FinancialStatementType] = {
    FinancialFieldCode.TOTAL_ASSETS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_LIABILITIES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_EQUITY: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.ACCOUNTS_RECEIVABLE: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.INVENTORY: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.PREPAYMENTS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.OTHER_RECEIVABLES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.CASH_BEGINNING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_OPERATING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_INVESTING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_FINANCING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.EXCHANGE_EFFECT: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.CASH_ENDING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_PROFIT_CASH_FLOW: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_PROFIT_INCOME: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.REVENUE: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.OPERATING_COST: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.EBIT: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.DEPRECIATION_AMORTIZATION: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.SELLING_GENERAL_ADMIN_EXPENSE: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.CURRENT_ASSETS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.CURRENT_LIABILITIES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_DEBT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.RETAINED_EARNINGS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.PROPERTY_PLANT_EQUIPMENT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.LONG_TERM_DEBT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.MARKET_CAP: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.SHARES_OUTSTANDING: FinancialStatementType.BALANCE_SHEET,
}


def make_financial_facts(
    state: StateStore,
    object_store: ObjectStore,
    *,
    source_suffix: str = "v1",
    company_id: str = "000001",
    period_end: date = date(2025, 12, 31),
    period_type: FinancialPeriodType = FinancialPeriodType.ANNUAL,
    duration_semantics: FinancialDurationSemantics = (
        FinancialDurationSemantics.REPORTED_PERIOD
    ),
    published_at: datetime = datetime(2026, 3, 20, tzinfo=UTC),
    values: dict[FinancialFieldCode, Decimal] | None = None,
    unit: FinancialUnit = FinancialUnit.TEN_THOUSAND_CNY,
    evidence_grade: EvidenceGrade = EvidenceGrade.PRIMARY_OFFICIAL,
) -> list[FinancialFact]:
    reported = values or FINANCIAL_GOLDEN_VALUES
    text = "\n".join(f"{code.value}: {value}" for code, value in reported.items())
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_textbox(pymupdf.Rect(48, 48, 540, 790), text, fontsize=8)
    pdf_bytes = pdf.tobytes()
    pdf.close()
    raw = object_store.put_bytes(pdf_bytes)
    source_id = f"fixture:financial:{company_id}:{period_end}:{source_suffix}"
    snapshot = SourceSnapshot(
        snapshot_id=f"{source_id}:{raw.sha256}",
        source_id=source_id,
        object_sha256=raw.sha256,
        fetched_at=published_at,
        available_to_system_at=published_at,
        source_url=f"https://example.invalid/{source_suffix}.pdf",
        mime="application/pdf",
        byte_size=raw.byte_size,
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id=f"document:{source_id}",
        title="Financial integrity recorded fixture",
        publisher="TEST_EXCHANGE",
        document_type=DocumentType.ANNUAL_REPORT,
        company_ids=[company_id],
        published_at=published_at,
        effective_at=published_at,
        disclosure_id=source_id,
        source_url=f"https://example.invalid/{source_suffix}.pdf",
        rights_status="TEST_FIXTURE",
    )
    documents = DocumentRepository(state)
    documents.register(document, snapshot)
    pages = DocumentPageRepository(state)
    report = PdfParseService(object_store, state, pages).parse(
        document, snapshot, ocr_enabled=False
    )
    page_model = pages.get_page_by_id(report.page_ids[0])
    assert page_model is not None
    parsed_text = object_store.get_bytes(page_model.text_object_sha256).decode("utf-8")
    evidence = ClaimEvidenceService(
        object_store,
        state,
        pages,
        documents,
        EvidenceRepository(state),
    ).create_page_evidence(
        page_id=page_model.page_id,
        char_start=0,
        char_end=len(parsed_text),
        evidence_grade=evidence_grade,
        fact_status=FactStatus.DIRECT,
        entity_ids=[f"company:{company_id}"],
    )
    pit = PointInTimeService(PointInTimeRepository(state), state, object_store).create(
        source_id=source_id,
        source_document_id=document.document_id,
        source_snapshot_id=snapshot.snapshot_id,
        period_end=period_end,
        published_at=published_at,
        effective_at=published_at,
        ingested_at=published_at,
        available_to_system_at=published_at,
        point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        availability_basis=AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
    )
    facts: list[FinancialFact] = []
    for code, value in reported.items():
        statement = _FINANCIAL_STATEMENTS[code]
        facts.append(
            FinancialFact(
                fact_id=f"fact:{source_suffix}:{code.value}",
                company_id=company_id,
                period_start=(
                    date(period_end.year, 1, 1)
                    if statement is not FinancialStatementType.BALANCE_SHEET
                    else None
                ),
                period_end=period_end,
                period_type=period_type,
                duration_semantics=(
                    FinancialDurationSemantics.INSTANT
                    if statement is FinancialStatementType.BALANCE_SHEET
                    and duration_semantics
                    is not FinancialDurationSemantics.REPORTED_PERIOD
                    else duration_semantics
                ),
                statement_type=statement,
                field_code=code,
                reported_value=value,
                unit=(
                    FinancialUnit.SHARES
                    if code is FinancialFieldCode.SHARES_OUTSTANDING
                    else unit
                ),
                source_snapshot_id=snapshot.snapshot_id,
                pit_id=pit.pit_id,
                evidence_ids=[evidence.evidence_id],
            )
        )
    return facts


def make_financial_request(
    state: StateStore,
    object_store: ObjectStore,
    *,
    industry_profile: FinancialIndustryProfile = FinancialIndustryProfile.GENERAL_INDUSTRIAL,
) -> FinancialAuditRequest:
    return FinancialAuditRequest(
        company_id="000001",
        as_of=datetime(2026, 3, 21, tzinfo=UTC),
        industry_profile=industry_profile,
        facts=make_financial_facts(state, object_store),
    )


def session_times(trading_date: date = date(2026, 7, 10)) -> list[datetime]:
    values: list[datetime] = []
    for start, end in ((time(9, 35), time(11, 30)), (time(13, 5), time(15, 0))):
        current = datetime.combine(trading_date, start, tzinfo=SHANGHAI)
        final = datetime.combine(trading_date, end, tzinfo=SHANGHAI)
        while current <= final:
            values.append(current)
            current += timedelta(minutes=5)
    return values


def make_batch(
    provider_id: str,
    *,
    volume_unit: VolumeUnit = VolumeUnit.SHARE,
    missing_index: int | None = None,
    bad_ohlc: bool = False,
    symbol: str = "600519",
) -> MarketDataBatch:
    timestamps = session_times()
    if missing_index is not None:
        timestamps.pop(missing_index)
    request = BarRequest(
        symbol=symbol,
        market=Market.XSHG,
        requested_start=datetime(2026, 7, 10, 0, 0, tzinfo=SHANGHAI),
        requested_end=datetime(2026, 7, 10, 23, 59, tzinfo=SHANGHAI),
        adjustment_mode=AdjustmentMode.NONE,
    )
    bars: list[MarketBar] = []
    for index, timestamp in enumerate(timestamps):
        open_price = Decimal("100.00") + Decimal(index) / 100
        close_price = open_price + Decimal("0.10")
        high_price = open_price + Decimal("0.20")
        low_price = open_price - Decimal("0.20")
        if bad_ohlc and index == 0:
            high_price = Decimal("99.00")
        volume_shares = Decimal(100000 + index * 100)
        volume = volume_shares / 100 if volume_unit == VolumeUnit.LOT_100_SHARES else volume_shares
        payload = {
            "provider": provider_id,
            "symbol": symbol,
            "timestamp": timestamp.isoformat(),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
        bars.append(
            MarketBar(
                observation_id=content_hash(payload),
                provider_id=provider_id,
                symbol=symbol,
                market=Market.XSHG,
                frequency=Frequency.M5,
                timestamp=timestamp,
                timestamp_semantics=TimestampSemantics.BAR_END,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                volume_unit=volume_unit,
                amount=volume_shares * close_price,
                amount_unit=AmountUnit.CNY,
                adjustment_mode=AdjustmentMode.NONE,
            )
        )
    return MarketDataBatch(
        batch_id=content_hash(
            {"provider": provider_id, "observations": [bar.observation_id for bar in bars]}
        ),
        provider_id=provider_id,
        request=request,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        actual_start=bars[0].timestamp,
        actual_end=bars[-1].timestamp,
        bar_count=len(bars),
        bars=bars,
        raw_snapshot_id=f"{provider_id}:{'a' * 64}",
        cursor=bars[-1].timestamp.isoformat(),
        provider_latency_ms=10,
        provider_status=ProviderStatus.AVAILABLE,
    )
