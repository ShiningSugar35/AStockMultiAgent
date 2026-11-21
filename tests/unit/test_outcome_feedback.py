from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astock.market_data.official_calendar import (
    OfficialCalendarSource,
    OfficialCalendarYear,
    OfficialTradingCalendarConfig,
    OfficialTradingCalendarPolicy,
)
from astock.research.debate_evaluation import QualityError, load_quality_policy
from astock.research.outcome_feedback import evaluate_outcome
from astock.schemas.reference_data import DailyBarObservation

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 2, 8, tzinfo=UTC)


def calendar():
    from astock.schemas.market import Market

    source = OfficialCalendarSource("exchange", "notice", "https://www.sse.com.cn/notice")
    year = OfficialCalendarYear(
        2026,
        NOW - timedelta(days=300),
        NOW - timedelta(days=299),
        {Market.XSHG: source, Market.XSHE: source, Market.BJSE: source},
        (),
    )
    return OfficialTradingCalendarConfig(
        "official-trading-calendar-v1",
        "CN_A_SHARE",
        OfficialTradingCalendarPolicy(
            "LOCAL_OFFICIAL_FIRST", True, True, True, "SEARCH_OFFICIAL_THEN_API_FALLBACK"
        ),
        {2026: year},
    )


class ReceiptProjection(SimpleNamespace):
    """Algorithm fixture, explicitly not a registrable research receipt.

    CLI authentication is tested independently; this fixture intentionally tests
    only the calculation against the fields it consumes.
    """

    def model_dump(self, **kwargs):
        return {
            "receipt_id": self.receipt_id,
            "receipt_hash": self.receipt_hash,
            "account_id": self.request_contract.account_id,
            "as_of": self.as_of.isoformat(),
            "published": self.publication.formal_recommendation_allowed,
        }


def receipt() -> Any:
    node = SimpleNamespace(
        node=SimpleNamespace(value="PUBLICATION_GATE"),
        status=SimpleNamespace(value="PASS"),
        completed_at=NOW,
    )
    return ReceiptProjection(
        receipt_id="r",
        receipt_hash="b" * 64,
        request_contract=SimpleNamespace(account_id=None),
        as_of=NOW,
        holding_reviews=(),
        publication=SimpleNamespace(formal_recommendation_allowed=True, status="PUBLISH"),
        dag=SimpleNamespace(publication_ready=True, executions=(node,)),
        candidate_rankings=(SimpleNamespace(instrument_id="XSHG:600000", eligible=True),),
        portfolio=SimpleNamespace(positions=(SimpleNamespace(instrument_id="XSHG:600000"),)),
    )


def bar(day, identity="XSHG:600000", close="11", volume="100"):
    from datetime import date

    from astock.schemas.market import Market

    day = date.fromisoformat(day)
    market, symbol = identity.split(":")
    stamp = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
    return DailyBarObservation.model_validate(
        dict(
            created_at=stamp,
            observation_id=(symbol[0] + str(day.day % 10)) * 32,
            instrument_id=identity,
            market=Market(market),
            symbol=symbol,
            session_date=day,
            session_close_at=stamp,
            open="10",
            high=max(Decimal(close), Decimal("12")),
            low="9",
            close=close,
            volume=volume,
            source_snapshot_id="s",
            available_to_system_at=stamp,
        )
    )


def run(stock=None, benchmark=None, **changes):
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    stock = (
        stock
        if stock is not None
        else tuple(bar("2026-11-" + str(d).zfill(2)) for d in (3, 4, 5, 6, 9))
    )
    benchmark = (
        benchmark
        if benchmark is not None
        else tuple(bar("2026-11-" + str(d).zfill(2), "XSHG:000001", "10") for d in (3, 4, 5, 6, 9))
    )
    args: dict[str, Any] = dict(
        evaluated_at=datetime(2026, 11, 9, 8, tzinfo=UTC),
        holding_sessions=5,
        context_id="feedback",
        corporate_action_dates=set(),
    )
    args.update(changes)
    return evaluate_outcome(
        receipt(), "XSHG:600000", "XSHG:000001", stock, benchmark, calendar(), policy, **args
    )


def test_next_open_horizon_and_reference_semantics():
    result = run()
    assert result["status"] == "MATURE"
    assert result["entry_reference_time"].startswith("2026-11-03T09:30")
    assert Decimal(result["gross_price_return"]) == Decimal(".1")
    assert Decimal(result["maximum_adverse_excursion"]) == Decimal("-.1")
    assert result["actual_trade_profit"] is None
    assert result["net_return"] is None
    assert result["ledger_write_allowed"] is False


def test_immaturity_missing_benchmark_and_missing_stock():
    assert run(evaluated_at=datetime(2026, 11, 6, 8, tzinfo=UTC))["status"] == "IMMATURE"
    assert run(benchmark=())["status"] == "MISSING_BENCHMARK"
    assert run(stock=())["status"] == "MISSING_BARS"
    wrong_dates = tuple(bar("2026-11-" + str(d).zfill(2), "XSHG:000001") for d in (2, 3, 4, 5, 6))
    assert run(benchmark=wrong_dates)["status"] == "MISSING_BENCHMARK"


def test_future_bars_do_not_change_earlier_feedback():
    stock = tuple(bar("2026-11-" + str(d).zfill(2)) for d in (3, 4, 5, 6, 9))
    assert run(stock=stock) == run(stock=(*stock, bar("2026-11-10", close="20")))


def test_suspension_and_identity_mismatch():
    stock = tuple(
        bar("2026-11-" + str(d).zfill(2), volume="0" if d == 4 else "100") for d in (3, 4, 5, 6, 9)
    )
    assert run(stock=stock)["status"] == "SUSPENDED_OR_NO_TRADE"
    with pytest.raises(QualityError, match="SECURITY_MISMATCH"):
        run(stock=(bar("2026-11-03", "XSHG:600001"),))


def test_unpublished_watch_and_conditional_are_not_performance():
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    item = receipt()
    item.publication.formal_recommendation_allowed = False
    args: dict[str, Any] = dict(
        evaluated_at=NOW + timedelta(days=8), holding_sessions=5, context_id="x"
    )
    assert (
        evaluate_outcome(item, "XSHG:600000", "XSHG:000001", (), (), calendar(), policy, **args)[
            "status"
        ]
        == "UNPUBLISHED"
    )
    item.publication.formal_recommendation_allowed = True
    item.portfolio.positions = ()
    assert (
        evaluate_outcome(item, "XSHG:600000", "XSHG:000001", (), (), calendar(), policy, **args)[
            "status"
        ]
        == "NOT_A_PUBLISHED_LONG_RECOMMENDATION"
    )
    item = receipt()
    item.publication.status = "CONDITIONAL_ONLY"
    assert (
        evaluate_outcome(item, "XSHG:600000", "XSHG:000001", (), (), calendar(), policy, **args)[
            "status"
        ]
        == "CONDITIONAL_ENTRY_NOT_EVALUATED"
    )
