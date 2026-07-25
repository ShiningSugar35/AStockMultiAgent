from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from astock.core.errors import PolicyError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.paper_trading import (
    LedgerService,
    PaperOperationService,
    load_fee_schedule,
    load_paper_trading_rules,
    paper_confirmation_hash,
    paper_confirmation_signing_bytes,
    paper_confirmation_bytes,
    paper_request_bytes,
    paper_request_hash,
)
from astock.schemas import (
    AdjustmentMode,
    CorporateActionObservation,
    CorporateActionStatus,
    DailyBarObservation,
    InstrumentRecord,
    InstrumentType,
    Market,
    OrderSide,
    PaperCancelOrderPayload,
    PaperMarkPayload,
    PaperOperationRequest,
    PaperOrderValidity,
    PaperPlaceOrderPayload,
    PaperRecoverPayload,
    PaperSettlePayload,
    PaperUserConfirmation,
    TradingSession,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=SHANGHAI)
_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
_PUBLIC_KEY_PEM = _SIGNING_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
_SERVICE_SECURITY = {
    "trusted_confirmation_keys": {"test-ed25519": _PUBLIC_KEY_PEM},
    "trading_rules": load_paper_trading_rules(
        PROJECT_ROOT / "configs" / "paper_trading_rules.yaml"
    ),
}


class _ReferenceFixture:
    def __init__(self, *, is_st: bool = False) -> None:
        self.is_st = is_st

    def calendar(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[TradingSession]:
        return [
            TradingSession(
                exchange=market,
                session_date=date(2026, 7, 17),
                is_open=True,
                source_snapshot_id="calendar-source-prior",
                available_to_system_at=NOW - timedelta(days=2),
            ),
            TradingSession(
                exchange=market,
                session_date=NOW.date(),
                is_open=True,
                source_snapshot_id="calendar-source",
                available_to_system_at=NOW - timedelta(days=1),
            )
        ]

    def instrument(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> InstrumentRecord:
        return InstrumentRecord(
            instrument_id=f"{market.value}:{symbol}",
            market=market,
            symbol=symbol,
            name="测试股份",
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=NOW.date(),
            is_st=self.is_st,
            listing_date=date(2010, 1, 1),
            source_snapshot_id="instrument-source",
            available_to_system_at=NOW - timedelta(days=1),
        )

    def daily(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> list[DailyBarObservation]:
        return [
            DailyBarObservation(
                observation_id="1" * 64,
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                session_date=date(2026, 7, 17),
                session_close_at=datetime(2026, 7, 17, 15, 0, tzinfo=SHANGHAI),
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                previous_close=Decimal("10"),
                volume=Decimal("10000"),
                adjustment_mode=AdjustmentMode.NONE,
                is_st=self.is_st,
                source_snapshot_id="daily-source",
                available_to_system_at=datetime(
                    2026, 7, 17, 15, 1, tzinfo=SHANGHAI
                ),
            )
        ]

    def corporate_actions(self, *args, **kwargs):
        return []

    def trading_classification(self, instrument: InstrumentRecord, *, visible_at: datetime):
        from astock.paper_trading.operation import PaperInstrumentTradingFacts

        boards = {
            "600519": "MAIN",
            "688001": "STAR",
            "300001": "CHINEXT",
            "000001": "MAIN",
            "920015": "BSE",
        }
        return PaperInstrumentTradingFacts(
            board=boards[instrument.symbol],
            risk_status="RISK_WARNING" if self.is_st else "NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id="fixture-explicit-classification",
        )


class _CorporateActionFixture(_ReferenceFixture):
    def __init__(self, action: CorporateActionObservation) -> None:
        super().__init__()
        self.action = action

    def corporate_actions(self, *args, **kwargs) -> list[CorporateActionObservation]:
        return [self.action]


class _NoDailyFixture(_ReferenceFixture):
    def daily(self, *args, **kwargs) -> list[DailyBarObservation]:
        return []


class _WrongDailyIdentityFixture(_ReferenceFixture):
    def daily(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> list[DailyBarObservation]:
        return super().daily(Market.XSHE, "000001", release_id, visible_at=visible_at)


class _StalePreviousCloseFixture(_ReferenceFixture):
    def calendar(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[TradingSession]:
        rows = super().calendar(market, release_id, visible_at=visible_at)
        return [
            TradingSession(
                exchange=market,
                session_date=date(2026, 7, 16),
                is_open=True,
                source_snapshot_id="calendar-source-stale",
                available_to_system_at=NOW - timedelta(days=3),
            ),
            *rows,
        ]

    def daily(
        self, market: Market, symbol: str, release_id: str, *, visible_at: datetime
    ) -> list[DailyBarObservation]:
        prior = super().daily(market, symbol, release_id, visible_at=visible_at)[0]
        return [
            prior.model_copy(
                update={
                    "session_date": date(2026, 7, 16),
                    "session_close_at": datetime(
                        2026, 7, 16, 15, 0, tzinfo=SHANGHAI
                    ),
                    "available_to_system_at": datetime(
                        2026, 7, 16, 15, 1, tzinfo=SHANGHAI
                    ),
                }
            )
        ]


def _operation_request(
    payload: (
        PaperPlaceOrderPayload
        | PaperCancelOrderPayload
        | PaperMarkPayload
        | PaperRecoverPayload
        | PaperSettlePayload
    ),
    idempotency_key: str,
    *,
    requested_at: datetime = NOW,
) -> PaperOperationRequest:
    request = PaperOperationRequest(
        operation_id="0" * 64,
        account_id="paper",
        idempotency_key=idempotency_key,
        requested_at=requested_at,
        expires_at=requested_at + timedelta(minutes=30),
        payload=payload,
    )
    return request.model_copy(update={"operation_id": paper_request_hash(request)})


def _request(
    *,
    market: Market = Market.XSHG,
    symbol: str = "600519",
    limit_price_fen: int = 1000,
    validity: PaperOrderValidity = PaperOrderValidity.DAY,
    requested_at: datetime = NOW,
) -> PaperOperationRequest:
    return _operation_request(
        PaperPlaceOrderPayload(
            market=market,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=limit_price_fen,
            validity=validity,
            calendar_release_id="1" * 64,
            instrument_release_id="2" * 64,
            daily_release_id="3" * 64,
            fee_rule_version="cn-a-share-paper-2026-07-13",
        ),
        f"place-{market.value}-{symbol}-{limit_price_fen}-{validity.value}",
        requested_at=requested_at,
    )


def _confirmation(request: PaperOperationRequest) -> PaperUserConfirmation:
    confirmation = PaperUserConfirmation(
        confirmation_id="0" * 64,
        operation_id=request.operation_id,
        request_hash=request.operation_id,
        account_id=request.account_id,
        operation_type=request.payload.operation_type,
        confirmed_at=request.requested_at + timedelta(minutes=1),
        expires_at=request.requested_at + timedelta(minutes=20),
        nonce=f"nonce-{request.operation_id}",
        key_id="test-ed25519",
        signature_algorithm="ED25519",
        signature_base64="A" * 16,
    )
    return _resign_confirmation(confirmation)


def _resign_confirmation(confirmation: PaperUserConfirmation) -> PaperUserConfirmation:
    confirmation = confirmation.model_copy(
        update={"confirmation_id": "0" * 64, "signature_base64": "A" * 16}
    )
    signature = _SIGNING_KEY.sign(paper_confirmation_signing_bytes(confirmation))
    confirmation = confirmation.model_copy(
        update={"signature_base64": base64.b64encode(signature).decode("ascii")}
    )
    return confirmation.model_copy(
        update={"confirmation_id": paper_confirmation_hash(confirmation)}
    )


def _service(
    state: StateStore, object_store: ObjectStore
) -> tuple[PaperOperationService, LedgerService]:
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 2_000_000)
    return (
        PaperOperationService(
            state,
            object_store,
            ledger,
            _ReferenceFixture(),
            load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
            clock=lambda: NOW + timedelta(minutes=2),
            **_SERVICE_SECURITY,
        ),
        ledger,
    )


def _bind_pending_settlement_identity(
    state: StateStore, *, market: Market, symbol: str
) -> None:
    with state.transaction() as connection:
        rows = connection.execute(
            "SELECT settlement_id FROM position_settlement WHERE symbol=? "
            "AND status='PENDING_CALENDAR_CONFIRMATION'",
            (symbol,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "INSERT OR IGNORE INTO paper_settlement_identity(settlement_id,market,"
                "instrument_id) VALUES(?,?,?)",
                (row["settlement_id"], market.value, f"{market.value}:{symbol}"),
            )


def test_place_requires_exact_unexpired_user_confirmation(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    with pytest.raises(PolicyError, match="confirmation is required"):
        service.execute(request, None)
    wrong = _confirmation(request).model_copy(update={"request_hash": "f" * 64})
    with pytest.raises(PolicyError, match="immutable payload"):
        service.execute(request, wrong)
    assert ledger.open_orders("paper") == []


def test_signature_configuration_bad_signature_and_nonce_replay_fail_closed(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    unsigned_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        trading_rules=load_paper_trading_rules(
            PROJECT_ROOT / "configs" / "paper_trading_rules.yaml"
        ),
    )
    with pytest.raises(PolicyError, match="not configured or trusted"):
        unsigned_service.execute(request, _confirmation(request))
    assert ledger.open_orders("paper") == []

    bad = _confirmation(request).model_copy(
        update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
    )
    bad = bad.model_copy(update={"confirmation_id": paper_confirmation_hash(bad)})
    with pytest.raises(PolicyError, match="signature verification failed"):
        service.execute(request, bad)
    assert ledger.open_orders("paper") == []

    service.execute(request, _confirmation(request))
    second = _request(limit_price_fen=1001)
    replayed_nonce = _confirmation(second).model_copy(
        update={"nonce": _confirmation(request).nonce}
    )
    replayed_nonce = _resign_confirmation(replayed_nonce)
    with pytest.raises(PolicyError, match="nonce has already been consumed"):
        service.execute(second, replayed_nonce)
    assert len(ledger.open_orders("paper")) == 1


def test_completed_operation_is_recoverable_after_confirmation_expiry(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    confirmation = _confirmation(request)
    first = service.execute(request, confirmation)
    later = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(days=2),
        **_SERVICE_SECURITY,
    )
    assert later.execute(request, confirmation) == first


def test_confirmed_place_is_immutable_and_idempotent(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    confirmation = _confirmation(request)
    first = service.execute(request, confirmation, expected_operation_type="PLACE_ORDER")
    second = service.execute(request, confirmation, expected_operation_type="PLACE_ORDER")
    assert first == second
    assert len(ledger.open_orders("paper")) == 1
    with state.connect() as connection:
        execution_status = connection.execute(
            "SELECT status FROM paper_operation_execution WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0]
        assert execution_status == "COMPLETE"
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_operation_transition WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] == 4
        stored = connection.execute(
            "SELECT payload_json,request_object_hash FROM paper_operation_request "
            "WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        assert stored["payload_json"].encode() == object_store.get_bytes(
            stored["request_object_hash"]
        )
        receipt = connection.execute(
            "SELECT key_id,nonce,payload_json,confirmation_object_hash FROM "
            "paper_operation_confirmation WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        assert (receipt["key_id"], receipt["nonce"]) == (
            confirmation.key_id,
            confirmation.nonce,
        )
        assert receipt["payload_json"].encode() == object_store.get_bytes(
            receipt["confirmation_object_hash"]
        )
        schedule = connection.execute(
            "SELECT schedule_hash,schedule_object_hash,schedule_json FROM "
            "paper_fee_schedule_release"
        ).fetchone()
        assert schedule["schedule_hash"] == schedule["schedule_object_hash"]
        assert schedule["schedule_json"].encode() == object_store.get_bytes(
            schedule["schedule_object_hash"]
        )
        assert connection.execute(
            "SELECT fee_schedule_hash FROM paper_order_rule_binding"
        ).fetchone()[0] == schedule["schedule_hash"]


def test_concurrent_exact_operation_commits_one_order(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    confirmation = _confirmation(request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(lambda _: service.execute(request, confirmation), range(2))
        )
    assert reports[0] == reports[1]
    assert len(ledger.open_orders("paper")) == 1
    with state.connect() as connection:
        assert connection.execute(
            "SELECT attempt_count FROM paper_operation_execution WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] in {1, 2}


def test_operation_idempotency_key_rejects_another_payload(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    first = _operation_request(
        _request(limit_price_fen=1000).payload, "same-user-key"
    )
    service.execute(first, _confirmation(first))
    collision = _operation_request(
        _request(limit_price_fen=1001).payload, "same-user-key"
    )
    with pytest.raises(PolicyError, match="idempotency-key collision"):
        service.execute(collision, _confirmation(collision))
    assert len(ledger.open_orders("paper")) == 1


def test_expired_confirmation_writes_no_order(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    request = _request()
    expired = _confirmation(request).model_copy(
        update={"expires_at": NOW + timedelta(minutes=1)}
    )
    expired = _resign_confirmation(expired)
    with pytest.raises(PolicyError, match="expired"):
        service.execute(request, expired)
    assert ledger.open_orders("paper") == []


def test_cancel_mark_and_recover_stay_inside_confirmed_boundary(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    placed = _request()
    service.execute(placed, _confirmation(placed))
    order = ledger.open_orders("paper")[0]

    mark = _operation_request(
        PaperMarkPayload(
            as_of=NOW,
            daily_release_ids={},
        ),
        "mark-1",
    )
    journal_before = ledger.status("paper")["last_event_seq"]
    marked = service.execute(mark, _confirmation(mark))
    assert marked.result["mark_id"]
    assert ledger.status("paper")["last_event_seq"] == journal_before

    cancel = _operation_request(
        PaperCancelOrderPayload(order_id=order.order_id), "cancel-1"
    )
    service.execute(cancel, _confirmation(cancel))
    assert ledger.open_orders("paper") == []

    recover = _operation_request(
        PaperRecoverPayload(as_of=NOW), "recover-1"
    )
    recovered = service.execute(recover, _confirmation(recover))
    assert recovered.result["status"] == "HEALTHY_NOOP", recovered.result


def test_mark_fails_closed_when_any_nonzero_position_lacks_release(
    state: StateStore, object_store: ObjectStore
) -> None:
    service, ledger = _service(state, object_store)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="mark-held-position",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=500,
    )
    ledger.record_fill(
        fill_id="mark-held-position-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=1000,
        commission_fen=500,
        occurred_at=NOW - timedelta(days=1),
    )
    _bind_pending_settlement_identity(state, market=Market.XSHG, symbol="600519")
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO paper_position_identity(account_id,symbol,market,instrument_id) "
            "VALUES('paper','600519','XSHG','XSHG:600519')"
        )
    mark = _operation_request(
        PaperMarkPayload(
            as_of=NOW,
            daily_release_ids={"XSHE:000001": "3" * 64},
        ),
        "mark-missing-held-symbol",
    )
    with pytest.raises(PolicyError) as captured:
        service.execute(mark, _confirmation(mark))
    assert captured.value.details["missing_instrument_ids"] == ["XSHG:600519"]
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_mark_snapshot").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("market", "symbol", "accepted", "rejected"),
    [
        (Market.XSHG, "600519", 1100, 1101),
        (Market.XSHG, "688001", 1200, 1201),
        (Market.XSHE, "300001", 1200, 1201),
    ],
)
def test_board_price_limit_boundaries(
    state: StateStore,
    object_store: ObjectStore,
    market: Market,
    symbol: str,
    accepted: int,
    rejected: int,
) -> None:
    service, ledger = _service(state, object_store)
    allowed = _request(market=market, symbol=symbol, limit_price_fen=accepted)
    service.execute(allowed, _confirmation(allowed))
    blocked = _request(market=market, symbol=symbol, limit_price_fen=rejected)
    with pytest.raises(PolicyError, match="price band"):
        service.execute(blocked, _confirmation(blocked))


def test_main_board_risk_warning_limit_is_ten_percent_after_2026_change(
    state: StateStore, object_store: ObjectStore
) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 2_000_000)
    service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(is_st=True),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    allowed = _request(limit_price_fen=1100)
    service.execute(allowed, _confirmation(allowed))
    blocked = _request(limit_price_fen=1101)
    with pytest.raises(PolicyError, match="price band"):
        service.execute(blocked, _confirmation(blocked))


def test_bse_order_rounding_remains_fail_closed(
    state: StateStore, object_store: ObjectStore
) -> None:
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 2_000_000)
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        schedule.model_copy(
            update={"applicable_markets": [*schedule.applicable_markets, Market.BJSE]}
        ),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    request = _request(market=Market.BJSE, symbol="920015", limit_price_fen=1000)
    with pytest.raises(PolicyError, match="BSE order-price rounding"):
        service.execute(request, _confirmation(request))


def test_session_previous_close_and_market_order_fail_closed(
    state: StateStore, object_store: ObjectStore
) -> None:
    lunch = datetime(2026, 7, 20, 12, 0, tzinfo=SHANGHAI)
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 2_000_000)
    lunch_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: lunch + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    lunch_request = _request(requested_at=lunch)
    with pytest.raises(PolicyError, match="outside verified continuous"):
        lunch_service.execute(lunch_request, _confirmation(lunch_request))

    no_daily = PaperOperationService(
        state,
        object_store,
        ledger,
        _NoDailyFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    missing = _request(limit_price_fen=999)
    with pytest.raises(PolicyError, match="Previous unadjusted close"):
        no_daily.execute(missing, _confirmation(missing))

    payload = _request().payload.model_dump(mode="json")
    payload["order_type"] = "MARKET"
    with pytest.raises(ValidationError):
        PaperPlaceOrderPayload.model_validate(payload)


def test_day_expiry_releases_cash_and_gtc_is_disabled(
    state: StateStore, object_store: ObjectStore
) -> None:
    morning, ledger = _service(state, object_store)
    day = _request(limit_price_fen=1000, validity=PaperOrderValidity.DAY)
    gtc = _request(limit_price_fen=1001, validity=PaperOrderValidity.GTC)
    morning.execute(day, _confirmation(day))
    with pytest.raises(PolicyError, match="GTC is disabled"):
        morning.execute(gtc, _confirmation(gtc))
    day_order_id = next(
        order.order_id
        for order in ledger.open_orders("paper")
        if order.client_request_id == day.operation_id
    )
    after_close = datetime(2026, 7, 20, 15, 1, tzinfo=SHANGHAI)
    recovery_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: after_close + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    recover = _operation_request(
        PaperRecoverPayload(as_of=after_close),
        "expire-day-orders",
        requested_at=after_close,
    )
    report = recovery_service.execute(recover, _confirmation(recover))
    assert report.result["status"] == "RECOVERED"
    assert report.result["expired_order_count"] == 1
    assert ledger.open_orders("paper") == []
    assert ledger.get_order(day_order_id).status.value == "EXPIRED"


def test_recover_detects_object_corruption_before_expiring_orders(
    state: StateStore, object_store: ObjectStore
) -> None:
    morning, ledger = _service(state, object_store)
    day = _request()
    morning.execute(day, _confirmation(day))
    order = ledger.open_orders("paper")[0]
    with state.connect() as connection:
        object_hash = connection.execute(
            "SELECT request_object_hash FROM paper_operation_request WHERE operation_id=?",
            (day.operation_id,),
        ).fetchone()[0]
    object_store.path_for(object_hash).write_bytes(b"corrupt")

    after_close = datetime(2026, 7, 20, 15, 1, tzinfo=SHANGHAI)
    recovery_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: after_close + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    recover = _operation_request(
        PaperRecoverPayload(as_of=after_close),
        "corrupt-object-recovery",
        requested_at=after_close,
    )
    result = recovery_service.execute(recover, _confirmation(recover)).result
    assert result["status"] == "CORRUPT"
    assert result["expired_order_count"] == 0
    assert ledger.get_order(order.order_id).status is not None
    assert ledger.get_order(order.order_id).status.value == "ACCEPTED"


def test_fee_schedule_market_and_effective_date_boundaries(
    state: StateStore, object_store: ObjectStore
) -> None:
    base = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 2_000_000)
    xshg_only = base.model_copy(update={"applicable_markets": [Market.XSHG]})
    market_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        xshg_only,
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    market_request = _request(market=Market.XSHE, symbol="000001")
    with pytest.raises(PolicyError, match="does not cover"):
        market_service.execute(market_request, _confirmation(market_request))

    future = base.model_copy(update={"effective_from": date(2026, 7, 21)})
    effective_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _ReferenceFixture(),
        future,
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    effective_request = _request(limit_price_fen=1002)
    with pytest.raises(PolicyError, match="not effective"):
        effective_service.execute(effective_request, _confirmation(effective_request))


def test_settle_response_crash_recovers_original_committed_result(
    state: StateStore, object_store: ObjectStore, monkeypatch
) -> None:
    service, ledger = _service(state, object_store)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="legacy-friday-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=500,
    )
    friday = datetime(2026, 7, 17, 7, 0, tzinfo=ZoneInfo("UTC"))
    ledger.record_fill(
        fill_id="legacy-friday-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=1000,
        commission_fen=500,
        occurred_at=friday,
    )
    _bind_pending_settlement_identity(state, market=Market.XSHG, symbol="600519")
    settle = _operation_request(
        PaperSettlePayload(
            as_of=NOW,
            market=Market.XSHG,
            calendar_release_id="1" * 64,
        ),
        "settle-response-crash",
    )
    confirmation = _confirmation(settle)

    def crash_after_commit(report) -> None:
        raise RuntimeError("simulated response crash")

    monkeypatch.setattr(service, "_after_commit", crash_after_commit)
    with pytest.raises(RuntimeError, match="response crash"):
        service.execute(settle, confirmation)
    assert ledger.status("paper")["positions"][0]["qty_available"] == 100
    with state.connect() as connection:
        assert connection.execute(
            "SELECT status FROM paper_operation_execution WHERE operation_id=?",
            (settle.operation_id,),
        ).fetchone()[0] == "COMPLETE"

    monkeypatch.setattr(service, "_after_commit", lambda report: None)
    recovered = service.execute(settle, confirmation)
    assert recovered.result["settled_qty"] == 100
    assert ledger.status("paper")["positions"][0]["qty_available"] == 100


def test_verified_stock_action_applies_once_and_collision_is_rejected(
    state: StateStore, object_store: ObjectStore
) -> None:
    action = CorporateActionObservation(
        observation_id="a" * 64,
        instrument_id="XSHG:600519",
        market=Market.XSHG,
        symbol="600519",
        action_type="STOCK_DISTRIBUTION_HINT",
        report_period="2025",
        announcement_date=date(2026, 6, 1),
        ex_date=date(2026, 7, 20),
        status=CorporateActionStatus.TERMS_VERIFIED,
        structured_terms={
            "dividRegistDate": "2026-07-17",
            "dividOperateDate": "2026-07-20",
            "dividPayDate": "2026-07-20",
            "dividCashPsBeforeTax": "0",
            "dividStocksPs": "0.1",
            "dividReserveToStockPs": "0",
        },
        official_document_snapshot_id="official-snapshot",
        official_document_url="https://example.invalid/official.pdf",
        official_announcement_id="official-action",
        ledger_eligible=True,
        source_snapshot_id="structured-snapshot",
        available_to_system_at=NOW - timedelta(days=1),
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 2_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="pre-record-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=500,
    )
    ledger.record_fill(
        fill_id="pre-record-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=1000,
        commission_fen=500,
        occurred_at=datetime(2026, 7, 16, 7, 0, tzinfo=ZoneInfo("UTC")),
    )
    _bind_pending_settlement_identity(state, market=Market.XSHG, symbol="600519")
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO paper_position_identity(account_id,symbol,market,instrument_id) "
            "VALUES('paper','600519','XSHG','XSHG:600519')"
        )
        connection.execute(
            "INSERT INTO paper_position_cost(account_id,symbol,total_cost_fen) "
            "VALUES('paper','600519',100000)"
        )
        connection.execute(
            "INSERT INTO paper_position_lot(lot_id,account_id,symbol,acquired_at,"
            "remaining_qty,total_cost_fen,source_fill_id,source_action_id) "
            "VALUES('formal-pre-record','paper','600519',?,100,100000,?,NULL)",
            (
                datetime(2026, 7, 16, 7, 0, tzinfo=ZoneInfo("UTC")).isoformat(),
                "pre-record-fill",
            ),
        )
    service = PaperOperationService(
        state,
        object_store,
        ledger,
        _CorporateActionFixture(action),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    settle = _operation_request(
        PaperSettlePayload(
            as_of=NOW,
            market=Market.XSHG,
            calendar_release_id="1" * 64,
            corporate_action_release_ids=["4" * 64],
        ),
        "settle-with-action",
    )
    confirmation = _confirmation(settle)
    report = service.execute(settle, confirmation)
    assert report.result["applied_action_ids"] == [action.observation_id]
    position = ledger.status("paper")["positions"][0]
    assert position["qty_total"] == 110
    assert position["qty_available"] == 110
    assert service.execute(settle, confirmation).result == report.result

    collision = action.model_copy(
        update={
            "structured_terms": {
                **action.structured_terms,
                "dividStocksPs": "0.2",
            }
        }
    )
    with pytest.raises(PolicyError, match="identity collision"):
        ledger.register_verified_corporate_action(collision, "4" * 64)

    fractional = action.model_copy(
        update={
            "observation_id": "c" * 64,
            "structured_terms": {
                **action.structured_terms,
                "dividStocksPs": "0.015",
            },
        }
    )
    fractional_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _CorporateActionFixture(fractional),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    fractional_settle = _operation_request(
        PaperSettlePayload(
            as_of=NOW,
            market=Market.XSHG,
            calendar_release_id="1" * 64,
            corporate_action_release_ids=["6" * 64],
        ),
        "settle-fractional-action",
    )
    with pytest.raises(PolicyError, match="Fractional"):
        fractional_service.execute(
            fractional_settle, _confirmation(fractional_settle)
        )
    assert ledger.status("paper")["positions"][0]["qty_total"] == 110

    ambiguous_cash = action.model_copy(
        update={
            "observation_id": "b" * 64,
            "action_type": "CASH_DIVIDEND_HINT",
            "structured_terms": {
                **action.structured_terms,
                "dividCashPsBeforeTax": "1.0",
                "dividStocksPs": "0",
            },
        }
    )
    cash_service = PaperOperationService(
        state,
        object_store,
        ledger,
        _CorporateActionFixture(ambiguous_cash),
        load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        clock=lambda: NOW + timedelta(minutes=2),
        **_SERVICE_SECURITY,
    )
    cash_settle = _operation_request(
        PaperSettlePayload(
            as_of=NOW,
            market=Market.XSHG,
            calendar_release_id="1" * 64,
            corporate_action_release_ids=["5" * 64],
        ),
        "settle-ambiguous-cash",
    )
    with pytest.raises(PolicyError, match="after-tax"):
        cash_service.execute(cash_settle, _confirmation(cash_settle))
    with state.connect() as connection:
        assert connection.execute(
            "SELECT status FROM paper_operation_execution WHERE operation_id=?",
            (cash_settle.operation_id,),
        ).fetchone()[0] == "NEEDS_INFO"
        assert connection.execute(
            "SELECT COUNT(*) FROM corporate_action_event WHERE event_id=?",
            (ambiguous_cash.observation_id,),
        ).fetchone()[0] == 0

    recover = _operation_request(PaperRecoverPayload(as_of=NOW), "recover-after-action")
    recovered = service.execute(recover, _confirmation(recover))
    assert recovered.result["status"] == "HEALTHY_NOOP", recovered.result


def test_paper_operation_supports_windows_chinese_paths(tmp_path: Path) -> None:
    state = StateStore(
        tmp_path / "模拟交易" / "状态.sqlite", PROJECT_ROOT / "migrations"
    )
    state.migrate()
    objects = ObjectStore(tmp_path / "模拟交易" / "对象" / "sha256")
    service, ledger = _service(state, objects)
    request = _request()
    service.execute(request, _confirmation(request))
    assert len(ledger.open_orders("paper")) == 1
