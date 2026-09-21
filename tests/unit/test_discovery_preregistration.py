from __future__ import annotations

from datetime import UTC, date, datetime

from astock.candidates.discovery_evaluation import (
    DiscoveryEvaluationSplit,
    DiscoveryThesisFamily,
)
from astock.candidates.discovery_preregistration import (
    DiscoveryUniverseBinding,
    build_discovery_preregistration,
)
from astock.schemas.market import InstrumentType, Market
from astock.schemas.reference_data import InstrumentRecord


def _record(
    market: Market,
    symbol: str,
    *,
    tradable: bool = True,
    is_st: bool = False,
    delisted: bool = False,
    instrument_type: InstrumentType = InstrumentType.STOCK,
) -> InstrumentRecord:
    return InstrumentRecord(
        instrument_id=f"{market.value}:{symbol}",
        market=market,
        symbol=symbol,
        name=f"Company {symbol}",
        instrument_type=instrument_type,
        tradable=tradable,
        status_date=date(2026, 9, 13),
        is_st=is_st,
        listing_date=date(2020, 1, 1),
        delisting_date=date(2026, 9, 1) if delisted else None,
        source_snapshot_id=f"snapshot:{market.value}:{symbol}",
        available_to_system_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def _binding(scope: str, token: str) -> DiscoveryUniverseBinding:
    return DiscoveryUniverseBinding(
        scope_key=scope,
        release_id=token * 64,
        content_hash=("a" if token != "a" else "b") * 64,
        manifest_object_hash=("c" if token != "c" else "d") * 64,
        available_to_system_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def _universe() -> tuple[InstrumentRecord, ...]:
    rows = []
    for index in range(80):
        rows.append(_record(Market.XSHG, f"{600000 + index:06d}"))
    for index in range(100):
        rows.append(_record(Market.XSHE, f"{1 + index:06d}"))
    for index in range(20):
        rows.append(_record(Market.BJSE, f"{920000 + index:06d}"))
    return tuple(rows)


def test_preregistration_is_deterministic_balanced_and_label_free() -> None:
    bindings = tuple(
        sorted(
            (
                _binding("XSHG", "1"),
                _binding("XSHE", "2"),
                _binding("BJSE", "3"),
            ),
            key=lambda item: (
                item.scope_key,
                item.available_to_system_at,
                item.release_id,
            ),
        )
    )
    first = build_discovery_preregistration(
        records=_universe(),
        release_bindings=bindings,
        selection_seed="wp42-freeze-20260920-v1",
    )
    second = build_discovery_preregistration(
        records=tuple(reversed(_universe())),
        release_bindings=bindings,
        selection_seed="wp42-freeze-20260920-v1",
    )

    assert first == second
    assert first.sample_count == 60
    assert first.holdout_count == 20
    assert first.labels_included is False
    assert first.industry_labels_included is False
    assert dict(first.family_counts) == {
        family.value: 12 for family in DiscoveryThesisFamily
    }
    for family in DiscoveryThesisFamily:
        family_rows = [item for item in first.samples if item.thesis_family is family]
        assert len(family_rows) == 12
        assert sum(
            item.split is DiscoveryEvaluationSplit.HOLDOUT for item in family_rows
        ) == 4
    assert len({item.instrument_id for item in first.samples}) == 60
    assert len({item.sample_id for item in first.samples}) == 60


def test_preregistration_filters_ineligible_instruments() -> None:
    valid = tuple(
        _record(Market.XSHG, f"{600100 + index:06d}")
        for index in range(70)
    )
    invalid = (
        _record(Market.XSHG, "600001", tradable=False),
        _record(Market.XSHG, "600002", is_st=True),
        _record(Market.XSHG, "600003", delisted=True),
        _record(
            Market.XSHG,
            "000300",
            instrument_type=InstrumentType.INDEX,
            tradable=False,
        ),
    )
    bindings = (_binding("XSHG", "4"),)
    result = build_discovery_preregistration(
        records=(*valid, *invalid),
        release_bindings=bindings,
        selection_seed="filter-test",
    )
    selected = {item.instrument_id for item in result.samples}
    assert not selected.intersection(
        {
            "XSHG:600001",
            "XSHG:600002",
            "XSHG:600003",
            "XSHG:000300",
        }
    )


def test_preregistration_changes_when_universe_binding_changes() -> None:
    first = build_discovery_preregistration(
        records=_universe(),
        release_bindings=(_binding("XSHG", "5"),),
        selection_seed="same-seed",
    )
    second = build_discovery_preregistration(
        records=_universe(),
        release_bindings=(_binding("XSHG", "6"),),
        selection_seed="same-seed",
    )
    assert first.universe_hash != second.universe_hash
    assert first.protocol_id != second.protocol_id
