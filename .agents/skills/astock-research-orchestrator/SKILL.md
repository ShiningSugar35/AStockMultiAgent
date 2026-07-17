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
6. Before a decision, require the relevant financial-integrity pack and run `uv run astock committee-input-resolve --artifact-id <id>...`; create a strict request from `committee-schema`, preview it with `committee-plan <request.json>`, then use `committee-decide <request.json>` and `committee-audit <decision_id>`.
7. If the verdict is `NEEDS_INFO`, route each `committee-task-status <task_id>` to `$evidence-investigation`; after a genuinely new frozen artifact exists, link it with `committee-task-resolve <task_id> <artifact_id>` and create a new committee request/bundle. The committee never performs the search itself.
8. For a durable Codex explanation, run `uv run astock codex-run-init <request> --artifact-id <DecisionPack_or_TradeProtocol_artifact_id> --require-registered-output`, import that exact registered object with `codex-run-import`, and finish with `codex-run-audit`.

## Output

Produce a frozen-input `DecisionPack` and one TradeProtocol for every committee decision. `REJECT`, `NEEDS_INFO`, and `WATCH` protocols must remain `BLOCKED`; no verdict or protocol implies an executed trade.

## Prohibitions

- Do not invent a buy recommendation when the research pipeline is incomplete.
- Do not write SQLite or canonical Parquet directly.
- Do not ask the committee to search or fetch new evidence.
- Do not create or send a real brokerage order.
