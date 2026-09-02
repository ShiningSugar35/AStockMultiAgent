"""Deterministic, offline A/B harness for fact-locked Chinese presentation.

The experiment deliberately lives outside the production package.  It can
measure candidate rewrites and prepare a blinded review pack, but it never
changes or wraps :class:`astock.research.presentation.ResponseGateway`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.research.presentation import (
    audit_public_answer,
    extract_fact_fingerprint,
    normalize_public_text,
    redact_sensitive_text,
)
from astock.schemas.presentation import FactFingerprint, ResponseContext, ResponseTaskType

_CRITICAL_FACT_FIELDS = (
    "entities",
    "security_codes",
    "numbers",
    "dates",
    "times",
    "direction_terms",
    "conclusion_strength_terms",
    "citations",
    "locked_phrases",
)
_CONSTRAINED_FILLERS = (
    "从整体来看",
    "基于现有材料来看",
    "在当前情况下",
    "总体而言",
    "就目前掌握的信息而言",
    "可以说",
    "需要特别指出的是",
)
_STYLE_FILLERS = (
    "综上所述",
    "值得注意的是",
    "需要强调的是",
    "从某种意义上说",
    "不难发现",
    "可以看出",
    *_CONSTRAINED_FILLERS,
)
_UNSUPPORTED_EXPERIENCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"我曾经",
        r"我亲自",
        r"我身边",
        r"我的朋友",
        r"身边的朋友",
        r"我买过",
        r"我用过",
        r"我体验过",
        r"亲眼见过",
    )
)
_INJECTION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"忽略(?:前述|以上|事实|规则|要求)",
        r"(?:system|developer)\s*prompt",
        r"绕过(?:审计|限制|事实锁)",
    )
)
_SCORE_FIELDS = ("naturalness", "clarity", "concision")


class _ExperimentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HumanizeArm(StrEnum):
    RULE_BASELINE = "RULE_BASELINE"
    PROJECT_CONSTRAINED = "PROJECT_CONSTRAINED"
    HUMANIZER_ZH_RISK_PROFILE = "HUMANIZER_ZH_RISK_PROFILE"


class ExperimentThresholds(_ExperimentModel):
    fact_drift_rate_max: float = Field(ge=0, le=1)
    sensitive_leak_count_max: int = Field(ge=0)
    unsupported_experience_count_max: int = Field(ge=0)
    naturalness_gain_min: float
    clarity_gain_min: float
    concision_gain_min: float
    estimated_cost_usd_max: float = Field(ge=0)
    minimum_human_reviewers_per_candidate: int = Field(ge=1)
    minimum_human_score_gain: float = Field(ge=0)


class ExperimentPreregistration(_ExperimentModel):
    schema_version: Literal["presentation-humanize-preregistration-v1"]
    experiment_id: str = Field(min_length=1)
    frozen_at: str
    blind_seed: str = Field(min_length=8)
    arms: list[HumanizeArm]
    thresholds: ExperimentThresholds
    production_default_enabled: Literal[False]
    network_requests_allowed: Literal[False]
    external_model_calls_allowed: Literal[False]
    source_input_must_be_redacted: Literal[True]
    human_review_required_for_admission: Literal[True]

    @field_validator("arms")
    @classmethod
    def complete_unique_arms(cls, value: list[HumanizeArm]) -> list[HumanizeArm]:
        required = set(HumanizeArm)
        if set(value) != required or len(value) != len(required):
            raise ValueError("preregistration must contain each experiment arm exactly once")
        return value


class ExperimentCase(_ExperimentModel):
    schema_version: Literal["presentation-humanize-case-v1"] = (
        "presentation-humanize-case-v1"
    )
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    source_text: str = Field(min_length=1)
    locked_entities: list[str] = Field(default_factory=list)
    locked_phrases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("locked_entities", "locked_phrases", "tags")
    @classmethod
    def sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("case lists must be sorted and unique")
        return value


class RecordedRiskOutput(_ExperimentModel):
    schema_version: Literal["presentation-humanize-recorded-risk-output-v1"] = (
        "presentation-humanize-recorded-risk-output-v1"
    )
    case_id: str
    output_text: str = Field(min_length=1)
    pattern_ids: list[str]
    external_skill_executed: Literal[False]

    @field_validator("pattern_ids")
    @classmethod
    def deterministic_patterns(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("risk pattern ids must be sorted and unique")
        return value


class AutomaticStyleScores(_ExperimentModel):
    naturalness: float = Field(ge=0, le=1)
    clarity: float = Field(ge=0, le=1)
    concision: float = Field(ge=0, le=1)
    character_count: int = Field(ge=0)
    sentence_count: int = Field(ge=0)
    template_phrase_count: int = Field(ge=0)
    unsupported_experience_count: int = Field(ge=0)


class CandidateAssessment(_ExperimentModel):
    schema_version: Literal["presentation-humanize-candidate-assessment-v1"] = (
        "presentation-humanize-candidate-assessment-v1"
    )
    case_id: str
    arm: HumanizeArm
    candidate_text: str
    redacted_source_text: str
    input_was_redacted: bool
    fact_signature_source: dict[str, list[str]]
    fact_signature_candidate: dict[str, list[str]]
    fact_drift_detected: bool
    sensitive_leak_detected: bool
    unsupported_experience_detected: bool
    prompt_injection_detected: bool
    presentation_safe_to_send: bool
    presentation_finding_codes: list[str]
    style_scores: AutomaticStyleScores
    estimated_cost_usd: float = Field(ge=0)
    external_network_requests: Literal[0]
    external_model_calls: Literal[0]
    external_skill_executed: Literal[False]
    execution_mode: Literal[
        "LOCAL_DETERMINISTIC", "RECORDED_RISK_PROFILE_ONLY"
    ]

    @field_validator("presentation_finding_codes")
    @classmethod
    def deterministic_findings(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("presentation findings must be sorted and unique")
        return value


class ArmSummary(_ExperimentModel):
    arm: HumanizeArm
    case_count: int = Field(ge=1)
    fact_drift_count: int = Field(ge=0)
    fact_drift_rate: float = Field(ge=0, le=1)
    sensitive_leak_count: int = Field(ge=0)
    unsupported_experience_count: int = Field(ge=0)
    prompt_injection_count: int = Field(ge=0)
    presentation_safe_count: int = Field(ge=0)
    average_scores: dict[str, float]
    score_gain_vs_rule_baseline: dict[str, float]
    estimated_cost_usd: float = Field(ge=0)
    automatic_gate_passed: bool
    human_review_status: Literal["PENDING", "COMPLETE"]
    production_status: Literal[
        "REFERENCE_BASELINE",
        "BLOCKED_HUMAN_REVIEW_PENDING",
        "REJECTED_AUTOMATIC_GATE",
        "REJECTED_RISK_PROFILE",
        "ELIGIBLE_FOR_BACKUP_PROPOSAL",
    ]
    reason_codes: list[str]

    @field_validator("reason_codes")
    @classmethod
    def deterministic_reasons(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("arm reason codes must be sorted and unique")
        return value


class HumanReviewSummary(_ExperimentModel):
    status: Literal["PENDING", "COMPLETE"]
    minimum_reviewers_per_candidate: int = Field(ge=1)
    candidate_count: int = Field(ge=1)
    rated_candidate_count: int = Field(ge=0)
    unique_reviewer_count: int = Field(ge=0)
    mean_scores_by_arm: dict[str, dict[str, float]]
    score_gain_vs_rule_baseline: dict[str, dict[str, float]]
    finding_codes: list[str]

    @field_validator("finding_codes")
    @classmethod
    def deterministic_findings(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("human review findings must be sorted and unique")
        return value


class ExperimentDecision(_ExperimentModel):
    production_default_enabled: Literal[False]
    production_proposal: Literal["NO_CHANGE", "CONTROLLED_BACKUP_PROPOSAL"]
    constrained_arm_status: str
    humanizer_risk_profile_status: Literal["REJECTED_RISK_PROFILE"]
    external_skill_execution_performed: Literal[False]
    actual_human_blind_review_performed: bool
    reason_codes: list[str]

    @field_validator("reason_codes")
    @classmethod
    def deterministic_reasons(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("decision reasons must be sorted and unique")
        return value


class HumanizeExperimentReport(_ExperimentModel):
    schema_version: Literal["presentation-humanize-experiment-report-v1"] = (
        "presentation-humanize-experiment-report-v1"
    )
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_id: str
    generated_at: str
    preregistration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    production_module_changed: Literal[False]
    network_requests: Literal[0]
    external_model_calls: Literal[0]
    total_estimated_cost_usd: float = Field(ge=0)
    arm_summaries: list[ArmSummary]
    human_review: HumanReviewSummary
    decision: ExperimentDecision
    assessments: list[CandidateAssessment]

    @model_validator(mode="after")
    def validate_report_id(self) -> HumanizeExperimentReport:
        expected = experiment_report_id(self)
        if self.report_id != expected:
            raise ValueError("experiment report id does not match canonical content")
        return self


class BlindCandidate(_ExperimentModel):
    blind_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    case_id: str
    arm: HumanizeArm
    redacted_source_text: str
    candidate_text: str


class _HumanRating(_ExperimentModel):
    blind_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    reviewer_alias: str = Field(pattern=r"^[A-Za-z0-9._-]{2,64}$")
    naturalness_1_5: int = Field(ge=1, le=5)
    clarity_1_5: int = Field(ge=1, le=5)
    concision_1_5: int = Field(ge=1, le=5)
    comments: str = Field(default="", max_length=2000)


def _experiment_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or Path(__file__).resolve().parent).resolve()


def _load_preregistration(root: Path) -> ExperimentPreregistration:
    return ExperimentPreregistration.model_validate(
        json.loads((root / "preregistration.json").read_text(encoding="utf-8"))
    )


def _load_cases(root: Path) -> list[ExperimentCase]:
    cases = [
        ExperimentCase.model_validate(item)
        for item in _read_jsonl(root / "fixtures" / "cases.jsonl")
    ]
    if not cases:
        raise ValueError("presentation humanize experiment requires at least one case")
    ids = [item.case_id for item in cases]
    if ids != sorted(set(ids)):
        raise ValueError("experiment cases must be sorted by unique case_id")
    for case in cases:
        prepared = prepare_source(case.source_text)
        signature = fact_signature(
            prepared,
            known_entities=case.locked_entities,
            known_phrases=case.locked_phrases,
        )
        if signature["entities"] != sorted(case.locked_entities):
            raise ValueError(f"case {case.case_id} is missing a locked entity")
        if signature["locked_phrases"] != sorted(case.locked_phrases):
            raise ValueError(f"case {case.case_id} is missing a locked phrase")
    return cases


def _load_risk_outputs(root: Path) -> dict[str, RecordedRiskOutput]:
    outputs = [
        RecordedRiskOutput.model_validate(item)
        for item in _read_jsonl(root / "fixtures" / "humanizer_zh_risk_outputs.jsonl")
    ]
    result = {item.case_id: item for item in outputs}
    if len(result) != len(outputs):
        raise ValueError("recorded risk outputs contain duplicate case ids")
    return result


def prepare_source(text: str) -> str:
    """Apply the production redactor before any experiment arm sees input."""

    return redact_sensitive_text(text)


def constrained_rewrite(case: ExperimentCase) -> str:
    """Apply bounded style edits and roll back on any fact-signature change."""

    prepared = prepare_source(case.source_text)
    baseline = normalize_public_text(prepared)
    candidate = baseline
    for phrase in _CONSTRAINED_FILLERS:
        candidate = re.sub(
            rf"(?m)(^|[。！？!?]\s*){re.escape(phrase)}[，,:：]?\s*",
            r"\1",
            candidate,
        )
    candidate = re.sub(r"[ \t]+", " ", candidate)
    candidate = re.sub(r"\n{3,}", "\n\n", candidate).strip()
    source_signature = fact_signature(
        prepared,
        known_entities=case.locked_entities,
        known_phrases=case.locked_phrases,
    )
    candidate_signature = fact_signature(
        candidate,
        known_entities=case.locked_entities,
        known_phrases=case.locked_phrases,
    )
    return candidate if source_signature == candidate_signature else baseline


def fact_signature(
    text: str,
    *,
    known_entities: Iterable[str] = (),
    known_phrases: Iterable[str] = (),
) -> dict[str, list[str]]:
    fingerprint = extract_fact_fingerprint(
        text,
        known_entities=known_entities,
        known_phrases=known_phrases,
    )
    return _fingerprint_signature(fingerprint)


def _fingerprint_signature(fingerprint: FactFingerprint) -> dict[str, list[str]]:
    return {
        field: sorted(set(getattr(fingerprint, field)))
        for field in _CRITICAL_FACT_FIELDS
    }


def automatic_style_scores(text: str) -> AutomaticStyleScores:
    stripped = text.strip()
    compact = re.sub(r"\s+", "", stripped)
    sentences = [
        item.strip()
        for item in re.split(r"[。！？!?\n]+", stripped)
        if item.strip()
    ]
    sentence_lengths = [len(re.sub(r"\s+", "", item)) for item in sentences]
    template_hits = sum(stripped.count(phrase) for phrase in _STYLE_FILLERS)
    template_chars = sum(stripped.count(phrase) * len(phrase) for phrase in _STYLE_FILLERS)
    unsupported_count = _unsupported_experience_count(stripped)
    duplicate_count = sum(count - 1 for count in Counter(sentences).values() if count > 1)
    symbol_piles = len(re.findall(r"(?:!{2,}|！{2,}|\?{2,}|？{2,}|-{3,}|={3,})", stripped))
    long_sentence_penalty = (
        fmean(max(0, length - 48) / 80 for length in sentence_lengths)
        if sentence_lengths
        else 1.0
    )
    max_sentence_penalty = (
        max(0, max(sentence_lengths, default=0) - 80) / 120
    )
    filler_ratio = template_chars / max(1, len(compact))
    duplicate_ratio = duplicate_count / max(1, len(sentences))
    naturalness = _clamp01(
        1.0
        - (0.07 * template_hits)
        - (0.22 * unsupported_count)
        - (0.12 * symbol_piles)
        - (0.16 * duplicate_ratio)
    )
    clarity = _clamp01(
        1.0
        - (0.42 * long_sentence_penalty)
        - (0.22 * max_sentence_penalty)
        - (0.12 * duplicate_ratio)
    )
    concision = _clamp01(
        1.0
        - (1.7 * filler_ratio)
        - (0.25 * duplicate_ratio)
        - (max(0, len(compact) - 320) / 640)
    )
    return AutomaticStyleScores(
        naturalness=round(naturalness, 6),
        clarity=round(clarity, 6),
        concision=round(concision, 6),
        character_count=len(stripped),
        sentence_count=len(sentences),
        template_phrase_count=template_hits,
        unsupported_experience_count=unsupported_count,
    )


def assess_candidate(
    case: ExperimentCase,
    *,
    arm: HumanizeArm,
    candidate_text: str,
) -> CandidateAssessment:
    redacted_source = prepare_source(case.source_text)
    source_signature = fact_signature(
        redacted_source,
        known_entities=case.locked_entities,
        known_phrases=case.locked_phrases,
    )
    candidate_signature = fact_signature(
        candidate_text,
        known_entities=case.locked_entities,
        known_phrases=case.locked_phrases,
    )
    required = extract_fact_fingerprint(
        redacted_source,
        known_entities=case.locked_entities,
        known_phrases=case.locked_phrases,
    )
    audit = audit_public_answer(
        candidate_text,
        context=ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH),
        source_text=redacted_source,
        required_fingerprint=required,
    )
    unsupported = _unsupported_experience_count(candidate_text) > 0
    injection = any(pattern.search(candidate_text) for pattern in _INJECTION_PATTERNS)
    sensitive_leak = redact_sensitive_text(candidate_text) != candidate_text
    fact_drift = source_signature != candidate_signature or audit.fact_drift_detected
    execution_mode: Literal[
        "LOCAL_DETERMINISTIC", "RECORDED_RISK_PROFILE_ONLY"
    ] = (
        "RECORDED_RISK_PROFILE_ONLY"
        if arm is HumanizeArm.HUMANIZER_ZH_RISK_PROFILE
        else "LOCAL_DETERMINISTIC"
    )
    return CandidateAssessment(
        case_id=case.case_id,
        arm=arm,
        candidate_text=candidate_text,
        redacted_source_text=redacted_source,
        input_was_redacted=redacted_source != case.source_text,
        fact_signature_source=source_signature,
        fact_signature_candidate=candidate_signature,
        fact_drift_detected=fact_drift,
        sensitive_leak_detected=sensitive_leak,
        unsupported_experience_detected=unsupported,
        prompt_injection_detected=injection,
        presentation_safe_to_send=audit.safe_to_send,
        presentation_finding_codes=sorted(set(audit.finding_codes)),
        style_scores=automatic_style_scores(candidate_text),
        estimated_cost_usd=0.0,
        external_network_requests=0,
        external_model_calls=0,
        external_skill_executed=False,
        execution_mode=execution_mode,
    )


def build_blind_candidates(
    assessments: Sequence[CandidateAssessment],
    *,
    blind_seed: str,
) -> list[BlindCandidate]:
    candidates = [
        BlindCandidate(
            blind_id=_blind_id(blind_seed, item.case_id, item.arm),
            case_id=item.case_id,
            arm=item.arm,
            redacted_source_text=item.redacted_source_text,
            candidate_text=item.candidate_text,
        )
        for item in assessments
    ]
    ids = [item.blind_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise RuntimeError("blind candidate id collision")
    return sorted(
        candidates,
        key=lambda item: sha256_bytes(
            f"{blind_seed}|order|{item.blind_id}".encode()
        ),
    )


def load_human_review(
    ratings_path: Path | None,
    *,
    blind_candidates: Sequence[BlindCandidate],
    thresholds: ExperimentThresholds,
) -> HumanReviewSummary:
    by_blind_id = {item.blind_id: item for item in blind_candidates}
    pending = HumanReviewSummary(
        status="PENDING",
        minimum_reviewers_per_candidate=thresholds.minimum_human_reviewers_per_candidate,
        candidate_count=len(blind_candidates),
        rated_candidate_count=0,
        unique_reviewer_count=0,
        mean_scores_by_arm={},
        score_gain_vs_rule_baseline={},
        finding_codes=["HUMAN_BLIND_REVIEW_PENDING"],
    )
    if ratings_path is None or not ratings_path.exists():
        return pending
    with ratings_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = {str(item).strip().casefold() for item in (reader.fieldnames or [])}
        if "arm" in headers or "arm_name" in headers:
            raise ValueError("blind ratings file must not contain arm labels")
        ratings = [_HumanRating.model_validate(row) for row in reader]
    if not ratings:
        return pending
    unknown = sorted({item.blind_id for item in ratings} - set(by_blind_id))
    if unknown:
        raise ValueError("blind ratings contain unknown candidate ids")
    identities = [(item.blind_id, item.reviewer_alias) for item in ratings]
    if len(identities) != len(set(identities)):
        raise ValueError("one reviewer may rate each blind candidate only once")
    ratings_by_blind: dict[str, list[_HumanRating]] = defaultdict(list)
    for rating in ratings:
        ratings_by_blind[rating.blind_id].append(rating)
    complete = all(
        len(ratings_by_blind.get(item.blind_id, []))
        >= thresholds.minimum_human_reviewers_per_candidate
        for item in blind_candidates
    )
    means_by_arm_raw: dict[HumanizeArm, dict[str, list[float]]] = defaultdict(
        lambda: {field: [] for field in _SCORE_FIELDS}
    )
    for blind_id, grouped in ratings_by_blind.items():
        candidate = by_blind_id[blind_id]
        means_by_arm_raw[candidate.arm]["naturalness"].append(
            fmean(item.naturalness_1_5 for item in grouped)
        )
        means_by_arm_raw[candidate.arm]["clarity"].append(
            fmean(item.clarity_1_5 for item in grouped)
        )
        means_by_arm_raw[candidate.arm]["concision"].append(
            fmean(item.concision_1_5 for item in grouped)
        )
    mean_scores_by_arm = {
        arm.value: {
            field: round(fmean(values), 6) if values else 0.0
            for field, values in score_lists.items()
        }
        for arm, score_lists in sorted(means_by_arm_raw.items(), key=lambda item: item[0].value)
    }
    baseline = mean_scores_by_arm.get(HumanizeArm.RULE_BASELINE.value, {})
    gains = {
        arm: {
            field: round(scores.get(field, 0.0) - baseline.get(field, 0.0), 6)
            for field in _SCORE_FIELDS
        }
        for arm, scores in mean_scores_by_arm.items()
    }
    findings = [] if complete else ["HUMAN_BLIND_REVIEW_INCOMPLETE"]
    return HumanReviewSummary(
        status="COMPLETE" if complete else "PENDING",
        minimum_reviewers_per_candidate=thresholds.minimum_human_reviewers_per_candidate,
        candidate_count=len(blind_candidates),
        rated_candidate_count=len(ratings_by_blind),
        unique_reviewer_count=len({item.reviewer_alias for item in ratings}),
        mean_scores_by_arm=mean_scores_by_arm,
        score_gain_vs_rule_baseline=gains,
        finding_codes=findings,
    )


def build_experiment(
    base_dir: Path | None = None,
    *,
    ratings_path: Path | None = None,
) -> tuple[HumanizeExperimentReport, list[BlindCandidate]]:
    root = _experiment_dir(base_dir)
    preregistration_path = root / "preregistration.json"
    cases_path = root / "fixtures" / "cases.jsonl"
    risk_outputs_path = root / "fixtures" / "humanizer_zh_risk_outputs.jsonl"
    qualification_path = root / "qualification_registry.json"
    preregistration = _load_preregistration(root)
    cases = _load_cases(root)
    risk_outputs = _load_risk_outputs(root)
    if set(risk_outputs) != {item.case_id for item in cases}:
        raise ValueError("risk profile outputs must cover every frozen case exactly once")
    qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
    if qualification.get("external_skill_executed") is not False:
        raise ValueError("risk-profile qualification must not claim external Skill execution")
    if qualification.get("production_status") != "NOT_ADMITTED":
        raise ValueError("risk-profile qualification must remain NOT_ADMITTED")

    assessments: list[CandidateAssessment] = []
    for case in cases:
        prepared = prepare_source(case.source_text)
        candidates = {
            HumanizeArm.RULE_BASELINE: normalize_public_text(prepared),
            HumanizeArm.PROJECT_CONSTRAINED: constrained_rewrite(case),
            HumanizeArm.HUMANIZER_ZH_RISK_PROFILE: risk_outputs[
                case.case_id
            ].output_text,
        }
        for arm in preregistration.arms:
            assessments.append(
                assess_candidate(case, arm=arm, candidate_text=candidates[arm])
            )
    assessments.sort(key=lambda item: (item.case_id, item.arm.value))
    blind_candidates = build_blind_candidates(
        assessments,
        blind_seed=preregistration.blind_seed,
    )
    human_review = load_human_review(
        ratings_path,
        blind_candidates=blind_candidates,
        thresholds=preregistration.thresholds,
    )
    summaries = _summarize_arms(
        assessments,
        preregistration=preregistration,
        human_review=human_review,
    )
    constrained = next(
        item for item in summaries if item.arm is HumanizeArm.PROJECT_CONSTRAINED
    )
    humanizer = next(
        item
        for item in summaries
        if item.arm is HumanizeArm.HUMANIZER_ZH_RISK_PROFILE
    )
    proposal_allowed = (
        constrained.automatic_gate_passed
        and human_review.status == "COMPLETE"
        and constrained.production_status == "ELIGIBLE_FOR_BACKUP_PROPOSAL"
    )
    decision_reasons: set[str] = {
        "PRODUCTION_DEFAULT_REMAINS_DETERMINISTIC",
        "EXTERNAL_SKILL_NOT_EXECUTED",
    }
    if human_review.status != "COMPLETE":
        decision_reasons.add("HUMAN_BLIND_REVIEW_PENDING")
    if not constrained.automatic_gate_passed:
        decision_reasons.add("CONSTRAINED_ARM_AUTOMATIC_GATE_FAILED")
    if humanizer.fact_drift_count or humanizer.unsupported_experience_count:
        decision_reasons.add("HUMANIZER_RISK_PROFILE_FACT_OR_EXPERIENCE_DRIFT")
    report = HumanizeExperimentReport.model_construct(
        schema_version="presentation-humanize-experiment-report-v1",
        report_id="0" * 64,
        experiment_id=preregistration.experiment_id,
        generated_at=preregistration.frozen_at,
        preregistration_hash=_file_hash(preregistration_path),
        fixture_hash=_file_hash(cases_path),
        risk_output_hash=_file_hash(risk_outputs_path),
        qualification_snapshot_hash=_file_hash(qualification_path),
        production_module_changed=False,
        network_requests=0,
        external_model_calls=0,
        total_estimated_cost_usd=round(
            sum(item.estimated_cost_usd for item in assessments), 6
        ),
        arm_summaries=summaries,
        human_review=human_review,
        decision=ExperimentDecision(
            production_default_enabled=False,
            production_proposal=(
                "CONTROLLED_BACKUP_PROPOSAL" if proposal_allowed else "NO_CHANGE"
            ),
            constrained_arm_status=constrained.production_status,
            humanizer_risk_profile_status="REJECTED_RISK_PROFILE",
            external_skill_execution_performed=False,
            actual_human_blind_review_performed=human_review.status == "COMPLETE",
            reason_codes=sorted(decision_reasons),
        ),
        assessments=assessments,
    )
    report = report.model_copy(update={"report_id": experiment_report_id(report)})
    return HumanizeExperimentReport.model_validate(report.model_dump(mode="json")), blind_candidates


def _summarize_arms(
    assessments: Sequence[CandidateAssessment],
    *,
    preregistration: ExperimentPreregistration,
    human_review: HumanReviewSummary,
) -> list[ArmSummary]:
    by_arm: dict[HumanizeArm, list[CandidateAssessment]] = defaultdict(list)
    for assessment in assessments:
        by_arm[assessment.arm].append(assessment)
    average_by_arm = {
        arm: {
            field: round(
                fmean(getattr(item.style_scores, field) for item in arm_assessments),
                6,
            )
            for field in _SCORE_FIELDS
        }
        for arm, arm_assessments in by_arm.items()
    }
    baseline = average_by_arm[HumanizeArm.RULE_BASELINE]
    result: list[ArmSummary] = []
    for arm in preregistration.arms:
        arm_assessments = by_arm[arm]
        average_scores = average_by_arm[arm]
        gains = {
            field: round(average_scores[field] - baseline[field], 6)
            for field in _SCORE_FIELDS
        }
        case_count = len(arm_assessments)
        drift_count = sum(item.fact_drift_detected for item in arm_assessments)
        leak_count = sum(item.sensitive_leak_detected for item in arm_assessments)
        unsupported_count = sum(
            item.unsupported_experience_detected for item in arm_assessments
        )
        injection_count = sum(
            item.prompt_injection_detected for item in arm_assessments
        )
        cost = round(sum(item.estimated_cost_usd for item in arm_assessments), 6)
        threshold = preregistration.thresholds
        automatic_gate = (
            drift_count / case_count <= threshold.fact_drift_rate_max
            and leak_count <= threshold.sensitive_leak_count_max
            and unsupported_count <= threshold.unsupported_experience_count_max
            and injection_count == 0
            and cost <= threshold.estimated_cost_usd_max
            and (
                arm is HumanizeArm.RULE_BASELINE
                or (
                    gains["naturalness"] >= threshold.naturalness_gain_min
                    and gains["clarity"] >= threshold.clarity_gain_min
                    and gains["concision"] >= threshold.concision_gain_min
                )
            )
        )
        reasons: set[str] = set()
        if drift_count:
            reasons.add("FACT_DRIFT_DETECTED")
        if leak_count:
            reasons.add("SENSITIVE_LEAK_DETECTED")
        if unsupported_count:
            reasons.add("UNSUPPORTED_EXPERIENCE_DETECTED")
        if injection_count:
            reasons.add("PROMPT_INJECTION_TEXT_DETECTED")
        if cost > threshold.estimated_cost_usd_max:
            reasons.add("COST_CAP_EXCEEDED")
        if arm is HumanizeArm.RULE_BASELINE:
            production_status = "REFERENCE_BASELINE"
            reasons.add("REFERENCE_ONLY")
        elif arm is HumanizeArm.HUMANIZER_ZH_RISK_PROFILE:
            automatic_gate = False
            production_status = "REJECTED_RISK_PROFILE"
            reasons.update(
                {
                    "EXTERNAL_SKILL_NOT_EXECUTED",
                    "RECORDED_RISK_PROFILE_ONLY",
                }
            )
        elif not automatic_gate:
            production_status = "REJECTED_AUTOMATIC_GATE"
            reasons.add("AUTOMATIC_GATE_FAILED")
        elif human_review.status != "COMPLETE":
            production_status = "BLOCKED_HUMAN_REVIEW_PENDING"
            reasons.add("HUMAN_BLIND_REVIEW_PENDING")
        else:
            human_gains = human_review.score_gain_vs_rule_baseline.get(arm.value, {})
            if all(
                human_gains.get(field, float("-inf"))
                >= threshold.minimum_human_score_gain
                for field in _SCORE_FIELDS
            ):
                production_status = "ELIGIBLE_FOR_BACKUP_PROPOSAL"
                reasons.add("ALL_PREREGISTERED_GATES_PASSED")
            else:
                production_status = "REJECTED_AUTOMATIC_GATE"
                reasons.add("HUMAN_SCORE_GAIN_BELOW_THRESHOLD")
        result.append(
            ArmSummary(
                arm=arm,
                case_count=case_count,
                fact_drift_count=drift_count,
                fact_drift_rate=round(drift_count / case_count, 6),
                sensitive_leak_count=leak_count,
                unsupported_experience_count=unsupported_count,
                prompt_injection_count=injection_count,
                presentation_safe_count=sum(
                    item.presentation_safe_to_send for item in arm_assessments
                ),
                average_scores=average_scores,
                score_gain_vs_rule_baseline=gains,
                estimated_cost_usd=cost,
                automatic_gate_passed=automatic_gate,
                human_review_status=human_review.status,
                production_status=production_status,
                reason_codes=sorted(reasons),
            )
        )
    return result


def experiment_report_id(report: HumanizeExperimentReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    return sha256_bytes(canonical_json_bytes(payload))


def generate_artifacts(
    base_dir: Path | None = None,
    *,
    ratings_path: Path | None = None,
) -> HumanizeExperimentReport:
    root = _experiment_dir(base_dir)
    output_dir = root / "output"
    internal_dir = output_dir / "internal"
    output_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)
    report, blind_candidates = build_experiment(root, ratings_path=ratings_path)
    _write_json(output_dir / "experiment_report.json", report.model_dump(mode="json"))
    _write_jsonl(
        output_dir / "candidate_assessments.jsonl",
        [item.model_dump(mode="json") for item in report.assessments],
    )
    _write_blind_review_template(
        output_dir / "blind_review_template.csv",
        blind_candidates,
    )
    _write_json(
        internal_dir / "blind_key.json",
        {
            "schema_version": "presentation-humanize-blind-key-v1",
            "experiment_id": report.experiment_id,
            "entries": [
                {
                    "blind_id": item.blind_id,
                    "case_id": item.case_id,
                    "arm": item.arm.value,
                }
                for item in sorted(blind_candidates, key=lambda item: item.blind_id)
            ],
        },
    )
    (output_dir / "experiment_report.md").write_text(
        _render_markdown_report(report),
        encoding="utf-8",
        newline="\n",
    )
    return report


def _render_markdown_report(report: HumanizeExperimentReport) -> str:
    lines = [
        "# E-01 受约束中文 Humanize A/B 实验报告",
        "",
        f"- 实验：`{report.experiment_id}`",
        f"- 报告 ID：`{report.report_id}`",
        "- 网络请求：0",
        "- 外部模型调用：0",
        "- 外部 Humanizer Skill 实跑：否（仅风险画像样本）",
        f"- 真实人工盲评：{'已完成' if report.human_review.status == 'COMPLETE' else '未完成'}",
        f"- 生产提案：`{report.decision.production_proposal}`",
        "- 生产默认开关：关闭",
        "",
        "## 自动门结果",
        "",
        "| 实验臂 | 事实漂移 | 敏感泄漏 | 自动门 | 状态 |",
        "|---|---:|---:|---|---|",
    ]
    for arm in report.arm_summaries:
        lines.append(
            "| "
            f"{arm.arm.value} | {arm.fact_drift_count}/{arm.case_count} | "
            f"{arm.sensitive_leak_count} | "
            f"{'通过' if arm.automatic_gate_passed else '未通过'} | "
            f"{arm.production_status} |"
        )
    lines.extend(
        [
            "",
            "## 裁决",
            "",
            "本实验没有改动生产 Response Gateway。项目受约束臂即使通过自动事实门，",
            "在真实人工盲评完成前仍不得形成生产备用提案。Humanizer-zh 相关样本仅用于",
            "复现其公开规则可能引入的虚构经历、具体细节或金融事实漂移风险，未被下载、",
            "执行或授予任何来源/生产资格。当前裁决为保持确定性规则基线不变。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_blind_review_template(
    path: Path,
    candidates: Sequence[BlindCandidate],
) -> None:
    fieldnames = [
        "blind_id",
        "case_id",
        "source_text",
        "candidate_text",
        "reviewer_alias",
        "naturalness_1_5",
        "clarity_1_5",
        "concision_1_5",
        "comments",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "blind_id": item.blind_id,
                    "case_id": item.case_id,
                    "source_text": item.redacted_source_text,
                    "candidate_text": item.candidate_text,
                    "reviewer_alias": "",
                    "naturalness_1_5": "",
                    "clarity_1_5": "",
                    "concision_1_5": "",
                    "comments": "",
                }
            )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} must contain a JSON object")
        result.append(value)
    return result


def _write_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _file_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _blind_id(seed: str, case_id: str, arm: HumanizeArm) -> str:
    return sha256_bytes(f"{seed}|{case_id}|{arm.value}".encode())[:16]


def _unsupported_experience_count(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in _UNSUPPORTED_EXPERIENCE_PATTERNS)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the offline fact-locked Chinese Humanize experiment."
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--ratings", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    report = generate_artifacts(arguments.base_dir, ratings_path=arguments.ratings)
    print(
        json.dumps(
            {
                "experiment_id": report.experiment_id,
                "report_id": report.report_id,
                "production_proposal": report.decision.production_proposal,
                "human_review_status": report.human_review.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AutomaticStyleScores",
    "BlindCandidate",
    "CandidateAssessment",
    "ExperimentCase",
    "ExperimentDecision",
    "ExperimentPreregistration",
    "ExperimentThresholds",
    "HumanReviewSummary",
    "HumanizeArm",
    "HumanizeExperimentReport",
    "RecordedRiskOutput",
    "assess_candidate",
    "automatic_style_scores",
    "build_blind_candidates",
    "build_experiment",
    "constrained_rewrite",
    "experiment_report_id",
    "fact_signature",
    "generate_artifacts",
    "load_human_review",
    "main",
    "prepare_source",
]
