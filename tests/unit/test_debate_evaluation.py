from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.research.debate_evaluation import (
    CrossResponse,
    DebateProposal,
    QualityError,
    compile_debate,
    load_quality_policy,
)
from astock.schemas.full_research import CurrentSourceSnapshot, SourceLineageEntry
from astock.schemas.research_team import IndependentBearCase, IndependentBullCase

NOW = datetime(2026, 11, 2, 8, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def inputs():
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    bull = IndependentBullCase.model_validate(
        dict(
            created_at=NOW,
            as_of=NOW,
            company_id="600000",
            independent_context_id="bull",
            case="利润增长可持续",
            valuation_per_share="15",
        )
    )
    bear = IndependentBearCase.model_validate(
        dict(
            created_at=NOW,
            as_of=NOW,
            company_id="600000",
            independent_context_id="bear",
            case="利润增长可能逆转",
            valuation_per_share="8",
        )
    )

    def source(identity):
        return SourceLineageEntry.model_validate(
            dict(
                created_at=NOW,
                source_id=identity,
                evidence_id=identity,
                family="FINANCIAL_REPORT",
                authority="PRIMARY_OFFICIAL",
                provider="official",
                source_identity=identity,
                object_hash="a" * 64,
                published_at=NOW,
                captured_at=NOW,
                available_to_system_at=NOW,
                ingestion_version="1",
                parser_version="1",
            )
        )

    evidence = CurrentSourceSnapshot.model_validate(
        dict(
            created_at=NOW,
            snapshot_id="sources",
            request_id="request",
            captured_at=NOW,
            sources=(source("e1"), source("e2")),
            critical_source_families=("FINANCIAL_REPORT",),
            lineage_coverage="1",
            status="PASS",
        )
    )
    issue = dict(
        proposal_id="one",
        topic_id="profit",
        importance="HIGH",
        impact="VALUATION",
        bull_quote=bull.case,
        bear_quote=bear.case,
        question="利润增长能否持续？",
        evidence_ids=["e1", "e2"],
    )
    proposal = DebateProposal.model_validate(
        dict(
            company_id="600000",
            decision_time=NOW,
            independent_context_id="compiler",
            assessment_complete=True,
            issues=[issue],
        )
    )
    return policy, bull, bear, evidence, proposal


def test_source_dedup_and_zero_tasks_without_dispute():
    policy, bull, bear, evidence, proposal = inputs()
    output = compile_debate(bull, bear, evidence, proposal, policy)
    assert output["reviewer_questions"][0]["source_hashes"] == ["a" * 64]
    empty = proposal.model_copy(update={"issues": ()})
    result = compile_debate(bull, bear, evidence, empty, policy)
    assert result["status"] == "NO_ISSUES_PROPOSED"
    assert result["incremental_tasks"] == []
    assert result["incremental_generation_budget"] == 0
    assert result["publication_authority"] is False


def test_identity_and_independence():
    policy, bull, bear, evidence, proposal = inputs()
    with pytest.raises(QualityError, match="SECURITY_MISMATCH"):
        compile_debate(
            bull, bear.model_copy(update={"company_id": "000001"}), evidence, proposal, policy
        )
    with pytest.raises(QualityError, match="INDEPENDENCE_MISMATCH"):
        compile_debate(
            bull,
            bear.model_copy(update={"independent_context_id": "bull"}),
            evidence,
            proposal,
            policy,
        )


def test_future_evidence_cannot_change_prior_result():
    policy, bull, bear, evidence, proposal = inputs()
    original = compile_debate(bull, bear, evidence, proposal, policy)
    future = evidence.sources[0].model_copy(
        update={
            "source_id": "future",
            "evidence_id": "future",
            "available_to_system_at": NOW + timedelta(days=1),
        }
    )
    extended = evidence.model_copy(update={"sources": (*evidence.sources, future)})
    assert compile_debate(bull, bear, extended, proposal, policy) == original


def test_missing_evidence_is_not_no_dispute():
    policy, bull, bear, evidence, proposal = inputs()
    issue = proposal.issues[0].model_copy(update={"evidence_ids": ("missing",)})
    result = compile_debate(
        bull, bear, evidence, proposal.model_copy(update={"issues": (issue,)}), policy
    )
    assert result["status"] == "INCOMPLETE"
    assert result["rejected_proposals"]


def test_response_does_not_resolve_counterevidence():
    policy, bull, bear, evidence, proposal = inputs()
    first = compile_debate(bull, bear, evidence, proposal, policy)
    response = CrossResponse.model_validate(
        dict(
            debate_input_hash=first["input_hash"],
            round_number=1,
            side="BULL",
            independent_context_id="cross-bull",
            issue_ids=first["unresolved_issue_ids"],
            evidence_ids=["e1"],
            response="反证尚未排除。",
        )
    )
    result = compile_debate(bull, bear, evidence, proposal, policy, responses=(response,))
    assert result["unresolved_issue_ids"] == first["unresolved_issue_ids"]
    assert [task["role"] for task in result["incremental_tasks"]] == ["BEAR"]
    assert result["publication_authority"] is False
    with pytest.raises(QualityError, match="CROSS_RESPONSE_BINDING"):
        compile_debate(bull, bear, evidence, proposal, policy, responses=(response, response))


def test_duplicate_source_issue_and_roundtrip():
    import json

    policy, bull, bear, evidence, proposal = inputs()
    duplicate = proposal.issues[0].model_copy(
        update={"proposal_id": "two", "evidence_ids": ("e2",)}
    )
    result = compile_debate(
        bull,
        bear,
        evidence,
        proposal.model_copy(update={"issues": (*proposal.issues, duplicate)}),
        policy,
    )
    assert len(result["reviewer_questions"]) == 1
    assert json.loads(json.dumps(result)) == result


def test_host_dispute_reviewer_once_and_reserved_failure(tmp_path):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.team import ResearchTeamService

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    team = object.__new__(ResearchTeamService)
    team.state = StateStore(store.path)
    team.objects = ObjectStore(tmp_path / "objects")
    payload = {
        "status": "DISPUTED",
        "maximum_rounds": 1,
        "generation_limit": 50,
        "artifact_id": "frozen-input",
    }
    team.reviewer_input = lambda plan_id: payload
    calls = []

    def reviewer(value):
        calls.append(value)
        return "Unresolved assumptions require evidence."

    first = team.run_dispute_reviewer("plan", reviewer)
    assert first["status"] == "COMPLETE"
    assert team.run_dispute_reviewer("plan", reviewer) == first
    assert len(calls) == 1
    assert first["publication_authority"] is False
    payload.update(status="NO_DISAGREEMENT", artifact_id="empty")
    assert team.run_dispute_reviewer("plan", reviewer)["status"] == "SKIPPED"
    assert len(calls) == 1
    payload.update(status="DISPUTED", artifact_id="interrupted")

    def broken(_):
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        team.run_dispute_reviewer("plan", broken)
    assert team.run_dispute_reviewer("plan", reviewer)["status"] == "RESERVED"
    assert len(calls) == 1


def test_dispute_reviewer_rejects_over_budget_response(tmp_path):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.team import ResearchTeamService

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    team = object.__new__(ResearchTeamService)
    team.state = StateStore(store.path)
    team.objects = ObjectStore(tmp_path / "objects")
    team.reviewer_input = lambda plan_id: {
        "status": "DISPUTED",
        "maximum_rounds": 1,
        "generation_limit": 3,
        "artifact_id": "input",
    }
    with pytest.raises(ValueError, match="REVIEWER_RESPONSE_OUT_OF_BOUNDS"):
        team.run_dispute_reviewer("plan", lambda _: "too long")
    assert team.run_dispute_reviewer("plan", lambda _: "ok")["status"] == "RESERVED"


def _registered_case_team(tmp_path, same_value=False, cases=None, source=None, bind_source=True):
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.team import ResearchTeamService
    from astock.schemas.research_team import (
        ResearchRoleOutput,
        ResearchRoleResult,
        ResearchTeamPlan,
        ResearchTeamTaskState,
    )

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    team = object.__new__(ResearchTeamService)
    team.state = StateStore(store.path)
    team.objects = ObjectStore(tmp_path / "objects")
    team.project_root = ROOT
    plan = ResearchTeamPlan.model_validate({
        "plan_id": "plan", "scope": "COMPANY", "company_id": "600000",
        "acquisition_report_artifact_id": "recorded-acquisition",
        "depth": "INSTITUTIONAL", "backend": "CHAT_ORCHESTRATED",
        "as_of": NOW, "created_at": NOW, "policy_version": "recorded-review-status",
        "automatic_resolution_budget_seconds": 60,
        "hardware_budget": {
            "resource_class": "LOW_RESOURCE", "cpu_count": 2, "memory_gib": 8,
            "provider_workers": 1, "agent_workers": 1, "duckdb_threads": 1,
            "max_parallel_companies": 1, "max_deep_candidates": 1, "created_at": NOW,
        },
        "tasks": [
            {
                "task_id": "bull-case", "role": "BULL", "stage": 0,
                "output_contract": "IndependentBullCase", "created_at": NOW,
            },
            {
                "task_id": "bear-case", "role": "BEAR", "stage": 0,
                "output_contract": "IndependentBearCase", "created_at": NOW,
            },
            {
                "task_id": "investment-red-team", "role": "REVIEWER", "stage": 1,
                "dependencies": ["bear-case", "bull-case"],
                "output_contract": "ChallengerAssessment", "created_at": NOW,
            },
        ],
    })
    team.get_plan = lambda plan_id: plan if plan_id == plan.plan_id else None
    _, bull, bear, _, _ = inputs()
    if cases is not None:
        bull, bear = cases
    if same_value:
        bear = bear.model_copy(update={"valuation_per_share": bull.valuation_per_share})
    source_hashes = []
    if source is not None:
        ref = team.objects.put_json(source.model_dump(mode="json"))
        for identity in ("source-one", "source-copy"):
            team.state.register_artifact(
                artifact_id=identity, artifact_type=type(source).__name__,
                schema_version=source.schema_version, object_hash=ref.sha256, input_hashes=[],
            )
        if bind_source:
            source_hashes = [ref.sha256]
    for task_id, case in (("bull-case", bull), ("bear-case", bear)):
        ref = team.objects.put_json(case.model_dump(mode="json"))
        team.state.register_artifact(
            artifact_id=task_id,
            artifact_type=type(case).__name__,
            schema_version=case.schema_version,
            object_hash=ref.sha256,
            input_hashes=source_hashes,
        )
        output = ResearchRoleOutput(
            plan_id="plan",
            task_id=task_id,
            output_contract=type(case).__name__,
            member_artifact_ids=[task_id],
            summary="case",
        )
        ref = team.objects.put_json(output.model_dump(mode="json"))
        team.state.register_artifact(
            artifact_id="output-" + task_id,
            artifact_type="ResearchRoleOutput",
            schema_version=output.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        result = ResearchRoleResult(
            plan_id="plan",
            task_id=task_id,
            state=ResearchTeamTaskState.COMPLETE,
            independent_context_id=task_id,
            output_artifact_ids=["output-" + task_id],
        )
        ref = team.objects.put_json(result.model_dump(mode="json"))
        team.state.set_checkpoint(
            scope_type="research-team-task",
            scope_key="plan:" + task_id,
            cursor={},
            status="COMPLETE",
            object_hash=ref.sha256,
        )
    return team


@pytest.mark.parametrize("same_value", [False, True])
def test_team_reviewer_inputs_bind_registered_cases_once(tmp_path, same_value):
    team = _registered_case_team(tmp_path, same_value=same_value)
    first = team.reviewer_input("plan")
    assert first == team.reviewer_input("plan")
    assert bool(first["reviewer_questions"]) is not same_value
    assert first["additional_llm_calls"] == 0
    assert first["publication_authority"] is False
    assert len(team.state.recent_artifact_records("ReviewerDisputeInput")) == 1


def test_status_does_not_dispatch_or_reserve_dispute_review(tmp_path, monkeypatch):
    from unittest.mock import Mock

    team = _registered_case_team(tmp_path)
    callback = Mock(side_effect=AssertionError("status must not call the host"))
    # Even a legacy attached callback cannot turn a status query into dispatch.
    monkeypatch.setattr(team, "dispute_reviewer", callback, raising=False)
    status = team.status("plan")
    assert status["status"] == "IN_PROGRESS"
    assert status["completed_tasks"] == ["bear-case", "bull-case"]
    assert status["ready_tasks"] == ["investment-red-team"]
    reviewer_input = status["reviewer_input"]
    assert isinstance(reviewer_input, dict)
    assert reviewer_input["status"] == "DISPUTED"
    assert reviewer_input["reviewer_questions"]
    assert reviewer_input["cross_review"] is None
    identity = reviewer_input["artifact_id"]
    assert isinstance(identity, str)
    assert team.status("plan") == status
    callback.assert_not_called()
    assert team.state.get_checkpoint("dispute-reviewer", identity) is None


def test_reviewer_prepared_operation_accepts_one_late_result(tmp_path):
    team = _registered_case_team(tmp_path)
    reservation = team.run_dispute_reviewer("plan")
    assert reservation["dispatch_granted"] and reservation["status"] == "RESERVED"
    repeated = team.run_dispute_reviewer("plan", lambda _: pytest.fail("must not redispatch"))
    assert repeated["operation_id"] == reservation["operation_id"]
    args = dict(
        operation_id=reservation["operation_id"], input_hash=reservation["input_hash"],
        response="The material dispute remains unresolved.",
    )
    identity = reservation["input"]["artifact_id"]
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        team.complete_dispute_reviewer(identity, **{**args, "operation_id": "foreign"})
    result = team.complete_dispute_reviewer(identity, **args)
    assert result["status"] == "COMPLETE" and not result["publication_authority"]
    assert team.complete_dispute_reviewer(identity, **args) == result
    assert team.run_dispute_reviewer("plan") == result
    with pytest.raises(ValueError, match="CALLBACK_CONFLICT"):
        team.complete_dispute_reviewer(identity, **{**args, "response": "different result"})


@pytest.mark.parametrize("kind,material,expected", [
    ("FACT", True, True), ("ASSUMPTION", True, True),
    ("CALCULATION", True, True), ("STYLE", True, False), ("FACT", False, False),
])
def test_material_typed_disputes_bind_sources_without_style_calls(
    tmp_path, kind, material, expected
):
    from astock.schemas.research_team import ResearchCasePosition

    _, bull, bear, _, _ = inputs()
    cases = []
    for case, stance in ((bull, "growth"), (bear, "reversal")):
        position = ResearchCasePosition(
            topic_id="profit", kind=kind, material=material, statement=case.case,
            position=stance, evidence_artifact_ids=["source-one", "source-copy"],
        )
        cases.append(case.model_copy(update={
            "positions": [position], "valuation_per_share": bull.valuation_per_share,
        }))
    source = _position_source()
    team = _registered_case_team(tmp_path, cases=cases, source=source)
    ref = team.objects.put_json(source.model_dump(mode="json"))
    result = team.reviewer_input("plan")
    assert bool(result["reviewer_questions"]) is expected
    if expected:
        assert result["reviewer_questions"][0]["source_hashes"] == [ref.sha256]
    else:
        assert team.run_dispute_reviewer("plan")["status"] == "SKIPPED"
    assert result["additional_llm_calls"] == 0


def test_structured_dispute_with_unbound_quote_rejected(tmp_path):
    from astock.schemas.research_team import ResearchCasePosition

    _, bull, bear, _, _ = inputs()
    bull = bull.model_copy(update={"positions": [ResearchCasePosition(
        topic_id="profit", kind="FACT", material=True, statement="not in the case",
        position="growth", evidence_artifact_ids=["irrelevant"],
    )]})
    team = _registered_case_team(tmp_path, cases=(bull, bear))
    with pytest.raises(ValueError, match="POSITION_NOT_BOUND"):
        team.reviewer_input("plan")


def _position_source():
    from astock.schemas.evidence import EvidenceGrade
    from astock.schemas.research import FrozenEvidencePack, ResearchCoverageStatus

    return FrozenEvidencePack(
        pack_id="recorded-position-input", company_id="600000", as_of=NOW,
        claim_ids=["claim:fixture"], evidence_ids=["evidence:fixture"],
        conflict_ids=[], open_conflict_ids=[],
        evidence_grade_by_id={"evidence:fixture": EvidenceGrade.PRIMARY_OFFICIAL},
        coverage_status=ResearchCoverageStatus.COMPLETE, degradation_codes=[],
        frozen_input_sha256="1" * 64, frozen_at=NOW,
    )


@pytest.mark.parametrize("fault", ["company", "future", "outside-case"])
def test_typed_dispute_rejects_foreign_future_or_unbound_sources(tmp_path, fault):
    from astock.schemas.research_team import ResearchCasePosition

    _, bull, bear, _, _ = inputs()
    bull = bull.model_copy(update={"positions": [ResearchCasePosition(
        topic_id="profit", kind="FACT", material=True, statement=bull.case,
        position="growth", evidence_artifact_ids=["source-one"],
    )]})
    source = _position_source()
    if fault == "company":
        source = source.model_copy(update={"company_id": "000001"})
    elif fault == "future":
        source = source.model_copy(update={"as_of": NOW + timedelta(days=1)})
    team = _registered_case_team(
        tmp_path, cases=(bull, bear), source=source, bind_source=fault != "outside-case",
    )
    with pytest.raises(ValueError, match="POSITION_SOURCE"):
        team.reviewer_input("plan")
