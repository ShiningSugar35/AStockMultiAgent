---
name: research-tech-scout
description: Scout external GitHub projects, quant research platforms, engineering releases, and practitioner communities for reusable research or infrastructure ideas. Use for external technology scouting, competitive capability scans, open-source discovery, or deciding whether an outside tool/pattern belongs in this project.
---

# 投研技术自由人

1. Inspect the relevant capability and local implementation first. Read `uv run astock agent-observability-report --lookback-days 30` or `uv run astock research-efficiency-report` only when existing telemetry can answer the current efficiency question. These commands inspect saved reports by default; `NOT_FOUND` is an honest absence, not a reason to generate a report or stop the task. Use explicit `--materialize` only when producing a new baseline is part of the requested work.
2. Search **current** primary sources first: official repositories, release notes, papers, vendor/platform documentation, then GitHub issues/discussions and practitioner communities for failure modes and unmet needs. Social media is discovery evidence only.
3. For every candidate, classify it as `ADAPT_PATTERN`, `SHADOW_EXPERIMENT`, `WATCH`, or `REJECT`. Prefer extracting a small design pattern over importing a large framework when the project already has an equivalent deterministic core.
4. Check maintenance, dependency cost, A-share applicability, source provenance, point-in-time behavior, reproducibility and overlap. Record relevant upstream conditions as evidence; Commons Clause is not a development or selection blocker for this project.
5. Quant/ML ideas must enter the existing prospective/shadow gates. A popular factor, agent, optimizer, or RL stack never receives production trading authority from stars, backtests, screenshots, or community testimonials.
6. Engineering ideas may be adopted directly only when they strengthen observability, correctness, recoverability, data quality, performance, or developer ergonomics without weakening evidence/PIT/accounting/ledger/broker boundaries.
7. Record useful external patterns in a dated scouting note under `docs/scouting/`; include the source, what is genuinely novel for this repository, what is duplicate, the smallest integration surface, and the verification required before promotion.
8. When an idea is implemented, use the existing impact-based validation plan for the changed contracts; expand only for a concrete unresolved risk. Reuse the same evidence for review and delivery. Remove rejected adapters or copied subsystems after confirming they have no remaining callers. At task completion register one observation through `uv run astock agent-observation-register REQUEST.json`, reusing an existing coordinator observation instead of duplicating it.

## References

- [Current architecture and contracts](../../../docs/README.md): locate the specific subsystem before proposing an addition.
- [External capability qualification](../../../docs/architecture/external-dependency-resilience-v1.md): read only when a candidate will enter a production source path.

## Output

Produce a compact scout table: **candidate → source → project fit → decision → smallest next action**. Separate verified upstream facts from community claims. Surface rejected duplication as explicitly as adopted ideas so the repository does not grow by accumulation.

## Prohibitions

- Do not treat GitHub stars, X/Reddit posts, influencer claims, or a single backtest as investment evidence.
- Do not copy an external multi-agent architecture when existing Skills/Workflows already cover the same roles.
- Do not add a dependency before checking whether a small local adapter/pattern is enough.
- Do not bypass Evidence, Committee, TradingClassification, or paper confirmation.
- Do not make automatic Skill mutations or production-weight changes from scouting results.
