---
name: research-tech-scout
description: Scout external GitHub projects, quant research platforms, engineering releases, and practitioner communities for reusable research or infrastructure ideas. Use for external technology scouting, competitive capability scans, open-source discovery, or deciding whether an outside tool/pattern belongs in this project.
---

# 投研技术自由人

1. Start from the current system, not from external hype. Read `uv run astock research-capability-status`, `uv run astock agent-observability-report --lookback-days 30`, `uv run astock research-efficiency-report`, and the relevant local Skill/Workflow before searching outside.
2. Search **current** primary sources first: official repositories, release notes, papers, vendor/platform documentation, then GitHub issues/discussions and practitioner communities for failure modes and unmet needs. Social media is discovery evidence only.
3. For every candidate, classify it as `ADAPT_PATTERN`, `SHADOW_EXPERIMENT`, `WATCH`, or `REJECT`. Prefer extracting a small design pattern over importing a large framework when the project already has an equivalent deterministic core.
4. Check license, maintenance activity, dependency weight, data rights, A-share applicability, point-in-time behavior, reproducibility, execution assumptions, and overlap with existing code before recommending adoption.
5. Quant/ML ideas must enter the existing prospective/shadow gates. A popular factor, agent, optimizer, or RL stack never receives production trading authority from stars, backtests, screenshots, or community testimonials.
6. Engineering ideas may be adopted directly only when they strengthen observability, correctness, recoverability, data quality, performance, or developer ergonomics without weakening evidence/PIT/accounting/ledger/broker boundaries.
7. Record useful external patterns in a dated scouting note under `docs/scouting/`; include the source, what is genuinely novel for this repository, what is duplicate, the smallest integration surface, and the verification required before promotion.
8. When an external idea is implemented, rerun its targeted tests plus the repository release gates. Do not leave an imported package or copied subsystem when a smaller local implementation passes the same acceptance criteria.

## Workflows

- [`docs/workflows/workflow-research-tech-scout.md`](../../../docs/workflows/workflow-research-tech-scout.md)

## Output

Produce a compact scout table: **candidate → source → project fit → decision → smallest next action**. Separate verified upstream facts from community claims. Surface rejected duplication as explicitly as adopted ideas so the repository does not grow by accumulation.

## Prohibitions

- Do not treat GitHub stars, X/Reddit posts, influencer claims, or a single backtest as investment evidence.
- Do not copy an external multi-agent architecture when existing Skills/Workflows already cover the same roles.
- Do not add a dependency before checking whether a small local adapter/pattern is enough.
- Do not bypass PIT, Evidence, Committee, TradingClassification, Paper confirmation, or `broker_execution_allowed=false`.
- Do not make automatic Skill mutations or production-weight changes from scouting results.
