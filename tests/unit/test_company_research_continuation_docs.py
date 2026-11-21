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
    ):
        assert required in workflow

    closure_rows = {
        line.split("`", 2)[1]: line
        for line in workflow.splitlines()
        if line.startswith("| `")
    }
    for state, required_terms in {
        "PUBLIC_DATA_UNAVAILABLE": ("exhausted", "no private-material demand"),
        "BUDGET_EXHAUSTED": (
            "Time/attempt", "bound evidence locally", "without claiming public-route exhaustion",
        ),
        "NEEDS_USER_INPUT": (
            "private", "authorization", "dependency only", "independent capabilities continue",
        ),
    }.items():
        assert state in closure_rows
        for term in required_terms:
            assert term in closure_rows[state], (state, term)
