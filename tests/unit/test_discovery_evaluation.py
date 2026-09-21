from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.candidates.discovery_evaluation import (
    DiscoveryArmOutcome,
    DiscoveryArmRun,
    DiscoveryCacheState,
    DiscoveryEvaluationLabel,
    DiscoveryEvaluationProtocol,
    DiscoveryEvaluationSample,
    DiscoveryEvaluationSplit,
    DiscoveryEvaluationState,
    DiscoveryEvaluationStatus,
    DiscoveryLatencyObservation,
    DiscoveryThesisFamily,
    FullRequestObservation,
    evaluate_discovery_ab,
)


def _samples() -> tuple[DiscoveryEvaluationSample, ...]:
    rows: list[DiscoveryEvaluationSample] = []
    ordinal = 0
    for family in DiscoveryThesisFamily:
        for local in range(12):
            split = (
                DiscoveryEvaluationSplit.HOLDOUT
                if local < 4
                else DiscoveryEvaluationSplit.DEVELOPMENT
            )
            holdout_labels = (
                DiscoveryEvaluationLabel.WORTH_DEEP_RESEARCH,
                DiscoveryEvaluationLabel.WORTH_DEEP_RESEARCH,
                DiscoveryEvaluationLabel.NEEDS_EVIDENCE,
                DiscoveryEvaluationLabel.NOT_SUPPORTED,
            )
            label = (
                holdout_labels[local]
                if local < 4
                else DiscoveryEvaluationLabel.NEEDS_EVIDENCE
            )
            rows.append(
                DiscoveryEvaluationSample(
                    sample_id=f"sample:{ordinal:03d}",
                    company_id=f"{600000 + ordinal:06d}",
                    thesis_family=family,
                    industry_group=f"industry:{ordinal % 6}",
                    label=label,
                    split=split,
                    reviewer_id=f"reviewer:{ordinal % 3}",
                    reviewer_blinded_to_channel_scores=True,
                    sampled_from_independent_universe=True,
                )
            )
            ordinal += 1
    return tuple(rows)


def _protocol(
    samples: tuple[DiscoveryEvaluationSample, ...] | None = None,
) -> DiscoveryEvaluationProtocol:
    frozen = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    return DiscoveryEvaluationProtocol(
        protocol_id="wp42:fixture",
        frozen_at=frozen,
        holdout_locked_at=frozen + timedelta(hours=1),
        last_parameter_change_at=frozen + timedelta(days=1),
        holdout_labels_revealed_at=frozen + timedelta(days=2),
        samples=samples or _samples(),
    )


def _arm(
    samples: tuple[DiscoveryEvaluationSample, ...],
    *,
    arm_id: str,
    promoted_label2: int,
    universe_hash: str = "a" * 64,
) -> DiscoveryArmRun:
    holdout_targets = [
        sample.sample_id
        for sample in samples
        if sample.split is DiscoveryEvaluationSplit.HOLDOUT
        and sample.label is DiscoveryEvaluationLabel.WORTH_DEEP_RESEARCH
    ]
    selected_targets = set(holdout_targets[:promoted_label2])
    outcomes = []
    for sample in samples:
        selected = sample.sample_id in selected_targets
        outcomes.append(
            DiscoveryArmOutcome(
                sample_id=sample.sample_id,
                seed_selected=selected,
                promotion_selected=selected,
                thesis_state=(
                    DiscoveryEvaluationState.VERIFIED
                    if selected
                    else DiscoveryEvaluationState.NOT_VERIFIED
                ),
            )
        )
    return DiscoveryArmRun(
        arm_id=arm_id,
        universe_hash=universe_hash,
        seed_budget=80,
        promotion_budget=60,
        deep_research_budget=12,
        actual_seed_count=80,
        actual_promotion_count=60,
        outcomes=tuple(outcomes),
    )


def _latencies() -> tuple[DiscoveryLatencyObservation, ...]:
    cold = tuple(
        DiscoveryLatencyObservation(
            observation_id=f"cold:{index}",
            cache_state=DiscoveryCacheState.COLD,
            full_discovery_seconds=210 + 10 * index,
            rule_merge_seconds=35 + index,
            raw_network_included=index % 2 == 0,
        )
        for index in range(10)
    )
    hot = tuple(
        DiscoveryLatencyObservation(
            observation_id=f"hot:{index}",
            cache_state=DiscoveryCacheState.HOT,
            full_discovery_seconds=65 + 5 * index,
            rule_merge_seconds=28 + 2 * index,
            raw_network_included=False,
        )
        for index in range(10)
    )
    return (*cold, *hot)


def _full_requests() -> tuple[FullRequestObservation, ...]:
    return tuple(
        FullRequestObservation(
            scenario_id=f"scenario:{index:02d}",
            duration_seconds=1500 + 30 * index,
            normal_case=index < 22,
            useful_delivery=index < 20,
            terminal=True,
        )
        for index in range(30)
    )


def test_wp42_pass_requires_quality_and_both_performance_gates() -> None:
    protocol = _protocol()
    arm_a = _arm(protocol.samples, arm_id="A", promoted_label2=6)
    arm_b = _arm(protocol.samples, arm_id="B", promoted_label2=8)

    report = evaluate_discovery_ab(
        protocol=protocol,
        arm_a=arm_a,
        arm_b=arm_b,
        latency_observations=_latencies(),
        full_request_observations=_full_requests(),
    )

    assert report.status is DiscoveryEvaluationStatus.PASS
    assert report.sample_count == 60
    assert report.holdout_count == 20
    assert dict(report.thesis_family_counts) == {
        family.value: 12 for family in DiscoveryThesisFamily
    }
    assert report.arm_a.promotion_recall.numerator == 6
    assert report.arm_a.promotion_recall.denominator == 10
    assert report.arm_b.promotion_recall.numerator == 8
    assert report.arm_b.promotion_recall.denominator == 10
    assert report.recall_delta == pytest.approx(0.2)
    assert report.arm_b.verified_precision.value == pytest.approx(1.0)
    assert report.arm_b.verified_precision.ci_low is not None
    assert report.arm_b.verified_precision.ci_high is not None
    assert report.new_error_count == 0
    assert report.latency.cold_count == 10
    assert report.latency.hot_count == 10
    assert report.latency.cold_full_p90_seconds == 290
    assert report.latency.hot_full_p90_seconds == 105
    assert report.latency.hot_rule_merge_p90_seconds == 44
    assert report.full_request.scenario_count == 30
    assert report.full_request.normal_case_count == 22
    assert report.full_request.normal_useful_count == 20
    assert report.quality_gate_pass is True
    assert report.local_performance_gate_pass is True
    assert report.full_request_gate_pass is True


def test_wp42_complete_measurement_that_misses_recall_is_fail_not_incomplete() -> None:
    protocol = _protocol()
    arm_a = _arm(protocol.samples, arm_id="A", promoted_label2=6)
    arm_b = _arm(protocol.samples, arm_id="B", promoted_label2=6)

    report = evaluate_discovery_ab(
        protocol=protocol,
        arm_a=arm_a,
        arm_b=arm_b,
        latency_observations=_latencies(),
        full_request_observations=_full_requests(),
    )

    assert report.status is DiscoveryEvaluationStatus.FAIL
    assert "QUALITY_RECALL_GATE_FAILED" in report.reasons
    assert report.local_performance_gate_pass is True
    assert report.full_request_gate_pass is True


def test_wp42_missing_sample_and_latency_evidence_is_incomplete() -> None:
    samples = _samples()[:10]
    protocol = _protocol(samples)
    arm_a = _arm(samples, arm_id="A", promoted_label2=1)
    arm_b = _arm(samples, arm_id="B", promoted_label2=2)

    report = evaluate_discovery_ab(
        protocol=protocol,
        arm_a=arm_a,
        arm_b=arm_b,
        latency_observations=(),
        full_request_observations=(),
    )

    assert report.status is DiscoveryEvaluationStatus.INCOMPLETE
    assert "SAMPLE_COUNT_LT_60" in report.reasons
    assert "THESIS_FAMILY_COUNT_LT_12" in report.reasons
    assert "HOLDOUT_COUNT_LT_20" in report.reasons
    assert "LOCAL_LATENCY_SAMPLE_INCOMPLETE" in report.reasons
    assert "FULL_REQUEST_SAMPLE_INCOMPLETE" in report.reasons


def test_wp42_same_universe_and_budget_are_hard_contracts() -> None:
    protocol = _protocol()
    arm_a = _arm(protocol.samples, arm_id="A", promoted_label2=6)
    arm_b = _arm(
        protocol.samples,
        arm_id="B",
        promoted_label2=8,
        universe_hash="b" * 64,
    )

    with pytest.raises(ValueError, match="same Universe and budgets"):
        evaluate_discovery_ab(
            protocol=protocol,
            arm_a=arm_a,
            arm_b=arm_b,
            latency_observations=_latencies(),
            full_request_observations=_full_requests(),
        )


def test_wp42_holdout_labels_must_remain_hidden_through_final_parameter_change() -> None:
    frozen = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="labels must stay hidden"):
        DiscoveryEvaluationProtocol(
            protocol_id="bad",
            frozen_at=frozen,
            holdout_locked_at=frozen + timedelta(hours=1),
            last_parameter_change_at=frozen + timedelta(days=2),
            holdout_labels_revealed_at=frozen + timedelta(days=1),
            samples=_samples(),
        )


def test_wp42_high_baseline_uses_non_regression_and_no_new_error_rule() -> None:
    protocol = _protocol()
    arm_a = _arm(protocol.samples, arm_id="A", promoted_label2=9)
    arm_b = _arm(protocol.samples, arm_id="B", promoted_label2=9)

    report = evaluate_discovery_ab(
        protocol=protocol,
        arm_a=arm_a,
        arm_b=arm_b,
        latency_observations=_latencies(),
        full_request_observations=_full_requests(),
    )
    assert report.status is DiscoveryEvaluationStatus.PASS
    assert report.recall_delta == pytest.approx(0.0)
    assert report.new_error_count == 0
