---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research requests across local data, evidence, company research, holding monitoring, and paper recovery. Use for requests such as what to research, whether to inspect a company, checking all holdings, updating the paper account, or any task that spans more than one project skill.
---

# A股研究总控

1. Run `uv run astock probe` and treat unavailable capabilities as unavailable.
2. Inspect existing work with `uv run astock research-chain-status <company_id>`; use `research-chain-audit` before reusing a frozen chain.
3. Run `uv run astock context-plan --skill <skill> --artifact-id <artifact_id>` with only the required registered artifacts, then route to the narrowest project Skill.
4. Prefer local/API facts, then MCP, then browser, then a manual investigation task. Run deterministic synchronization and quality commands before reasoning from market data.
5. Build missing Phase 4 nodes only through their validated research/position CLI; do not duplicate a node that already audits PASS.
6. For a durable Phase 4 result, run `uv run astock codex-run-init <request> --artifact-id <artifact_id> --require-registered-output`.
7. Put the exact already-registered deterministic object in the declared Schema and import it through `uv run astock codex-run-import`; finish with `codex-run-audit`.

## Output

Produce a frozen-input `RunManifest` plus one validated registered domain artifact. Before Phase 6 is implemented, do not claim a committee `DecisionPack`; return the exact missing stage or evidence instead.

## Prohibitions

- Do not invent a buy recommendation when the research pipeline is incomplete.
- Do not write SQLite or canonical Parquet directly.
- Do not ask the committee to search or fetch new evidence.
- Do not create or send a real brokerage order.
