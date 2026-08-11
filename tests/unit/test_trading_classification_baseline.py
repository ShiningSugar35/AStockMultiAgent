from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import FetchStatus, Market, SourceSnapshot
from astock.schemas.research_runtime import TradingClassificationCorporateActionBaseline

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)


def _snapshot(
    state: StateStore,
    objects: ObjectStore,
    *,
    source_id: str = "cninfo-disclosures:index",
    available_at: datetime = NOW - timedelta(minutes=2),
) -> SourceSnapshot:
    ref = objects.put_json({"announcements": [], "source": source_id})
    snapshot = SourceSnapshot(
        snapshot_id=f"{source_id}:{ref.sha256}",
        source_id=source_id,
        object_sha256=ref.sha256,
        fetched_at=available_at,
        available_to_system_at=available_at,
        source_url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        mime="application/json",
        byte_size=ref.byte_size,
        headers_hash=content_hash({"source": source_id}),
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_DISCLOSURE",
    )
    state.register_snapshot(snapshot)
    return snapshot


def _baseline(
    snapshot: SourceSnapshot,
    *,
    baseline_id: str,
) -> TradingClassificationCorporateActionBaseline:
    return TradingClassificationCorporateActionBaseline(
        baseline_id=baseline_id,
        company_id="300750",
        market=Market.XSHE,
        symbol="300750",
        as_of=NOW - timedelta(minutes=1),
        window_start="2026-06-12",
        window_end="2026-07-27",
        reference_status="OFFICIAL_ENUMERATION_COMPLETE",
        raw_snapshot_ids=[snapshot.snapshot_id],
        official_query_snapshot_ids=[snapshot.snapshot_id],
        candidate_announcement_ids=[],
        observed_record_count=0,
        reason_codes=[],
        absence_is_officially_certified=True,
        created_at=NOW - timedelta(minutes=1),
    )


def test_official_baseline_requires_real_cninfo_snapshot_and_pit_hash(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = TradingClassificationService(state, objects)
    snapshot = _snapshot(state, objects)
    baseline = _baseline(snapshot, baseline_id="official-baseline-ok")

    artifact_id = service._register_official_baseline(baseline)

    record = state.artifact_record(artifact_id)
    assert record is not None
    assert record["type"] == "TradingClassificationCorporateActionBaseline"
    assert record["input_hashes"] == [snapshot.object_sha256]


def test_official_baseline_rejects_non_cninfo_or_future_snapshot(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = TradingClassificationService(state, objects)

    non_official = _snapshot(state, objects, source_id="eastmoney-reference")
    with pytest.raises(ValueError, match="CNINFO index snapshot"):
        service._register_official_baseline(
            _baseline(non_official, baseline_id="non-official-baseline")
        )

    future = _snapshot(
        state,
        objects,
        available_at=NOW,
    )
    with pytest.raises(ValueError, match="future-visible"):
        service._register_official_baseline(
            _baseline(future, baseline_id="future-baseline")
        )


def test_official_capture_uses_bounded_recent_implementation_window() -> None:
    source = (
        PROJECT_ROOT / "src" / "astock" / "research" / "trading_classification.py"
    ).read_text(encoding="utf-8")

    assert "local_date - timedelta(days=45)" in source
    assert "start_date=coverage_start" in source
    assert "end_date=local_date" in source
    assert "_validate_official_enumeration(batches)" in source
    assert "_OFFICIAL_BASELINE_MAX_AGE = timedelta(minutes=5)" in source
