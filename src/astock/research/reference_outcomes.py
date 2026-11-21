"""Shared read-only canonical price and corporate-action inputs.

Source integrity, units, visibility and exhaustive action coverage have one
reader for recommendation outcomes and prospective factor diagnostics.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.debate_evaluation import QualityError
from astock.schemas.reference_data import (
    CorporateActionObservation,
    CorporateActionStatus,
    DailyBarObservation,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
)


def verified_action_dates(
    state: StateStore,
    objects: ObjectStore,
    parquet_root: Path | None,
    instrument_id: str,
    *,
    start: date,
    end: date,
    as_of: datetime,
    max_bars: int,
) -> tuple[set[date] | None, dict | None]:
    """Only a COMPLETE official-terms release covering the whole span proves absence.

    Current sync_corporate_actions() deliberately publishes complete=False.
    Thus today's hint/exact-item releases can identify individual actions but
    cannot certify a zero-action period, regardless of record count or recency.
    """
    import pyarrow.parquet as pq

    from astock.investor_orchestration.regime_reference_views import (
        CanonicalRegimeReferenceViews,
    )

    row = state.get_market_reference_release(
        ReferenceDatasetKind.CORPORATE_ACTION.value,
        instrument_id,
        as_of=as_of,
    )
    if row is None or row.get("coverage_status") != ReferenceCoverageStatus.COMPLETE.value:
        return None, None
    coverage = json.loads(row["coverage_json"])
    if (
        not isinstance(coverage, dict)
        or coverage.get("reason_codes")
        or not isinstance(coverage.get("record_count"), int)
        or not 0 < coverage["record_count"] <= max_bars
    ):
        return None, None
    try:
        first = date.fromisoformat(coverage["requested_start"])
        last = date.fromisoformat(coverage["requested_end"])
    except (TypeError, ValueError, KeyError):
        return None, None
    if first > start or last < end:
        return None, None

    view = CanonicalRegimeReferenceViews(state, objects, parquet_root)
    release = view._release_row(str(row["release_id"]))
    manifest = view.reference._verified_manifest(release)
    if (
        manifest.dataset_kind is not ReferenceDatasetKind.CORPORATE_ACTION
        or manifest.scope_key != instrument_id
        or manifest.available_to_system_at > as_of
        or manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE
        or manifest.coverage.requested_start != first
        or manifest.coverage.requested_end != last
        or manifest.coverage.record_count != coverage["record_count"]
    ):
        raise QualityError("CORPORATE_ACTION_COVERAGE_MISMATCH")

    events: set[date] = set()
    seen: set[str] = set()
    for descriptor in manifest.canonical_files:
        path = (view.parquet.root / descriptor.path).resolve()
        if not path.is_relative_to(view.parquet.root):
            raise QualityError("CORPORATE_ACTION_FILE_OUTSIDE_ROOT")
        for batch in pq.ParquetFile(path).iter_batches(batch_size=256, columns=["record_json"]):
            for raw in batch.column(0).to_pylist():
                payload = json.loads(raw)
                payload.setdefault("created_at", manifest.available_to_system_at)
                item = CorporateActionObservation.model_validate(payload)
                if (
                    item.instrument_id != instrument_id
                    or item.observation_id in seen
                    or item.status is not CorporateActionStatus.TERMS_VERIFIED
                    or item.source_snapshot_id not in manifest.raw_snapshot_ids
                    or item.available_to_system_at > manifest.available_to_system_at
                    or item.ex_date is None
                ):
                    raise QualityError("CORPORATE_ACTION_TERMS_OR_IDENTITY_UNVERIFIED")
                seen.add(item.observation_id)
                if start <= item.ex_date <= end:
                    events.add(item.ex_date)
    if len(seen) != manifest.coverage.record_count:
        raise QualityError("CORPORATE_ACTION_RELEASE_COUNT_MISMATCH")
    return events, {
        "release_id": manifest.release_id,
        "artifact_id": str(release["manifest_artifact_id"]),
        "object_hash": str(release["manifest_object_hash"]),
        "coverage_start": first.isoformat(),
        "coverage_end": last.isoformat(),
        "official_terms_verified": True,
    }


class ReferencePriceInputReader:
    """One report's bounded cache of already-verified daily parents."""

    def __init__(
        self, state: StateStore, objects: ObjectStore, parquet_root: Path | None,
        *, evaluated_at: datetime, max_bars: int,
    ) -> None:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("reference inputs require an aware cutoff")
        self.state, self.objects, self.parquet_root = state, objects, parquet_root
        self.evaluated_at, self.max_bars = evaluated_at, max_bars
        # Context (stores, cutoff, limit) is fixed per reader. No global/process cache.
        self._daily = lru_cache(maxsize=8)(self._read_daily)

    def _read_daily(self, identity: str) -> tuple[tuple[DailyBarObservation, ...], dict | None]:
        from astock.technical.registered import load_daily_release

        row = self.state.get_market_reference_release(
            ReferenceDatasetKind.DAILY_UNADJUSTED.value, identity, as_of=self.evaluated_at,
        )
        if row is None:
            return (), None
        manifest, values, artifact_id, object_hash = load_daily_release(
            self.state, self.objects, row["release_id"], limit=self.max_bars,
            parquet_root=self.parquet_root, max_release_rows=self.max_bars,
        )
        if manifest.available_to_system_at > self.evaluated_at:
            raise QualityError("CANONICAL_VISIBILITY_MISMATCH")
        return values, {"artifact_id": artifact_id, "object_hash": object_hash}

    def load(
        self, instrument_id: str, benchmark_id: str, signal_at: datetime,
    ) -> tuple[tuple[DailyBarObservation, ...], tuple[DailyBarObservation, ...],
               set[date] | None, dict]:
        shanghai = ZoneInfo("Asia/Shanghai")
        start = signal_at.astimezone(shanghai).date()
        end = self.evaluated_at.astimezone(shanghai).date()
        lineage: dict = {"daily_inputs": []}
        action_dates, action_proof = (None, None)
        if start <= end:
            action_dates, action_proof = verified_action_dates(
                self.state, self.objects, self.parquet_root, instrument_id,
                start=start, end=end, as_of=self.evaluated_at, max_bars=self.max_bars,
            )
        if action_proof is not None:
            lineage["corporate_actions"] = action_proof
        stock, stock_ref = self._daily(instrument_id)
        benchmark, benchmark_ref = self._daily(benchmark_id)
        lineage["daily_inputs"] = [item for item in (stock_ref, benchmark_ref) if item is not None]
        return stock, benchmark, action_dates, lineage


def load_reference_price_inputs(
    state: StateStore, objects: ObjectStore, parquet_root: Path | None, *,
    instrument_id: str, benchmark_id: str, signal_at: datetime,
    evaluated_at: datetime, max_bars: int,
) -> tuple[
    tuple[DailyBarObservation, ...], tuple[DailyBarObservation, ...], set[date] | None, dict,
]:
    return ReferencePriceInputReader(
        state, objects, parquet_root, evaluated_at=evaluated_at, max_bars=max_bars,
    ).load(instrument_id, benchmark_id, signal_at)
