from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_company_research_skill_requires_same_request_automatic_continuation() -> None:
    skill = (REPO_ROOT / ".agents" / "skills" / "company-deep-research" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "Same-request automatic continuation contract",
        "AUTO_RESOLUTION_REQUIRED",
        "CurrentResearchAutomaticResolution",
        "TEAM_RESEARCH_REQUIRED",
        "READY_FOR_INVESTOR_VIEW",
        "OBSERVATION_ONLY_FOR_INVESTOR_VIEW",
        "NEEDS_USER_INPUT",
        "investment_conclusion_blocked=true",
        "Broker execution remains forbidden",
    ):
        assert required in skill


def test_current_company_workflow_does_not_return_public_evidence_gaps_to_user() -> None:
    workflow = (
        REPO_ROOT / "docs" / "workflows" / "workflow-current-company-research.md"
    ).read_text(encoding="utf-8")

    for required in (
        "Same-request automatic continuation",
        "exact official",
        "typed automatic-resolution artifact",
        "same_request_continuation_required=true",
        "investment_conclusion_blocked=true",
        "broker_execution_allowed=false",
        "genuinely private source material",
    ):
        assert required in workflow
