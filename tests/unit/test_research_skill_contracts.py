from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills"

SKILLS = {
    "macro-policy-regime": {
        "tasks": ("macro-regime", "policy-regime"),
        "outputs": ("MacroRegimeProfile", "PolicyRegimeProfile"),
        "checks": ("MACRO_REGIME", "POLICY_REGIME"),
        "terms": ("macro", "policy", "point-in-time", "typed"),
    },
    "industry-value-chain": {
        "tasks": ("industry-value-chain",),
        "outputs": ("IndustryValueChainProfile",),
        "checks": ("INDUSTRY_PROFILE",),
        "terms": ("industry", "value chain", "point-in-time", "typed"),
    },
    "catalyst-event-research": {
        "tasks": ("company-catalyst",),
        "outputs": ("CatalystRiskPack",),
        "checks": ("CATALYST_RISK",),
        "terms": ("catalyst", "event", "official", "typed"),
    },
    "governance-management-quality": {
        "tasks": ("governance-management-quality",),
        "outputs": ("GovernanceManagementQualityPack",),
        "checks": ("GOVERNANCE_QUALITY",),
        "terms": ("governance", "related-party", "point-in-time", "typed"),
    },
    "investment-red-team": {
        "tasks": ("investment-red-team",),
        "outputs": ("InvestmentRedTeamReport",),
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
        assert "uv run astock research-team-status" in text
        assert "uv run astock research-team-role-output" in text
        assert "uv run astock research-team-task-result" in text
        assert "artifact" in lower
        assert "abstain" in lower
        assert "broker_execution_allowed=false" in text
        for category in ("tasks", "outputs", "checks", "terms"):
            for term in contract[category]:
                assert term.lower() in lower, (skill_name, term)


def test_company_workflow_dispatches_specialists_without_merging_bull_and_bear() -> None:
    workflow = (
        REPO_ROOT / "docs" / "workflows" / "workflow-current-company-research.md"
    ).read_text(encoding="utf-8")
    collapsed = _collapsed(workflow)

    for skill_name in SKILLS:
        assert f".agents/skills/{skill_name}/SKILL.md" in workflow
    for role in (
        "`macro`",
        "`policy`",
        "`industry`",
        "`catalyst`",
        "`governance`",
        "`bull`",
        "`bear`",
        "`reviewer`",
        "`model-risk`",
    ):
        assert role in workflow
    assert "`bull` and `bear` tasks remain separate, independent contexts" in collapsed
    assert "dispatched only for the downstream `reviewer`" in collapsed
    assert "committee may consume only registered typed outputs" in collapsed


def test_specialist_crosswalk_binds_runtime_roles_and_skills() -> None:
    crosswalk = (
        REPO_ROOT / "docs" / "architecture" / "research-specialist-skills-crosswalk.md"
    ).read_text(encoding="utf-8")
    collapsed = _collapsed(crosswalk)

    for skill_name in SKILLS:
        assert f".agents/skills/{skill_name}/SKILL.md" in crosswalk
    for required in (
        "CurrentResearchContinuation",
        "ResearchTeamService",
        "READY_FOR_INVESTOR_VIEW",
        "OBSERVATION_ONLY_FOR_INVESTOR_VIEW",
        "committee reads only admitted typed outputs",
        "Broker execution remains disabled",
    ):
        assert required in collapsed
    assert "| `reviewer` |" in crosswalk
    assert "`bull-case` and `bear-case`" in crosswalk
    assert "| `bear`, `reviewer` |" not in crosswalk


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
    assert "broker_execution_allowed: false" in policy
