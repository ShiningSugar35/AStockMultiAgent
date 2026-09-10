from __future__ import annotations

from pathlib import Path

import pytest

from astock.candidates.seeds import _sina_activity_unavailable
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.tencent_reference import TencentReferenceProvider
from astock.schemas import Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _quote_line(prefix: str, code: str, name: str = "TestCo") -> str:
    fields = [""] * 60
    fields[0] = "1"
    fields[1] = name
    fields[2] = code
    fields[3] = "12.34"
    fields[4] = "12.00"
    fields[30] = "20260910130000"
    fields[35] = "12.34/1000/12340000"
    fields[38] = "2.50"
    fields[44] = "88.80"
    return f'v_{prefix}{code}="{"~".join(fields)}";'


def test_tencent_batch_parser_supports_sh_sz_bj_and_required_seed_metrics() -> None:
    cases = [
        (Market.XSHG, "sh", "600001"),
        (Market.XSHE, "sz", "000001"),
        (Market.BJSE, "bj", "920001"),
    ]
    for market, prefix, code in cases:
        rows = TencentReferenceProvider._parse_batch(
            _quote_line(prefix, code),
            market=market,
            requested={code},
        )
        assert rows == [
            {
                "symbol": f"{prefix}{code}",
                "code": code,
                "name": "TestCo",
                "trade": 12.34,
                "settlement": 12.0,
                "amount": 12_340_000.0,
                "turnoverratio": 2.5,
                "float_market_cap_cny": 8_880_000_000.0,
                "quote_time": "2026-09-10T05:00:00+00:00",
            }
        ]


def test_tencent_parser_rejects_cross_market_and_unrequested_symbols() -> None:
    with pytest.raises(ValueError, match="market/symbol provenance mismatch"):
        TencentReferenceProvider._parse_batch(
            _quote_line("sz", "000001"),
            market=Market.XSHG,
            requested={"000001"},
        )
    with pytest.raises(ValueError, match="market/symbol provenance mismatch"):
        TencentReferenceProvider._parse_batch(
            _quote_line("sh", "600001"),
            market=Market.XSHG,
            requested={"600002"},
        )


def test_tencent_blank_quote_is_coverage_miss_not_batch_corruption() -> None:
    text = '\n'.join(
        [
            _quote_line("sh", "600001"),
            'v_sh600002="";',
        ]
    )
    rows = TencentReferenceProvider._parse_batch(
        text,
        market=Market.XSHG,
        requested={"600001", "600002"},
    )
    assert [row["code"] for row in rows] == ["600001"]


def test_tencent_premarket_zero_activity_uses_settlement_proxy() -> None:
    row: dict[str, object] = {
        "symbol": "sh600001",
        "code": "600001",
        "name": "TestCo",
        "trade": 0.0,
        "settlement": 12.0,
        "amount": 0.0,
        "turnoverratio": 0.0,
        "float_market_cap_cny": 8_880_000_000.0,
    }
    payload: dict[str, object] = {
        "_astock_source": "TENCENT_QUOTE_BATCH",
        "_astock_request": {"market": "XSHG", "purpose": "RESEARCH_SEED_ONLY"},
        "rows": [row],
    }

    assert _sina_activity_unavailable(payload)
    row["trade"] = 12.1
    assert not _sina_activity_unavailable(payload)


def test_recorded_tencent_provider_persists_normalized_lineage(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    provider = TencentReferenceProvider(
        objects,
        state,
        fixture_root=PROJECT_ROOT / "tests" / "fixtures" / "reference" / "tencent",
    )

    payload, snapshot = provider.fetch_identity("600519", Market.XSHG, live=False)

    assert payload["_astock_source"] == "TENCENT_QUOTE_BATCH"
    assert payload["requested_symbol_count"] == 1
    assert payload["returned_symbol_count"] == 1
    assert payload["complete"] is True
    assert snapshot.source_id == "tencent-reference"
    assert state.get_snapshot(snapshot.snapshot_id) is not None
    assert objects.verify(snapshot.object_sha256)
    raw_ids = payload["raw_snapshot_ids"]
    assert isinstance(raw_ids, list) and len(raw_ids) == 1
    raw = state.get_snapshot(str(raw_ids[0]))
    assert raw is not None and raw.source_id == "tencent-reference"
    assert objects.verify(raw.object_sha256)
