from __future__ import annotations

from pathlib import Path

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / ".agents" / "skills"


def test_web_agent_product_commands_are_registered() -> None:
    commands = {command.name for command in app.registered_commands if command.name}
    assert {
        "research-seeds-schema",
        "research-seeds",
        "research-seeds-status",
        "research-seeds-audit",
        "candidate-input-schema",
        "candidate-input-stage",
        "candidate-input-run",
        "research-plan",
        "research-run-company",
        "research-status",
        "research-audit",
        "trade-plan-view",
        "portfolio-schema",
        "portfolio-paper-evaluate",
        "portfolio-evaluate",
        "portfolio-construct",
        "portfolio-status",
        "portfolio-audit",
    } <= commands


def test_natural_language_skills_route_company_trade_and_portfolio_questions() -> None:
    orchestrator = (
        SKILLS_ROOT / "astock-research-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    company = (SKILLS_ROOT / "company-deep-research" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    portfolio = (SKILLS_ROOT / "portfolio-manager" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for term in (
        "$company-deep-research",
        "$portfolio-manager",
        "$candidate-scan",
        "trade-plan-view",
        "ClassifiedTradeProtocol",
        "candidate-input-run",
        "research-seeds --live",
    ):
        assert term in orchestrator
    assert "trade-plan-view" in company
    assert "scenario" in company.casefold()
    assert "portfolio-paper-evaluate" in portfolio
    assert "EQUAL_WEIGHT_CONSTRAINED" in portfolio
    assert "inverse volatility" in portfolio.casefold()
    assert "hierarchical risk" in portfolio.casefold()
    assert "Ledoit-Wolf" in portfolio
    assert "RESEARCH_READY" in portfolio


def test_scheme_safety_boundary_disallows_model_risk_bypass_and_default_broker_orders() -> None:
    scheme = (PROJECT_ROOT / "低成本A股多Agent投研系统方案.md").read_text(
        encoding="utf-8"
    )
    assert "不允许大模型绕过风险规则" in scheme
    assert "不保留默认自动连接券商下单接口" in scheme
