"""Read-through adapter over existing verified canonical releases and ArtifactStore.

No market data is copied back into SQLite, and no provider is called. Each release
is verified once per invocation by the existing canonical reference reader.
"""

from __future__ import annotations

import json
from collections import deque
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.reference_data import DailyBarObservation, DatasetReleaseManifest
from astock.schemas.research_team import MarketContextPack
from astock.schemas.technical import TechnicalIndicatorSnapshot
from astock.technical.policy import TechnicalAnalysisPolicy
from astock.technical.service import SHANGHAI, TechnicalAnalysisService


def load_daily_release(
    state: StateStore,
    objects: ObjectStore,
    release_id: str,
    *,
    limit: int,
    parquet_root: Path | None = None,
    max_release_rows: int | None = None,
) -> tuple[DatasetReleaseManifest, tuple[DailyBarObservation, ...], str, str]:
    """Reuse the canonical reader; a bounded tail is a view, not another fact store."""
    from astock.investor_orchestration.regime_reference_views import CanonicalRegimeReferenceViews

    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("daily release view requires a bounded row limit")
    views = CanonicalRegimeReferenceViews(state, objects, parquet_root)
    # Parent integrity is still verified by the canonical reader; a tail cap
    # alone cannot bound CPU spent checking the complete release. Reject very
    # long parents *before* decoding their rows on low-resource installations.
    if max_release_rows is not None:
        if type(max_release_rows) is not int or max_release_rows < limit:
            raise ValueError("invalid technical canonical release row budget")
        meta = views._release_row(release_id)
        coverage = json.loads(meta["coverage_json"])
        count = coverage.get("record_count") if isinstance(coverage, dict) else None
        if type(count) is not int or not 0 <= count <= max_release_rows:
            raise ValueError("canonical release exceeds verified low-resource row budget")
    row, manifest, records = views._daily_rows(release_id, tail_limit=limit)
    # Canonical order is checked by the consumer; never silently fill/sort corrupt inputs.
    bars = tuple(deque(records.values(), maxlen=limit))
    if not bars:
        raise ValueError("canonical daily release has no observations")
    return manifest, bars, str(row["manifest_artifact_id"]), str(row["manifest_object_hash"])


def analyze_registered_daily(
    state: StateStore,
    objects: ObjectStore,
    release_id: str,
    *,
    policy: TechnicalAnalysisPolicy,
    calendar_path: Path,
    as_of: datetime | None = None,
    parquet_root: Path | None = None,
    register: bool = False,
    market_context: MarketContextPack | None = None,
) -> TechnicalIndicatorSnapshot:
    from astock.market_data.official_calendar import load_official_trading_calendar

    manifest, bars, artifact_id, source_hash = load_daily_release(
        state,
        objects,
        release_id,
        limit=policy.max_history_bars,
        parquet_root=parquet_root,
        max_release_rows=policy.max_release_rows,
    )
    boundary = as_of or manifest.available_to_system_at
    if boundary.tzinfo is None or boundary.utcoffset() is None:
        raise ValueError("technical decision boundary must be timezone-aware")
    boundary = boundary.astimezone(UTC)
    if manifest.available_to_system_at > boundary:
        raise ValueError("daily release is unavailable at this decision boundary")
    if market_context is not None:
        if (
            market_context.daily_release_artifact_id != artifact_id
            or market_context.daily_release_object_hash != source_hash
            or market_context.company_id != bars[-1].symbol
            or market_context.current_close != bars[-1].close
            or market_context.as_of != boundary
        ):
            raise ValueError("technical source does not match the market context binding")
    calendar = load_official_trading_calendar(calendar_path)
    expected = calendar.open_dates(
        bars[-1].market,
        bars[0].session_date,
        boundary.astimezone(SHANGHAI).date(),
    )
    # No authority is inferred for unbound corporate-action coverage. The existing
    # company-action/EntryQuality analysis remains authoritative and the snapshot warns.
    snapshot = TechnicalAnalysisService(policy).build(
        bars,
        as_of=boundary,
        source_artifact_id=artifact_id,
        source_object_hash=source_hash,
        expected_session_dates=expected,
        corporate_action_dates=None,
    )
    if register:
        # The only persistence path consumes the computation above, not caller-supplied
        # indicator values. Reuse the source verification; do not reread its raw chain.
        artifact_id = "TechnicalIndicatorSnapshot:" + snapshot.snapshot_id.removeprefix(
            "technical:"
        )
        ref = objects.put_json(snapshot.model_dump(mode="json"))
        state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="TechnicalIndicatorSnapshot",
            schema_version=snapshot.schema_version,
            object_hash=ref.sha256,
            input_hashes=[source_hash, snapshot.policy_sha256, snapshot.context_sha256],
        )
    return snapshot

def attach_market_context(
    state: StateStore,
    objects: ObjectStore,
    context: MarketContextPack,
    *,
    policy: TechnicalAnalysisPolicy,
    calendar_path: Path,
    parquet_root: Path | None = None,
) -> tuple[MarketContextPack, TechnicalIndicatorSnapshot]:
    """Enrich an existing context from its exact registered daily parent.

    No network or caller-supplied indicator values are accepted. Invalid parent,
    company, time or price bindings fail before a derived artifact is persisted;
    callers may keep their original context and surface the missing provenance.
    """
    with closing(state.connect()) as connection:
        row = connection.execute(
            "SELECT release_id FROM market_reference_release "
            "WHERE manifest_artifact_id=? AND manifest_object_hash=?",
            (context.daily_release_artifact_id, context.daily_release_object_hash),
        ).fetchone()
    if row is None:
        raise ValueError("market context has no matching registered daily release")
    snapshot = analyze_registered_daily(
        state, objects, str(row["release_id"]),
        policy=policy, calendar_path=calendar_path, as_of=context.as_of,
        parquet_root=parquet_root, register=True, market_context=context,
    )
    artifact_id = "TechnicalIndicatorSnapshot:" + snapshot.snapshot_id.removeprefix("technical:")
    record = state.artifact_record(artifact_id)
    if record is None:
        raise ValueError("technical snapshot registration is missing")
    enriched = MarketContextPack.model_validate({
        **context.model_dump(mode="python"),
        "technical_snapshot_artifact_id": artifact_id,
        "technical_snapshot_object_hash": str(record["object_hash"]),
    })
    return enriched, snapshot
