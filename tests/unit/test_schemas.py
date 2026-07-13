from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from astock.core.hashing import content_hash
from astock.schemas import (
    AdjustmentMode,
    AuthorCollectionCoverageReport,
    BarRequest,
    CollectionTerminalCondition,
    CoverageStatus,
    LedgerEntry,
    Market,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_content_hash_ignores_created_at() -> None:
    values = {
        "symbol": "600519",
        "market": Market.XSHG,
        "requested_start": datetime(2026, 7, 1, tzinfo=SHANGHAI),
        "requested_end": datetime(2026, 7, 2, tzinfo=SHANGHAI),
        "adjustment_mode": AdjustmentMode.NONE,
    }
    first = BarRequest(**values, created_at=datetime(2026, 7, 1, tzinfo=UTC))
    second = BarRequest(**values, created_at=datetime(2026, 7, 2, tzinfo=UTC))
    assert first.created_at != second.created_at
    assert content_hash(first) == content_hash(second)


def test_bar_request_rejects_reversed_range() -> None:
    with pytest.raises(ValidationError):
        BarRequest(
            symbol="600519",
            market=Market.XSHG,
            requested_start=datetime(2026, 7, 2, tzinfo=SHANGHAI),
            requested_end=datetime(2026, 7, 1, tzinfo=SHANGHAI),
        )


def test_ledger_entry_requires_exactly_one_side() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(
            entry_id="entry",
            event_id="event",
            account_id="cash",
            debit_fen=1,
            credit_fen=1,
            occurred_at=datetime.now(SHANGHAI),
        )


def test_author_coverage_distinguishes_empty_from_failure() -> None:
    report = AuthorCollectionCoverageReport(
        author_id="mr-dang-77",
        content_type="answers",
        discovered_count=0,
        scheduled_count=0,
        success_count=0,
        failed_count=0,
        restricted_count=0,
        skipped_duplicate_count=0,
        updated_count=0,
        missing_count=0,
        terminal_condition=CollectionTerminalCondition.CONFIRMED_EMPTY,
        coverage_status=CoverageStatus.COMPLETE,
    )
    assert report.terminal_condition is CollectionTerminalCondition.CONFIRMED_EMPTY
