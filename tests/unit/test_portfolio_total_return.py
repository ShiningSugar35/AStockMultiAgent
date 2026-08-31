from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from astock.portfolio.total_return import build_total_return_research_series
from astock.schemas.market import Market
from astock.schemas.reference_data import CorporateActionObservation, CorporateActionStatus


def _action(
    *,
    ex_date: date,
    available_at: datetime,
    status: CorporateActionStatus = CorporateActionStatus.TERMS_VERIFIED,
    terms: dict[str, str],
    suffix: str = "1",
) -> CorporateActionObservation:
    linked = status is not CorporateActionStatus.DISCOVERED_STRUCTURED
    official_snapshot_id = f"official:{suffix}" if linked else None
    official_url = "https://example.invalid/official" if linked else None
    official_announcement_id = f"announcement:{suffix}" if linked else None
    return CorporateActionObservation(
        observation_id=(suffix * 64)[:64],
        instrument_id="XSHG:600000",
        market=Market.XSHG,
        symbol="600000",
        action_type="DIVIDEND",
        announcement_date=ex_date - timedelta(days=10),
        ex_date=ex_date,
        status=status,
        structured_terms=terms,
        source_snapshot_id=f"snapshot:{suffix}",
        available_to_system_at=available_at,
        official_document_snapshot_id=official_snapshot_id,
        official_document_url=official_url,
        official_announcement_id=official_announcement_id,
    )


def test_cash_dividend_does_not_create_false_economic_loss() -> None:
    as_of = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)
    action = _action(
        ex_date=date(2026, 6, 2),
        available_at=as_of - timedelta(days=5),
        terms={"dividCashPsBeforeTax": "10"},
    )
    series = build_total_return_research_series(
        {"2026-06-01": 100.0, "2026-06-02": 90.0, "2026-06-03": 91.0},
        [action],
        as_of=as_of,
    )
    assert series.closes_by_date["2026-06-02"] == 100.0
    assert abs(series.closes_by_date["2026-06-03"] / 100.0 - 91.0 / 90.0) < 1e-12
    assert series.applied_action_ids == (action.observation_id,)


def test_stock_dividend_adjusts_share_count_in_total_return_series() -> None:
    as_of = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)
    action = _action(
        ex_date=date(2026, 6, 2),
        available_at=as_of - timedelta(days=5),
        terms={"dividStocksPs": "0.1"},
    )
    series = build_total_return_research_series(
        {"2026-06-01": 100.0, "2026-06-02": 100.0 / 1.1},
        [action],
        as_of=as_of,
    )
    assert abs(series.closes_by_date["2026-06-02"] - 100.0) < 1e-9


def test_future_available_action_cannot_change_earlier_portfolio_history() -> None:
    earlier = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
    later = earlier + timedelta(days=10)
    action = _action(
        ex_date=date(2026, 6, 2),
        available_at=later,
        terms={"dividCashPsBeforeTax": "10"},
    )
    raw = {"2026-06-01": 100.0, "2026-06-02": 90.0}
    earlier_series = build_total_return_research_series(raw, [action], as_of=earlier)
    later_series = build_total_return_research_series(raw, [action], as_of=later)
    assert earlier_series.closes_by_date == raw
    assert later_series.closes_by_date["2026-06-02"] == 100.0


def test_unverified_action_is_never_applied() -> None:
    as_of = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)
    action = _action(
        ex_date=date(2026, 6, 2),
        available_at=as_of - timedelta(days=5),
        status=CorporateActionStatus.DISCOVERED_STRUCTURED,
        terms={"dividCashPsBeforeTax": "10"},
    )
    raw = {"2026-06-01": 100.0, "2026-06-02": 90.0}
    series = build_total_return_research_series(raw, [action], as_of=as_of)
    assert series.closes_by_date == raw
    assert series.warning_codes == ("UNVERIFIED_CORPORATE_ACTION_NOT_APPLIED",)
