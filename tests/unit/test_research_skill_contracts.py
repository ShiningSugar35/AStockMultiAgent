from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from astock.research.team import ResearchTeamService
from astock.schemas.research_team import ResearchTaskRole

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills"
CROSSWALK = REPO_ROOT / "docs" / "architecture" / "research-specialist-skills-crosswalk.md"

SKILLS = {
    "macro-policy-regime": {
        "tasks": ("macro-regime", "policy-regime"),
        "outputs": ("MacroResearchOutcome", "PolicyRegimeProfile"),
        "checks": ("MACRO_REGIME", "POLICY_REGIME"),
        "terms": ("macro", "policy", "point-in-time", "typed"),
    },
    "industry-value-chain": {
        "tasks": ("industry-value-chain",),
        "outputs": ("IndustryResearchOutcome",),
        "checks": ("INDUSTRY_PROFILE",),
        "terms": ("industry", "value chain", "point-in-time", "typed"),
    },
    "catalyst-event-research": {
        "tasks": ("company-catalyst",),
        "outputs": ("NewsEventResearchPack",),
        "checks": ("CATALYST_RISK",),
        "terms": ("catalyst", "event", "official", "typed"),
    },
    "governance-management-quality": {
        "tasks": ("governance-management-quality",),
        "outputs": ("GovernanceAssessment",),
        "checks": ("GOVERNANCE_QUALITY",),
        "terms": ("governance", "related-party", "point-in-time", "typed"),
    },
    "investment-red-team": {
        "tasks": ("investment-red-team",),
        "outputs": ("ChallengerAssessment",),
        "checks": ("INDEPENDENT_REVIEW",),
        "terms": ("red team", "disconfirming", "kill criteria", "typed"),
    },
    "model-risk-backtest-validation": {
        "tasks": ("model-risk-validation",),
        "outputs": ("ModelRiskValidationReport",),
        "checks": ("MODEL_RISK_VALIDATION",),
        "terms": ("model-risk", "leakage", "transaction costs", "typed"),
    },
}


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _references_shared_contract(path: Path, text: str) -> bool:
    targets = re.findall(
        r"\[[^\]]+\]\(([^)]+)#shared-execution-contract\)",
        text,
    )
    return any((path.parent / target).resolve() == CROSSWALK for target in targets)


def test_specialist_research_skills_are_canonical_and_executable() -> None:
    for skill_name, contract in SKILLS.items():
        path = SKILL_ROOT / skill_name / "SKILL.md"
        assert path.is_file(), skill_name
        text = path.read_text(encoding="utf-8")
        lower = text.lower()

        assert text.startswith("---\n")
        assert f"name: {skill_name}\n" in text
        assert "description:" in text
        for heading in (
            "## Inputs",
            "## Procedure",
            "## Required output contract",
            "## Gates and abstention",
            "## Verification",
        ):
            assert heading in text, (skill_name, heading)
        assert _references_shared_contract(path, text), skill_name
        assert "artifact" in lower
        assert "abstain" in lower
        for category in ("tasks", "outputs", "checks", "terms"):
            for term in contract[category]:
                assert term.lower() in lower, (skill_name, term)


def test_company_workflow_dispatches_specialists_without_merging_bull_and_bear() -> None:
    workflow = (
        REPO_ROOT / "docs" / "workflows" / "workflow-current-company-research.md"
    ).read_text(encoding="utf-8")
    lower = _collapsed(workflow).lower()
    shared = CROSSWALK.read_text(encoding="utf-8")

    assert "$company-deep-research" in workflow
    for role in (
        "macro", "policy", "industry", "catalyst", "governance",
        "bull", "bear", "reviewer", "model-risk",
    ):
        assert re.search(rf"\b{re.escape(role)}\b", lower), role
    assert re.search(r"\btyped\b[^.\n]*\boutputs\b", lower)
    assert re.search(r"\bcommittee\b[^.\n]*\bcannot\b[^.\n]*\bfetch\b", lower)

    # Task identity and dependencies enforce separation without duplicating role tables.
    tasks = {
        task.task_id: task
        for task in ResearchTeamService._company_tasks(datetime(2026, 1, 1, tzinfo=UTC))
    }
    bull, bear = tasks["bull-case"], tasks["bear-case"]
    reviewer = tasks["investment-red-team"]
    assert bull.role is ResearchTaskRole.BULL
    assert bear.role is ResearchTaskRole.BEAR
    assert reviewer.role is ResearchTaskRole.REVIEWER
    assert bull.output_contract == "IndependentBullCase"
    assert bear.output_contract == "IndependentBearCase"
    assert reviewer.output_contract == "ChallengerAssessment"
    assert "bear-case" not in bull.dependencies
    assert "bull-case" not in bear.dependencies
    assert set(reviewer.dependencies) == {"bull-case", "bear-case"}
    assert reviewer.task_id in tasks["committee"].dependencies

    assert re.search(
        r"\bBull\b[^\n]*\bBear\b[^\n]*\bdifferent\b[^\n]*independent_context_id",
        shared,
    )
    assert re.search(r"\bReviewer\b[^\n]*\bneither\b[^\n]*\bcontext\b", shared)
    assert re.search(r"\bReviewer\b[^\n]*\bdoes not browse\b", shared)
    assert re.search(r"\bCommittee\b[^\n]*\bonly\b[^\n]*\bfrozen\b[^\n]*\badmitted\b", shared)


def test_specialist_crosswalk_binds_runtime_roles_and_skills() -> None:
    crosswalk = CROSSWALK.read_text(encoding="utf-8")
    assert re.search(r"^## Shared execution contract\s*$", crosswalk, re.MULTILINE)
    assert "ResearchRoleResult" in crosswalk

    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in crosswalk.splitlines()
        if line.startswith("|")
    ]
    for skill_name, contract in SKILLS.items():
        expected_skill = SKILL_ROOT / skill_name / "SKILL.md"
        matched_rows = []
        for row in rows:
            if len(row) != 4:
                continue
            links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", row[1])
            if any((CROSSWALK.parent / link).resolve() == expected_skill for link in links):
                matched_rows.append(row)
        assert len(matched_rows) == 1, skill_name
        assert expected_skill.is_file(), skill_name
        row = matched_rows[0]
        assert set(re.findall(r"`([^`]+)`", row[0])) == set(contract["tasks"])
        assert set(re.findall(r"`([^`]+)`", row[2])) == set(contract["outputs"])
        assert set(re.findall(r"`([^`]+)`", row[3])) == set(contract["checks"])


def test_python_team_dag_and_policy_bind_specialist_contracts() -> None:
    team_source = (REPO_ROOT / "src" / "astock" / "research" / "team.py").read_text(
        encoding="utf-8"
    )
    policy = (REPO_ROOT / "configs" / "research_team.yaml").read_text(encoding="utf-8")

    for contract in SKILLS.values():
        for task in contract["tasks"]:
            assert f'"{task}"' in team_source
        for output in contract["outputs"]:
            assert f'"{output}"' in team_source
        for check in contract["checks"]:
            assert f'"{check}"' in team_source
            assert f"- {check}" in policy
    assert '"bull-case"' in team_source
    assert '"bear-case"' in team_source
    assert '["bear-case", "bull-case"]' in team_source
