"""Bounded frozen-input questions: no voting, network, or publication authority."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas.full_research import CurrentSourceSnapshot
from astock.schemas.research_team import IndependentBearCase, IndependentBullCase


class QualityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class QualityPolicy(DataModel):
    schema_version: Literal["research-quality-policy-v1"]
    policy_version: str = Field(min_length=1)
    max_request_bytes: int = Field(gt=0, le=1048576, strict=True)
    max_artifact_bytes: int = Field(gt=0, le=16777216, strict=True)
    max_text_chars: int = Field(gt=0, le=65536, strict=True)
    max_sources: int = Field(gt=0, le=4096, strict=True)
    max_issues: int = Field(gt=0, le=128, strict=True)
    max_questions: int = Field(gt=0, le=32, strict=True)
    cross_response_rounds: Literal[0, 1]
    cross_response_limit: int = Field(gt=0, le=8192, strict=True)
    importance_order: tuple[Literal["CRITICAL", "HIGH", "NORMAL"], ...]
    impact_order: tuple[Literal["VERDICT", "VALUATION", "TIMING"], ...]
    max_bars: int = Field(gt=0, le=10000, strict=True)
    max_calendar_days: int = Field(gt=0, le=3660, strict=True)
    holding_sessions: tuple[int, ...]
    max_feedback_samples: int = Field(gt=0, le=4096, strict=True)
    receipt_max_age_hours: int = Field(gt=0, le=8760, strict=True)
    max_events: int = Field(gt=0, le=128, strict=True)
    session_boundaries: tuple[str, str, str, str]

    @model_validator(mode="after")
    def coherent(self):
        if (
            set(self.importance_order) != {"CRITICAL", "HIGH", "NORMAL"}
            or len(self.importance_order) != 3
        ):
            raise ValueError("INVALID_POLICY")
        if (
            set(self.impact_order) != {"VERDICT", "VALUATION", "TIMING"}
            or len(self.impact_order) != 3
        ):
            raise ValueError("INVALID_POLICY")
        if not self.holding_sessions or any(
            type(x) is not int or x <= 0 for x in self.holding_sessions
        ):
            raise ValueError("INVALID_POLICY")
        if len(set(self.holding_sessions)) != len(self.holding_sessions):
            raise ValueError("INVALID_POLICY")
        if (
            max(self.holding_sessions) > self.max_calendar_days
            or self.max_questions > self.max_issues
        ):
            raise ValueError("INVALID_POLICY")
        boundaries = tuple(time.fromisoformat(x) for x in self.session_boundaries)
        if (
            any(x.tzinfo is not None for x in boundaries)
            or tuple(sorted(set(boundaries))) != boundaries
        ):
            raise ValueError("INVALID_POLICY")
        return self


def load_quality_policy(path: Path) -> QualityPolicy:
    try:
        return QualityPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise QualityError("INVALID_QUALITY_POLICY") from None


def digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sealed(kind: str, body: dict) -> dict:
    payload = {"schema_version": kind + "-v1", **body}
    return {**payload, "result_id": kind + ":" + digest(payload)}


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QualityError("AMBIGUOUS_TIME")
    return value.astimezone(UTC)


class DebateIssue(DataModel):
    proposal_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    importance: Literal["CRITICAL", "HIGH", "NORMAL"]
    impact: Literal["VERDICT", "VALUATION", "TIMING"]
    bull_quote: str = Field(min_length=1)
    bear_quote: str = Field(min_length=1)
    question: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class DebateProposal(DataModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    decision_time: AwareDatetime
    independent_context_id: str = Field(min_length=1)
    assessment_complete: bool
    issues: tuple[DebateIssue, ...]


class CrossResponse(DataModel):
    debate_input_hash: str
    round_number: Literal[1]
    side: Literal["BULL", "BEAR"]
    independent_context_id: str = Field(min_length=1)
    issue_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    response: str = Field(min_length=1)


def compile_debate(
    bull: IndependentBullCase,
    bear: IndependentBearCase,
    evidence: CurrentSourceSnapshot,
    proposal: DebateProposal,
    policy: QualityPolicy,
    *,
    responses: tuple[CrossResponse, ...] = (),
) -> dict:
    """Quotes locate existing arguments, not prove semantic truth. Disputes stay open.

    Initial cases remain independent. Generated tasks have stable IDs so the host
    ResearchTeam can resume them, and never form a second persistent task queue.
    """
    cutoff = aware(proposal.decision_time)
    contexts = {
        bull.independent_context_id,
        bear.independent_context_id,
        proposal.independent_context_id,
    }
    if len(contexts) != 3:
        raise QualityError("INDEPENDENCE_MISMATCH")
    if bull.company_id != bear.company_id or proposal.company_id != bull.company_id:
        raise QualityError("SECURITY_MISMATCH")
    if bull.as_of != cutoff or bear.as_of != cutoff or evidence.captured_at > cutoff:
        raise QualityError("DEBATE_TIME_MISMATCH")
    if len(proposal.issues) > policy.max_issues or len(evidence.sources) > policy.max_sources:
        raise QualityError("INPUT_LIMIT")
    texts = [bull.case, bear.case, proposal.independent_context_id]
    for issue in proposal.issues:
        texts.extend(
            (issue.proposal_id, issue.topic_id, issue.bull_quote, issue.bear_quote, issue.question)
        )
        if len(issue.evidence_ids) > policy.max_sources:
            raise QualityError("INPUT_LIMIT")
    if any(len(x) > policy.max_text_chars for x in texts):
        raise QualityError("INPUT_LIMIT")
    visible = {}
    for source in evidence.sources:
        clocks = [source.captured_at, source.available_to_system_at]
        clocks.extend(
            x for x in (source.observed_at, source.release_at, source.published_at) if x is not None
        )
        if max(clocks) <= cutoff and source.evidence_id:
            old = visible.get(source.evidence_id)
            if old is not None and old.object_hash != source.object_hash:
                raise QualityError("EVIDENCE_IDENTITY_CONFLICT")
            visible[source.evidence_id] = source
    buckets = {(a, b): [] for a in policy.importance_order for b in policy.impact_order}
    rejected, seen_ids = [], set()
    merged = {}
    importance_rank = {v: i for i, v in enumerate(policy.importance_order)}
    impact_rank = {v: i for i, v in enumerate(policy.impact_order)}
    for issue in proposal.issues:
        if issue.proposal_id in seen_ids:
            raise QualityError("DUPLICATE_PROPOSAL_ID")
        seen_ids.add(issue.proposal_id)
        if issue.bull_quote not in bull.case or issue.bear_quote not in bear.case:
            rejected.append({"proposal_id": issue.proposal_id, "code": "CASE_QUOTE_NOT_BOUND"})
            continue
        if any(x not in visible for x in issue.evidence_ids):
            rejected.append(
                {"proposal_id": issue.proposal_id, "code": "EVIDENCE_NOT_VISIBLE_OR_UNVERIFIED"}
            )
            continue
        sources = sorted({visible[x].object_hash for x in issue.evidence_ids})
        # Source overlap alone does not make two substantive questions duplicates.
        key = (issue.topic_id, tuple(sources), issue.bull_quote, issue.bear_quote, issue.question)
        question = {
            "topic_id": issue.topic_id,
            "importance": issue.importance,
            "impact": issue.impact,
            "bull_quote": issue.bull_quote,
            "bear_quote": issue.bear_quote,
            "question": issue.question,
            "source_hashes": sources,
            "evidence_ids": sorted(set(issue.evidence_ids)),
        }
        prior = merged.get(key)
        if prior is None:
            merged[key] = question
        else:
            prior["importance"] = min(
                (prior["importance"], issue.importance), key=lambda item: importance_rank[item]
            )
            prior["impact"] = min(
                (prior["impact"], issue.impact), key=lambda item: impact_rank[item]
            )
            prior["evidence_ids"] = sorted(set(prior["evidence_ids"]) | set(issue.evidence_ids))
    for question in merged.values():
        question["issue_id"] = "debate-issue:" + digest(question)
        buckets[(question["importance"], question["impact"])].append(question)
    ordered = [
        x for a in policy.importance_order for b in policy.impact_order for x in buckets[(a, b)]
    ]
    questions = ordered[: policy.max_questions]
    deferred = [x["issue_id"] for x in ordered[policy.max_questions :]]
    input_body = {
        "policy": policy.model_dump(mode="json"),
        "bull": bull.model_dump(mode="json"),
        "bear": bear.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"),
        "visible_evidence": [
            {
                "evidence_id": key,
                "source_hash": value.object_hash,
                "visibility_boundary": aware(value.available_to_system_at).isoformat(),
            }
            for key, value in sorted(visible.items())
        ],
        "evidence_request_id": evidence.request_id,
        "evidence_capture_boundary": aware(evidence.captured_at).isoformat(),
        "questions": questions,
        "deferred_issue_ids": deferred,
        "open_conflict_ids": sorted(
            x.conflict_id for x in evidence.conflicts if x.status == "OPEN"
        ),
    }
    input_hash = digest(input_body)
    allowed_issues = {x["issue_id"] for x in questions}
    allowed_evidence = {x for q in questions for x in q["evidence_ids"]}
    response_sides, response_contexts = set(), set(contexts)
    if len(responses) > 2 or (responses and not policy.cross_response_rounds):
        raise QualityError("CROSS_RESPONSE_LIMIT")
    for response in responses:
        if response.debate_input_hash != input_hash or response.side in response_sides:
            raise QualityError("CROSS_RESPONSE_BINDING")
        if response.independent_context_id in response_contexts:
            raise QualityError("INDEPENDENCE_MISMATCH")
        if (
            len(response.response) > min(policy.max_text_chars, policy.cross_response_limit)
            or len(response.issue_ids) > policy.max_questions
            or len(response.evidence_ids) > policy.max_sources
        ):
            raise QualityError("INPUT_LIMIT")
        if (
            not set(response.issue_ids) <= allowed_issues
            or not set(response.evidence_ids) <= allowed_evidence
        ):
            raise QualityError("CROSS_RESPONSE_OUTSIDE_FROZEN_INPUT")
        response_sides.add(response.side)
        response_contexts.add(response.independent_context_id)
    incomplete = bool(
        rejected or deferred or not proposal.assessment_complete or evidence.status != "PASS"
    )
    status = "INCOMPLETE" if incomplete else ("DISPUTED" if questions else "NO_ISSUES_PROPOSED")
    tasks = []
    if questions and policy.cross_response_rounds:
        tasks = [
            {
                "task_id": "cross-response:" + digest({"input": input_hash, "side": side}),
                "role": side,
                "round_number": 1,
                "debate_input_hash": input_hash,
                "issue_ids": [q["issue_id"] for q in questions],
                "generation_limit": policy.cross_response_limit,
                "independent_context_required": True,
                "frozen_evidence_only": True,
            }
            for side in ("BULL", "BEAR")
            if side not in response_sides
        ]
    return sealed(
        "debate-evaluation",
        {
            "context_id": proposal.independent_context_id,
            "company_id": proposal.company_id,
            "decision_boundary": cutoff.isoformat(),
            "policy_version": policy.policy_version,
            "input_hash": input_hash,
            "status": status,
            "reviewer_questions": questions,
            "rejected_proposals": rejected,
            "deferred_issue_ids": deferred,
            "unresolved_issue_ids": [q["issue_id"] for q in questions] + deferred,
            "open_conflict_ids": input_body["open_conflict_ids"],
            "responses": [r.model_dump(mode="json") for r in responses],
            "incremental_tasks": tasks,
            "incremental_generation_budget": len(tasks) * policy.cross_response_limit,
            "publication_authority": False,
            "committee_vote": None,
        },
    )
