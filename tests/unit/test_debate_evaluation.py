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


@pytest.mark.parametrize("same_value", [False, True])
def test_team_reviewer_inputs_bind_registered_cases_once(tmp_path, same_value):
    from types import SimpleNamespace

    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from astock.research.team import ResearchTeamService
    from astock.schemas.research_team import (
        ResearchRoleOutput,
        ResearchRoleResult,
        ResearchTeamTaskState,
    )

    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    team = object.__new__(ResearchTeamService)
    team.state = StateStore(store.path)
    team.objects = ObjectStore(tmp_path / "objects")
    team.project_root = ROOT
    from typing import Any, cast

    team.get_plan = lambda plan_id: cast(Any, SimpleNamespace(company_id="600000"))
    _, bull, bear, _, _ = inputs()
    if same_value:
        bear = bear.model_copy(update={"valuation_per_share": bull.valuation_per_share})
    for task_id, case in (("bull-case", bull), ("bear-case", bear)):
        ref = team.objects.put_json(case.model_dump(mode="json"))
        team.state.register_artifact(
            artifact_id=task_id,
            artifact_type=type(case).__name__,
            schema_version=case.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
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
    first = team.reviewer_input("plan")
    assert first == team.reviewer_input("plan")
    assert bool(first["reviewer_questions"]) is not same_value
    assert first["additional_llm_calls"] == 0
    assert first["publication_authority"] is False
    assert len(team.state.recent_artifact_records("ReviewerDisputeInput")) == 1
