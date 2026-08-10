"""Confirmed, immutable operation boundary for the local paper account."""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from pydantic import ValidationError

from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.paper_trading.ledger import LedgerService
from astock.schemas import (
    CorporateActionObservation,
    CorporateActionStatus,
    DailyBarObservation,
    DatasetReleaseManifest,
    InstrumentRecord,
    Market,
    PaperCancelOrderPayload,
    PaperMarkPayload,
    PaperOperationReport,
    PaperOperationRequest,
    PaperOperationStatus,
    PaperOrderValidity,
    PaperPlaceOrderPayload,
    PaperRecoverPayload,
    PaperSettlePayload,
    PaperUserConfirmation,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferencePitStatus,
    ReplayFeeSchedule,
    TradingSession,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CONFIRMATION_DOMAIN = b"ASTOCK:PAPER_OPERATION_CONFIRMATION:v2\x00"
_RecordT = TypeVar(
    "_RecordT", TradingSession, InstrumentRecord, DailyBarObservation, CorporateActionObservation
)


class PaperReferenceVerifier(Protocol):
    def calendar(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[TradingSession]: ...

    def instrument(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> InstrumentRecord: ...

    def daily(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> list[DailyBarObservation]: ...

    def corporate_actions(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[CorporateActionObservation]: ...

    def trading_classification(
        self,
        instrument: InstrumentRecord,
        *,
        visible_at: datetime,
    ) -> PaperInstrumentTradingFacts: ...


@dataclass(frozen=True, slots=True)
class PaperInstrumentTradingFacts:
    """Explicit PIT classification derived only from frozen reference/rule artifacts."""

    board: str
    risk_status: str
    fixed_price_limit_eligible: bool
    suspension_status_verified: bool
    suspended: bool
    evidence_id: str
    special_regime: str = "ORDINARY"
    price_limit_rate_bps: int | None = None
    rule_version: str | None = None
    instrument_release_id: str | None = None
    calendar_release_id: str | None = None
    daily_release_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaperBoardRule:
    market: Market
    board: str
    symbol_prefixes: tuple[str, ...]
    effective_from: date
    no_fixed_price_limit_first_n_sessions: int
    source_urls: tuple[str, ...]

    def matches(self, symbol: str, trade_date: date) -> bool:
        return self.effective_from <= trade_date and any(
            symbol.startswith(prefix) for prefix in self.symbol_prefixes
        )


@dataclass(frozen=True, slots=True)
class PaperPriceLimitRule:
    market: Market
    board: str
    risk_status: str
    effective_from: date
    rate_bps: int
    source_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperTradingRuleBook:
    rule_version: str
    price_limit_rules: tuple[PaperPriceLimitRule, ...]
    board_rules: tuple[PaperBoardRule, ...] = ()

    def board_rule(
        self,
        *,
        market: Market,
        symbol: str,
        trade_date: date,
    ) -> PaperBoardRule:
        matches = [
            rule
            for rule in self.board_rules
            if rule.market is market and rule.matches(symbol, trade_date)
        ]
        if len(matches) != 1:
            raise _needs_info("No exact effective-dated board-code rule is frozen")
        return matches[0]

    def price_limit_bps(
        self,
        *,
        market: Market,
        board: str,
        risk_status: str,
        trade_date: date,
    ) -> int:
        matches = [
            rule
            for rule in self.price_limit_rules
            if rule.market is market
            and rule.board == board
            and rule.risk_status == risk_status
            and rule.effective_from <= trade_date
        ]
        if len(matches) != 1 or trade_date.isoformat() < "2026-07-06":
            raise _needs_info("No exact effective-dated price-limit rule is frozen")
        return matches[0].rate_bps


def load_paper_trading_rules(path: Path) -> PaperTradingRuleBook:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("price_limit_rules"), list):
        raise ValueError("Paper trading rules must contain price_limit_rules")
    rules: list[PaperPriceLimitRule] = []
    for value in raw["price_limit_rules"]:
        if not isinstance(value, dict):
            raise ValueError("Invalid paper price-limit rule")
        rules.append(
            PaperPriceLimitRule(
                market=Market(str(value["market"])),
                board=str(value["board"]),
                risk_status=str(value["risk_status"]),
                effective_from=datetime.fromisoformat(str(value["effective_from"])).date(),
                rate_bps=int(value["rate_bps"]),
                source_urls=tuple(str(item) for item in value.get("source_urls", [])),
            )
        )
    board_rules: list[PaperBoardRule] = []
    for value in raw.get("board_rules", []):
        if not isinstance(value, dict):
            raise ValueError("Invalid paper board rule")
        prefixes = tuple(str(item) for item in value.get("symbol_prefixes", []))
        if not prefixes or len(prefixes) != len(set(prefixes)):
            raise ValueError("Paper board rule prefixes must be non-empty and unique")
        first_n = int(value.get("no_fixed_price_limit_first_n_sessions", 0))
        if first_n < 0:
            raise ValueError("Paper board special-session count cannot be negative")
        board_rules.append(
            PaperBoardRule(
                market=Market(str(value["market"])),
                board=str(value["board"]),
                symbol_prefixes=prefixes,
                effective_from=datetime.fromisoformat(str(value["effective_from"])).date(),
                no_fixed_price_limit_first_n_sessions=first_n,
                source_urls=tuple(str(item) for item in value.get("source_urls", [])),
            )
        )
    return PaperTradingRuleBook(
        str(raw["rule_version"]),
        tuple(rules),
        tuple(board_rules),
    )


def load_paper_authorization_keys(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Paper trading rules must be a YAML object")
    values = raw.get("authorization_keys", [])
    if not isinstance(values, list):
        raise ValueError("authorization_keys must be a list")
    result: dict[str, str] = {}
    for value in values:
        if (
            not isinstance(value, dict)
            or not value.get("key_id")
            or not value.get("public_key_pem")
        ):
            raise ValueError("Invalid paper authorization key")
        key_id = str(value["key_id"])
        if key_id in result:
            raise ValueError("Duplicate paper authorization key id")
        result[key_id] = str(value["public_key_pem"])
    return result


class MarketReferencePaperVerifier:
    """Read only PIT reference releases and derive versioned trading facts."""

    def __init__(
        self,
        reference: MarketReferenceService,
        trading_rules: PaperTradingRuleBook | None = None,
    ) -> None:
        self.reference = reference
        self.trading_rules = trading_rules

    def calendar(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[TradingSession]:
        records = self._records(
            ReferenceDatasetKind.TRADING_CALENDAR,
            market.value,
            release_id,
            visible_at,
            TradingSession,
        )
        if any(item.exchange is not market for item in records):
            raise _needs_info("Calendar release contains a mismatched exchange identity")
        return records

    def instrument(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> InstrumentRecord:
        records: list[InstrumentRecord] = []
        for scope in (market.value, "ALL"):
            try:
                records = self._records(
                    ReferenceDatasetKind.INSTRUMENT_MASTER,
                    scope,
                    release_id,
                    visible_at,
                    InstrumentRecord,
                )
                break
            except PolicyError:
                continue
        matches = [item for item in records if item.market is market and item.symbol == symbol]
        if len(matches) != 1:
            raise _needs_info("Instrument release does not prove one tradable symbol")
        return matches[0]

    def resolve_instrument(
        self,
        symbol: str,
        *,
        visible_at: datetime,
    ) -> tuple[InstrumentRecord, str]:
        matches: list[tuple[InstrumentRecord, str]] = []
        for scope in ("ALL", Market.XSHG.value, Market.XSHE.value, Market.BJSE.value):
            try:
                manifest = self._visible_manifest(
                    ReferenceDatasetKind.INSTRUMENT_MASTER,
                    scope,
                    visible_at=visible_at,
                    require_certified=False,
                )
                records = self._records(
                    ReferenceDatasetKind.INSTRUMENT_MASTER,
                    scope,
                    manifest.release_id,
                    visible_at,
                    InstrumentRecord,
                    require_certified=False,
                )
            except PolicyError:
                continue
            matches.extend(
                (item, manifest.release_id)
                for item in records
                if item.symbol == symbol and item.tradable
            )
        unique = {
            (item.instrument_id, release_id): (item, release_id) for item, release_id in matches
        }
        identities = {item.instrument_id for item, _ in unique.values()}
        if len(identities) != 1:
            raise _needs_info("Visible instrument master does not prove one tradable identity")
        chosen = sorted(unique.values(), key=lambda item: item[1])[-1]
        return chosen

    def daily(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> list[DailyBarObservation]:
        records = self._records(
            ReferenceDatasetKind.DAILY_UNADJUSTED,
            f"{market.value}:{symbol}",
            release_id,
            visible_at,
            DailyBarObservation,
        )
        instrument_id = f"{market.value}:{symbol}"
        if any(
            item.market is not market
            or item.symbol != symbol
            or item.instrument_id != instrument_id
            for item in records
        ):
            raise _needs_info("Daily release contains a mismatched instrument identity")
        return records

    def corporate_actions(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[CorporateActionObservation]:
        rows = self.reference.state.list_market_reference_releases(
            ReferenceDatasetKind.CORPORATE_ACTION.value
        )
        match = next(
            (
                row
                for row in rows
                if row["release_id"] == release_id
                and str(row["scope_key"]).startswith(f"{market.value}:")
            ),
            None,
        )
        if match is None:
            raise _needs_info("Corporate-action release is not registered for the market")
        return self._records(
            ReferenceDatasetKind.CORPORATE_ACTION,
            str(match["scope_key"]),
            release_id,
            visible_at,
            CorporateActionObservation,
        )

    def trading_classification(
        self,
        instrument: InstrumentRecord,
        *,
        visible_at: datetime,
    ) -> PaperInstrumentTradingFacts:
        if self.trading_rules is None:
            raise _needs_info("Versioned paper trading rules are not configured")
        local = visible_at.astimezone(_SHANGHAI)
        trade_date = local.date()
        if instrument.available_to_system_at > visible_at or instrument.status_date > trade_date:
            raise _needs_info("Instrument classification contains future-visible status")
        if instrument.listing_date is None:
            raise _needs_info(
                "Instrument listing date is required for special-regime classification"
            )
        if instrument.listing_date > trade_date:
            raise _needs_info("Instrument is not yet listed at the requested as_of")
        if instrument.delisting_date is not None and instrument.delisting_date <= trade_date:
            raise _needs_info(
                "Delisted/delisting instrument requires a dedicated special-regime source"
            )

        instrument_manifest = self._instrument_manifest(instrument, visible_at=visible_at)
        board_rule = self.trading_rules.board_rule(
            market=instrument.market,
            symbol=instrument.symbol,
            trade_date=trade_date,
        )
        calendar_manifest = self._visible_manifest(
            ReferenceDatasetKind.TRADING_CALENDAR,
            instrument.market.value,
            visible_at=visible_at,
            require_certified=False,
        )
        sessions = self._records(
            ReferenceDatasetKind.TRADING_CALENDAR,
            instrument.market.value,
            calendar_manifest.release_id,
            visible_at,
            TradingSession,
            require_certified=False,
        )
        open_dates = sorted(item.session_date for item in sessions if item.is_open)
        completed_open_dates = [item for item in open_dates if item <= trade_date]
        if not completed_open_dates:
            raise _needs_info("No point-in-time visible open session exists for classification")
        target_session = completed_open_dates[-1]
        if target_session == trade_date and local.time() < time(15, 0):
            raise _needs_info(
                "Current-session suspension cannot be verified before the session close"
            )

        daily_manifest = self._visible_manifest(
            ReferenceDatasetKind.DAILY_UNADJUSTED,
            f"{instrument.market.value}:{instrument.symbol}",
            visible_at=visible_at,
            require_certified=False,
        )
        daily = self._records(
            ReferenceDatasetKind.DAILY_UNADJUSTED,
            f"{instrument.market.value}:{instrument.symbol}",
            daily_manifest.release_id,
            visible_at,
            DailyBarObservation,
            require_certified=False,
        )
        target_rows = [item for item in daily if item.session_date == target_session]
        if len(target_rows) != 1:
            raise _needs_info(
                "Latest open session lacks one exact daily record; suspension is not inferable"
            )
        target_bar = target_rows[0]
        suspended = target_bar.volume == 0

        special_regime = "ORDINARY"
        fixed_limit = True
        rate_bps: int | None = None
        listing_age_days = (target_session - instrument.listing_date).days
        if listing_age_days <= 45:
            if instrument.listing_date not in open_dates:
                raise _needs_info(
                    "Calendar coverage does not include the listing date needed for IPO regime"
                )
            sessions_since_listing = [
                item for item in open_dates if instrument.listing_date <= item <= target_session
            ]
            if len(sessions_since_listing) <= board_rule.no_fixed_price_limit_first_n_sessions:
                special_regime = "IPO_INITIAL_NO_FIXED_PRICE_LIMIT"
                fixed_limit = False
        if suspended:
            special_regime = "SUSPENDED"
            fixed_limit = False
        if fixed_limit:
            rate_bps = self.trading_rules.price_limit_bps(
                market=instrument.market,
                board=board_rule.board,
                risk_status="RISK_WARNING" if instrument.is_st else "NORMAL",
                trade_date=target_session,
            )

        # A mature ordinary classification must prove recent continuity so a first-day
        # relisting/special-session condition cannot be silently inferred away.
        recent_open_dates = [item for item in open_dates if item <= target_session][-5:]
        observed_dates = {item.session_date for item in daily}
        if special_regime == "ORDINARY" and (
            len(recent_open_dates) < 5
            or any(item not in observed_dates for item in recent_open_dates)
        ):
            raise _needs_info(
                "Recent daily continuity is insufficient to exclude a special trading regime"
            )

        evidence_id = content_hash(
            {
                "rule_version": self.trading_rules.rule_version,
                "instrument_snapshot_id": instrument.source_snapshot_id,
                "instrument_release_id": instrument_manifest.release_id,
                "calendar_release_id": calendar_manifest.release_id,
                "daily_release_id": daily_manifest.release_id,
                "target_session": target_session.isoformat(),
                "board": board_rule.board,
                "risk_status": "RISK_WARNING" if instrument.is_st else "NORMAL",
                "special_regime": special_regime,
                "suspended": suspended,
            }
        )
        return PaperInstrumentTradingFacts(
            board=board_rule.board,
            risk_status="RISK_WARNING" if instrument.is_st else "NORMAL",
            fixed_price_limit_eligible=fixed_limit,
            suspension_status_verified=True,
            suspended=suspended,
            evidence_id=evidence_id,
            special_regime=special_regime,
            price_limit_rate_bps=rate_bps,
            rule_version=self.trading_rules.rule_version,
            instrument_release_id=instrument_manifest.release_id,
            calendar_release_id=calendar_manifest.release_id,
            daily_release_id=daily_manifest.release_id,
        )

    def _instrument_manifest(
        self,
        instrument: InstrumentRecord,
        *,
        visible_at: datetime,
    ) -> DatasetReleaseManifest:
        for scope in (instrument.market.value, "ALL"):
            try:
                manifest = self._visible_manifest(
                    ReferenceDatasetKind.INSTRUMENT_MASTER,
                    scope,
                    visible_at=visible_at,
                    require_certified=False,
                )
                records = self._records(
                    ReferenceDatasetKind.INSTRUMENT_MASTER,
                    scope,
                    manifest.release_id,
                    visible_at,
                    InstrumentRecord,
                    require_certified=False,
                )
            except PolicyError:
                continue
            if any(item == instrument for item in records):
                return manifest
        raise _needs_info("Instrument is not bound to one visible reference release")

    def _visible_manifest(
        self,
        kind: ReferenceDatasetKind,
        scope: str,
        *,
        visible_at: datetime,
        require_certified: bool,
    ) -> DatasetReleaseManifest:
        status = self.reference.status(kind, scope, as_of=visible_at)
        if status.get("status") != "AVAILABLE":
            raise _needs_info(f"Verified {kind.value} release is unavailable")
        try:
            manifest = DatasetReleaseManifest.model_validate(status["release"])
        except (KeyError, ValidationError) as exc:
            raise _needs_info("Reference release manifest is invalid") from exc
        if manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE:
            raise _needs_info("Reference release coverage is not COMPLETE")
        if require_certified and manifest.pit_status is not ReferencePitStatus.CERTIFIED:
            raise _needs_info("Operational reference requires COMPLETE/CERTIFIED coverage")
        if not require_certified and manifest.pit_status is ReferencePitStatus.UNVERIFIED:
            raise _needs_info("Research classification requires PIT-visible reference coverage")
        return manifest

    def _records(
        self,
        kind: ReferenceDatasetKind,
        scope: str,
        release_id: str,
        visible_at: datetime,
        model: type[_RecordT],
        *,
        require_certified: bool = True,
    ) -> list[_RecordT]:
        manifest = self._visible_manifest(
            kind,
            scope,
            visible_at=visible_at,
            require_certified=require_certified,
        )
        if manifest.release_id != release_id:
            raise _needs_info("Requested release is not the point-in-time visible head")
        records: list[_RecordT] = []
        for descriptor in manifest.canonical_files:
            path = (self.reference.parquet.root / descriptor.path).resolve()
            if not path.is_relative_to(self.reference.parquet.root):
                raise _policy("Reference descriptor escapes the Parquet root")
            try:
                if sha256_bytes(path.read_bytes()) != descriptor.sha256:
                    raise _needs_info("Verified reference Parquet content hash changed")
                values = pq.read_table(path, columns=["record_json"]).column(0).to_pylist()
                records.extend(model.model_validate(json.loads(value)) for value in values)
            except (OSError, TypeError, ValueError, ValidationError) as exc:
                raise _needs_info("Verified reference records cannot be decoded") from exc
        if len(records) != manifest.coverage.record_count:
            raise _needs_info("Verified reference row count changed")
        if any(item.available_to_system_at > visible_at for item in records):
            raise _needs_info("Reference release contains a future-visible record")
        return records


class PaperOperationService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        ledger: LedgerService,
        references: PaperReferenceVerifier,
        fee_schedule: ReplayFeeSchedule,
        *,
        clock: Callable[[], datetime] | None = None,
        trusted_confirmation_keys: Mapping[str, str | bytes] | None = None,
        trading_rules: PaperTradingRuleBook | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.ledger = ledger
        self.references = references
        self.fee_schedule = fee_schedule
        self.clock = clock or (lambda: datetime.now(UTC))
        self.trusted_confirmation_keys = dict(trusted_confirmation_keys or {})
        self.trading_rules = trading_rules or PaperTradingRuleBook("UNCONFIGURED", ())

    def execute(
        self,
        request: PaperOperationRequest,
        confirmation: PaperUserConfirmation | None,
        *,
        expected_operation_type: str | None = None,
    ) -> PaperOperationReport:
        request_hash = paper_request_hash(request)
        if request.operation_id != request_hash:
            raise _policy("Operation id does not match the immutable request hash")
        if expected_operation_type and request.payload.operation_type != expected_operation_type:
            raise _policy("Operation payload does not match the CLI command")

        # A terminal result is immutable and recoverable even after the original
        # authorization window has elapsed.  Never re-authorize completed work.
        previous = self._completed_report(request.operation_id)
        if previous is not None:
            if previous.request_hash != request_hash:
                raise _policy("Completed operation request hash collision")
            return previous
        committed = self._committed_result(request.operation_id)
        if committed is not None:
            return self._finish_committed(request, request_hash, committed)

        authorization_key = self._validate_confirmation(
            request,
            confirmation,
            request_hash,
        )
        assert confirmation is not None
        request_bytes = paper_request_bytes(request)
        confirmation_bytes = paper_confirmation_bytes(confirmation)
        request_ref = self.objects.put_bytes(request_bytes)
        confirmation_ref = self.objects.put_bytes(confirmation_bytes)
        authorization_key_ref = self.objects.put_bytes(authorization_key)
        self._register(
            request,
            confirmation,
            request_hash,
            request_ref.sha256,
            confirmation_ref.sha256,
            authorization_key_ref.sha256,
        )
        previous = self._completed_report(request.operation_id)
        if previous is not None:
            return previous

        try:
            with self.ledger.atomic():
                raced = self._completed_report(request.operation_id)
                if raced is not None:
                    report = raced
                else:
                    result = self._dispatch(request)
                    self._commit_result(request.operation_id, result)
                    if (
                        isinstance(request.payload, PaperRecoverPayload)
                        and result.get("status") == "RECOVERED"
                    ):
                        with self.ledger.transaction() as connection:
                            self._transition(
                                connection,
                                request.operation_id,
                                PaperOperationStatus.RECOVERED,
                                self.clock().astimezone(UTC).isoformat(),
                                reason_code="DAY_ORDER_EXPIRY_APPLIED",
                            )
                    completed_at = self.clock().astimezone(UTC)
                    report = PaperOperationReport(
                        created_at=completed_at,
                        operation_id=request.operation_id,
                        operation_type=request.payload.operation_type,
                        account_id=request.account_id,
                        status=PaperOperationStatus.COMPLETE,
                        request_hash=request_hash,
                        confirmation_id=confirmation.confirmation_id,
                        result=result,
                        completed_at=completed_at,
                    )
                    self._complete(report)
        except PolicyError as exc:
            status = (
                PaperOperationStatus.NEEDS_INFO
                if exc.failure_class is FailureClass.DATA_QUALITY
                else PaperOperationStatus.REJECTED
            )
            self._fail(request.operation_id, status, reason_code=exc.failure_class.value)
            raise
        except Exception:
            self._fail(
                request.operation_id,
                PaperOperationStatus.INTERRUPTED,
                reason_code="UNEXPECTED_EXCEPTION",
            )
            raise
        self._after_commit(report)
        return report

    def _validate_confirmation(
        self,
        request: PaperOperationRequest,
        confirmation: PaperUserConfirmation | None,
        request_hash: str,
    ) -> bytes:
        if confirmation is None:
            raise _policy("A signed independent user confirmation is required")
        if confirmation.schema_version != "paper-user-confirmation-v2":
            raise _policy("Unsupported paper confirmation schema version")
        if confirmation.confirmation_id != paper_confirmation_hash(confirmation):
            raise _policy("Confirmation id does not match its immutable payload")
        if (
            confirmation.operation_id != request.operation_id
            or confirmation.request_hash != request_hash
            or confirmation.account_id != request.account_id
            or confirmation.operation_type != request.payload.operation_type
        ):
            raise _policy("Confirmation does not bind the exact request hash")
        key_pem = self.trusted_confirmation_keys.get(confirmation.key_id)
        if key_pem is None:
            raise _policy("Confirmation key is not configured or trusted")
        key_bytes = key_pem.encode("utf-8") if isinstance(key_pem, str) else key_pem
        if not paper_confirmation_signature_valid(confirmation, key_bytes):
            raise _policy("Confirmation signature verification failed")
        if confirmation.confirmed_at < request.requested_at:
            raise _policy("Confirmation predates the request")
        now = self.clock().astimezone(UTC)
        if confirmation.confirmed_at.astimezone(UTC) > now:
            raise _policy("Confirmation timestamp is in the future")
        if now > request.expires_at.astimezone(UTC) or now > confirmation.expires_at.astimezone(
            UTC
        ):
            raise _policy("Operation confirmation has expired")
        return key_bytes

    def _register(
        self,
        request: PaperOperationRequest,
        confirmation: PaperUserConfirmation,
        request_hash: str,
        request_object_hash: str,
        confirmation_object_hash: str,
        authorization_key_object_hash: str,
    ) -> None:
        now = self.clock().astimezone(UTC).isoformat()
        request_json = paper_request_bytes(request).decode()
        confirmation_json = paper_confirmation_bytes(confirmation).decode()
        confirmation_hash = paper_confirmation_hash(confirmation)
        with self.ledger.transaction() as connection:
            account = connection.execute(
                "SELECT 1 FROM paper_account WHERE account_id=?", (request.account_id,)
            ).fetchone()
            if account is None:
                raise ValueError(f"Unknown paper account: {request.account_id}")
            collision = connection.execute(
                "SELECT operation_id,request_hash FROM paper_operation_request "
                "WHERE account_id=? AND operation_type=? AND idempotency_key=?",
                (request.account_id, request.payload.operation_type, request.idempotency_key),
            ).fetchone()
            if collision is not None and (
                collision["operation_id"] != request.operation_id
                or collision["request_hash"] != request_hash
            ):
                raise _policy("Paper operation idempotency-key collision")
            existing = connection.execute(
                "SELECT request_hash,request_object_hash,payload_json "
                "FROM paper_operation_request WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            expected_request = (request_hash, request_object_hash, request_json)
            if existing is not None and tuple(existing) != expected_request:
                raise _policy("Paper operation identity collision")
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_operation_request(operation_id,account_id,operation_type,"
                    "idempotency_key,request_hash,request_object_hash,requested_at,expires_at,"
                    "payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.operation_id,
                        request.account_id,
                        request.payload.operation_type,
                        request.idempotency_key,
                        request_hash,
                        request_object_hash,
                        request.requested_at.isoformat(),
                        request.expires_at.isoformat(),
                        request_json,
                        now,
                    ),
                )
                self._transition(
                    connection, request.operation_id, PaperOperationStatus.PLANNED, now
                )
            stored_confirmation = connection.execute(
                "SELECT operation_id,request_hash,confirmation_hash,confirmation_object_hash,"
                "payload_json FROM paper_operation_confirmation WHERE confirmation_id=?",
                (confirmation.confirmation_id,),
            ).fetchone()
            expected_confirmation = (
                request.operation_id,
                request_hash,
                confirmation_hash,
                confirmation_object_hash,
                confirmation_json,
            )
            if (
                stored_confirmation is not None
                and tuple(stored_confirmation) != expected_confirmation
            ):
                raise _policy("Paper confirmation identity collision")
            if stored_confirmation is None:
                connection.execute(
                    "INSERT INTO paper_operation_confirmation(confirmation_id,operation_id,"
                    "request_hash,confirmed_at,expires_at,confirmation_hash,"
                    "confirmation_object_hash,key_id,nonce,signature_algorithm,payload_json,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        confirmation.confirmation_id,
                        request.operation_id,
                        request_hash,
                        confirmation.confirmed_at.isoformat(),
                        confirmation.expires_at.isoformat(),
                        confirmation_hash,
                        confirmation_object_hash,
                        confirmation.key_id,
                        confirmation.nonce,
                        confirmation.signature_algorithm,
                        confirmation_json,
                        now,
                    ),
                )
            key_binding = connection.execute(
                "SELECT key_id,public_key_object_hash FROM "
                "paper_confirmation_key_binding WHERE confirmation_id=?",
                (confirmation.confirmation_id,),
            ).fetchone()
            expected_key_binding = (
                confirmation.key_id,
                authorization_key_object_hash,
            )
            if key_binding is not None and tuple(key_binding) != expected_key_binding:
                raise _policy("Paper confirmation authorization-key collision")
            if key_binding is None:
                connection.execute(
                    "INSERT INTO paper_confirmation_key_binding("
                    "confirmation_id,key_id,public_key_object_hash,created_at"
                    ") VALUES(?,?,?,?)",
                    (
                        confirmation.confirmation_id,
                        confirmation.key_id,
                        authorization_key_object_hash,
                        now,
                    ),
                )
            nonce = connection.execute(
                "SELECT operation_id,confirmation_id FROM paper_confirmation_nonce "
                "WHERE key_id=? AND nonce=?",
                (confirmation.key_id, confirmation.nonce),
            ).fetchone()
            if nonce is not None and tuple(nonce) != (
                request.operation_id,
                confirmation.confirmation_id,
            ):
                raise _policy("Confirmation nonce has already been consumed")
            if nonce is None:
                connection.execute(
                    "INSERT INTO paper_confirmation_nonce(key_id,nonce,operation_id,"
                    "confirmation_id,consumed_at) VALUES(?,?,?,?,?)",
                    (
                        confirmation.key_id,
                        confirmation.nonce,
                        request.operation_id,
                        confirmation.confirmation_id,
                        now,
                    ),
                )
            execution = connection.execute(
                "SELECT status FROM paper_operation_execution WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if execution is None:
                connection.execute(
                    "INSERT INTO paper_operation_execution(operation_id,status,attempt_count) "
                    "VALUES(?,?,1)",
                    (request.operation_id, PaperOperationStatus.VALIDATED.value),
                )
                self._transition(
                    connection, request.operation_id, PaperOperationStatus.VALIDATED, now
                )
            elif execution["status"] not in {
                PaperOperationStatus.COMMITTED.value,
                PaperOperationStatus.COMPLETE.value,
            }:
                connection.execute(
                    "UPDATE paper_operation_execution SET attempt_count=attempt_count+1,"
                    "status=? WHERE operation_id=?",
                    (PaperOperationStatus.VALIDATED.value, request.operation_id),
                )
                self._transition(
                    connection, request.operation_id, PaperOperationStatus.VALIDATED, now
                )

    def _dispatch(self, request: PaperOperationRequest) -> dict[str, object]:
        payload = request.payload
        if isinstance(payload, PaperPlaceOrderPayload):
            return self._place(request, payload)
        if isinstance(payload, PaperCancelOrderPayload):
            return self._cancel(request, payload)
        if isinstance(payload, PaperSettlePayload):
            return self._settle(request, payload)
        if isinstance(payload, PaperMarkPayload):
            return self._mark(request, payload)
        if isinstance(payload, PaperRecoverPayload):
            return self._recover(request, payload)
        raise AssertionError("unreachable operation payload")

    def _place(
        self, request: PaperOperationRequest, payload: PaperPlaceOrderPayload
    ) -> dict[str, object]:
        if payload.market is Market.INDEX:
            raise _policy("Index orders are not supported")
        if payload.qty % 100:
            raise _policy("A-share order quantity must use 100-share lots")
        if payload.validity is PaperOrderValidity.GTC:
            raise _needs_info("GTC is disabled until each session can re-freeze all PIT rules")
        if payload.fee_rule_version != self.fee_schedule.rule_version:
            raise _policy("Order fee rule does not match the configured release")
        local = request.requested_at.astimezone(_SHANGHAI)
        if payload.market not in self.fee_schedule.applicable_markets:
            raise _policy("Fee schedule does not cover the order market")
        if local.date() < self.fee_schedule.effective_from:
            raise _policy("Fee schedule is not effective for the order session")
        sessions = self.references.calendar(
            payload.market, payload.calendar_release_id, visible_at=request.requested_at
        )
        if any(
            item.exchange is not payload.market
            or item.available_to_system_at > request.requested_at
            for item in sessions
        ):
            raise _needs_info("Calendar release identity or availability is mismatched")
        session = next((item for item in sessions if item.session_date == local.date()), None)
        if session is None or not session.is_open:
            raise _policy("Order date is not a verified open trading session")
        if not _is_continuous_session_time(local.time()):
            raise _policy("Order was requested outside verified continuous sessions")
        instrument = self.references.instrument(
            payload.market,
            payload.symbol,
            payload.instrument_release_id,
            visible_at=request.requested_at,
        )
        if (
            instrument.market is not payload.market
            or instrument.symbol != payload.symbol
            or instrument.instrument_id != f"{payload.market.value}:{payload.symbol}"
            or instrument.available_to_system_at > request.requested_at
            or not instrument.tradable
            or instrument.status_date > local.date()
        ):
            raise _policy("Instrument is not proven tradable for the order session")
        classification = self.references.trading_classification(
            instrument, visible_at=request.requested_at
        )
        if (
            classification.board not in {"MAIN", "STAR", "CHINEXT", "BSE"}
            or classification.risk_status not in {"NORMAL", "RISK_WARNING"}
            or not classification.fixed_price_limit_eligible
            or not classification.suspension_status_verified
            or classification.suspended
        ):
            raise _needs_info("Instrument trading classification is unknown or excluded")
        if payload.market is Market.BJSE:
            raise _needs_info("BSE order-price rounding semantics are not yet frozen")
        daily = self.references.daily(
            payload.market,
            payload.symbol,
            payload.daily_release_id,
            visible_at=request.requested_at,
        )
        instrument_id = f"{payload.market.value}:{payload.symbol}"
        if any(
            item.market is not payload.market
            or item.symbol != payload.symbol
            or item.instrument_id != instrument_id
            or item.available_to_system_at > request.requested_at
            for item in daily
        ):
            raise _needs_info("Daily release identity or availability is mismatched")
        previous_open_date = max(
            (
                item.session_date
                for item in sessions
                if item.is_open and item.session_date < local.date()
            ),
            default=None,
        )
        prior = max(
            (item for item in daily if item.session_date < local.date()),
            key=lambda item: item.session_date,
            default=None,
        )
        if prior is None or previous_open_date is None or prior.session_date != previous_open_date:
            raise _needs_info(
                "Previous unadjusted close for the latest open session is unavailable"
            )
        previous_close_fen = _yuan_to_fen(prior.close)
        is_st = instrument.is_st
        limit_bps = self.trading_rules.price_limit_bps(
            market=payload.market,
            board=classification.board,
            risk_status=classification.risk_status,
            trade_date=local.date(),
        )
        lower, upper = _price_bounds(previous_close_fen, limit_bps)
        if not lower <= payload.limit_price_fen <= upper:
            raise _policy("Limit price breaches the verified board/ST price band")
        gross = payload.qty * payload.limit_price_fen
        commission = _commission(gross, self.fee_schedule)
        transfer = _round_fen(Decimal(gross) * self.fee_schedule.transfer_fee_rate)
        self._register_fee_schedule()
        order = self.ledger.place_order(
            account_id=request.account_id,
            client_request_id=request.operation_id,
            symbol=payload.symbol,
            side=payload.side,
            qty=payload.qty,
            limit_price_fen=payload.limit_price_fen,
            fee_reserve_fen=commission + transfer,
            effective_rule_version=self.fee_schedule.rule_version,
            submitted_at=request.requested_at,
        )
        expires_at = (
            datetime.combine(local.date(), time(15, 0), _SHANGHAI)
            if payload.validity is PaperOrderValidity.DAY
            else None
        )
        with self.ledger.transaction() as connection:
            authorization = connection.execute(
                "SELECT confirmation_id,key_id,confirmation_hash FROM "
                "paper_operation_confirmation WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if authorization is None:
                raise _policy("Verified authorization receipt disappeared")
            row = connection.execute(
                "SELECT * FROM paper_order_rule_binding WHERE order_id=?", (order.order_id,)
            ).fetchone()
            values = (
                request.operation_id,
                payload.market.value,
                payload.symbol,
                instrument.instrument_id,
                classification.board,
                classification.risk_status,
                self.trading_rules.rule_version,
                payload.validity.value,
                expires_at.isoformat() if expires_at else None,
                payload.calendar_release_id,
                payload.instrument_release_id,
                payload.daily_release_id,
                payload.fee_rule_version,
                paper_fee_schedule_hash(self.fee_schedule),
                authorization["confirmation_id"],
                authorization["key_id"],
                authorization["confirmation_hash"],
                previous_close_fen,
                limit_bps,
                int(is_st),
            )
            if row is None:
                connection.execute(
                    "INSERT INTO paper_order_rule_binding(order_id,operation_id,market,symbol,"
                    "instrument_id,board,risk_status,trading_rule_version,validity,"
                    "expires_at,calendar_release_id,instrument_release_id,daily_release_id,"
                    "fee_rule_version,fee_schedule_hash,confirmation_id,authorization_key_id,"
                    "confirmation_hash,previous_close_fen,price_limit_bps,is_st) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order.order_id, *values),
                )
                connection.execute(
                    "INSERT INTO paper_order_transition(order_id,transition_seq,from_status,"
                    "to_status,source_operation_id,occurred_at) VALUES(?,1,NULL,?,?,?)",
                    (
                        order.order_id,
                        order.status.value,
                        request.operation_id,
                        request.requested_at.isoformat(),
                    ),
                )
            elif tuple(row)[1:] != values:
                raise _policy("Order policy snapshot identity collision")
        return {"order": order.model_dump(mode="json", exclude={"created_at"})}

    def _cancel(
        self, request: PaperOperationRequest, payload: PaperCancelOrderPayload
    ) -> dict[str, object]:
        before = self.ledger.get_order(payload.order_id)
        if before.account_id != request.account_id:
            raise _policy("Order belongs to another paper account")
        order = self.ledger.cancel_order(payload.order_id)
        self.ledger.record_order_transition(
            order.order_id,
            before.status,
            order.status,
            occurred_at=self.clock(),
            source_operation_id=request.operation_id,
        )
        return {"order": order.model_dump(mode="json", exclude={"created_at"})}

    def _settle(
        self, request: PaperOperationRequest, payload: PaperSettlePayload
    ) -> dict[str, object]:
        if payload.as_of > request.requested_at:
            raise _policy("Settlement as_of cannot be in the future")
        sessions = self.references.calendar(
            payload.market, payload.calendar_release_id, visible_at=request.requested_at
        )
        if any(
            item.exchange is not payload.market
            or item.available_to_system_at > request.requested_at
            for item in sessions
        ):
            raise _needs_info("Settlement calendar identity or availability is mismatched")
        open_dates = sorted(item.session_date for item in sessions if item.is_open)
        policy = {
            "calendar_release_id": payload.calendar_release_id,
            "open_dates": [item.isoformat() for item in open_dates],
            "as_of": payload.as_of.isoformat(),
        }
        policy_ref = self.objects.put_json(policy)
        verified_actions: list[tuple[CorporateActionObservation, str]] = []
        registered_actions: list[str] = []
        applied_actions: list[str] = []
        for release_id in payload.corporate_action_release_ids:
            actions = self.references.corporate_actions(
                payload.market, release_id, visible_at=request.requested_at
            )
            for action in actions:
                if action.market is not payload.market or action.instrument_id != (
                    f"{payload.market.value}:{action.symbol}"
                ):
                    raise _policy("Corporate-action instrument identity mismatch")
                if (
                    action.status is not CorporateActionStatus.TERMS_VERIFIED
                    or not action.ledger_eligible
                ):
                    raise _needs_info("Corporate action is not TERMS_VERIFIED and ledger eligible")
                verified_actions.append((action, release_id))
        verified_actions.sort(key=_corporate_action_sort_key)
        with self.ledger.atomic():
            settled = self.ledger.settle_buys_with_calendar(
                request.account_id,
                as_of=payload.as_of,
                open_session_dates=open_dates,
                calendar_release_id=payload.calendar_release_id,
                market=payload.market,
            )
            for action, release_id in verified_actions:
                applied = self.ledger.apply_verified_corporate_action(
                    request.account_id,
                    action,
                    release_id,
                    as_of=payload.as_of,
                )
                registered_actions.append(action.observation_id)
                if applied:
                    applied_actions.append(action.observation_id)
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT calendar_release_id,open_sessions_hash,policy_object_hash,as_of "
                "FROM paper_settlement_policy WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            expected = (
                payload.calendar_release_id,
                content_hash(policy["open_dates"]),
                policy_ref.sha256,
                payload.as_of.isoformat(),
            )
            if existing is not None and tuple(existing) != expected:
                raise _policy("Settlement policy identity collision")
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_settlement_policy(operation_id,calendar_release_id,"
                    "open_sessions_hash,policy_object_hash,as_of) VALUES(?,?,?,?,?)",
                    (request.operation_id, *expected),
                )
        return {
            "settled_qty": settled,
            "registered_action_ids": registered_actions,
            "applied_action_ids": applied_actions,
        }

    def _mark(self, request: PaperOperationRequest, payload: PaperMarkPayload) -> dict[str, object]:
        if payload.as_of > request.requested_at:
            raise _policy("Mark as_of cannot be in the future")
        held_symbols = {
            str(item["symbol"])
            for item in self.ledger.status(request.account_id)["positions"]
            if int(item["qty_total"]) > 0
        }
        with closing(self.state.connect()) as connection:
            identities = connection.execute(
                "SELECT symbol,market,instrument_id FROM paper_position_identity "
                "WHERE account_id=?",
                (request.account_id,),
            ).fetchall()
        identity_by_symbol = {str(row["symbol"]): row for row in identities}
        invalid_identity = sorted(
            symbol
            for symbol in held_symbols
            if symbol not in identity_by_symbol
            or identity_by_symbol[symbol]["instrument_id"]
            != f"{identity_by_symbol[symbol]['market']}:{symbol}"
        )
        if invalid_identity:
            raise PolicyError(
                "Mark position identity is missing or mismatched",
                failure_class=FailureClass.DATA_QUALITY,
                details={"invalid_identity_symbols": invalid_identity},
            )
        held_instrument_ids = {
            str(identity_by_symbol[symbol]["instrument_id"]) for symbol in held_symbols
        }
        provided_instrument_ids = set(payload.daily_release_ids)
        missing_instruments = sorted(held_instrument_ids - provided_instrument_ids)
        unexpected_instruments = sorted(provided_instrument_ids - held_instrument_ids)
        if missing_instruments or unexpected_instruments:
            raise PolicyError(
                "Mark requires one exact PIT release for every and only nonzero positions",
                failure_class=FailureClass.DATA_QUALITY,
                details={
                    "missing_instrument_ids": missing_instruments,
                    "unexpected_instrument_ids": unexpected_instruments,
                },
            )
        prices: dict[str, int] = {}
        for instrument_id, release_id in sorted(payload.daily_release_ids.items()):
            market_text, symbol = instrument_id.split(":", maxsplit=1)
            market = Market(market_text)
            rows = self.references.daily(market, symbol, release_id, visible_at=payload.as_of)
            if any(
                item.market is not market
                or item.symbol != symbol
                or item.instrument_id != instrument_id
                or item.available_to_system_at > payload.as_of
                for item in rows
            ):
                raise _needs_info("Mark release identity or availability is mismatched")
            eligible = [
                item
                for item in rows
                if item.session_close_at <= payload.as_of
                and item.available_to_system_at <= payload.as_of
            ]
            if not eligible:
                raise _policy(f"No unadjusted mark is available for {symbol}")
            prices[symbol] = _yuan_to_fen(max(eligible, key=lambda item: item.session_date).close)
        journal_before = self.ledger.status(request.account_id)["last_event_seq"]
        nav = self.ledger.portfolio_nav(
            request.account_id, prices, as_of=payload.as_of, require_all_prices=True
        )
        journal_after = self.ledger.status(request.account_id)["last_event_seq"]
        if journal_after != journal_before:
            raise RuntimeError("paper mark unexpectedly mutated the journal")
        snapshot = {
            "operation_id": request.operation_id,
            "account_id": request.account_id,
            "as_of": payload.as_of.isoformat(),
            "prices_fen": prices,
            "release_ids": payload.daily_release_ids,
            "nav": nav.model_dump(mode="json"),
        }
        snapshot_ref = self.objects.put_json(snapshot)
        nav_hash = content_hash(snapshot)
        mark_id = content_hash({"operation_id": request.operation_id, "nav_hash": nav_hash})
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT operation_id,account_id,as_of,prices_json,release_ids_json,nav_json,"
                "nav_hash,snapshot_object_hash FROM paper_mark_snapshot WHERE mark_id=?",
                (mark_id,),
            ).fetchone()
            expected = (
                request.operation_id,
                request.account_id,
                payload.as_of.isoformat(),
                canonical_json_bytes(prices).decode(),
                canonical_json_bytes(payload.daily_release_ids).decode(),
                canonical_json_bytes(nav.model_dump(mode="json")).decode(),
                nav_hash,
                snapshot_ref.sha256,
            )
            if existing is not None and tuple(existing) != expected:
                raise _policy("Mark snapshot identity collision")
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_mark_snapshot(mark_id,operation_id,account_id,as_of,"
                    "prices_json,release_ids_json,nav_json,nav_hash,snapshot_object_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (mark_id, *expected),
                )
        return {"mark_id": mark_id, "nav": nav.model_dump(mode="json")}

    def _recover(
        self, request: PaperOperationRequest, payload: PaperRecoverPayload
    ) -> dict[str, object]:
        if payload.as_of > request.requested_at:
            raise _policy("Recovery as_of cannot be in the future")
        object_issues = self._paper_object_issues(request.account_id)
        if object_issues:
            result: dict[str, object] = {
                "status": "CORRUPT",
                "expired_order_count": 0,
                "issues": object_issues,
                "integrity": self.state.integrity_check(),
            }
        else:
            result = self.ledger.recover(
                request.account_id,
                as_of=payload.as_of,
                expire_day_orders=payload.expire_day_orders,
                source_operation_id=request.operation_id,
            )
        snapshot = {
            "operation_id": request.operation_id,
            "account_id": request.account_id,
            "result": result,
        }
        snapshot_ref = self.objects.put_json(snapshot)
        snapshot_hash = content_hash(snapshot)
        recovery_id = content_hash(
            {"operation_id": request.operation_id, "snapshot_hash": snapshot_hash}
        )
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT operation_id,status,snapshot_hash,snapshot_object_hash,created_at "
                "FROM paper_recovery_snapshot WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
            expected = (
                request.operation_id,
                str(result["status"]),
                snapshot_hash,
                snapshot_ref.sha256,
                self.clock().astimezone(UTC).isoformat(),
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_recovery_snapshot(recovery_id,operation_id,status,"
                    "snapshot_hash,snapshot_object_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (recovery_id, *expected),
                )
            elif tuple(existing)[:4] != expected[:4]:
                raise _policy("Recovery snapshot identity collision")
        return cast(dict[str, object], result)

    def _paper_object_issues(self, account_id: str) -> list[str]:
        issues: list[str] = []
        with closing(self.state.connect()) as connection:
            operations = connection.execute(
                "SELECT r.operation_id,r.request_hash,r.request_object_hash,"
                "c.confirmation_hash,c.confirmation_object_hash,e.status,e.result_hash,"
                "e.report_object_hash,k.key_id,k.public_key_object_hash "
                "FROM paper_operation_request r "
                "JOIN paper_operation_confirmation c ON c.operation_id=r.operation_id "
                "LEFT JOIN paper_confirmation_key_binding k "
                "ON k.confirmation_id=c.confirmation_id "
                "JOIN paper_operation_execution e ON e.operation_id=r.operation_id "
                "WHERE r.account_id=?",
                (account_id,),
            ).fetchall()
            for row in operations:
                stored_confirmation: PaperUserConfirmation | None = None
                if not self.objects.verify(str(row["request_object_hash"])):
                    issues.append(f"REQUEST_OBJECT_CORRUPT:{row['operation_id']}")
                else:
                    try:
                        stored_request = PaperOperationRequest.model_validate_json(
                            self.objects.get_bytes(str(row["request_object_hash"]))
                        )
                        if paper_request_hash(stored_request) != row["request_hash"]:
                            issues.append(f"REQUEST_HASH_MISMATCH:{row['operation_id']}")
                    except ValidationError:
                        issues.append(f"REQUEST_OBJECT_INVALID:{row['operation_id']}")
                if not self.objects.verify(str(row["confirmation_object_hash"])):
                    issues.append(f"CONFIRMATION_OBJECT_CORRUPT:{row['operation_id']}")
                else:
                    try:
                        stored_confirmation = PaperUserConfirmation.model_validate_json(
                            self.objects.get_bytes(str(row["confirmation_object_hash"]))
                        )
                        if paper_confirmation_hash(stored_confirmation) != row["confirmation_hash"]:
                            issues.append(f"CONFIRMATION_HASH_MISMATCH:{row['operation_id']}")
                    except (OSError, ValidationError):
                        issues.append(f"CONFIRMATION_OBJECT_INVALID:{row['operation_id']}")
                if row["public_key_object_hash"] is None or not self.objects.verify(
                    str(row["public_key_object_hash"])
                ):
                    issues.append(f"AUTHORIZATION_KEY_OBJECT_CORRUPT:{row['operation_id']}")
                else:
                    try:
                        signature_valid = (
                            stored_confirmation is not None
                            and row["key_id"] == stored_confirmation.key_id
                            and paper_confirmation_signature_valid(
                                stored_confirmation,
                                self.objects.get_bytes(str(row["public_key_object_hash"])),
                            )
                        )
                    except OSError:
                        signature_valid = False
                    if not signature_valid:
                        issues.append(f"CONFIRMATION_SIGNATURE_INVALID:{row['operation_id']}")
                if row["status"] == PaperOperationStatus.COMPLETE.value and (
                    not row["report_object_hash"]
                    or not self.objects.verify(str(row["report_object_hash"]))
                ):
                    issues.append(f"REPORT_OBJECT_CORRUPT:{row['operation_id']}")
                elif row["status"] == PaperOperationStatus.COMPLETE.value:
                    try:
                        self._completed_report(str(row["operation_id"]))
                    except PolicyError:
                        issues.append(f"REPORT_BINDING_MISMATCH:{row['operation_id']}")
            object_queries = (
                (
                    "paper_replay_bar_commit",
                    "SELECT commit_id,commit_object_hash FROM paper_replay_bar_commit "
                    "WHERE account_id=?",
                    "commit_id",
                    "commit_object_hash",
                ),
                (
                    "paper_mark_snapshot",
                    "SELECT mark_id,snapshot_object_hash FROM paper_mark_snapshot "
                    "WHERE account_id=?",
                    "mark_id",
                    "snapshot_object_hash",
                ),
                (
                    "paper_settlement_policy",
                    "SELECT p.operation_id,p.policy_object_hash FROM paper_settlement_policy p "
                    "JOIN paper_operation_request r ON r.operation_id=p.operation_id "
                    "WHERE r.account_id=?",
                    "operation_id",
                    "policy_object_hash",
                ),
                (
                    "paper_recovery_snapshot",
                    "SELECT p.recovery_id,p.snapshot_object_hash FROM paper_recovery_snapshot p "
                    "JOIN paper_operation_request r ON r.operation_id=p.operation_id "
                    "WHERE r.account_id=?",
                    "recovery_id",
                    "snapshot_object_hash",
                ),
                (
                    "paper_corporate_action_application",
                    "SELECT p.action_observation_id,p.application_object_hash "
                    "FROM paper_corporate_action_application p "
                    "WHERE p.account_id=?",
                    "action_observation_id",
                    "application_object_hash",
                ),
            )
            for table, query, identity, object_column in object_queries:
                rows = connection.execute(query, (account_id,)).fetchall()
                for row in rows:
                    if not self.objects.verify(str(row[object_column])):
                        issues.append(f"OBJECT_CORRUPT:{table}:{row[identity]}")
        return issues

    def _register_fee_schedule(self) -> None:
        schedule_bytes = paper_fee_schedule_bytes(self.fee_schedule)
        schedule_json = schedule_bytes.decode()
        schedule_hash = sha256_bytes(schedule_bytes)
        ref = self.objects.put_bytes(schedule_bytes)
        values = (
            schedule_hash,
            ref.sha256,
            schedule_json,
            self.fee_schedule.effective_from.isoformat(),
            canonical_json_bytes(
                [item.value for item in self.fee_schedule.applicable_markets]
            ).decode(),
        )
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT schedule_hash,schedule_object_hash,schedule_json,effective_from,"
                "markets_json "
                "FROM paper_fee_schedule_release WHERE rule_version=?",
                (self.fee_schedule.rule_version,),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise _policy("Fee schedule version collision")
            if existing is None:
                connection.execute(
                    "INSERT INTO paper_fee_schedule_release(rule_version,schedule_hash,"
                    "schedule_object_hash,schedule_json,effective_from,markets_json,registered_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        self.fee_schedule.rule_version,
                        *values,
                        self.clock().astimezone(UTC).isoformat(),
                    ),
                )

    def _complete(self, report: PaperOperationReport) -> None:
        result_json = canonical_json_bytes(report.result).decode()
        result_hash = content_hash(report.result)
        report_ref = self.objects.put_json(report.model_dump(mode="json"))
        now = report.completed_at.isoformat()
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT status,result_hash,report_object_hash FROM paper_operation_execution "
                "WHERE operation_id=?",
                (report.operation_id,),
            ).fetchone()
            if existing is not None and existing["status"] == PaperOperationStatus.COMPLETE.value:
                if (
                    existing["result_hash"] != result_hash
                    or existing["report_object_hash"] != report_ref.sha256
                ):
                    raise _policy("Completed operation report identity collision")
                return
            connection.execute(
                "UPDATE paper_operation_execution SET status=?,result_json=?,result_hash=?,"
                "report_object_hash=?,completed_at=? WHERE operation_id=?",
                (
                    PaperOperationStatus.COMPLETE.value,
                    result_json,
                    result_hash,
                    report_ref.sha256,
                    now,
                    report.operation_id,
                ),
            )
            self._transition(connection, report.operation_id, PaperOperationStatus.COMPLETE, now)

    def _finish_committed(
        self,
        request: PaperOperationRequest,
        request_hash: str,
        result: dict[str, object],
    ) -> PaperOperationReport:
        """Finalize a durable historical COMMITTED result without re-authorizing it."""

        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT r.request_hash,c.confirmation_id FROM paper_operation_request r "
                "JOIN paper_operation_confirmation c ON c.operation_id=r.operation_id "
                "WHERE r.operation_id=?",
                (request.operation_id,),
            ).fetchone()
        if row is None or row["request_hash"] != request_hash:
            raise _policy("Committed operation request binding is missing or mismatched")
        completed_at = self.clock().astimezone(UTC)
        report = PaperOperationReport(
            created_at=completed_at,
            operation_id=request.operation_id,
            operation_type=request.payload.operation_type,
            account_id=request.account_id,
            status=PaperOperationStatus.RECOVERED,
            request_hash=request_hash,
            confirmation_id=str(row["confirmation_id"]),
            result=result,
            completed_at=completed_at,
        )
        with self.ledger.atomic():
            with self.ledger.transaction() as connection:
                self._transition(
                    connection,
                    request.operation_id,
                    PaperOperationStatus.RECOVERED,
                    completed_at.isoformat(),
                    reason_code="COMMITTED_RESULT_RESPONSE_RECOVERY",
                )
            self._complete(report)
        self._after_commit(report)
        return report

    def _after_commit(self, report: PaperOperationReport) -> None:
        """Response boundary hook used to prove retry-after-response-loss semantics."""

    def _commit_result(self, operation_id: str, result: dict[str, object]) -> None:
        result_json = canonical_json_bytes(result).decode()
        result_hash = content_hash(result)
        now = self.clock().astimezone(UTC).isoformat()
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT status,result_json,result_hash FROM paper_operation_execution "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is None:
                raise RuntimeError("Paper operation execution disappeared")
            if existing["status"] in {
                PaperOperationStatus.COMMITTED.value,
                PaperOperationStatus.COMPLETE.value,
                PaperOperationStatus.RECOVERED.value,
            }:
                if (existing["result_json"], existing["result_hash"]) != (
                    result_json,
                    result_hash,
                ):
                    raise _policy("Committed operation result identity collision")
                return
            connection.execute(
                "UPDATE paper_operation_execution SET status=?,result_json=?,result_hash=? "
                "WHERE operation_id=?",
                (
                    PaperOperationStatus.COMMITTED.value,
                    result_json,
                    result_hash,
                    operation_id,
                ),
            )
            self._transition(connection, operation_id, PaperOperationStatus.COMMITTED, now)

    def _committed_result(self, operation_id: str) -> dict[str, object] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT status,result_json,result_hash FROM paper_operation_execution "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None or row["status"] != PaperOperationStatus.COMMITTED.value:
            return None
        try:
            result = json.loads(str(row["result_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _policy("Committed operation result cannot be decoded") from exc
        if not isinstance(result, dict) or content_hash(result) != row["result_hash"]:
            raise _policy("Committed operation result hash mismatch")
        return cast(dict[str, object], result)

    def _fail(
        self,
        operation_id: str,
        status: PaperOperationStatus,
        *,
        reason_code: str,
    ) -> None:
        now = self.clock().astimezone(UTC).isoformat()
        with self.ledger.transaction() as connection:
            current = connection.execute(
                "SELECT status FROM paper_operation_execution WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if current is None or current["status"] in {
                PaperOperationStatus.COMMITTED.value,
                PaperOperationStatus.COMPLETE.value,
                PaperOperationStatus.RECOVERED.value,
            }:
                return
            connection.execute(
                "UPDATE paper_operation_execution SET status=? WHERE operation_id=?",
                (status.value, operation_id),
            )
            self._transition(connection, operation_id, status, now, reason_code=reason_code)

    def _completed_report(self, operation_id: str) -> PaperOperationReport | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT e.status,e.result_hash,e.report_object_hash,e.completed_at,"
                "r.account_id,r.operation_type,r.request_hash,c.confirmation_id "
                "FROM paper_operation_execution e "
                "JOIN paper_operation_request r ON r.operation_id=e.operation_id "
                "JOIN paper_operation_confirmation c ON c.operation_id=e.operation_id "
                "WHERE e.operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None or row["status"] != PaperOperationStatus.COMPLETE.value:
            return None
        if not row["report_object_hash"] or not self.objects.verify(str(row["report_object_hash"])):
            raise _policy("Completed operation report object hash mismatch")
        object_hash = str(row["report_object_hash"])
        try:
            report = PaperOperationReport.model_validate_json(self.objects.get_bytes(object_hash))
        except (ValueError, ValidationError) as exc:
            raise _policy("Completed operation report is corrupt") from exc
        expected_binding = (
            operation_id,
            str(row["operation_type"]),
            str(row["account_id"]),
            str(row["request_hash"]),
            str(row["confirmation_id"]),
            str(row["completed_at"]),
        )
        actual_binding = (
            report.operation_id,
            report.operation_type,
            report.account_id,
            report.request_hash,
            report.confirmation_id,
            report.completed_at.isoformat(),
        )
        if (
            actual_binding != expected_binding
            or report.status not in {PaperOperationStatus.COMPLETE, PaperOperationStatus.RECOVERED}
            or content_hash(report.result) != row["result_hash"]
        ):
            raise _policy("Completed operation report binding mismatch")
        return report

    @staticmethod
    def _transition(
        connection: sqlite3.Connection,
        operation_id: str,
        status: PaperOperationStatus,
        occurred_at: str,
        *,
        reason_code: str | None = None,
    ) -> None:
        latest = connection.execute(
            "SELECT status FROM paper_operation_transition WHERE operation_id=? "
            "ORDER BY transition_seq DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if latest is not None and latest["status"] == status.value:
            return
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(transition_seq),0)+1 "
                "FROM paper_operation_transition WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO paper_operation_transition(operation_id,transition_seq,status,"
            "reason_code,occurred_at) VALUES(?,?,?,?,?)",
            (operation_id, seq, status.value, reason_code, occurred_at),
        )


def paper_request_hash(request: PaperOperationRequest) -> str:
    payload = _strip_created_at(request.model_dump(mode="json"))
    payload.pop("operation_id", None)
    return sha256_bytes(canonical_json_bytes(payload))


def paper_request_bytes(request: PaperOperationRequest) -> bytes:
    """Exact immutable request bytes persisted in SQLite and ObjectStore."""

    return canonical_json_bytes(_strip_created_at(request.model_dump(mode="json")))


def paper_confirmation_hash(confirmation: PaperUserConfirmation) -> str:
    payload = _strip_created_at(confirmation.model_dump(mode="json"))
    payload.pop("confirmation_id", None)
    return sha256_bytes(canonical_json_bytes(payload))


def paper_confirmation_bytes(confirmation: PaperUserConfirmation) -> bytes:
    """Exact immutable confirmation bytes persisted in SQLite and ObjectStore."""

    return canonical_json_bytes(_strip_created_at(confirmation.model_dump(mode="json")))


def paper_confirmation_signing_bytes(confirmation: PaperUserConfirmation) -> bytes:
    """Domain-separated bytes verified by the production authorization boundary."""

    return _CONFIRMATION_DOMAIN + canonical_json_bytes(
        {
            "schema_version": confirmation.schema_version,
            "operation_id": confirmation.operation_id,
            "request_hash": confirmation.request_hash,
            "account_id": confirmation.account_id,
            "operation_type": confirmation.operation_type,
            "confirmed_at": confirmation.confirmed_at,
            "expires_at": confirmation.expires_at,
            "nonce": confirmation.nonce,
            "key_id": confirmation.key_id,
            "signature_algorithm": confirmation.signature_algorithm,
        }
    )


def paper_confirmation_signature_valid(
    confirmation: PaperUserConfirmation,
    public_key_pem: str | bytes,
) -> bool:
    """Reverify one frozen manual confirmation without relying on mutable config."""

    try:
        public_key = serialization.load_pem_public_key(
            public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
        )
        signature = base64.b64decode(confirmation.signature_base64, validate=True)
        signed = paper_confirmation_signing_bytes(confirmation)
        if confirmation.signature_algorithm == "ED25519":
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                return False
            public_key.verify(signature, signed)
        else:
            if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
                public_key.curve,
                ec.SECP256R1,
            ):
                return False
            public_key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        return False
    return True


def paper_fee_schedule_bytes(schedule: ReplayFeeSchedule) -> bytes:
    return canonical_json_bytes(_strip_created_at(schedule.model_dump(mode="json")))


def paper_fee_schedule_hash(schedule: ReplayFeeSchedule) -> str:
    return sha256_bytes(paper_fee_schedule_bytes(schedule))


def load_paper_operation(path: Path) -> PaperOperationRequest:
    return PaperOperationRequest.model_validate_json(path.read_bytes())


def load_paper_confirmation(path: Path) -> PaperUserConfirmation:
    return PaperUserConfirmation.model_validate_json(path.read_bytes())


def _strip_created_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_created_at(item) for key, item in value.items() if key != "created_at"}
    if isinstance(value, list):
        return [_strip_created_at(item) for item in value]
    return value


def _policy(message: str) -> PolicyError:
    return PolicyError(message, failure_class=FailureClass.POLICY_REJECTED)


def _needs_info(message: str) -> PolicyError:
    return PolicyError(message, failure_class=FailureClass.DATA_QUALITY)


def _corporate_action_sort_key(
    item: tuple[CorporateActionObservation, str],
) -> tuple[date, date, str, str]:
    action, release_id = item
    try:
        record_date = date.fromisoformat(action.structured_terms["dividRegistDate"])
        if action.ex_date is None:
            raise ValueError("missing ex date")
        effective_date = action.ex_date
        pay_date = action.structured_terms.get("dividPayDate")
        if pay_date:
            effective_date = max(effective_date, date.fromisoformat(pay_date))
    except (KeyError, ValueError) as exc:
        raise _needs_info("Corporate action lacks sortable exact dates") from exc
    return record_date, effective_date, action.observation_id, release_id


def _is_continuous_session_time(value: time) -> bool:
    return time(9, 30) <= value <= time(11, 30) or time(13, 0) <= value <= time(15, 0)


def _price_bounds(previous_close_fen: int, limit_bps: int) -> tuple[int, int]:
    rate = Decimal(limit_bps) / Decimal(10_000)
    lower = _round_fen(Decimal(previous_close_fen) * (Decimal(1) - rate))
    upper = _round_fen(Decimal(previous_close_fen) * (Decimal(1) + rate))
    return lower, upper


def _commission(gross_fen: int, schedule: ReplayFeeSchedule) -> int:
    value = _round_fen(Decimal(gross_fen) * schedule.commission_rate)
    return max(value, schedule.minimum_commission_fen) if schedule.commission_rate else 0


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _yuan_to_fen(value: Decimal) -> int:
    return _round_fen(value * 100)


__all__ = [
    "MarketReferencePaperVerifier",
    "PaperOperationService",
    "PaperReferenceVerifier",
    "load_paper_confirmation",
    "load_paper_operation",
    "paper_confirmation_hash",
    "paper_confirmation_signature_valid",
    "paper_request_hash",
]
