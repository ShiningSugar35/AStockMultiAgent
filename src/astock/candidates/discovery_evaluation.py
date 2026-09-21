"""Fail-closed WP42 quality and latency comparison for skill-driven discovery.

The evaluator measures research-opportunity discovery, not future returns. It
requires a pre-registered independent company-thesis sample, a hidden holdout,
same Universe/budgets for A and B, explicit unknown states, and measured cold/hot
latencies. Missing measurements produce INCOMPLETE, never PASS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal


class DiscoveryEvaluationSplit(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    HOLDOUT = "HOLDOUT"


class DiscoveryEvaluationLabel(IntEnum):
    NOT_SUPPORTED = 0
    NEEDS_EVIDENCE = 1
    WORTH_DEEP_RESEARCH = 2


class DiscoveryEvaluationState(StrEnum):
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"
    NOT_VERIFIED = "NOT_VERIFIED"


class DiscoveryEvaluationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class DiscoveryThesisFamily(StrEnum):
    QUALITY_VALUE = "QUALITY_VALUE"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    BOTTLENECK = "BOTTLENECK"
    EVENT = "EVENT"
    CYCLE = "CYCLE"


class DiscoveryCacheState(StrEnum):
    COLD = "COLD"
    HOT = "HOT"


@dataclass(frozen=True)
class DiscoveryEvaluationSample:
    sample_id: str
    company_id: str
    thesis_family: DiscoveryThesisFamily
    industry_group: str
    label: DiscoveryEvaluationLabel
    split: DiscoveryEvaluationSplit
    reviewer_id: str
    reviewer_blinded_to_channel_scores: Literal[True]
    sampled_from_independent_universe: Literal[True]

    def __post_init__(self) -> None:
        if not self.sample_id.strip() or not self.industry_group.strip():
            raise ValueError("evaluation sample identities must be non-empty")
        if len(self.company_id) != 6 or not self.company_id.isdigit():
            raise ValueError("evaluation sample company_id must be six digits")
        if not self.reviewer_id.strip():
            raise ValueError("evaluation sample requires an independent reviewer")


@dataclass(frozen=True)
class DiscoveryArmOutcome:
    sample_id: str
    seed_selected: bool
    promotion_selected: bool
    thesis_state: DiscoveryEvaluationState

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("arm outcome sample_id must be non-empty")
        if self.promotion_selected and not self.seed_selected:
            raise ValueError("promotion cannot select a sample that was not a Seed")


@dataclass(frozen=True)
class DiscoveryArmRun:
    arm_id: str
    universe_hash: str
    seed_budget: int
    promotion_budget: int
    deep_research_budget: int
    actual_seed_count: int
    actual_promotion_count: int
    outcomes: tuple[DiscoveryArmOutcome, ...]

    def __post_init__(self) -> None:
        if not self.arm_id.strip():
            raise ValueError("evaluation arm requires an id")
        if len(self.universe_hash) != 64:
            raise ValueError("evaluation arm requires a Universe SHA-256 identity")
        if (
            self.seed_budget != 80
            or self.promotion_budget != 60
            or self.deep_research_budget not in {8, 12, 16}
        ):
            raise ValueError("evaluation arm budgets must preserve 80/60/8-12-16 contracts")
        if not 0 <= self.actual_seed_count <= self.seed_budget:
            raise ValueError("actual Seed count exceeds its budget")
        if not 0 <= self.actual_promotion_count <= self.promotion_budget:
            raise ValueError("actual Promotion count exceeds its budget")
        sample_ids = [item.sample_id for item in self.outcomes]
        if sample_ids != sorted(set(sample_ids)):
            raise ValueError("arm outcomes must be sorted and unique by sample_id")


@dataclass(frozen=True)
class DiscoveryLatencyObservation:
    observation_id: str
    cache_state: DiscoveryCacheState
    full_discovery_seconds: float
    rule_merge_seconds: float
    raw_network_included: bool

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("latency observation requires an id")
        if (
            not math.isfinite(self.full_discovery_seconds)
            or not math.isfinite(self.rule_merge_seconds)
            or self.full_discovery_seconds < 0
            or self.rule_merge_seconds < 0
        ):
            raise ValueError("latency values must be finite and non-negative")
        if self.rule_merge_seconds > self.full_discovery_seconds:
            raise ValueError("rule merge latency cannot exceed full discovery latency")


@dataclass(frozen=True)
class FullRequestObservation:
    scenario_id: str
    duration_seconds: float
    normal_case: bool
    useful_delivery: bool
    terminal: bool

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("full-request observation requires a scenario id")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("full-request duration must be finite and non-negative")
        if self.useful_delivery and not self.terminal:
            raise ValueError("non-terminal scenarios cannot count as useful delivery")


@dataclass(frozen=True)
class DiscoveryEvaluationProtocol:
    protocol_id: str
    frozen_at: datetime
    holdout_locked_at: datetime
    holdout_labels_revealed_at: datetime
    last_parameter_change_at: datetime
    samples: tuple[DiscoveryEvaluationSample, ...]

    def __post_init__(self) -> None:
        if not self.protocol_id.strip():
            raise ValueError("evaluation protocol requires an id")
        timestamps = (
            self.frozen_at,
            self.holdout_locked_at,
            self.holdout_labels_revealed_at,
            self.last_parameter_change_at,
        )
        if any(item.tzinfo is None or item.utcoffset() is None for item in timestamps):
            raise ValueError("evaluation protocol timestamps must be timezone-aware")
        if self.holdout_locked_at > self.last_parameter_change_at:
            raise ValueError("holdout must be locked before the final parameter change")
        if self.holdout_labels_revealed_at <= self.last_parameter_change_at:
            raise ValueError("holdout labels must stay hidden through the final parameter change")
        sample_ids = [item.sample_id for item in self.samples]
        if sample_ids != sorted(set(sample_ids)):
            raise ValueError("evaluation samples must be sorted and unique")
        if any(not item.reviewer_blinded_to_channel_scores for item in self.samples):
            raise ValueError("reviewers must be blinded to discovery-channel scores")
        if any(not item.sampled_from_independent_universe for item in self.samples):
            raise ValueError("evaluation samples cannot be selected only from A/B outputs")


@dataclass(frozen=True)
class ProportionMetric:
    numerator: int
    denominator: int
    value: float | None
    ci_low: float | None
    ci_high: float | None


@dataclass(frozen=True)
class ArmMetrics:
    promotion_recall: ProportionMetric
    verified_precision: ProportionMetric
    unknown_rate: ProportionMetric
    family_promotion_recall: tuple[tuple[str, ProportionMetric], ...]
    industry_promotion_recall: tuple[tuple[str, ProportionMetric], ...]


@dataclass(frozen=True)
class LatencyMetrics:
    cold_count: int
    hot_count: int
    cold_full_p90_seconds: float | None
    hot_full_p90_seconds: float | None
    hot_rule_merge_p90_seconds: float | None
    cold_raw_network_included_count: int


@dataclass(frozen=True)
class FullRequestMetrics:
    scenario_count: int
    normal_case_count: int
    normal_useful_count: int
    p50_seconds: float | None
    p75_seconds: float | None
    p90_seconds: float | None
    max_seconds: float | None


@dataclass(frozen=True)
class DiscoveryEvaluationReport:
    status: DiscoveryEvaluationStatus
    reasons: tuple[str, ...]
    sample_count: int
    holdout_count: int
    thesis_family_counts: tuple[tuple[str, int], ...]
    arm_a: ArmMetrics
    arm_b: ArmMetrics
    recall_delta: float | None
    new_error_count: int
    latency: LatencyMetrics
    full_request: FullRequestMetrics
    quality_gate_pass: bool
    local_performance_gate_pass: bool
    full_request_gate_pass: bool


def _wilson(numerator: int, denominator: int) -> ProportionMetric:
    if denominator == 0:
        return ProportionMetric(numerator, denominator, None, None, None)
    value = numerator / denominator
    z = 1.959963984540054
    z2 = z * z
    center = (value + z2 / (2 * denominator)) / (1 + z2 / denominator)
    margin = (
        z
        * math.sqrt(
            (value * (1 - value) + z2 / (4 * denominator))
            / denominator
        )
        / (1 + z2 / denominator)
    )
    return ProportionMetric(
        numerator=numerator,
        denominator=denominator,
        value=value,
        ci_low=max(0.0, center - margin),
        ci_high=min(1.0, center + margin),
    )


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _outcomes(run: DiscoveryArmRun) -> dict[str, DiscoveryArmOutcome]:
    return {item.sample_id: item for item in run.outcomes}


def _recall_for(
    samples: tuple[DiscoveryEvaluationSample, ...],
    outcomes: dict[str, DiscoveryArmOutcome],
) -> ProportionMetric:
    targets = [
        sample for sample in samples
        if sample.label is DiscoveryEvaluationLabel.WORTH_DEEP_RESEARCH
    ]
    numerator = sum(outcomes[sample.sample_id].promotion_selected for sample in targets)
    return _wilson(numerator, len(targets))


def _precision_for(
    samples: tuple[DiscoveryEvaluationSample, ...],
    outcomes: dict[str, DiscoveryArmOutcome],
) -> ProportionMetric:
    verified = [
        sample
        for sample in samples
        if outcomes[sample.sample_id].thesis_state is DiscoveryEvaluationState.VERIFIED
    ]
    numerator = sum(
        sample.label is DiscoveryEvaluationLabel.WORTH_DEEP_RESEARCH
        for sample in verified
    )
    return _wilson(numerator, len(verified))


def _unknown_for(
    samples: tuple[DiscoveryEvaluationSample, ...],
    outcomes: dict[str, DiscoveryArmOutcome],
) -> ProportionMetric:
    numerator = sum(
        outcomes[sample.sample_id].thesis_state is DiscoveryEvaluationState.UNKNOWN
        for sample in samples
    )
    return _wilson(numerator, len(samples))


def _group_recall(
    samples: tuple[DiscoveryEvaluationSample, ...],
    outcomes: dict[str, DiscoveryArmOutcome],
    *,
    field: Literal["family", "industry"],
) -> tuple[tuple[str, ProportionMetric], ...]:
    keys = sorted(
        {
            sample.thesis_family.value if field == "family" else sample.industry_group
            for sample in samples
        }
    )
    result: list[tuple[str, ProportionMetric]] = []
    for key in keys:
        group = tuple(
            sample
            for sample in samples
            if (
                sample.thesis_family.value if field == "family" else sample.industry_group
            )
            == key
        )
        result.append((key, _recall_for(group, outcomes)))
    return tuple(result)


def _arm_metrics(
    samples: tuple[DiscoveryEvaluationSample, ...],
    run: DiscoveryArmRun,
) -> ArmMetrics:
    outcomes = _outcomes(run)
    return ArmMetrics(
        promotion_recall=_recall_for(samples, outcomes),
        verified_precision=_precision_for(samples, outcomes),
        unknown_rate=_unknown_for(samples, outcomes),
        family_promotion_recall=_group_recall(samples, outcomes, field="family"),
        industry_promotion_recall=_group_recall(samples, outcomes, field="industry"),
    )


def _latency_metrics(
    observations: tuple[DiscoveryLatencyObservation, ...],
) -> LatencyMetrics:
    cold = [item for item in observations if item.cache_state is DiscoveryCacheState.COLD]
    hot = [item for item in observations if item.cache_state is DiscoveryCacheState.HOT]
    return LatencyMetrics(
        cold_count=len(cold),
        hot_count=len(hot),
        cold_full_p90_seconds=_nearest_rank(
            [item.full_discovery_seconds for item in cold],
            0.90,
        ),
        hot_full_p90_seconds=_nearest_rank(
            [item.full_discovery_seconds for item in hot],
            0.90,
        ),
        hot_rule_merge_p90_seconds=_nearest_rank(
            [item.rule_merge_seconds for item in hot],
            0.90,
        ),
        cold_raw_network_included_count=sum(item.raw_network_included for item in cold),
    )


def _full_request_metrics(
    observations: tuple[FullRequestObservation, ...],
) -> FullRequestMetrics:
    durations = [item.duration_seconds for item in observations]
    normal = [item for item in observations if item.normal_case]
    return FullRequestMetrics(
        scenario_count=len(observations),
        normal_case_count=len(normal),
        normal_useful_count=sum(item.useful_delivery for item in normal),
        p50_seconds=_nearest_rank(durations, 0.50),
        p75_seconds=_nearest_rank(durations, 0.75),
        p90_seconds=_nearest_rank(durations, 0.90),
        max_seconds=max(durations) if durations else None,
    )


def evaluate_discovery_ab(
    *,
    protocol: DiscoveryEvaluationProtocol,
    arm_a: DiscoveryArmRun,
    arm_b: DiscoveryArmRun,
    latency_observations: tuple[DiscoveryLatencyObservation, ...],
    full_request_observations: tuple[FullRequestObservation, ...],
) -> DiscoveryEvaluationReport:
    """Evaluate the frozen holdout; no future-return label is accepted anywhere."""

    if (
        arm_a.universe_hash != arm_b.universe_hash
        or arm_a.seed_budget != arm_b.seed_budget
        or arm_a.promotion_budget != arm_b.promotion_budget
        or arm_a.deep_research_budget != arm_b.deep_research_budget
    ):
        raise ValueError("A/B comparison requires the same Universe and budgets")

    expected_ids = [item.sample_id for item in protocol.samples]
    if [item.sample_id for item in arm_a.outcomes] != expected_ids:
        raise ValueError("arm A outcomes do not exactly cover the frozen sample")
    if [item.sample_id for item in arm_b.outcomes] != expected_ids:
        raise ValueError("arm B outcomes do not exactly cover the frozen sample")

    family_counts = tuple(
        (
            family.value,
            sum(sample.thesis_family is family for sample in protocol.samples),
        )
        for family in DiscoveryThesisFamily
    )
    holdout = tuple(
        sample
        for sample in protocol.samples
        if sample.split is DiscoveryEvaluationSplit.HOLDOUT
    )
    reasons: list[str] = []

    if len(protocol.samples) < 60:
        reasons.append("SAMPLE_COUNT_LT_60")
    if any(count < 12 for _, count in family_counts):
        reasons.append("THESIS_FAMILY_COUNT_LT_12")
    if len(holdout) < 20:
        reasons.append("HOLDOUT_COUNT_LT_20")

    a_metrics = _arm_metrics(holdout, arm_a)
    b_metrics = _arm_metrics(holdout, arm_b)
    recall_delta = (
        None
        if a_metrics.promotion_recall.value is None
        or b_metrics.promotion_recall.value is None
        else b_metrics.promotion_recall.value - a_metrics.promotion_recall.value
    )
    if a_metrics.promotion_recall.denominator == 0:
        reasons.append("HOLDOUT_HAS_NO_LABEL2_RECALL_DENOMINATOR")

    a_outcomes = _outcomes(arm_a)
    b_outcomes = _outcomes(arm_b)
    new_error_count = sum(
        sample.label is DiscoveryEvaluationLabel.NOT_SUPPORTED
        and b_outcomes[sample.sample_id].thesis_state is DiscoveryEvaluationState.VERIFIED
        and a_outcomes[sample.sample_id].thesis_state is not DiscoveryEvaluationState.VERIFIED
        for sample in holdout
    )

    quality_gate = False
    a_recall = a_metrics.promotion_recall.value
    b_recall = b_metrics.promotion_recall.value
    a_precision = a_metrics.verified_precision.value
    b_precision = b_metrics.verified_precision.value
    if b_recall is not None and b_precision is not None and a_recall is not None:
        recall_gate = (
            b_recall >= a_recall and new_error_count == 0
            if a_recall >= 0.90
            else b_recall >= a_recall and b_recall - a_recall >= 0.10 - 1e-12
        )
        precision_gate = b_precision >= 0.90 and (
            a_precision is None or b_precision >= a_precision
        )
        quality_gate = recall_gate and precision_gate
        if not recall_gate:
            reasons.append("QUALITY_RECALL_GATE_FAILED")
        if not precision_gate:
            reasons.append("QUALITY_PRECISION_GATE_FAILED")
    elif len(holdout) >= 20:
        reasons.append("QUALITY_METRIC_DENOMINATOR_MISSING")

    latency = _latency_metrics(latency_observations)
    local_complete = latency.cold_count >= 10 and latency.hot_count >= 10
    if not local_complete:
        reasons.append("LOCAL_LATENCY_SAMPLE_INCOMPLETE")
    local_performance_gate = (
        local_complete
        and latency.cold_full_p90_seconds is not None
        and latency.cold_full_p90_seconds <= 300
        and latency.hot_full_p90_seconds is not None
        and latency.hot_full_p90_seconds <= 120
        and latency.hot_rule_merge_p90_seconds is not None
        and latency.hot_rule_merge_p90_seconds <= 60
    )
    if local_complete and not local_performance_gate:
        reasons.append("LOCAL_PERFORMANCE_GATE_FAILED")

    full_request = _full_request_metrics(full_request_observations)
    full_complete = (
        full_request.scenario_count >= 30
        and full_request.normal_case_count >= 22
    )
    if not full_complete:
        reasons.append("FULL_REQUEST_SAMPLE_INCOMPLETE")
    full_request_gate = (
        full_complete
        and full_request.normal_useful_count >= 20
        and full_request.p50_seconds is not None
        and full_request.p50_seconds <= 2100
        and full_request.p75_seconds is not None
        and full_request.p75_seconds <= 2400
        and full_request.p90_seconds is not None
        and full_request.p90_seconds <= 2700
        and full_request.max_seconds is not None
        and full_request.max_seconds <= 2700
        and all(item.terminal for item in full_request_observations)
    )
    if full_complete and not full_request_gate:
        reasons.append("FULL_REQUEST_GATE_FAILED")

    incomplete_codes = {
        "SAMPLE_COUNT_LT_60",
        "THESIS_FAMILY_COUNT_LT_12",
        "HOLDOUT_COUNT_LT_20",
        "HOLDOUT_HAS_NO_LABEL2_RECALL_DENOMINATOR",
        "QUALITY_METRIC_DENOMINATOR_MISSING",
        "LOCAL_LATENCY_SAMPLE_INCOMPLETE",
        "FULL_REQUEST_SAMPLE_INCOMPLETE",
    }
    status = (
        DiscoveryEvaluationStatus.INCOMPLETE
        if any(code in incomplete_codes for code in reasons)
        else (
            DiscoveryEvaluationStatus.PASS
            if quality_gate and local_performance_gate and full_request_gate
            else DiscoveryEvaluationStatus.FAIL
        )
    )

    return DiscoveryEvaluationReport(
        status=status,
        reasons=tuple(sorted(set(reasons))),
        sample_count=len(protocol.samples),
        holdout_count=len(holdout),
        thesis_family_counts=family_counts,
        arm_a=a_metrics,
        arm_b=b_metrics,
        recall_delta=recall_delta,
        new_error_count=new_error_count,
        latency=latency,
        full_request=full_request,
        quality_gate_pass=quality_gate,
        local_performance_gate_pass=local_performance_gate,
        full_request_gate_pass=full_request_gate,
    )


__all__ = [
    "DiscoveryArmOutcome",
    "DiscoveryArmRun",
    "DiscoveryCacheState",
    "DiscoveryEvaluationLabel",
    "DiscoveryEvaluationProtocol",
    "DiscoveryEvaluationReport",
    "DiscoveryEvaluationSample",
    "DiscoveryEvaluationSplit",
    "DiscoveryEvaluationState",
    "DiscoveryEvaluationStatus",
    "DiscoveryLatencyObservation",
    "DiscoveryThesisFamily",
    "FullRequestObservation",
    "evaluate_discovery_ab",
]
