from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.candidates.discovery_evaluation import (
    DiscoveryEvaluationLabel,
    DiscoveryEvaluationSplit,
    DiscoveryThesisFamily,
)
from astock.candidates.discovery_labeling import (
    DiscoveryReviewLabel,
    DiscoveryReviewPack,
    build_evaluation_protocol,
    validate_review_pack,
)
from astock.candidates.discovery_preregistration import (
    DiscoveryPreregistration,
    DiscoveryUniverseBinding,
    PreregisteredDiscoverySample,
)
from astock.schemas.market import Market


def _prereg() -> DiscoveryPreregistration:
    binding = DiscoveryUniverseBinding(
        scope_key="XSHG",
        release_id="a" * 64,
        content_hash="b" * 64,
        manifest_object_hash="c" * 64,
        available_to_system_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    samples = []
    ordinal = 0
    for family in DiscoveryThesisFamily:
        for local in range(12):
            samples.append(
                PreregisteredDiscoverySample(
                    sample_id=f"sample:{ordinal:03d}",
                    instrument_id=f"XSHG:{600000 + ordinal:06d}",
                    market=Market.XSHG,
                    symbol=f"{600000 + ordinal:06d}",
                    name=f"Company {ordinal}",
                    thesis_family=family,
                    split=(
                        DiscoveryEvaluationSplit.HOLDOUT
                        if local < 4
                        else DiscoveryEvaluationSplit.DEVELOPMENT
                    ),
                    source_snapshot_id=f"snapshot:{ordinal}",
                    selection_hash=f"{ordinal + 1:064x}",
                )
            )
            ordinal += 1
    samples = sorted(samples, key=lambda item: item.sample_id)
    return DiscoveryPreregistration(
        schema_version="wp42-discovery-preregistration-v1",
        protocol_id="wp42-preregistration:test",
        selection_seed="seed",
        universe_hash="d" * 64,
        filter_policy="STOCK_AND_TRADABLE_AND_NOT_ST_AND_NOT_DELISTED",
        release_bindings=(binding,),
        samples=tuple(samples),
        sample_count=60,
        holdout_count=20,
        family_counts=tuple((family.value, 12) for family in DiscoveryThesisFamily),
    )


def _pack(
    prereg: DiscoveryPreregistration,
    split: DiscoveryEvaluationSplit,
    reviewed_at: datetime,
) -> DiscoveryReviewPack:
    labels = []
    for sample in prereg.samples:
        if sample.split is not split:
            continue
        labels.append(
            DiscoveryReviewLabel(
                sample_id=sample.sample_id,
                label=DiscoveryEvaluationLabel.NEEDS_EVIDENCE,
                industry_group=f"industry:{int(sample.symbol) % 6}",
                reviewer_ids=("reviewer:independent",),
                rationale="Current public facts are insufficient for a stronger conclusion.",
                source_refs=(sample.source_snapshot_id,),
            )
        )
    return DiscoveryReviewPack(
        protocol_id=prereg.protocol_id,
        preregistration_sha256="e" * 64,
        split=split,
        reviewed_at=reviewed_at,
        reviewer_blinded_to_channel_scores=True,
        labels=tuple(sorted(labels, key=lambda item: item.sample_id)),
    )


def test_review_packs_must_exactly_cover_the_frozen_split() -> None:
    prereg = _prereg()
    reviewed_at = datetime(2026, 9, 19, tzinfo=UTC)
    dev = _pack(prereg, DiscoveryEvaluationSplit.DEVELOPMENT, reviewed_at)

    validate_review_pack(
        prereg,
        dev,
        preregistration_sha256="e" * 64,
    )

    missing = dev.__class__(
        protocol_id=dev.protocol_id,
        preregistration_sha256=dev.preregistration_sha256,
        split=dev.split,
        reviewed_at=dev.reviewed_at,
        reviewer_blinded_to_channel_scores=True,
        labels=dev.labels[:-1],
    )
    with pytest.raises(ValueError, match="exactly cover"):
        validate_review_pack(
            prereg,
            missing,
            preregistration_sha256="e" * 64,
        )


def test_holdout_labels_cannot_be_imported_before_final_parameter_change() -> None:
    prereg = _prereg()
    final_change = datetime(2026, 9, 20, tzinfo=UTC)
    holdout = _pack(
        prereg,
        DiscoveryEvaluationSplit.HOLDOUT,
        final_change - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="must remain hidden"):
        validate_review_pack(
            prereg,
            holdout,
            preregistration_sha256="e" * 64,
            last_parameter_change_at=final_change,
        )


def test_evaluation_protocol_combines_development_and_post_freeze_holdout() -> None:
    prereg = _prereg()
    frozen = datetime(2026, 9, 18, tzinfo=UTC)
    final_change = datetime(2026, 9, 20, tzinfo=UTC)
    development = _pack(
        prereg,
        DiscoveryEvaluationSplit.DEVELOPMENT,
        frozen + timedelta(hours=2),
    )
    holdout = _pack(
        prereg,
        DiscoveryEvaluationSplit.HOLDOUT,
        final_change + timedelta(hours=1),
    )

    protocol = build_evaluation_protocol(
        preregistration=prereg,
        preregistration_sha256="e" * 64,
        development=development,
        holdout=holdout,
        frozen_at=frozen,
        holdout_locked_at=frozen + timedelta(hours=1),
        last_parameter_change_at=final_change,
    )

    assert len(protocol.samples) == 60
    assert sum(
        sample.split is DiscoveryEvaluationSplit.HOLDOUT
        for sample in protocol.samples
    ) == 20
    assert all(
        sample.reviewer_blinded_to_channel_scores
        and sample.sampled_from_independent_universe
        for sample in protocol.samples
    )
