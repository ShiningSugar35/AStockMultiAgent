from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SKILLS = PROJECT_ROOT / ".agents" / "skills"
HUMAN_SKILLS = PROJECT_ROOT / "skills"
WORKFLOWS = PROJECT_ROOT / "docs" / "workflows"

EXPECTED_WORKFLOWS = {
    "workflow-full-market-research-team.md",
    "workflow-current-company-research.md",
    "workflow-candidate-discovery.md",
    "workflow-evidence-recovery.md",
    "workflow-financial-integrity.md",
    "workflow-committee-trade-plan.md",
    "workflow-portfolio-construction.md",
    "workflow-portfolio-transition-and-hedging.md",
    "workflow-holding-rebalance-decision.md",
    "workflow-holding-monitoring.md",
    "workflow-paper-trading.md",
    "workflow-knowledge-ingest.md",
    "workflow-prospective-evaluation.md",
    "workflow-adaptive-edge.md",
    "workflow-continuous-investment-monitoring.md",
    "workflow-ten-stock-q3-watch.md",
}


def test_skill_catalog_has_one_canonical_skill_tree_and_visible_index() -> None:
    catalog = (HUMAN_SKILLS / "README.md").read_text(encoding="utf-8")

    assert ".agents/skills" in catalog
    assert "docs/workflows/README.md" in catalog
    assert not list(HUMAN_SKILLS.rglob("SKILL.md"))
    canonical_names = {path.parent.name for path in CANONICAL_SKILLS.glob("*/SKILL.md")}
    for name in canonical_names:
        assert f"${name}" in catalog


def test_workflow_catalog_is_complete_and_every_workflow_has_operational_sections() -> None:
    actual = {path.name for path in WORKFLOWS.glob("workflow-*.md")}
    assert actual == EXPECTED_WORKFLOWS

    index = (WORKFLOWS / "README.md").read_text(encoding="utf-8")
    for name in EXPECTED_WORKFLOWS:
        assert name in index
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert text.startswith("# Workflow")
        assert "## When to use" in text
        assert "## Flow" in text
        assert "## Stop conditions" in text


def test_skill_and_workflow_references_resolve_without_forcing_one_to_one_wrappers() -> None:
    link_pattern = re.compile(r"\]\(([^)]+)\)")
    cross_capability_skills = {
        "astock-research-orchestrator", "candidate-scan", "company-deep-research",
        "evidence-investigation", "holding-monitor", "continuous-investment-monitor",
        "portfolio-manager", "paper-trading-recovery", "knowledge-ingest",
    }
    paths = [
        *CANONICAL_SKILLS.glob("*/SKILL.md"),
        *WORKFLOWS.glob("*.md"),
        HUMAN_SKILLS / "README.md",
        PROJECT_ROOT / "docs/architecture/research-specialist-skills-crosswalk.md",
    ]
    for path in paths:
        body = path.read_text(encoding="utf-8")
        linked = link_pattern.findall(body)
        if path.parent.name in cross_capability_skills and path.name == "SKILL.md":
            assert any("docs/workflows/workflow-" in target for target in linked), path
        for target in linked:
            if "://" in target or target.startswith("#"):
                continue
            local_path = (path.parent / target.split("#", 1)[0]).resolve()
            assert local_path.is_relative_to(PROJECT_ROOT), (path, target)
            assert local_path.exists(), (path, target)


def test_current_company_and_evidence_workflows_preserve_policy_and_gap_scope() -> None:
    current = (WORKFLOWS / "workflow-current-company-research.md").read_text(encoding="utf-8")
    evidence = (WORKFLOWS / "workflow-evidence-recovery.md").read_text(encoding="utf-8")
    adaptive = (WORKFLOWS / "workflow-adaptive-edge.md").read_text(encoding="utf-8")

    assert "research-acquire-current" in current
    assert "question timestamp" in current
    assert "current-research-policy" in current
    assert "authoritative Web" in current
    assert "research-investor-answer-audit" in current
    assert "SourceAccessRouter" in current
    assert "research-current-continuation-resolve" in current
    assert "normal investor answers must not expose those backend fields" in current
    assert "active Current Research policy" in evidence
    assert "Authoritative Web fallback" in evidence
    assert "ProviderRecoveryProposal" in evidence
    assert "Schema Repair" in evidence
    assert "Adaptive Edge / Deterministic Core" in adaptive
    for workflow in (current, evidence):
        for state in ("BUDGET_EXHAUSTED", "PUBLIC_DATA_UNAVAILABLE", "NEEDS_USER_INPUT"):
            assert state in workflow
    assert "identified private input" in evidence
    assert "independent" in evidence


def test_ten_stock_prebound_watcher_survives_single_round_completion() -> None:
    workflow = (WORKFLOWS / "workflow-ten-stock-q3-watch.md").read_text(encoding="utf-8")
    architecture = (
        PROJECT_ROOT / "docs/architecture/scheduled-research-orchestration-v1.md"
    ).read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for text in (workflow, architecture, agents):
        assert "continuation watcher" in text
        assert "单个" in text and "round" in text
        assert "不得停用" in text or "不随单个 round 完成而停用" in text
