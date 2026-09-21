"""Deterministic WP42 sample pre-registration over an independently frozen Universe.

This module freezes sample identities before A/B outputs are available. It does
not carry labels or infer industry membership from company names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from astock.candidates.discovery_evaluation import (
    DiscoveryEvaluationSplit,
    DiscoveryThesisFamily,
)
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.schemas.market import InstrumentType, Market
from astock.schemas.reference_data import InstrumentRecord


def _hash_text(*parts: str) -> str:
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DiscoveryUniverseBinding:
    scope_key: str
    release_id: str
    content_hash: str
    manifest_object_hash: str
    available_to_system_at: datetime

    def __post_init__(self) -> None:
        if not self.scope_key.strip():
            raise ValueError("Universe binding scope_key is required")
        for name, value in (
            ("release_id", self.release_id),
            ("content_hash", self.content_hash),
            ("manifest_object_hash", self.manifest_object_hash),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"Universe binding {name} must be SHA-256")
        if (
            self.available_to_system_at.tzinfo is None
            or self.available_to_system_at.utcoffset() is None
        ):
            raise ValueError("Universe binding time must be timezone-aware")


@dataclass(frozen=True)
class PreregisteredDiscoverySample:
    sample_id: str
    instrument_id: str
    market: Market
    symbol: str
    name: str
    thesis_family: DiscoveryThesisFamily
    split: DiscoveryEvaluationSplit
    source_snapshot_id: str
    selection_hash: str

    def __post_init__(self) -> None:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("pre-registered instrument identity mismatch")
        if len(self.symbol) != 6 or not self.symbol.isdigit():
            raise ValueError("pre-registered symbol must be six digits")
        if not self.name.strip() or not self.source_snapshot_id.strip():
            raise ValueError("pre-registered sample requires name and source snapshot")
        if len(self.selection_hash) != 64:
            raise ValueError("pre-registered selection hash must be SHA-256")


@dataclass(frozen=True)
class DiscoveryPreregistration:
    schema_version: str
    protocol_id: str
    selection_seed: str
    universe_hash: str
    filter_policy: str
    release_bindings: tuple[DiscoveryUniverseBinding, ...]
    samples: tuple[PreregisteredDiscoverySample, ...]
    sample_count: int
    holdout_count: int
    family_counts: tuple[tuple[str, int], ...]
    labels_included: bool = False
    industry_labels_included: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != "wp42-discovery-preregistration-v1":
            raise ValueError("unsupported discovery pre-registration schema")
        if not self.selection_seed.strip():
            raise ValueError("pre-registration seed is required")
        if len(self.universe_hash) != 64:
            raise ValueError("pre-registration Universe hash must be SHA-256")
        if self.labels_included or self.industry_labels_included:
            raise ValueError("pre-registration must not contain reviewer labels")
        if self.sample_count != len(self.samples):
            raise ValueError("pre-registration sample_count mismatch")
        if self.holdout_count != sum(
            sample.split is DiscoveryEvaluationSplit.HOLDOUT for sample in self.samples
        ):
            raise ValueError("pre-registration holdout_count mismatch")
        sample_ids = [sample.sample_id for sample in self.samples]
        if sample_ids != sorted(set(sample_ids)):
            raise ValueError("pre-registration sample IDs must be sorted unique")
        instrument_ids = [sample.instrument_id for sample in self.samples]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("pre-registration must use distinct companies")
        expected_counts = tuple(
            (
                family.value,
                sum(sample.thesis_family is family for sample in self.samples),
            )
            for family in DiscoveryThesisFamily
        )
        if self.family_counts != expected_counts:
            raise ValueError("pre-registration family count mismatch")


def _eligible(records: tuple[InstrumentRecord, ...]) -> dict[str, InstrumentRecord]:
    result: dict[str, InstrumentRecord] = {}
    for record in records:
        if (
            record.instrument_type is not InstrumentType.STOCK
            or not record.tradable
            or record.is_st
            or record.delisting_date is not None
        ):
            continue
        previous = result.get(record.instrument_id)
        if previous is not None and previous != record:
            raise ValueError("conflicting current instrument identity in frozen Universe")
        result[record.instrument_id] = record
    return result


def _proportional_quotas(
    counts: dict[Market, int],
    total: int,
) -> dict[Market, int]:
    if total <= 0 or total > sum(counts.values()):
        raise ValueError("invalid pre-registration sample count")
    positive = {market: count for market, count in counts.items() if count > 0}
    if not positive:
        raise ValueError("pre-registration Universe has no eligible instruments")
    denominator = sum(positive.values())
    exact = {
        market: total * count / denominator
        for market, count in positive.items()
    }
    quotas = {market: int(value) for market, value in exact.items()}
    remaining = total - sum(quotas.values())
    ranked = sorted(
        positive,
        key=lambda market: (
            -(exact[market] - quotas[market]),
            market.value,
        ),
    )
    for market in ranked[:remaining]:
        quotas[market] += 1
    return quotas


def build_discovery_preregistration(
    *,
    records: tuple[InstrumentRecord, ...],
    release_bindings: tuple[DiscoveryUniverseBinding, ...],
    selection_seed: str,
    sample_count: int = 60,
    holdout_count: int = 20,
) -> DiscoveryPreregistration:
    """Freeze company-thesis identities without looking at A/B outputs or labels."""

    if not selection_seed.strip():
        raise ValueError("selection_seed is required")
    family_count = len(DiscoveryThesisFamily)
    if sample_count < 60 or sample_count % family_count:
        raise ValueError("sample_count must be >=60 and divisible by thesis-family count")
    if holdout_count < 20 or holdout_count % family_count or holdout_count > sample_count:
        raise ValueError("holdout_count must be >=20, <=sample_count and family-balanced")
    if not release_bindings:
        raise ValueError("pre-registration requires immutable Universe release bindings")
    if release_bindings != tuple(
        sorted(
            release_bindings,
            key=lambda item: (
                item.scope_key,
                item.available_to_system_at,
                item.release_id,
            ),
        )
    ):
        raise ValueError("Universe release bindings must be sorted")

    eligible = _eligible(records)
    by_market: dict[Market, list[InstrumentRecord]] = {}
    for record in eligible.values():
        by_market.setdefault(record.market, []).append(record)
    quotas = _proportional_quotas(
        {market: len(values) for market, values in by_market.items()},
        sample_count,
    )

    universe_seed = {
        "bindings": [
            {
                "scope_key": binding.scope_key,
                "release_id": binding.release_id,
                "content_hash": binding.content_hash,
                "manifest_object_hash": binding.manifest_object_hash,
                "available_to_system_at": binding.available_to_system_at.isoformat(),
            }
            for binding in release_bindings
        ],
        "filter_policy": "STOCK_AND_TRADABLE_AND_NOT_ST_AND_NOT_DELISTED",
    }
    universe_hash = content_hash(universe_seed)

    selected: list[tuple[str, InstrumentRecord]] = []
    for market in sorted(quotas, key=lambda item: item.value):
        ranked = sorted(
            (
                (
                    _hash_text(
                        selection_seed,
                        universe_hash,
                        market.value,
                        record.instrument_id,
                    ),
                    record,
                )
                for record in by_market[market]
            ),
            key=lambda item: (item[0], item[1].instrument_id),
        )
        selected.extend(ranked[: quotas[market]])

    selected.sort(
        key=lambda item: (
            _hash_text(
                selection_seed,
                universe_hash,
                "family-assignment",
                item[1].instrument_id,
            ),
            item[1].instrument_id,
        )
    )
    per_family = sample_count // family_count
    holdout_per_family = holdout_count // family_count
    provisional: list[
        tuple[str, InstrumentRecord, DiscoveryThesisFamily, str]
    ] = []
    for index, (selection_hash, record) in enumerate(selected):
        family = tuple(DiscoveryThesisFamily)[index // per_family]
        family_hash = _hash_text(
            selection_seed,
            universe_hash,
            family.value,
            record.instrument_id,
        )
        provisional.append((selection_hash, record, family, family_hash))

    holdout_ids: set[str] = set()
    for family in DiscoveryThesisFamily:
        family_rows = sorted(
            (
                row for row in provisional if row[2] is family
            ),
            key=lambda item: (item[3], item[1].instrument_id),
        )
        holdout_ids.update(
            row[1].instrument_id
            for row in family_rows[:holdout_per_family]
        )

    samples = tuple(
        sorted(
            (
                PreregisteredDiscoverySample(
                    sample_id=(
                        "wp42-sample:"
                        + _hash_text(
                            universe_hash,
                            record.instrument_id,
                            family.value,
                        )
                    ),
                    instrument_id=record.instrument_id,
                    market=record.market,
                    symbol=record.symbol,
                    name=record.name,
                    thesis_family=family,
                    split=(
                        DiscoveryEvaluationSplit.HOLDOUT
                        if record.instrument_id in holdout_ids
                        else DiscoveryEvaluationSplit.DEVELOPMENT
                    ),
                    source_snapshot_id=record.source_snapshot_id,
                    selection_hash=family_hash,
                )
                for _selection_hash, record, family, family_hash in provisional
            ),
            key=lambda item: item.sample_id,
        )
    )
    family_counts = tuple(
        (
            family.value,
            sum(sample.thesis_family is family for sample in samples),
        )
        for family in DiscoveryThesisFamily
    )
    protocol_id = "wp42-preregistration:" + content_hash(
        {
            "selection_seed": selection_seed,
            "universe_hash": universe_hash,
            "sample_ids": [sample.sample_id for sample in samples],
            "splits": [
                [sample.sample_id, sample.split.value]
                for sample in samples
            ],
        }
    )
    result = DiscoveryPreregistration(
        schema_version="wp42-discovery-preregistration-v1",
        protocol_id=protocol_id,
        selection_seed=selection_seed,
        universe_hash=universe_hash,
        filter_policy="STOCK_AND_TRADABLE_AND_NOT_ST_AND_NOT_DELISTED",
        release_bindings=release_bindings,
        samples=samples,
        sample_count=len(samples),
        holdout_count=sum(
            sample.split is DiscoveryEvaluationSplit.HOLDOUT for sample in samples
        ),
        family_counts=family_counts,
    )
    # A serialization smoke check makes hidden accidental dataclass fields visible
    # during tests/reviews without persisting anything here.
    canonical_json_bytes(
        {
            "protocol_id": result.protocol_id,
            "universe_hash": result.universe_hash,
            "sample_ids": [sample.sample_id for sample in result.samples],
        }
    )
    return result


__all__ = [
    "DiscoveryPreregistration",
    "DiscoveryUniverseBinding",
    "PreregisteredDiscoverySample",
    "build_discovery_preregistration",
]
