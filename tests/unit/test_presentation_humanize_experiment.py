from __future__ import annotations

import csv
from pathlib import Path

import pytest

from experiments.presentation_humanize.harness import (
    ExperimentCase,
    HumanizeArm,
    build_experiment,
    constrained_rewrite,
    generate_artifacts,
    prepare_source,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ROOT = PROJECT_ROOT / "experiments" / "presentation_humanize"


def _summary(report, arm: HumanizeArm):
    return next(item for item in report.arm_summaries if item.arm is arm)


def test_frozen_experiment_passes_automatic_fact_lock_but_stays_out_of_production() -> None:
    report, blind = build_experiment(EXPERIMENT_ROOT)
    constrained = _summary(report, HumanizeArm.PROJECT_CONSTRAINED)
    risk = _summary(report, HumanizeArm.HUMANIZER_ZH_RISK_PROFILE)

    assert report.production_module_changed is False
    assert report.network_requests == 0
    assert report.external_model_calls == 0
    assert report.total_estimated_cost_usd == 0
    assert report.human_review.status == "PENDING"
    assert report.decision.production_default_enabled is False
    assert report.decision.production_proposal == "NO_CHANGE"
    assert report.decision.external_skill_execution_performed is False
    assert report.decision.actual_human_blind_review_performed is False
    assert "HUMAN_BLIND_REVIEW_PENDING" in report.decision.reason_codes

    assert constrained.automatic_gate_passed is True
    assert constrained.fact_drift_rate == 0
    assert constrained.sensitive_leak_count == 0
    assert constrained.unsupported_experience_count == 0
    assert constrained.score_gain_vs_rule_baseline["naturalness"] >= 0.005
    assert constrained.score_gain_vs_rule_baseline["clarity"] >= 0.005
    assert constrained.score_gain_vs_rule_baseline["concision"] >= 0.005
    assert constrained.production_status == "BLOCKED_HUMAN_REVIEW_PENDING"

    assert risk.automatic_gate_passed is False
    assert risk.production_status == "REJECTED_RISK_PROFILE"
    assert risk.unsupported_experience_count > 0
    assert risk.prompt_injection_count > 0
    assert len(blind) == len(report.assessments)
    assert all(item.external_network_requests == 0 for item in report.assessments)
    assert all(item.external_model_calls == 0 for item in report.assessments)
    assert all(item.external_skill_executed is False for item in report.assessments)


def test_risk_detectors_are_repeatable_across_multiple_candidates() -> None:
    report, _ = build_experiment(EXPERIMENT_ROOT)
    risk_assessments = [
        item for item in report.assessments if item.arm is HumanizeArm.HUMANIZER_ZH_RISK_PROFILE
    ]
    unsupported = [item for item in risk_assessments if item.unsupported_experience_detected]
    injections = [item for item in risk_assessments if item.prompt_injection_detected]

    assert len(unsupported) >= 6
    assert {item.case_id for item in injections} == {"case_05_semiconductor"}
    second_report, _ = build_experiment(EXPERIMENT_ROOT)
    second_risk = _summary(second_report, HumanizeArm.HUMANIZER_ZH_RISK_PROFILE)
    assert second_risk.unsupported_experience_count == len(unsupported)
    assert second_risk.prompt_injection_count == len(injections)


def test_source_redaction_happens_before_experiment_arms() -> None:
    secret_field = "author" + "ization"
    bearer_prefix = "Bear" + "er"
    source = f"说明；{secret_field}: {bearer_prefix} placeholder-value"
    prepared = prepare_source(source)
    assert "[REDACTED]" in prepared
    assert "placeholder-value" not in prepared

    case = ExperimentCase(
        case_id="dynamic_redaction_case",
        source_text=source + "；从整体来看，证券600519仍需持有。",
        locked_entities=[],
        locked_phrases=[],
        tags=[],
    )
    candidate = constrained_rewrite(case)
    assert "placeholder-value" not in candidate
    assert "[REDACTED]" in candidate

    report, _ = build_experiment(EXPERIMENT_ROOT)
    assert all(not item.sensitive_leak_detected for item in report.assessments)


def test_blind_template_never_contains_arm_labels(tmp_path: Path) -> None:
    report = generate_artifacts(EXPERIMENT_ROOT)
    template = EXPERIMENT_ROOT / "output" / "blind_review_template.csv"
    with template.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        rows = list(reader)

    assert "arm" not in {item.casefold() for item in headers}
    assert "arm_name" not in {item.casefold() for item in headers}
    assert len(rows) == len(report.assessments)
    assert len({row["blind_id"] for row in rows}) == len(rows)

    leaked = tmp_path / "leaked.csv"
    leaked.write_text(
        "blind_id,arm,reviewer_alias,naturalness_1_5,clarity_1_5,concision_1_5\n"
        "0000000000000000,RULE_BASELINE,r1,3,3,3\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not contain arm labels"):
        build_experiment(EXPERIMENT_ROOT, ratings_path=leaked)


def test_complete_test_only_ratings_can_validate_admission_logic_without_claiming_real_review(
    tmp_path: Path,
) -> None:
    baseline, blind = build_experiment(EXPERIMENT_ROOT)
    ratings = tmp_path / "synthetic_mechanics_only.csv"
    by_blind = {
        candidate.blind_id: candidate
        for candidate in blind
    }
    with ratings.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "blind_id",
            "reviewer_alias",
            "naturalness_1_5",
            "clarity_1_5",
            "concision_1_5",
            "comments",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for blind_id, candidate in sorted(by_blind.items()):
            score = 4 if candidate.arm is HumanizeArm.PROJECT_CONSTRAINED else 3
            if candidate.arm is HumanizeArm.HUMANIZER_ZH_RISK_PROFILE:
                score = 2
            for reviewer in ("mechanics_r1", "mechanics_r2"):
                writer.writerow(
                    {
                        "blind_id": blind_id,
                        "reviewer_alias": reviewer,
                        "naturalness_1_5": score,
                        "clarity_1_5": score,
                        "concision_1_5": score,
                        "comments": "test-only mechanics fixture",
                    }
                )

    completed, _ = build_experiment(EXPERIMENT_ROOT, ratings_path=ratings)
    constrained = _summary(completed, HumanizeArm.PROJECT_CONSTRAINED)
    assert baseline.human_review.status == "PENDING"
    assert completed.human_review.status == "COMPLETE"
    assert constrained.production_status == "ELIGIBLE_FOR_BACKUP_PROPOSAL"
    assert completed.decision.production_proposal == "CONTROLLED_BACKUP_PROPOSAL"
    assert completed.decision.production_default_enabled is False
