from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.dialects import load_provider_dialects
from astock.providers.financial_base import FinancialRawCaptureError
from astock.providers.sina_financial import SinaFinancialProvider
from astock.providers.symbols import baostock_code, eastmoney_secid, sina_symbol
from astock.schemas import BarRequest, InstrumentType, Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state


def test_unknown_financial_dialect_fails_after_raw_snapshot_is_persisted(tmp_path: Path) -> None:
    state = _state(tmp_path)
    objects = ObjectStore(tmp_path / "objects")
    dialect = load_provider_dialects(PROJECT_ROOT / "configs" / "provider_dialects.yaml")[
        "sina-financial"
    ]
    unknown = replace(dialect, response_shape="future-sina-shape-v99")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "status": {"code": 0},
                    "data": {"report_list": {}},
                }
            },
            request=request,
        )

    provider = SinaFinancialProvider(
        objects,
        state,
        PROJECT_ROOT / "tests" / "fixtures" / "financial_sources",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dialect=unknown,
    )

    with pytest.raises(FinancialRawCaptureError) as error:
        provider.fetch("600989", Market.XSHG, date(2025, 12, 31), live=True)

    assert error.value.failure_code == "FINANCIAL_DIALECT_UNRECOGNIZED"
    assert len(error.value.snapshots) == 1
    snapshot = error.value.snapshots[0]
    assert state.get_snapshot(snapshot.snapshot_id) is not None
    assert objects.verify(snapshot.object_sha256)


def test_index_exchange_is_separate_from_instrument_type_and_prefix_is_only_request_hint() -> None:
    now = datetime(2026, 8, 13, 8, tzinfo=UTC)
    request = BarRequest(
        symbol="000300",
        market=Market.INDEX,
        exchange=Market.XSHE,
        instrument_type=InstrumentType.INDEX,
        requested_start=now,
        requested_end=now,
    )

    assert request.market is Market.INDEX
    assert request.exchange is Market.XSHE
    assert request.instrument_type is InstrumentType.INDEX
    assert eastmoney_secid(request) == "0.000300"
    assert sina_symbol(request) == "sz000300"
    assert baostock_code("000300", Market.INDEX, exchange=Market.XSHE) == "sz.000300"

    # Missing formal exchange may use a provider request hint, but the request identity
    # remains INDEX and no InstrumentRecord is created from this heuristic.
    hinted = request.model_copy(update={"symbol": "399001", "exchange": None})
    assert hinted.market is Market.INDEX
    assert eastmoney_secid(hinted) == "0.399001"
