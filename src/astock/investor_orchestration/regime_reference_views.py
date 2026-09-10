"""Read-through regime inputs over canonical Parquet releases and macro captures.

Locators are NOT new artifacts or facts. They identify an observation inside an
existing, verified release and bind its source hash. Reading never copies rows
back to SQLite/ObjectStore and never calls a provider. Availability includes the
parent release, so freshly imported history cannot masquerade as a past capture.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MacroReleaseSnapshot
from astock.investor_orchestration.utils import content_hash
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.schemas.reference_data import (
    DailyBarObservation,
    DatasetReleaseManifest,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferencePitStatus,
)

_REFERENCE_PREFIX = "reference-observation:"
_MACRO_PREFIX = "macro-observation:"
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("regime reference cutoff must be timezone-aware")
    return value


class CanonicalRegimeReferenceViews:
    def __init__(
        self, state: StateStore, objects: ObjectStore, parquet_root: Path | None = None
    ) -> None:
        if not state.path.is_file():
            raise ValueError("canonical state is unavailable; a read view cannot initialize it")
        self.state = state
        self.objects = objects
        self.parquet = ReferenceParquetStore(parquet_root or state.path.parent / "data" / "parquet")
        root = Path(__file__).resolve().parents[3]
        self.reference = MarketReferenceService(
            state, objects, self.parquet, root / "tests" / "fixtures"
        )

    @staticmethod
    def recognizes(locator: str) -> bool:
        return locator.startswith((_REFERENCE_PREFIX, _MACRO_PREFIX))

    def _release_row(self, release_id: str) -> dict[str, Any]:
        if not _HEX.fullmatch(release_id):
            raise ValueError("invalid canonical reference release identity")
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT r.*,a.type AS artifact_type,a.schema_version AS artifact_schema_version,"
                "a.object_hash AS artifact_object_hash,a.input_hashes_json "
                "FROM market_reference_release r JOIN artifact_registry a "
                "ON a.artifact_id=r.manifest_artifact_id WHERE r.release_id=?",
                (release_id,),
            ).fetchone()
        if row is None:
            raise ValueError("canonical reference release is unavailable")
        return dict(row)

    def _daily_rows(
        self, release_id: str
    ) -> tuple[dict[str, Any], DatasetReleaseManifest, dict[str, DailyBarObservation]]:
        row = self._release_row(release_id)
        # Reuse the existing release/raw-chain/file/logical-content verification.
        manifest = self.reference._verified_manifest(row)
        if manifest.dataset_kind is not ReferenceDatasetKind.DAILY_UNADJUSTED:
            raise ValueError("regime daily view requires a canonical daily-price release")
        if manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE:
            raise ValueError("regime daily history is incomplete or conflicted")
        if manifest.pit_status is ReferencePitStatus.UNVERIFIED:
            raise ValueError("regime daily history lacks verified availability")
        result: dict[str, DailyBarObservation] = {}
        sources = {
            identity: self.state.get_snapshot(identity) for identity in manifest.raw_snapshot_ids
        }
        for descriptor in manifest.canonical_files:
            path = (self.parquet.root / descriptor.path).resolve()
            if not path.is_relative_to(self.parquet.root):
                raise ValueError("canonical reference file escapes its configured data root")
            raw_rows = pq.ParquetFile(path).read(columns=["record_json"]).column(0).to_pylist()
            for raw in raw_rows:
                # Canonical Parquet omits model-envelope created_at. Restore
                # that bookkeeping time deterministically, not with wall-clock now.
                payload = json.loads(raw)
                payload.setdefault("created_at", manifest.available_to_system_at)
                value = DailyBarObservation.model_validate(payload)
                if value.observation_id in result:
                    raise ValueError(
                        "canonical daily release contains duplicate observation identity"
                    )
                if value.instrument_id != manifest.scope_key:
                    raise ValueError(
                        "daily observation identity differs from the canonical release scope"
                    )
                if (
                    value.source_snapshot_id not in manifest.raw_snapshot_ids
                    or value.available_to_system_at > manifest.available_to_system_at
                ):
                    raise ValueError("daily observation is not bound to its parent release")
                source = sources.get(value.source_snapshot_id)
                if source is None or source.available_to_system_at > value.available_to_system_at:
                    raise ValueError("daily observation backdates its raw source availability")
                result[value.observation_id] = value
        if len(result) != manifest.coverage.record_count:
            raise ValueError("daily observation count differs from canonical release coverage")
        return row, manifest, result

    def daily_locators(self, scope_key: str, *, as_of: datetime) -> tuple[str, ...]:
        """Select the canonical release visible at cutoff, not the current head."""
        _aware(as_of)
        row = self.state.get_market_reference_release(
            ReferenceDatasetKind.DAILY_UNADJUSTED.value, scope_key, as_of=as_of
        )
        if row is None:
            return ()
        _, manifest, records = self._daily_rows(str(row["release_id"]))
        if manifest.available_to_system_at > as_of:
            raise ValueError("canonical daily release selection crossed the requested cutoff")
        selected = sorted(
            records.values(), key=lambda item: (item.session_date, item.observation_id)
        )
        return tuple(
            f"{_REFERENCE_PREFIX}{manifest.release_id}:{item.observation_id}"
            for item in selected
            if item.available_to_system_at <= as_of
        )

    def _macro_release(self, release_id: str) -> MacroReleaseSnapshot:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM macro_release_snapshots_v2 WHERE release_id=?",
                (release_id,),
            ).fetchone()
        if row is None:
            raise ValueError("canonical macro release is unavailable")
        release = MacroReleaseSnapshot.model_validate_json(row[0])
        if release.release_id != release_id or release.parse_status == "FAILED":
            raise ValueError("macro release identity or parser result is invalid")
        if release.capture_mode not in {"LIVE", "RECORDED"}:
            raise ValueError("unverified legacy macro capture cannot provide a regime input")
        if release.raw_object_id != f"sha256:{release.source_hash}":
            raise ValueError("macro capture raw identity differs from its source hash")
        self.objects.get_bytes(release.source_hash)
        return release

    def macro_locators(
        self, *, authority: str, series_key: str, as_of: datetime
    ) -> tuple[str, ...]:
        _aware(as_of)
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT release_id FROM macro_release_snapshots_v2 WHERE authority=? "
                "AND julianday(captured_at)<=julianday(?) ORDER BY captured_at,release_id",
                (authority, as_of.isoformat()),
            ).fetchall()
        locators = []
        for row in rows:
            release = self._macro_release(str(row[0]))
            if release.captured_at > as_of:
                continue
            for observation in release.observations:
                if observation.authority != authority or observation.series_key != series_key:
                    continue
                if observation.available_to_system_at > as_of:
                    continue
                # The locator also commits to the existing metadata projection.
                # Its hash is not mislabeled as a physically stored raw object.
                digest = content_hash(observation)
                locators.append(
                    f"{_MACRO_PREFIX}{release.release_id}:{observation.observation_id}:{digest}"
                )
        return tuple(locators)

    def load(self, locator: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.load_many((locator,))[locator]

    def load_many(
        self, locators: Sequence[str]
    ) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """Verify each parent once per invocation, never cache across invocations."""
        if isinstance(locators, str) or len(locators) > 100000:
            raise ValueError("regime view input must be a bounded locator sequence")
        daily: dict[str, Any] = {}
        macro: dict[str, MacroReleaseSnapshot] = {}
        result = {}
        for locator in dict.fromkeys(locators):
            if not isinstance(locator, str) or not locator:
                raise ValueError("invalid canonical observation locator")
            result[locator] = self._load_one(locator, daily, macro)
        return result

    def _load_one(
        self, locator: str, daily: dict[str, Any], macro: dict[str, MacroReleaseSnapshot]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve a row identity against a freshly checked parent release.

        `object_hash` names an actual immutable source object: the daily manifest
        or raw macro capture. Locators are read projections, not stored artifacts.
        """
        if locator.startswith(_REFERENCE_PREFIX):
            parts = locator[len(_REFERENCE_PREFIX) :].split(":")
            if len(parts) != 2 or not all(_HEX.fullmatch(part) for part in parts):
                raise ValueError("invalid canonical daily observation locator")
            release_id, observation_id = parts
            if release_id not in daily:
                daily[release_id] = self._daily_rows(release_id)
            row, manifest, records = daily[release_id]
            observation = records.get(observation_id)
            if observation is None:
                raise ValueError("daily observation is absent from its bound release")
            payload = observation.model_dump(mode="json")
            # Effective view visibility includes canonical release publication;
            # the underlying observation record remains byte-for-byte untouched.
            payload["available_to_system_at"] = manifest.available_to_system_at.isoformat()
            return (
                {
                    "type": "DailyBarObservation",
                    "schema_version": observation.schema_version,
                    "object_hash": str(row["manifest_object_hash"]),
                    "parent_artifact_id": str(row["manifest_artifact_id"]),
                },
                payload,
            )
        if locator.startswith(_MACRO_PREFIX):
            parts = locator[len(_MACRO_PREFIX) :].split(":")
            if len(parts) != 3 or not _HEX.fullmatch(parts[2]):
                raise ValueError("invalid canonical macro observation locator")
            release_id, observation_id, expected = parts
            if release_id not in macro:
                macro[release_id] = self._macro_release(release_id)
            release = macro[release_id]
            values = [
                item for item in release.observations if item.observation_id == observation_id
            ]
            if len(values) != 1:
                raise ValueError("macro observation identity is absent or duplicated")
            observation = values[0]
            if content_hash(observation) != expected:
                raise ValueError("macro observation metadata changed since it was frozen")
            if (
                observation.source_hash != release.source_hash
                or observation.authority != release.authority
                or observation.release_family != release.release_family
                or observation.available_to_system_at < release.captured_at
                or observation.published_at > observation.available_to_system_at
            ):
                raise ValueError(
                    "macro observation lineage or availability differs from its capture"
                )
            return (
                {
                    "type": "MacroObservation",
                    "schema_version": "macro-observation-view-v1",
                    "object_hash": release.source_hash,
                    "parent_release_id": release.release_id,
                },
                observation.model_dump(mode="json"),
            )
        raise ValueError("not a canonical regime observation locator")
