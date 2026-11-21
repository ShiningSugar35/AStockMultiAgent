"""Synthetic native-release accounting fixtures, not historical/OOS market evidence."""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from astock.core.errors import PolicyError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.cninfo import freeze_disclosure_batch
from astock.experiments.canonical_release import (
    MarketAssemblyRequest,
    assemble_registered_market_release,
)
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.paper_trading.operation import (
    MarketReferencePaperVerifier,
    PaperBoardRule,
    PaperPriceLimitRule,
    PaperTradingRuleBook,
)
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import FetchStatus, SourceSnapshot
from astock.schemas.documents import (
    DisclosureExchange,
    DisclosureSearchBatch,
    DisclosureSearchRequest,
)
from astock.schemas.market import InstrumentType, Market
from astock.schemas.paper import PaperTradingClassification, ReplayFeeSchedule
from astock.schemas.reference_data import (
    DailyBarObservation,
    DatasetReleaseManifest,
    InstrumentRecord,
    ReferenceBatch,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    TradingSession,
)
from astock.schemas.research_runtime import (
    TradingClassificationCorporateActionBaseline,
    TradingClassificationDraft,
)

ROOT = Path(__file__).resolve().parents[2]
DAYS = tuple(date(2026, 7, day) for day in (20, 21, 22, 23, 24, 27))


def native_inputs(
    tmp_path, *, raw_has_more=False, future_signal=False, future_reference_heads=False,
):
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    reference = MarketReferenceService(
        state, objects, ReferenceParquetStore(tmp_path / "data/parquet"),
        ROOT / "tests/fixtures/reference",
    )

    def raw(value, stamp, source="synthetic-recorded"):
        ref = objects.put_json(value)
        snapshot = SourceSnapshot(
            snapshot_id=content_hash({
                "hash": ref.sha256, "time": stamp.isoformat(), "source": source
            }),
            source_id=source, object_sha256=ref.sha256, byte_size=ref.byte_size,
            fetched_at=stamp, available_to_system_at=stamp, created_at=stamp,
            fetch_status=FetchStatus.SUCCEEDED, mime="application/json",
            rights_status="PUBLIC_RESEARCH_FIXTURE",
        )
        state.register_snapshot(snapshot)
        return snapshot.snapshot_id

    def publish(kind, scope, rows, stamp, *, start=None, end=None):
        report = reference._release(
            command="recorded-software-proof", dataset_kind=kind, scope_key=scope,
            provider_id="synthetic-recorded",
            raw_snapshot_ids=sorted({item.source_snapshot_id for item in rows}),
            records=rows, requested_start=start, requested_end=end, available_at=stamp,
            complete=True, reasons=[],
        )
        assert report.release_id
        return report.release_id

    before = datetime(2026, 7, 17, tzinfo=UTC)
    base = raw({"synthetic": "instrument/calendar"}, before)
    listing_date = date(2010, 1, 1)
    instrument = InstrumentRecord(
        instrument_id="XSHG:600001", market=Market.XSHG, symbol="600001", name="录制夹具",
        instrument_type=InstrumentType.STOCK, tradable=True, status_date=before.date(),
        is_st=False, listing_date=listing_date, source_snapshot_id=base,
        available_to_system_at=before, created_at=before,
    )
    instrument_id = publish(ReferenceDatasetKind.INSTRUMENT_MASTER, "XSHG", [instrument], before)
    dates = [date(2026, 7, 10)+timedelta(days=offset) for offset in range(18)]
    calendar_id = publish(
        ReferenceDatasetKind.TRADING_CALENDAR, "XSHG",
        [TradingSession(
            exchange=Market.XSHG, session_date=day, is_open=day.weekday() < 5,
            source_snapshot_id=base, available_to_system_at=before, created_at=before,
        ) for day in dates], before, start=dates[0], end=dates[-1],
    )
    rules = PaperTradingRuleBook(
        rule_version="recorded-rules", effective_from=date(2010, 1, 1),
        price_limit_rules=(PaperPriceLimitRule(
            Market.XSHG, "MAIN", "NORMAL", date(2010, 1, 1), 1000,
            ("https://example.invalid/rule",),
        ),),
        board_rules=(PaperBoardRule(
            Market.XSHG, "MAIN", ("600",), date(2010, 1, 1), 5,
            ("https://example.invalid/rule",),
        ),),
    )
    classification = TradingClassificationService(
        state, objects, reference=reference, trading_rules=rules
    )
    rule_id = classification._freeze_rulebook(before)
    stock, index, identities = [], [], []
    stock_id: str | None = None
    index_id: str | None = None
    stamp: datetime | None = None
    for ordinal, day in enumerate(DAYS):
        stamp = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
        for rows, market, symbol, close in (
            (stock, Market.XSHG, "600001", Decimal("10")+Decimal(ordinal)/10),
            (index, Market.INDEX, "000300", Decimal("100.123")+ordinal),
        ):
            source = raw({"date": day.isoformat(), "symbol": symbol, "close": str(close)}, stamp)
            rows.append(DailyBarObservation(
                observation_id=content_hash({"source": source, "symbol": symbol}),
                instrument_id=f"{market.value}:{symbol}", market=market, symbol=symbol,
                session_date=day, session_close_at=stamp, open=close, high=close, low=close,
                close=close, previous_close=rows[-1].close if rows else close,
                volume=Decimal(100000), is_st=False, source_snapshot_id=source,
                available_to_system_at=stamp, created_at=stamp,
            ))
        stock_id = publish(
            ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600001", stock, stamp,
            start=DAYS[0], end=day,
        )
        index_id = publish(
            ReferenceDatasetKind.DAILY_UNADJUSTED, "INDEX:000300", index, stamp,
            start=DAYS[0], end=day,
        )
        snapshot_id = raw(
            {
                "announcements": [], "totalRecordNum": 0, "hasMore": raw_has_more,
                "recorded_query_end": day.isoformat(),
            },
            stamp, "cninfo-disclosures:index",
        )
        batch = DisclosureSearchBatch(
            batch_id=content_hash({"day": day.isoformat()}), provider_id="cninfo-disclosures",
            request=DisclosureSearchRequest(
                symbol="600001", exchange=DisclosureExchange.SSE,
                start_date=listing_date, end_date=day, page_number=1, page_size=30,
                created_at=stamp,
            ),
            announcements=[], total_count=0, total_pages=1, has_more=False,
            raw_snapshot_id=snapshot_id, resolution_snapshot_ids=[], provider_latency_ms=0,
            created_at=stamp,
        )
        receipt_id = freeze_disclosure_batch(state, objects, batch)
        baseline = TradingClassificationCorporateActionBaseline(
            baseline_id=f"recorded:{ordinal}", company_id="600001", market=Market.XSHG,
            symbol="600001", as_of=stamp, window_start="2010-01-01", window_end=day.isoformat(),
            reference_status="OFFICIAL_ENUMERATION_COMPLETE", raw_snapshot_ids=[snapshot_id],
            official_query_snapshot_ids=[snapshot_id], official_query_artifact_ids=[receipt_id],
            observed_record_count=0, candidate_announcement_ids=[], reason_codes=[],
            absence_is_officially_certified=True, created_at=stamp,
        )
        baseline_id = classification._register_official_baseline(baseline)
        draft = TradingClassificationDraft(
            company_id="600001", market=Market.XSHG, symbol="600001",
            as_of=stamp, effective_from=stamp, valid_until=stamp,
            classification=PaperTradingClassification(
                instrument_id="XSHG:600001", board="MAIN", risk_status="NORMAL",
                fixed_price_limit_eligible=True, suspension_status_verified=True,
                suspended=False, evidence_id=f"recorded:{ordinal}", created_at=stamp,
            ),
            special_no_price_limit=False, price_limit_rate_bps=1000,
            rulebook_artifact_id=rule_id, instrument_release_id=instrument_id,
            calendar_release_id=calendar_id, daily_release_id=stock_id,
            corporate_action_baseline_artifact_id=baseline_id,
            resolver_version="market-reference-trading-classification-v1",
            source_artifact_ids=sorted([
                rule_id, baseline_id, f"market-reference:{instrument_id}",
                f"market-reference:{calendar_id}", f"market-reference:{stock_id}",
            ]), created_at=stamp,
        )
        identities.append(classification.freeze(draft).artifact_id)
    assert stamp is not None and stock_id is not None and index_id is not None
    if future_signal:
        # A later capture needs its own raw/observation identity. Altering only
        # volatile availability would correctly reuse the immutable old batch.
        recapture = raw({
            "symbol": stock[0].symbol, "session_date": stock[0].session_date.isoformat(),
            "close": str(stock[0].close), "recorded_capture": "late",
        }, stamp)
        stock[0] = stock[0].model_copy(update={
            "observation_id": content_hash({"source": recapture, "symbol": stock[0].symbol}),
            "source_snapshot_id": recapture, "available_to_system_at": stamp,
        })
        stock_id = publish(
            ReferenceDatasetKind.DAILY_UNADJUSTED, "XSHG:600001", stock, stamp,
            start=DAYS[0], end=DAYS[-1],
        )
    if future_reference_heads:
        future = stamp + timedelta(days=1)
        source = raw({"synthetic": "later-instrument-and-calendar-head"}, future)
        publish(
            ReferenceDatasetKind.INSTRUMENT_MASTER, "XSHG",
            [instrument.model_copy(update={
                "name": "录制夹具后续版本", "status_date": future.date(),
                "source_snapshot_id": source, "available_to_system_at": future,
                "created_at": future,
            })], future,
        )
        publish(
            ReferenceDatasetKind.TRADING_CALENDAR, "XSHG",
            [TradingSession(
                exchange=Market.XSHG, session_date=day, is_open=day.weekday() < 5,
                source_snapshot_id=source, available_to_system_at=future, created_at=future,
            ) for day in dates], future, start=dates[0], end=dates[-1],
        )
    fee = ReplayFeeSchedule(
        rule_version="recorded-dated-fees", effective_from=date(2010, 1, 1),
        applicable_markets=[Market.XSHG], commission_rate=Decimal(".0003"),
        minimum_commission_fen=500, stamp_tax_sell_rate=Decimal(".0005"),
        transfer_fee_rate=Decimal(".00001"), source_urls=["https://example.invalid/fee"],
        created_at=before,
    )
    ref = objects.put_json(fee.model_dump(mode="json"))
    state.register_artifact(
        artifact_id="ReplayFeeSchedule:recorded", artifact_type="ReplayFeeSchedule",
        schema_version=fee.schema_version, object_hash=ref.sha256, input_hashes=[],
    )
    request = MarketAssemblyRequest(
        daily_release_id=stock_id, benchmark_release_id=index_id,
        classification_artifact_ids=tuple(identities),
        fee_artifact_ids=("ReplayFeeSchedule:recorded",),
        start=DAYS[0], end=DAYS[-1], as_of=stamp, initial_cash_fen=1000000,
        slippage_bps=Decimal(5), maximum_participation=Decimal(".05"),
    )
    return state, objects, reference, request


def assemble(args, **kwargs):
    return assemble_registered_market_release(
        *args, calendar_path=ROOT / "configs/official_trading_calendar.yaml", **kwargs
    )


def test_full_native_proof_assembles_and_registers_the_existing_release(tmp_path):
    args = native_inputs(tmp_path)
    result = assemble(args, register=True)
    assert result["status"] == "ASSEMBLED_RESEARCH_ONLY", result
    assert len(result["release"]["sessions"]) == 6
    assert not result["market_validation_claim"] and not result["ledger_write_allowed"]
    assert Decimal(result["release"]["sessions"][1]["limit_up"]) == Decimal("11")
    assert result["artifact_id"] == assemble(args, register=True)["artifact_id"]
    state, objects, _, request = args
    artifact = state.artifact_record(result["artifact_id"])
    assert artifact and objects.verify(artifact["object_hash"])
    missing = request.model_copy(update={
        "classification_artifact_ids": request.classification_artifact_ids[1:]
    })
    declined = assemble((state, objects, args[2], missing))
    assert "EXACT_DATED_TRADING_CLASSIFICATION_REQUIRED" in declined["reason_codes"]


def test_empty_enum_flag_does_not_override_raw_terminal_response(tmp_path):
    result = assemble(native_inputs(tmp_path, raw_has_more=True))
    assert result["status"] == "MISSING_PROOF"
    assert "RAW_TERMINAL_EMPTY_ENUMERATION_REQUIRED" in result["reason_codes"]
    assert result["artifact_id"] is None


def test_current_capture_cannot_be_backdated_into_historical_signals(tmp_path):
    result = assemble(native_inputs(tmp_path, future_signal=True))
    assert result["status"] == "MISSING_PROOF"
    assert "HISTORICAL_SIGNAL_VINTAGE_UNAVAILABLE" in result["reason_codes"]


def complete_action_parent(args, *, complete=True, in_window=False):
    """A recorded complete native contract; it is not live historical coverage."""
    from astock.schemas.reference_data import CorporateActionObservation, CorporateActionStatus

    state, objects, reference, request = args
    stamp = request.as_of
    raw = objects.put_json({"recorded_official_action": "prior dividend"})
    snapshot = SourceSnapshot(
        snapshot_id="recorded-prior-action:" + raw.sha256, source_id="recorded-official",
        object_sha256=raw.sha256, byte_size=raw.byte_size,
        fetched_at=stamp, available_to_system_at=stamp, created_at=stamp,
        fetch_status=FetchStatus.SUCCEEDED, mime="application/json",
    )
    state.register_snapshot(snapshot)
    action = CorporateActionObservation(
        observation_id=content_hash({"action": "prior-dividend"}),
        instrument_id="XSHG:600001", market=Market.XSHG, symbol="600001",
        action_type="CASH_DIVIDEND", announcement_date=date(2026, 5, 1),
        ex_date=DAYS[2] if in_window else date(2026, 5, 20),
        status=CorporateActionStatus.TERMS_VERIFIED,
        structured_terms={"cash_per_share": "1"},
        official_document_snapshot_id=snapshot.snapshot_id,
        official_document_url="https://example.invalid/official-action",
        official_announcement_id="recorded-action",
        source_snapshot_id=snapshot.snapshot_id, available_to_system_at=stamp, created_at=stamp,
    )
    release = reference._release(
        command="recorded-software-proof", dataset_kind=ReferenceDatasetKind.CORPORATE_ACTION,
        scope_key="XSHG:600001", provider_id="synthetic-recorded",
        raw_snapshot_ids=[snapshot.snapshot_id], records=[action],
        requested_start=date(2026, 5, 1), requested_end=DAYS[-1],
        available_at=stamp, complete=complete, reasons=[],
    )
    return (*args[:3], request.model_copy(update={
        "corporate_action_release_id": release.release_id
    }))


def test_existing_complete_action_contract_allows_a_no_event_window(tmp_path):
    # Nonterminal empty query cannot qualify on its own. A separate full native
    # action release is an alternative proof, so ordinary windows are not tied
    # permanently to a listing with zero lifetime announcements.
    args = complete_action_parent(native_inputs(tmp_path, raw_has_more=True))
    result = assemble(args)
    assert result["status"] == "ASSEMBLED_RESEARCH_ONLY", result
    assert result["action_coverage_basis"] == "CANONICAL_COMPLETE_RELEASE"
    assert not result["market_validation_claim"]


def test_partial_action_parent_is_not_promoted_and_in_window_events_are_explicit(tmp_path):
    args = complete_action_parent(native_inputs(tmp_path / "partial"), complete=False)
    assert assemble(args)["status"] == "MISSING_PROOF"
    args = complete_action_parent(native_inputs(tmp_path / "event"), in_window=True)
    result = assemble(args)
    assert result["status"] == "MISSING_PROOF"
    assert "IN_WINDOW_CORPORATE_ACTION_EXECUTION_NOT_SUPPORTED" in result["reason_codes"]


def _historical_and_current_reference(args, kind):
    state, objects, reference, request = args
    release = TradingClassificationService(state, objects).load(
        request.classification_artifact_ids[0],
    ).release
    identity = (
        release.instrument_release_id
        if kind is ReferenceDatasetKind.INSTRUMENT_MASTER else release.calendar_release_id
    )
    assert identity is not None
    current = DatasetReleaseManifest.model_validate(reference.status(kind, "XSHG")["release"])
    assert current.release_id != identity and current.available_to_system_at > request.as_of
    return release, identity, current


def test_historical_reference_ids_survive_future_instrument_and_calendar_heads(tmp_path):
    args = native_inputs(tmp_path, future_reference_heads=True)
    for kind in (ReferenceDatasetKind.INSTRUMENT_MASTER, ReferenceDatasetKind.TRADING_CALENDAR):
        _historical_and_current_reference(args, kind)
    result = assemble(args)
    assert result["status"] == "ASSEMBLED_RESEARCH_ONLY", result

    # Historical assembly must not weaken the existing current-head paper reader.
    _, calendar_id, _ = _historical_and_current_reference(
        args, ReferenceDatasetKind.TRADING_CALENDAR,
    )
    with pytest.raises(PolicyError, match="current reference head"):
        MarketReferencePaperVerifier(args[2]).calendar(
            Market.XSHG, calendar_id, visible_at=args[3].as_of,
        )


@pytest.mark.parametrize(
    "kind", [ReferenceDatasetKind.INSTRUMENT_MASTER, ReferenceDatasetKind.TRADING_CALENDAR],
)
def test_historical_classification_cannot_use_a_future_reference_head(tmp_path, kind):
    args = native_inputs(tmp_path, future_reference_heads=True)
    state, objects, reference, request = args
    old, identity, current = _historical_and_current_reference(args, kind)
    field = (
        "instrument_release_id"
        if kind is ReferenceDatasetKind.INSTRUMENT_MASTER else "calendar_release_id"
    )
    payload = old.model_dump(
        mode="python", exclude={"release_id", "source_object_hashes", "schema_version"},
    )
    payload[field] = current.release_id
    payload["source_artifact_ids"] = sorted(
        f"market-reference:{current.release_id}"
        if value == f"market-reference:{identity}" else value
        for value in old.source_artifact_ids
    )
    changed = TradingClassificationService(state, objects).freeze(
        TradingClassificationDraft.model_validate(payload),
    )
    request = request.model_copy(update={
        "classification_artifact_ids": (
            changed.artifact_id, *request.classification_artifact_ids[1:],
        ),
    })
    result = assemble((state, objects, reference, request))
    assert result["status"] == "MISSING_PROOF"
    assert result["artifact_id"] is None


@pytest.mark.parametrize(
    "kind", [ReferenceDatasetKind.INSTRUMENT_MASTER, ReferenceDatasetKind.TRADING_CALENDAR],
)
def test_historical_reference_verifies_old_bytes_even_with_a_valid_future_head(tmp_path, kind):
    args = native_inputs(tmp_path, future_reference_heads=True)
    state, objects, reference, _ = args
    _, identity, _ = _historical_and_current_reference(args, kind)
    record = state.artifact_record(f"market-reference:{identity}")
    assert record is not None
    old = DatasetReleaseManifest.model_validate_json(
        objects.get_bytes(str(record["object_hash"])),
    )
    (reference.parquet.root / old.canonical_files[0].path).write_bytes(b"corrupted old fixture")
    assert reference.status(kind, "XSHG")["status"] == "AVAILABLE"
    result = assemble(args)
    assert result["status"] == "MISSING_PROOF"
    assert result["artifact_id"] is None


@pytest.mark.parametrize(
    "utc_close,scientific_volume",
    [(True, False), (False, True), (False, False)],
)
def test_reference_parquet_roundtrip_uses_one_json_contract(
    tmp_path, utc_close, scientific_volume,
):
    close_at = datetime(2026, 7, 20, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    if utc_close:
        close_at = close_at.astimezone(UTC)
    volume = Decimal("1E+5") if scientific_volume else Decimal("100000")
    bar = DailyBarObservation(
        observation_id="a" * 64, instrument_id="XSHG:600001", market=Market.XSHG,
        symbol="600001", session_date=close_at.date(), session_close_at=close_at,
        open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"),
        volume=volume, source_snapshot_id="recorded", available_to_system_at=close_at,
        created_at=close_at,
    )
    batch = ReferenceBatch(
        batch_id="b" * 64, dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
        scope_key=bar.instrument_id, provider_id="recorded", raw_snapshot_ids=["recorded"],
        records=[bar], available_to_system_at=close_at, created_at=close_at,
        coverage=ReferenceCoverage(
            requested_start=bar.session_date, requested_end=bar.session_date,
            actual_start=bar.session_date, actual_end=bar.session_date,
            record_count=1, status=ReferenceCoverageStatus.COMPLETE, created_at=close_at,
        ),
    )
    parquet = ReferenceParquetStore(tmp_path / "parquet")
    path, logical_hash = parquet.write_canonical(batch)
    descriptor = parquet.describe(path, logical_content_hash=logical_hash, created_at=close_at)
    assert parquet.verify_descriptor(
        descriptor, dataset_kind=batch.dataset_kind.value, scope_key=batch.scope_key,
        provider_id=batch.provider_id, batch_id=batch.batch_id,
        available_to_system_at=close_at, expected_row_count=1,
    )
    table = pq.ParquetFile(path).read()
    payload = json.loads(table.column("record_json")[0].as_py())
    restored = DailyBarObservation.model_validate({**payload, "created_at": close_at})
    assert restored == bar
    original_bytes = path.read_bytes()
    assert parquet.write_canonical(batch) == (path, logical_hash)
    assert path.read_bytes() == original_bytes

    # Preserve the previous on-disk representation to exercise reuse, rather
    # than rewriting existing files as part of the serializer fix.
    legacy_rows = table.to_pylist()
    legacy_rows[0]["record_json"] = canonical_json_bytes(
        bar.model_dump(mode="python", exclude={"created_at"})
    )
    pq.write_table(pa.Table.from_pylist(legacy_rows, schema=table.schema), path)
    legacy_bytes = path.read_bytes()
    if utc_close or scientific_volume:
        with pytest.raises(ValueError, match="logical content does not match"):
            parquet.write_canonical(batch)
    else:
        # Already valid non-UTC/fixed-point files retain their physical bytes.
        assert parquet.write_canonical(batch) == (path, logical_hash)
    assert path.read_bytes() == legacy_bytes
