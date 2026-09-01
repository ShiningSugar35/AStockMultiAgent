from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.candidates.seeds import _market_coverage_reconciliation, _universe_coverage_proof
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.config import load_provider_registry
from astock.schemas import InstrumentRecord, InstrumentType
from astock.schemas.evidence import SourceSnapshot
from astock.schemas.market import Market
from astock.schemas.research_seeds import (
    ResearchSeedReport,
    ResearchSeedStatus,
    ResearchUniverseCoverageStatus,
)
from astock.schemas.universe_coverage import (
    MarketCoverageReconciliation,
    UniverseCoverageLevel,
    UniverseCoverageProof,
    UniverseDenominatorAuthority,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


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
        source_url=f"https://example.invalid/{label}",
        mime="application/json",
        byte_size=ref.byte_size,
        rights_status="PUBLIC_RESEARCH_FIXTURE",
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    return snapshot


def _bjse_market_payload(*, total: int = 1) -> dict[str, object]:
    rows = [
        {
            "f12": "920001",
            "f14": "北证甲",
            "f2": 10.0,
            "f6": 1_000_000.0,
            "f8": 2.0,
            "f21": 1_000_000_000.0,
        },
        {
            "f12": "920002",
            "f14": "北证乙",
            "f2": 11.0,
            "f6": 1_100_000.0,
            "f8": 2.1,
            "f21": 1_100_000_000.0,
        },
    ]
    return {
        "rc": 0,
        "data": {"total": total, "diff": rows[: max(total, 0)] if total != 1 else rows[:1]},
        "_astock_request": {"market": Market.BJSE.value, "purpose": "RESEARCH_SEED_ONLY"},
    }


def test_official_decorated_denominator_reconciles_original_derived_and_proof_snapshots(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")

    market_payload = _bjse_market_payload()
    market_snapshot = _snapshot(
        state,
        objects,
        source_id="fake-seed-provider",
        label="market",
        payload=market_payload,
    )
    proof_payload: dict[str, object] = {
        "_astock_source": "BSE_OFFICIAL_LIST",
        "_astock_request": {"market": Market.BJSE.value},
        "complete": True,
        "total": 1,
        "coverage_denominator": 1,
        "rows": [{"code": "920001", "name": "北证甲"}],
    }
    proof_snapshot = _snapshot(
        state,
        objects,
        source_id="bse-official-reference",
        label="denominator",
        payload=proof_payload,
    )
    decorated = {
        **market_payload,
        "coverage_denominator": 1,
        "coverage_numerator": 1,
        "market_snapshot_id": market_snapshot.snapshot_id,
        "market_snapshot_object_hash": market_snapshot.object_sha256,
        "coverage_proof_source_id": proof_snapshot.source_id,
        "coverage_proof_capability": "instrument.bjse_coverage",
        "coverage_proof_snapshot_ids": [proof_snapshot.snapshot_id],
        "coverage_proof_object_hashes": [proof_snapshot.object_sha256],
        "coverage_proof_complete": True,
    }
    derived_snapshot = _snapshot(
        state,
        objects,
        source_id="fake-seed-provider",
        label="official-covered",
        payload=decorated,
    )

    result = _market_coverage_reconciliation(
        decorated,
        derived_snapshot,
        Market.BJSE,
        state=state,
        objects=objects,
        registry=registry,
    )

    assert result.coverage_level is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
    assert result.denominator_authority is UniverseDenominatorAuthority.PRIMARY_OFFICIAL
    assert result.denominator_capability == "instrument.bjse_coverage"
    assert result.denominator_object_hash == proof_snapshot.object_sha256
    assert result.numerator_object_hash == market_snapshot.object_sha256
    assert result.source_snapshot_ids == sorted(
        [derived_snapshot.snapshot_id, market_snapshot.snapshot_id, proof_snapshot.snapshot_id]
    )
    assert result.coverage_ratio == 1.0
    assert result.missing_count == 0
    assert result.extra_count == 0


def test_decorated_proof_with_wrong_capability_fails_closed(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    market_payload = _bjse_market_payload()
    market_snapshot = _snapshot(
        state,
        objects,
        source_id="fake-seed-provider",
        label="market",
        payload=market_payload,
    )
    proof_payload: dict[str, object] = {
        "_astock_source": "BSE_OFFICIAL_LIST",
        "_astock_request": {"market": Market.BJSE.value},
        "complete": True,
        "total": 1,
        "coverage_denominator": 1,
        "rows": [{"code": "920001", "name": "北证甲"}],
    }
    proof_snapshot = _snapshot(
        state,
        objects,
        source_id="bse-official-reference",
        label="denominator",
        payload=proof_payload,
    )
    decorated = {
        **market_payload,
        "coverage_denominator": 1,
        "coverage_numerator": 1,
        "market_snapshot_id": market_snapshot.snapshot_id,
        "market_snapshot_object_hash": market_snapshot.object_sha256,
        "coverage_proof_source_id": proof_snapshot.source_id,
        "coverage_proof_capability": "instrument.master",
        "coverage_proof_snapshot_ids": [proof_snapshot.snapshot_id],
        "coverage_proof_object_hashes": [proof_snapshot.object_sha256],
        "coverage_proof_complete": True,
    }
    derived_snapshot = _snapshot(
        state,
        objects,
        source_id="fake-seed-provider",
        label="wrong-capability",
        payload=decorated,
    )

    result = _market_coverage_reconciliation(
        decorated,
        derived_snapshot,
        Market.BJSE,
        state=state,
        objects=objects,
        registry=registry,
    )

    assert result.coverage_level is UniverseCoverageLevel.PARTIAL
    assert result.denominator_count is None
    assert "COVERAGE_PROOF_LINEAGE_MALFORMED" in result.reason_codes


def test_rows_exceeding_self_reported_denominator_are_audited_as_partial(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    payload = _bjse_market_payload(total=2)
    data = payload["data"]
    assert isinstance(data, dict)
    data["total"] = 1
    snapshot = _snapshot(
        state,
        objects,
        source_id="fake-seed-provider",
        label="extra-row",
        payload=payload,
    )

    result = _market_coverage_reconciliation(
        payload,
        snapshot,
        Market.BJSE,
        state=state,
        objects=objects,
        registry=registry,
    )

    assert result.coverage_level is UniverseCoverageLevel.PARTIAL
    assert result.numerator_count == 2
    assert result.denominator_count == 1
    assert result.missing_count == 0
    assert result.extra_count == 1
    assert "MARKET_UNIVERSE_EXTRA_ROWS" in result.reason_codes


def test_legacy_v1_full_boolean_is_downgraded_without_typed_proof() -> None:
    report = ResearchSeedReport.model_validate(
        {
            "schema_version": "research-seed-report-v1",
            "report_id": "legacy-engineering-full",
            "as_of": NOW,
            "data_cutoff_at": NOW,
            "status": "EMPTY",
            "profiles": [],
            "seeds": [],
            "source_snapshot_ids": [],
            "source_object_hashes": [],
            "warning_codes": [],
            "market_coverage_ratios": {"XSHG": 1.0, "XSHE": 1.0, "BJSE": 1.0},
            "universe_coverage_status": "FULL",
            "formal_full_market_coverage_allowed": True,
            "market_seed_count": 0,
            "expert_seed_count": 0,
            "existing_candidate_seed_count": 0,
        }
    )

    assert report.universe_coverage_level is UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
    assert not report.formal_full_market_coverage_allowed


def _official_reconciliation(market: Market, digit: str) -> MarketCoverageReconciliation:
    capability = "instrument.bjse_coverage" if market is Market.BJSE else "instrument.master"
    return MarketCoverageReconciliation(
        market=market,
        coverage_level=UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
        denominator_source_id=f"official-{market.value.lower()}",
        denominator_capability=capability,
        denominator_authority=UniverseDenominatorAuthority.PRIMARY_OFFICIAL,
        denominator_count=1,
        numerator_count=1,
        missing_count=0,
        extra_count=0,
        coverage_ratio=1.0,
        source_version="official-master-v1",
        observed_at=NOW,
        available_to_system_at=NOW,
        denominator_object_hash=digit * 64,
        numerator_object_hash=str((int(digit) + 3) % 10) * 64,
        source_snapshot_ids=[f"snapshot-{market.value.lower()}"],
        created_at=NOW,
    )


def test_typed_all_market_official_proof_is_the_only_formal_full_path() -> None:
    reconciliations = [
        _official_reconciliation(Market.BJSE, "1"),
        _official_reconciliation(Market.XSHE, "3"),
        _official_reconciliation(Market.XSHG, "2"),
    ]
    proof = _universe_coverage_proof(
        as_of=NOW,
        reconciliations={item.market: item for item in reconciliations},
    )
    source_snapshot_ids = sorted(
        snapshot_id for item in reconciliations for snapshot_id in item.source_snapshot_ids
    )
    source_object_hashes = sorted(
        {
            object_hash
            for item in reconciliations
            for object_hash in (item.denominator_object_hash, item.numerator_object_hash)
            if object_hash is not None
        }
    )

    report = ResearchSeedReport(
        report_id="typed-official-full",
        as_of=NOW,
        data_cutoff_at=NOW,
        status=ResearchSeedStatus.EMPTY,
        profiles=[],
        seeds=[],
        source_snapshot_ids=source_snapshot_ids,
        source_object_hashes=source_object_hashes,
        warning_codes=[],
        market_coverage_ratios={market: 1.0 for market in (Market.XSHG, Market.XSHE, Market.BJSE)},
        universe_coverage_proof=proof,
        universe_coverage_status=ResearchUniverseCoverageStatus.FULL,
        market_seed_count=0,
        expert_seed_count=0,
        existing_candidate_seed_count=0,
        created_at=NOW,
    )

    assert report.formal_full_market_coverage_allowed
    assert report.universe_coverage_level is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED


def test_universe_coverage_proof_rejects_tampered_identity() -> None:
    reconciliations = {
        market: _official_reconciliation(market, digit)
        for market, digit in (
            (Market.XSHG, "2"),
            (Market.XSHE, "3"),
            (Market.BJSE, "1"),
        )
    }
    proof = _universe_coverage_proof(as_of=NOW, reconciliations=reconciliations)
    payload = proof.model_dump(mode="python")
    payload["proof_id"] = "f" * 64 if proof.proof_id != "f" * 64 else "e" * 64

    with pytest.raises(ValidationError, match="proof id"):
        UniverseCoverageProof.model_validate(payload)


def test_universe_coverage_proof_rejects_market_evidence_available_after_as_of() -> None:
    reconciliations = {
        market: _official_reconciliation(market, digit)
        for market, digit in (
            (Market.XSHG, "2"),
            (Market.XSHE, "3"),
            (Market.BJSE, "1"),
        )
    }
    reconciliations[Market.XSHG] = reconciliations[Market.XSHG].model_copy(
        update={"available_to_system_at": NOW + timedelta(seconds=1)}
    )

    with pytest.raises(ValidationError, match="cannot predate a market proof"):
        _universe_coverage_proof(as_of=NOW, reconciliations=reconciliations)


def test_formal_level_rejects_secondary_authority() -> None:
    with pytest.raises(ValidationError, match="official/authorized denominator"):
        MarketCoverageReconciliation(
            market=Market.BJSE,
            coverage_level=UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
            denominator_source_id="secondary",
            denominator_capability="instrument.bjse_coverage",
            denominator_authority=UniverseDenominatorAuthority.SECONDARY_SELF_REPORTED,
            denominator_count=1,
            numerator_count=1,
            missing_count=0,
            extra_count=0,
            coverage_ratio=1.0,
            source_version="v1",
            observed_at=NOW,
            available_to_system_at=NOW,
            denominator_object_hash="1" * 64,
            numerator_object_hash="2" * 64,
            source_snapshot_ids=["snapshot-secondary"],
            created_at=NOW,
        )


def test_official_complete_flag_cannot_self_manufacture_denominator(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    raw_snapshot = _snapshot(
        state,
        objects,
        source_id="bse-official-reference",
        label="complete-without-denominator",
        payload={
            "_astock_source": "BSE_OFFICIAL_LIST",
            "_astock_request": {
                "market": Market.BJSE.value,
                "purpose": "INSTRUMENT_MASTER",
            },
            "complete": True,
            "rows": [{"code": "920001", "name": "北证甲"}],
        },
    )
    service = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    record = InstrumentRecord(
        instrument_id="BJSE:920001",
        market=Market.BJSE,
        symbol="920001",
        name="北证甲",
        instrument_type=InstrumentType.STOCK,
        tradable=True,
        status_date=NOW.date(),
        is_st=False,
        source_snapshot_id=raw_snapshot.snapshot_id,
        available_to_system_at=NOW,
        created_at=NOW,
    )

    reconciliations = service._instrument_master_reconciliations(
        scope_key=Market.BJSE.value,
        provider_id="bse-official-reference",
        raw_snapshot_ids=[raw_snapshot.snapshot_id],
        records=[record],
        available_at=NOW,
        canonical_object_hash="a" * 64,
    )

    assert len(reconciliations) == 1
    result = reconciliations[0]
    assert result.coverage_level is UniverseCoverageLevel.PARTIAL
    assert result.denominator_count is None
    assert result.denominator_object_hash is None
    assert result.denominator_authority is UniverseDenominatorAuthority.UNKNOWN
    assert "DENOMINATOR_UNAVAILABLE" in result.reason_codes
