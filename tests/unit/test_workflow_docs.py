from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SKILLS = PROJECT_ROOT / ".agents" / "skills"
HUMAN_SKILLS = PROJECT_ROOT / "skills"
WORKFLOWS = PROJECT_ROOT / "docs" / "workflows"

EXPECTED_WORKFLOWS = {
    "workflow-current-company-research.md",
    "workflow-candidate-discovery.md",
    "workflow-evidence-recovery.md",
    "workflow-financial-integrity.md",
    "workflow-committee-trade-plan.md",
    "workflow-portfolio-construction.md",
    "workflow-holding-monitoring.md",
    "workflow-paper-trading.md",
    "workflow-knowledge-ingest.md",
    "workflow-prospective-evaluation.md",
    "workflow-adaptive-edge.md",
    "workflow-research-tech-scout.md",
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


def test_every_repo_skill_links_to_existing_workflow_docs() -> None:
    link_pattern = re.compile(r"\.\./\.\./\.\./docs/workflows/(workflow-[^)]+\.md)")
    for skill_path in CANONICAL_SKILLS.glob("*/SKILL.md"):
        body = skill_path.read_text(encoding="utf-8")
        assert "## Workflows" in body, skill_path.parent.name
        linked = link_pattern.findall(body)
        assert linked, skill_path.parent.name
        for name in linked:
            assert (WORKFLOWS / name).is_file(), (skill_path.parent.name, name)


def test_current_company_and_evidence_workflows_lock_policy_web_manual_order() -> None:
    current = (WORKFLOWS / "workflow-current-company-research.md").read_text(encoding="utf-8")
    evidence = (WORKFLOWS / "workflow-evidence-recovery.md").read_text(encoding="utf-8")
    adaptive = (WORKFLOWS / "workflow-adaptive-edge.md").read_text(encoding="utf-8")

    assert "research-acquire-current" in current
    assert "question timestamp" in current
    assert "current-research-policy" in current
    assert "authoritative Web" in current
    assert "research-investor-answer-audit" in current
    assert "SourceAccessRouter" in current
    assert "active Current Research policy" in evidence
    assert "Authoritative Web fallback" in evidence
    assert "Manual intervention is last" in evidence
    assert "ProviderRecoveryProposal" in evidence
    assert "Schema Repair" in evidence
    assert "Adaptive Edge / Deterministic Core" in adaptive
    assert "Manual remains last" in adaptive
