"""Assemble a restricted research release from existing canonical evidence.

Ordinary fixed-limit stocks can use an existing COMPLETE corporate-action release
with no event in the window, or an actually empty, exhaustive query receipt.
This proves a bounded software source contract, never market effectiveness.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, Field

from astock.core.errors import AStockError, FailureClass, PolicyError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.cninfo import load_disclosure_batch
from astock.experiments.market_evaluator import SHANGHAI, FrozenMarketRelease, Session
from astock.experiments.validation import Budget, Frozen, digest
from astock.market_data.official_calendar import load_official_trading_calendar
from astock.market_data.reference import MarketReferenceService
from astock.paper_trading.operation import MarketReferencePaperVerifier, _price_bounds
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import FetchStatus
from astock.schemas.documents import DisclosureCategory, DisclosureExchange
from astock.schemas.market import Frequency, Market, MarketBar, TimestampSemantics
from astock.schemas.paper import ReplayFeeSchedule
from astock.schemas.reference_data import (
    CorporateActionStatus,
    DailyBarObservation,
    DatasetReleaseManifest,
    InstrumentRecord,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    TradingSession,
)
from astock.schemas.research_runtime import (
    TradingClassificationCorporateActionBaseline,
    TradingClassificationRelease,
)
from astock.technical.registered import load_daily_release


class MarketAssemblyRequest(Frozen):
    daily_release_id: str
    benchmark_release_id: str
    classification_artifact_ids: tuple[str, ...] = Field(default=(), max_length=504)
    fee_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    corporate_action_release_id: str | None = None
    start: date
    end: date
    as_of: AwareDatetime
    initial_cash_fen: int = Field(gt=0)
    slippage_bps: Decimal = Field(ge=0, le=1000)
    maximum_participation: Decimal = Field(gt=0, le=1)
    max_bars: int = Field(default=504, ge=2, le=504)


class _ProofUnavailable(ValueError):
    pass


class _HistoricalReferenceVerifier(MarketReferencePaperVerifier):
    """Reuse canonical verification with the head visible at each historical cutoff.

    Paper operations intentionally consume the current head. This research-only
    adapter changes only selection; the same complete-manifest and typed-record
    verification continues to enforce the explicitly frozen release identity.
    """

    def _visible_manifest(
        self, kind: ReferenceDatasetKind, scope: str, *, visible_at: datetime,
    ) -> DatasetReleaseManifest:
        status = self.reference.status(kind, scope, as_of=visible_at)
        if status.get("status") != "AVAILABLE":
            raise PolicyError(
                "Historical reference release is unavailable or corrupt",
                failure_class=FailureClass.DATA_QUALITY,
            )
        manifest = DatasetReleaseManifest.model_validate(status["release"])
        if (
            manifest.dataset_kind is not kind or manifest.scope_key != scope
            or manifest.available_to_system_at > visible_at
            or manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE
        ):
            raise PolicyError(
                "Historical reference identity, visibility or coverage is invalid",
                failure_class=FailureClass.DATA_QUALITY,
            )
        return manifest


@dataclass
class _ProofReader:
    state: StateStore
    objects: ObjectStore
    reference: MarketReferenceService
    request: MarketAssemblyRequest
    hashes: set[str] = field(default_factory=set)
    snapshots: set[str] = field(default_factory=set)

    def artifact(self, identity: str, kind: str) -> dict[str, Any]:
        record = self.state.artifact_record(identity)
        if record is None or record["type"] != kind:
            raise _ProofUnavailable("REGISTERED_" + kind.upper() + "_REQUIRED")
        value = str(record["object_hash"])
        if not self.objects.verify(value):
            raise _ProofUnavailable("REGISTERED_INPUT_INTEGRITY_FAILED")
        raw = self.objects.get_bytes(value)
        if len(raw) > 2_000_000:
            raise _ProofUnavailable("REGISTERED_INPUT_BYTE_BUDGET")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise _ProofUnavailable("REGISTERED_INPUT_OBJECT_REQUIRED")
        self.hashes.add(value)
        return payload


def _daily_inputs(
    proof: _ProofReader, calendar_path: Path,
) -> tuple[DatasetReleaseManifest, DatasetReleaseManifest,
           tuple[DailyBarObservation, ...], dict[date, DailyBarObservation]]:
    request = proof.request
    if request.end < request.start or request.end > request.as_of.astimezone(SHANGHAI).date():
        raise _ProofUnavailable("REQUEST_WINDOW_INVALID")
    parents = [load_daily_release(
        proof.state, proof.objects, identity, limit=request.max_bars,
        max_release_rows=20000, parquet_root=proof.reference.parquet.root,
    ) for identity in (request.daily_release_id, request.benchmark_release_id)]
    stock, stock_all, _, stock_hash = parents[0]
    benchmark, benchmark_all, _, benchmark_hash = parents[1]
    proof.hashes.update((stock_hash, benchmark_hash))
    if max(stock.available_to_system_at, benchmark.available_to_system_at) > request.as_of:
        raise _ProofUnavailable("CANONICAL_PARENT_UNAVAILABLE_AT_AS_OF")
    bars = tuple(bar for bar in stock_all if request.start <= bar.session_date <= request.end)
    benchmarks = {bar.session_date: bar for bar in benchmark_all
                  if request.start <= bar.session_date <= request.end}
    if not 2 <= len(bars) <= request.max_bars:
        raise _ProofUnavailable("CANONICAL_WINDOW_COVERAGE_REQUIRED")
    market = bars[0].market
    if market not in {Market.XSHG, Market.XSHE} or any(
        item.market is not Market.INDEX for item in benchmarks.values()
    ):
        raise _ProofUnavailable("ORDINARY_SSE_SZSE_AND_INDEX_SLICE_ONLY")
    calendar = load_official_trading_calendar(calendar_path)
    open_dates = calendar.open_dates(market, request.start, request.end)
    if open_dates is None:
        raise _ProofUnavailable("EXCHANGE_SESSION_COVERAGE_REQUIRED")
    expected = set(open_dates)
    if (tuple(bar.session_date for bar in bars) != tuple(sorted(expected))
            or set(benchmarks) != expected):
        raise _ProofUnavailable("EXCHANGE_SESSION_COVERAGE_REQUIRED")
    for current, following in zip(bars[:-1], bars[1:], strict=True):
        if max(
            current.available_to_system_at, benchmarks[current.session_date].available_to_system_at
        ) > following.session_close_at:
            raise _ProofUnavailable("HISTORICAL_SIGNAL_VINTAGE_UNAVAILABLE")
    return stock, benchmark, bars, benchmarks


def _dated_classification(
    proof: _ProofReader, service: TradingClassificationService,
    loaded: list[Any], bar: DailyBarObservation, verifier: MarketReferencePaperVerifier,
) -> tuple[TradingClassificationRelease, InstrumentRecord, list[TradingSession]]:
    stamp, symbol, market = bar.session_close_at, bar.symbol, bar.market
    selected = [item for item in loaded
                if item.release.effective_from <= stamp <= item.release.valid_until]
    if len(selected) != 1:
        raise _ProofUnavailable("EXACT_DATED_TRADING_CLASSIFICATION_REQUIRED")
    record = selected[0]
    release = record.release
    if (release.company_id != symbol or release.market is not market
            or release.as_of > stamp or release.resolver_version is None
            or release.special_regime != "ORDINARY" or release.price_limit_regime != "FIXED"
            or release.classification.suspended
            or not release.classification.suspension_status_verified
            or service.audit(record.artifact_id)["status"] != "PASS"):
        raise _ProofUnavailable("ORDINARY_PIT_CLASSIFICATION_PROOF_REQUIRED")
    proof.hashes.add(record.object_hash)
    proof.hashes.update(release.source_object_hashes)
    if (not release.instrument_release_id or not release.daily_release_id
            or not release.calendar_release_id):
        raise _ProofUnavailable("CLASSIFICATION_REFERENCE_LINEAGE_REQUIRED")
    instrument = verifier.instrument(
        market, symbol, release.instrument_release_id, visible_at=stamp
    )
    days = verifier.calendar(market, release.calendar_release_id, visible_at=stamp)
    native_bars = verifier.daily(market, symbol, release.daily_release_id, visible_at=stamp)
    _historical_identity(instrument, days, native_bars, bar)
    return release, instrument, days


def _historical_identity(
    instrument: InstrumentRecord, days: list[TradingSession],
    native_bars: list[DailyBarObservation], bar: DailyBarObservation,
) -> None:
    day = bar.session_date
    same = [item for item in native_bars if item.session_date == day]
    if (instrument.listing_date is None or instrument.listing_date > day
            or instrument.status_date > day or not instrument.tradable
            or instrument.available_to_system_at > bar.session_close_at
            or any(item.available_to_system_at > bar.session_close_at for item in days)
            or instrument.delisting_date is not None and instrument.delisting_date <= day
            or not any(item.session_date == day and item.is_open for item in days)
            or len(same) != 1
            or same[0].model_dump(exclude={"created_at"})
            != bar.model_dump(exclude={"created_at"})):
        raise _ProofUnavailable("HISTORICAL_SECURITY_OR_DAILY_IDENTITY_MISMATCH")


def _limit_bounds(
    proof: _ProofReader, release: TradingClassificationRelease,
    instrument: InstrumentRecord, days: list[TradingSession], bar: DailyBarObservation,
) -> tuple[int, int]:
    if not release.rulebook_artifact_id:
        raise _ProofUnavailable("RULEBOOK_AND_ACTION_PROOF_REQUIRED")
    rules = proof.artifact(release.rulebook_artifact_id, "TradingClassificationRuleBook")
    _board_proof(rules, release, instrument, days, bar)
    risk = "RISK_WARNING" if instrument.is_st else "NORMAL"
    if release.classification.risk_status != risk:
        raise _ProofUnavailable("HISTORICAL_RISK_STATUS_MISMATCH")
    limits = [item for item in rules.get("price_limit_rules", []) if (
        item["market"] == bar.market.value and item["board"] == release.classification.board
        and item["risk_status"] == risk
        and date.fromisoformat(item["effective_from"]) <= bar.session_date
    )]
    if (len(limits) != 1 or limits[0]["rate_bps"] != release.price_limit_rate_bps
            or not limits[0]["source_urls"]):
        raise _ProofUnavailable("EXACT_EFFECTIVE_LIMIT_RULE_REQUIRED")
    if (bar.previous_close is None
            or bar.previous_close*100 != (bar.previous_close*100).to_integral_value()):
        raise _ProofUnavailable("EXACT_PREVIOUS_CLOSE_REQUIRED")
    lower, upper = _price_bounds(
        int(bar.previous_close*100), int(release.price_limit_rate_bps or 0)
    )
    if (not Decimal(lower)/100 <= bar.low <= bar.high <= Decimal(upper)/100 or bar.volume <= 0):
        raise _ProofUnavailable("RAW_BAR_OUTSIDE_VERIFIED_TRADING_TERMS")
    return lower, upper


def _board_proof(
    rules: dict[str, Any], release: TradingClassificationRelease,
    instrument: InstrumentRecord, days: list[TradingSession], bar: DailyBarObservation,
) -> None:
    boards = [item for item in rules.get("board_rules", []) if (
        item["market"] == bar.market.value
        and date.fromisoformat(item["effective_from"]) <= bar.session_date
        and any(bar.symbol.startswith(prefix) for prefix in item["symbol_prefixes"])
    )]
    assert instrument.listing_date is not None
    age_lower_bound = sum(
        item.is_open and instrument.listing_date <= item.session_date <= bar.session_date
        for item in days
    )
    if (len(boards) != 1 or boards[0]["board"] != release.classification.board
            or not boards[0]["source_urls"]
            or age_lower_bound <= boards[0]["no_fixed_price_limit_first_n_sessions"]):
        raise _ProofUnavailable("BOARD_AND_POST_IPO_SESSION_PROOF_REQUIRED")


def _empty_disclosure_proof(
    proof: _ProofReader, service: TradingClassificationService,
    release: TradingClassificationRelease, instrument: InstrumentRecord,
    bar: DailyBarObservation,
) -> None:
    if not release.corporate_action_baseline_artifact_id:
        raise _ProofUnavailable("RULEBOOK_AND_ACTION_PROOF_REQUIRED")
    baseline = TradingClassificationCorporateActionBaseline.model_validate(proof.artifact(
        release.corporate_action_baseline_artifact_id,
        "TradingClassificationCorporateActionBaseline",

    ))
    if (not baseline.official_query_artifact_ids or baseline.as_of > bar.session_close_at
            or baseline.company_id != bar.symbol or baseline.market is not bar.market
            or not baseline.absence_is_officially_certified):
        raise _ProofUnavailable("FROZEN_OFFICIAL_QUERY_RECEIPT_REQUIRED")
    queries = [load_disclosure_batch(proof.state, proof.objects, identity)
               for identity in baseline.official_query_artifact_ids]
    queries.sort(key=lambda item: item.request.page_number)
    service._validate_official_enumeration(queries)
    first = queries[0].request
    exchange = DisclosureExchange.SSE if bar.market is Market.XSHG else DisclosureExchange.SZSE
    assert instrument.listing_date is not None
    if (first.symbol != bar.symbol or first.exchange != exchange
            or first.start_date > instrument.listing_date or first.end_date != bar.session_date
            or first.keyword or first.category is not DisclosureCategory.ALL
            or first.start_date.isoformat() != baseline.window_start
            or first.end_date.isoformat() != baseline.window_end
            or baseline.observed_record_count != 0
            or any(query.total_count or query.announcements for query in queries)
            or {query.raw_snapshot_id for query in queries}
            != set(baseline.official_query_snapshot_ids)):
        raise _ProofUnavailable("EXHAUSTIVE_NO_ACTION_SLICE_PROOF_REQUIRED")
    for query in queries:
        _raw_empty_query(proof, query.raw_snapshot_id, baseline, bar)


def _raw_empty_query(
    proof: _ProofReader, snapshot_id: str,
    baseline: TradingClassificationCorporateActionBaseline, bar: DailyBarObservation,
) -> None:
    source = proof.state.get_snapshot(snapshot_id)
    if source is None:
        raise _ProofUnavailable("RAW_TERMINAL_EMPTY_ENUMERATION_REQUIRED")
    raw = json.loads(proof.objects.get_bytes(source.object_sha256))
    if not isinstance(raw, dict):
        raise _ProofUnavailable("RAW_TERMINAL_EMPTY_ENUMERATION_REQUIRED")
    total = raw.get("totalRecordNum", raw.get("totalAnnouncement"))
    terminal = raw.get("hasMore")
    if (source.available_to_system_at > min(bar.session_close_at, baseline.as_of)
            or raw.get("announcements") != [] or type(total) is not int or total != 0
            or not (terminal is False or type(terminal) is str and terminal == "false")):
        raise _ProofUnavailable("RAW_TERMINAL_EMPTY_ENUMERATION_REQUIRED")
    proof.snapshots.add(source.snapshot_id)
    proof.hashes.add(source.object_sha256)


def _complete_action_window(
    proof: _ProofReader, verifier: MarketReferencePaperVerifier, first: DailyBarObservation,
) -> bool:
    identity = proof.request.corporate_action_release_id
    if identity is None:
        return False
    declared = DatasetReleaseManifest.model_validate(proof.artifact(
        "market-reference:" + identity, "DatasetReleaseManifest"
    ))
    if declared.available_to_system_at > proof.request.as_of:
        raise _ProofUnavailable("ACTION_RELEASE_UNAVAILABLE_AT_AS_OF")
    manifest = verifier._visible_manifest(
        ReferenceDatasetKind.CORPORATE_ACTION, f"{first.market.value}:{first.symbol}",
        visible_at=proof.request.as_of,
    )
    coverage = manifest.coverage
    if (manifest.release_id != identity or coverage.requested_start is None
            or coverage.requested_end is None or coverage.requested_start > proof.request.start
            or coverage.requested_end < proof.request.end or coverage.record_count > 20000):
        raise _ProofUnavailable("COMPLETE_ACTION_RELEASE_WINDOW_REQUIRED")
    rows = verifier.corporate_actions(first.market, identity, visible_at=proof.request.as_of)
    for item in rows:
        if (item.symbol != first.symbol or item.market is not first.market
                or item.available_to_system_at > proof.request.as_of
                or item.status is not CorporateActionStatus.TERMS_VERIFIED or item.ex_date is None):
            raise _ProofUnavailable("COMPLETE_ACTION_RELEASE_TERMS_REQUIRED")
        if proof.request.start <= item.ex_date <= proof.request.end:
            raise _ProofUnavailable("IN_WINDOW_CORPORATE_ACTION_EXECUTION_NOT_SUPPORTED")
        official = proof.state.get_snapshot(str(item.official_document_snapshot_id))
        if (official is None or official.fetch_status is not FetchStatus.SUCCEEDED
                or not proof.objects.verify(official.object_sha256)
                or official.available_to_system_at > proof.request.as_of):
            raise _ProofUnavailable("COMPLETE_ACTION_OFFICIAL_SOURCE_REQUIRED")
        proof.snapshots.add(official.snapshot_id)
        proof.hashes.add(official.object_sha256)
    for snapshot_id in manifest.raw_snapshot_ids:
        source = proof.state.get_snapshot(snapshot_id)
        if source is None or source.available_to_system_at > proof.request.as_of:
            raise _ProofUnavailable("COMPLETE_ACTION_RAW_SOURCE_REQUIRED")
        proof.snapshots.add(snapshot_id)
        proof.hashes.add(source.object_sha256)
    return True


def _market_bar(item: DailyBarObservation, provider: str) -> MarketBar:
    return MarketBar(
        observation_id=item.observation_id, provider_id=provider,
        symbol=item.symbol, market=item.market, frequency=Frequency.D1,
        timestamp=item.session_close_at, timestamp_semantics=TimestampSemantics.BAR_END,
        open=item.open, high=item.high, low=item.low, close=item.close,
        volume=item.volume, volume_unit=item.volume_unit, amount=item.amount,
        amount_unit=item.amount_unit, created_at=item.available_to_system_at,
    )


def assemble_registered_market_release(
    state: StateStore, objects: ObjectStore, reference: MarketReferenceService,
    request: MarketAssemblyRequest, *, calendar_path: Path, register: bool = False,
) -> dict[str, Any]:
    proof = _ProofReader(state, objects, reference, request)
    reasons: set[str] = set()
    def fail(code: str) -> dict[str, Any]:
        reasons.add(code)
        return {
            "schema_version": "strategy-market-assembly-v1", "status": "MISSING_PROOF",
            "reason_codes": sorted(reasons), "release": None, "artifact_id": None,
            "market_validation_claim": False, "ledger_write_allowed": False,
        }
    try:
        stock, benchmark, bars, benchmarks = _daily_inputs(proof, calendar_path)
        verifier = _HistoricalReferenceVerifier(reference)
        service = TradingClassificationService(state, objects)
        loaded = [service.load(identity) for identity in request.classification_artifact_ids]
        fees = tuple(ReplayFeeSchedule.model_validate(proof.artifact(identity, "ReplayFeeSchedule"))
                     for identity in request.fee_artifact_ids)
        fee_dates = [item.effective_from for item in fees]
        if fee_dates != sorted(set(fee_dates)) or any(not item.source_urls for item in fees):
            return fail("DATED_FEE_POLICY_PROVENANCE_REQUIRED")
        complete_actions = _complete_action_window(proof, verifier, bars[0])
        sessions = []
        timer = Budget(30)
        for index, bar in enumerate(bars):
            timer.check()
            try:
                release, instrument, days = _dated_classification(
                    proof, service, loaded, bar, verifier
                )
                lower, upper = _limit_bounds(proof, release, instrument, days, bar)
                if not complete_actions:
                    _empty_disclosure_proof(proof, service, release, instrument, bar)
                if index and bars[index-1].close != bar.previous_close:
                    raise _ProofUnavailable("PREVIOUS_CLOSE_OR_ACTION_CONTINUITY_UNPROVEN")
                if not any(
                    fee.effective_from <= bar.session_date and bar.market in fee.applicable_markets
                           for fee in fees):
                    raise _ProofUnavailable("EFFECTIVE_FEE_SCHEDULE_REQUIRED")
                proof.snapshots.update((
                    bar.source_snapshot_id, benchmarks[bar.session_date].source_snapshot_id
                ))
                sessions.append(Session(
                    bar=_market_bar(bar, stock.provider_id),
                    benchmark=_market_bar(benchmarks[bar.session_date], benchmark.provider_id),
                    suspended=False, limit_down=Decimal(lower)/100, limit_up=Decimal(upper)/100,
                ))
            except _ProofUnavailable as exc:
                reasons.add(str(exc))
        if reasons:
            return fail("CANONICAL_EXECUTION_PROOF_INCOMPLETE")
        frozen = FrozenMarketRelease(
            symbol=bars[0].symbol, benchmark_id=benchmarks[bars[0].session_date].symbol,
            source_snapshot_ids=tuple(sorted(proof.snapshots)), sessions=tuple(sessions),
            corporate_actions=(), corporate_action_coverage="TERMS_VERIFIED_EXHAUSTIVE",
            calendar_coverage="EXCHANGE_SESSIONS_EXHAUSTIVE", fees=fees,
            initial_cash_fen=request.initial_cash_fen, slippage_bps=request.slippage_bps,
            maximum_participation=request.maximum_participation,
        )
        artifact_id = None
        if register:
            ref = objects.put_json(frozen.model_dump(mode="json"))
            artifact_id = "StrategyCanonicalMarketRelease:" + digest(frozen)
            state.register_artifact(
                artifact_id=artifact_id, artifact_type="StrategyCanonicalMarketRelease",
                schema_version=frozen.schema_version, object_hash=ref.sha256,
                input_hashes=sorted(proof.hashes),
            )
        return {
            "schema_version": "strategy-market-assembly-v1", "status": "ASSEMBLED_RESEARCH_ONLY",
            "release": frozen.model_dump(mode="json"), "release_hash": digest(frozen),
            "artifact_id": artifact_id, "reason_codes": [], "market_validation_claim": False,
            "qualification_scope": "ORDINARY_FIXED_LIMIT_WITH_VERIFIED_NO_ACTION_WINDOW",
            "action_coverage_basis": (
                "CANONICAL_COMPLETE_RELEASE" if complete_actions else "EMPTY_QUERY_RECEIPTS"
            ),
            "fee_assumptions_require_broker_confirmation": any(
                item.requires_broker_confirmation for item in fees
            ),
            "ledger_write_allowed": False,
        }
    except _ProofUnavailable as exc:
        return fail(str(exc))
    except (ValueError, OSError, AStockError, KeyError, TypeError):
        return fail("REGISTERED_CANONICAL_PROOF_INVALID")
