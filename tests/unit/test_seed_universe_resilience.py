from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from astock.candidates.seeds import (
    ResearchSeedProviderRouter,
    _market_coverage_reconciliation,
    _seed_payload_coverage_ratio,
)
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.config import load_provider_registry
from astock.schemas import FetchStatus, Market, SourceSnapshot
from astock.schemas.universe_coverage import (
    UniverseCoverageLevel,
    UniverseDenominatorAuthority,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 10, 5, 30, tzinfo=UTC)


def _snapshot(
    state: StateStore,
    objects: ObjectStore,
    *,
    source_id: str,
    label: str,
    payload: dict[str, object],
) -> SourceSnapshot:
    ref = objects.put_json(payload)
    snapshot = SourceSnapshot(
        snapshot_id=f"{source_id}:{label}:{ref.sha256}",
        source_id=source_id,
        object_sha256=ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        source_url=f"https://example.invalid/{source_id}/{label}",
        mime="application/json",
        byte_size=ref.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_RESEARCH_FIXTURE",
    )
    state.register_snapshot(snapshot)
    return snapshot


class _FailingSeedProvider:
    provider_id = "failing-seed"

    def fetch_seed_snapshot(self, market: Market, *, live: bool = False):  # type: ignore[no-untyped-def]
        del market, live
        raise ValueError("seed source unavailable")


class _FailingCoverageProvider:
    provider_id = "sse-official-reference"

    def fetch_master(self, market: Market, *, live: bool = False):  # type: ignore[no-untyped-def]
        del market, live
        raise ValueError("official master temporarily unavailable")


class _CoverageProvider:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        provider_id: str,
        payload: dict[str, object],
    ) -> None:
        self.provider_id = provider_id
        self.state = state
        self.objects = objects
        self.payload = payload
        self.calls = 0

    def fetch_master(
        self,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        del market, live
        self.calls += 1
        return self.payload, _snapshot(
            self.state,
            self.objects,
            source_id=self.provider_id,
            label=f"master-{self.calls}",
            payload=self.payload,
        )


class _QuoteFallback:
    provider_id = "tencent-reference"

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.requested: tuple[str, ...] = ()

    def fetch_seed_snapshot_for_symbols(
        self,
        market: Market,
        symbols: Sequence[str],
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        assert live
        self.requested = tuple(symbols)
        prefix = {Market.XSHG: "sh", Market.XSHE: "sz", Market.BJSE: "bj"}[market]
        rows = [
            {
                "symbol": f"{prefix}{code}",
                "code": code,
                "name": f"测试{index}",
                "trade": 10.0 + index,
                "settlement": 9.8 + index,
                "amount": 1_000_000_000.0 + index,
                "turnoverratio": 2.0,
                "float_market_cap_cny": 20_000_000_000.0 + index,
            }
            for index, code in enumerate(symbols)
        ]
        payload: dict[str, object] = {
            "schema_version": "tencent-quote-batch-v1",
            "_astock_source": "TENCENT_QUOTE_BATCH",
            "_astock_request": {
                "market": market.value,
                "purpose": "RESEARCH_SEED_ONLY",
            },
            "requested_symbol_count": len(symbols),
            "returned_symbol_count": len(symbols),
            "complete": True,
            "rows": rows,
        }
        return payload, _snapshot(
            self.state,
            self.objects,
            source_id=self.provider_id,
            label=f"quote-{market.value}",
            payload=payload,
        )


def _official_payload(market: Market, codes: list[str]) -> dict[str, object]:
    source = {
        Market.XSHG: "sse-official-reference",
        Market.XSHE: "szse-official-reference",
        Market.BJSE: "BSE_OFFICIAL_LIST",
    }[market]
    return {
        "schema_version": "exchange-official-master-v1",
        "_astock_source": source,
        "_astock_request": {"market": market.value, "purpose": "INSTRUMENT_MASTER"},
        "complete": True,
        "total": len(codes),
        "coverage_denominator": len(codes),
        "rows": [{"code": code, "name": f"股票{index}"} for index, code in enumerate(codes)],
    }


def _secondary_payload(market: Market, codes: list[str]) -> dict[str, object]:
    return {
        "_astock_request": {"market": market.value, "purpose": "INSTRUMENT_MASTER"},
        "data": {
            "total": len(codes),
            "diff": [
                {"f12": code, "f13": 1, "f14": f"股票{index}"}
                for index, code in enumerate(codes)
            ],
        },
    }


def _state(tmp_path: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state, ObjectStore(tmp_path / "objects")


def test_official_master_drives_tencent_quote_fallback_after_seed_sources_fail(
    tmp_path: Path,
) -> None:
    state, objects = _state(tmp_path)
    codes = ["600001", "600002"]
    coverage = _CoverageProvider(
        state,
        objects,
        provider_id="sse-official-reference",
        payload=_official_payload(Market.XSHG, codes),
    )
    quote = _QuoteFallback(state, objects)
    router = ResearchSeedProviderRouter(
        providers=[_FailingSeedProvider()],
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
        coverage_providers={Market.XSHG: [coverage]},
        quote_fallbacks=[quote],
    )

    payload, snapshot = router.fetch_seed_snapshot(Market.XSHG, live=True)

    assert quote.requested == tuple(codes)
    assert payload["coverage_denominator"] == 2
    assert payload["coverage_numerator"] == 2
    assert payload["coverage_proof_source_id"] == "sse-official-reference"
    assert _seed_payload_coverage_ratio(payload, Market.XSHG) == 1.0
    assert snapshot.source_id == "tencent-reference"

    reconciliation = _market_coverage_reconciliation(
        payload,
        snapshot,
        Market.XSHG,
        state=state,
        objects=objects,
        registry=load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml"),
    )
    assert reconciliation.coverage_level is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
    assert reconciliation.denominator_authority is UniverseDenominatorAuthority.PRIMARY_OFFICIAL


def test_secondary_complete_master_can_preserve_research_but_not_formal_full_authority(
    tmp_path: Path,
) -> None:
    state, objects = _state(tmp_path)
    codes = ["600001", "600002"]
    secondary = _CoverageProvider(
        state,
        objects,
        provider_id="eastmoney-reference",
        payload=_secondary_payload(Market.XSHG, codes),
    )
    quote = _QuoteFallback(state, objects)
    router = ResearchSeedProviderRouter(
        providers=[_FailingSeedProvider()],
        minimum_rows_by_market={Market.XSHG: 2, Market.XSHE: 2, Market.BJSE: 1},
        state=state,
        objects=objects,
        coverage_providers={Market.XSHG: [_FailingCoverageProvider(), secondary]},
        quote_fallbacks=[quote],
    )

    payload, snapshot = router.fetch_seed_snapshot(Market.XSHG, live=True)
    reconciliation = _market_coverage_reconciliation(
        payload,
        snapshot,
        Market.XSHG,
        state=state,
        objects=objects,
        registry=load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml"),
    )

    assert reconciliation.coverage_ratio == 1.0
    assert reconciliation.denominator_source_id == "eastmoney-reference"
    assert (
        reconciliation.denominator_authority
        is UniverseDenominatorAuthority.SECONDARY_SELF_REPORTED
    )
    assert reconciliation.coverage_level is UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
    assert "DECORATED_DENOMINATOR_NOT_OFFICIAL" in reconciliation.reason_codes
