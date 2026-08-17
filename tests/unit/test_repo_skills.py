from __future__ import annotations

from pathlib import Path

import yaml

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"

EXPECTED = {
    "astock-research-orchestrator": ("multi-step", "research"),
    "candidate-scan": ("candidate", "watchlist"),
    "company-deep-research": ("company", "deep"),
    "financial-integrity-audit": ("financial", "audit"),
    "holding-monitor": ("holding", "positions"),
    "paper-trading-recovery": ("paper", "recovery"),
    "portfolio-manager": ("portfolio", "allocation"),
    "knowledge-ingest": ("allowlisted", "history"),
    "evidence-investigation": ("evidence", "gap"),
    "research-tech-scout": ("external", "scout"),
}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert "TODO" not in text
    _, raw, _ = text.split("---", 2)
    return yaml.safe_load(raw)


def test_all_repo_skills_have_valid_trigger_metadata_and_ui() -> None:
    assert {path.name for path in SKILLS_ROOT.iterdir() if path.is_dir()} == set(EXPECTED)
    for name, trigger_terms in EXPECTED.items():
        folder = SKILLS_ROOT / name
        metadata = frontmatter(folder / "SKILL.md")
        assert metadata["name"] == name
        assert set(metadata) == {"name", "description"}
        description = metadata["description"].lower()
        assert all(term in description for term in trigger_terms)
        body = (folder / "SKILL.md").read_text(encoding="utf-8")
        assert "uv run astock" in body
        assert "## Output" in body
        assert "## Prohibitions" in body
        assert "1." in body
        ui = yaml.safe_load((folder / "agents" / "openai.yaml").read_text(encoding="utf-8"))
        assert 25 <= len(ui["interface"]["short_description"]) <= 64
        assert f"${name}" in ui["interface"]["default_prompt"]


def test_agents_file_discovers_every_repo_skill() -> None:
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for name in EXPECTED:
        assert f"${name}" in agents
    assert "禁止未来函数" in agents
    assert "不自动向券商发单" in agents


def test_phase4_repo_skill_commands_exist_and_use_strict_codex_binding() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    expected_commands = {
        "research-chain-status",
        "research-chain-audit",
        "research-evidence-freeze",
        "research-base-case-build",
        "research-specialist-route",
        "research-specialist-diagnose",
        "research-memo-compose",
        "position-plan-status",
        "holding-review-run",
        "holding-review-audit",
        "context-plan",
        "codex-run-init",
        "codex-run-import",
        "codex-run-audit",
    }
    assert expected_commands <= command_names
    for name in (
        "astock-research-orchestrator",
        "company-deep-research",
        "holding-monitor",
    ):
        body = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        assert "--require-registered-output" in body
        assert "codex-run-audit" in body
    assert "research-chain-audit" in (
        SKILLS_ROOT / "company-deep-research" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "holding-review-run" in (
        SKILLS_ROOT / "holding-monitor" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_phase6_repo_skill_commands_exist_and_keep_committee_offline() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    expected_commands = {
        "committee-schema",
        "committee-input-resolve",
        "committee-plan",
        "committee-decide",
        "committee-status",
        "committee-audit",
        "committee-recover",
        "committee-task-status",
        "committee-task-resolve",
    }
    assert expected_commands <= command_names
    for name in (
        "astock-research-orchestrator",
        "company-deep-research",
        "holding-monitor",
        "evidence-investigation",
    ):
        body = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        assert "committee-" in body
        assert "Do not" in body
    orchestrator = (
        SKILLS_ROOT / "astock-research-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "The committee never performs the search itself" in orchestrator


def test_phase7_repo_skill_commands_exist_and_keep_shadow_isolated() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    expected_commands = {
        "shadow-schema",
        "shadow-study-plan",
        "shadow-study-create",
        "shadow-independence-key",
        "shadow-assign",
        "market-regime-classify",
        "shadow-forward-market-freeze",
        "shadow-observation-record",
        "shadow-evaluate",
        "shadow-status",
        "shadow-audit",
        "shadow-recover",
        "phase8-admission",
        "adaptive-research-status",
    }
    assert expected_commands <= command_names
    orchestrator = (
        SKILLS_ROOT / "astock-research-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "ELIGIBLE_RULE_STATE_MACHINE_RESEARCH" in orchestrator
    assert "Do not change weights or the paper ledger" in orchestrator
    assert "AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL" in orchestrator
    assert "explicit rule-research approval" in orchestrator
    recovery = (SKILLS_ROOT / "paper-trading-recovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Shadow studies and their observations must never" in recovery
    assert "Do not write shadow-study results" in recovery
    assert "adaptive-research-status" in recovery
    assert "ledger-write command" in recovery



def test_current_investor_skills_exhaust_policy_driven_automatic_fallback_before_manual_help(
) -> None:
    orchestrator = (
        SKILLS_ROOT / "astock-research-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    company = (SKILLS_ROOT / "company-deep-research" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    investigation = (SKILLS_ROOT / "evidence-investigation" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for body in (orchestrator, company):
        assert "research-acquire-current" in body
        assert "question timestamp" in body or "question-time" in body
        assert "authoritative Web" in body
        assert "manual" in body.lower()
        assert "ProviderRecoveryProposal" in body
        assert "SchemaRepair" in body or "Schema Repair" in body
        assert "artifact" in body
        assert "internal" in body.lower()
    assert "Do **not** run `uv run astock probe` before every investment question" in orchestrator
    assert "current-research-policy" in orchestrator
    assert (
        "Only after allowlisted automated/provider paths **and** authoritative Web "
        "search are exhausted"
        in orchestrator
    )
    assert "research-investor-answer-audit" in orchestrator
    assert "never become the investor answer" in orchestrator
    assert "active policy budget" in company
    assert "current-research-policy" in investigation
    assert "authoritative Web search before asking the user" in investigation
    assert "single `ManualInvestigationTask`-style checklist" in investigation


def test_agent_observability_and_tech_scout_commands_are_discoverable() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    assert {
        "agent-observation-schema",
        "agent-observation-register",
        "agent-observability-report",
        "agent-observability-audit",
        "market-canonical-gc",
    } <= command_names
    scout = (SKILLS_ROOT / "research-tech-scout" / "SKILL.md").read_text(encoding="utf-8")
    assert "ADAPT_PATTERN" in scout
    assert "SHADOW_EXPERIMENT" in scout
    assert "GitHub" in scout
    assert "social" in scout.lower()


def test_session_portfolio_commands_keep_orders_and_fills_separate() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    assert {
        "local-portfolio-init",
        "local-portfolio-status",
        "local-portfolio-sync-paper",
        "local-portfolio-review",
        "local-portfolio-audit",
        "local-portfolio-rebuild",
        "sync-hourly",
        "paper-replay",
    } <= command_names
    assert "local-portfolio-trade" not in command_names
    orchestrator = (
        SKILLS_ROOT / "astock-research-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    recovery = (SKILLS_ROOT / "paper-trading-recovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert (
        "A submitted order is never called a position until the fill ledger confirms it"
        in orchestrator
    )
    assert "Default replay resolution is **60m**" in recovery
    assert "--resolution 5m" in recovery
