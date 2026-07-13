---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research requests across local data, evidence, company research, holding monitoring, and paper recovery. Use for requests such as what to research, whether to inspect a company, checking all holdings, updating the paper account, or any task that spans more than one project skill.
---

# A股研究总控

1. Run `uv run astock probe` and treat unavailable capabilities as unavailable.
2. Run `uv run astock context-plan` with only the required Skills and frozen artifacts.
3. Route to the narrowest project Skill; do not duplicate its work.
4. Prefer local/API facts, then MCP, then browser, then a manual investigation task.
5. Run deterministic synchronization and quality commands before reasoning from market data.
6. Initialize a run with `uv run astock codex-run-init` when producing a durable conclusion.
7. Put the result in the declared Schema and import it through `uv run astock codex-run-import`.

## Output

Produce a `RunManifest` plus one validated domain artifact. If a required module or evidence set is missing, set the run to `NEEDS_INFO` and state the exact missing capability.

## Prohibitions

- Do not invent a buy recommendation when the research pipeline is incomplete.
- Do not write SQLite or canonical Parquet directly.
- Do not ask the committee to search or fetch new evidence.
- Do not create or send a real brokerage order.
