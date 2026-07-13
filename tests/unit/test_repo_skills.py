from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"

EXPECTED = {
    "astock-research-orchestrator": ("multi-step", "research"),
    "candidate-scan": ("candidate", "watchlist"),
    "company-deep-research": ("company", "deep"),
    "financial-integrity-audit": ("financial", "audit"),
    "holding-monitor": ("holding", "positions"),
    "paper-trading-recovery": ("paper", "recovery"),
    "knowledge-ingest": ("allowlisted", "history"),
    "evidence-investigation": ("evidence", "gap"),
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
