"""WP42 reviewer-label contracts over the frozen pre-registration.

Development labels can be reviewed before final parameter tuning. Holdout labels
must remain absent from the implementation environment until after the final
parameter change. Both packs must exactly cover their frozen sample identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from astock.candidates.discovery_evaluation import (
    DiscoveryEvaluationLabel,
    DiscoveryEvaluationProtocol,
    DiscoveryEvaluationSample,
    DiscoveryEvaluationSplit,
)
from astock.candidates.discovery_preregistration import DiscoveryPreregistration


@dataclass(frozen=True)
class DiscoveryReviewLabel:
    sample_id: str
    label: DiscoveryEvaluationLabel
    industry_group: str
    reviewer_ids: tuple[str, ...]
    rationale: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("review label requires sample_id")
        if not self.industry_group.strip():
            raise ValueError("review label requires an explicit industry group")
        if not self.reviewer_ids or self.reviewer_ids != tuple(sorted(set(self.reviewer_ids))):
            raise ValueError("reviewer IDs must be non-empty sorted unique")
        if not self.rationale.strip():
            raise ValueError("review label requires a rationale")
        if not self.source_refs or self.source_refs != tuple(sorted(set(self.source_refs))):
            raise ValueError("review source refs must be non-empty sorted unique")


@dataclass(frozen=True)
class DiscoveryReviewPack:
    protocol_id: str
    preregistration_sha256: str
    split: DiscoveryEvaluationSplit
    reviewed_at: datetime
    reviewer_blinded_to_channel_scores: bool
    labels: tuple[DiscoveryReviewLabel, ...]

    def __post_init__(self) -> None:
        if not self.protocol_id.strip():
            raise ValueError("review pack requires protocol_id")
        if len(self.preregistration_sha256) != 64:
            raise ValueError("review pack preregistration SHA-256 is invalid")
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("review pack reviewed_at must be timezone-aware")
        if not self.reviewer_blinded_to_channel_scores:
            raise ValueError("reviewers must remain blinded to A/B channel scores")
        sample_ids = [label.sample_id for label in self.labels]
        if sample_ids != sorted(set(sample_ids)):
            raise ValueError("review labels must be sorted unique by sample_id")


def validate_review_pack(
    preregistration: DiscoveryPreregistration,
    pack: DiscoveryReviewPack,
    *,
    preregistration_sha256: str,
    last_parameter_change_at: datetime | None = None,
) -> None:
    if pack.protocol_id != preregistration.protocol_id:
        raise ValueError("review pack protocol drift")
    if pack.preregistration_sha256 != preregistration_sha256:
        raise ValueError("review pack preregistration hash drift")
    expected = sorted(
        sample.sample_id
        for sample in preregistration.samples
        if sample.split is pack.split
    )
    actual = [label.sample_id for label in pack.labels]
    if actual != expected:
        raise ValueError("review pack does not exactly cover its frozen split")
    if pack.split is DiscoveryEvaluationSplit.HOLDOUT:
        if last_parameter_change_at is None:
            raise ValueError("holdout review requires the final parameter-change timestamp")
        if (
            last_parameter_change_at.tzinfo is None
            or last_parameter_change_at.utcoffset() is None
        ):
            raise ValueError("last_parameter_change_at must be timezone-aware")
        if pack.reviewed_at <= last_parameter_change_at:
            raise ValueError(
                "holdout labels must remain hidden until after the final parameter change"
            )


def build_evaluation_protocol(
    *,
    preregistration: DiscoveryPreregistration,
    preregistration_sha256: str,
    development: DiscoveryReviewPack,
    holdout: DiscoveryReviewPack,
    frozen_at: datetime,
    holdout_locked_at: datetime,
    last_parameter_change_at: datetime,
) -> DiscoveryEvaluationProtocol:
    validate_review_pack(
        preregistration,
        development,
        preregistration_sha256=preregistration_sha256,
    )
    validate_review_pack(
        preregistration,
        holdout,
        preregistration_sha256=preregistration_sha256,
        last_parameter_change_at=last_parameter_change_at,
    )
    if development.split is not DiscoveryEvaluationSplit.DEVELOPMENT:
        raise ValueError("development pack has the wrong split")
    if holdout.split is not DiscoveryEvaluationSplit.HOLDOUT:
        raise ValueError("holdout pack has the wrong split")

    labels = {
        label.sample_id: label
        for pack in (development, holdout)
        for label in pack.labels
    }
    samples = tuple(
        DiscoveryEvaluationSample(
            sample_id=sample.sample_id,
            company_id=sample.symbol,
            thesis_family=sample.thesis_family,
            industry_group=labels[sample.sample_id].industry_group,
            label=labels[sample.sample_id].label,
            split=sample.split,
            reviewer_id="+".join(labels[sample.sample_id].reviewer_ids),
            reviewer_blinded_to_channel_scores=True,
            sampled_from_independent_universe=True,
        )
        for sample in preregistration.samples
    )
    return DiscoveryEvaluationProtocol(
        protocol_id=preregistration.protocol_id,
        frozen_at=frozen_at,
        holdout_locked_at=holdout_locked_at,
        holdout_labels_revealed_at=holdout.reviewed_at,
        last_parameter_change_at=last_parameter_change_at,
        samples=samples,
    )


__all__ = [
    "DiscoveryReviewLabel",
    "DiscoveryReviewPack",
    "build_evaluation_protocol",
    "validate_review_pack",
]
