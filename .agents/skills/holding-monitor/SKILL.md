---
name: holding-monitor
description: Review what changed for positions recorded in the local user portfolio or deterministic paper account. Use when the user asks about holdings, or automatically alongside another investment task to decide whether each held name should be held, added, trimmed, or exited.
---

# 持仓复核

1. Start from `uv run astock local-portfolio-sync-paper` when the paper account exists, then read `uv run astock local-portfolio-status`. The Git-ignored Markdown files are the Agent-facing state; the paper ledger is the deterministic order/fill source.
2. On **every investment-related Agent task**, review current holdings in addition to the user's main question. Do delta research rather than repeating the full company report: check new official disclosures, material financial changes, catalysts, thesis invalidation, valuation movement, and relevant market/execution changes since the last review.
3. Refresh hourly data for held symbols/open orders and run session-on-demand paper replay before judging whether an order filled while the Agent was offline. Use 5m replay only when the hourly path is materially ambiguous.
4. Classify each held position into exactly one user-facing action: `HOLD / ADD / TRIM / EXIT`. `ADD/TRIM/EXIT` must be supported by current evidence and existing risk/portfolio constraints; otherwise use `HOLD` and state what would change it.
5. Record the review with `uv run astock local-portfolio-review <market> <symbol> --action <...> --thesis-status <...> --note <...>` so the next Agent session has a concise review boundary.
6. For a material `ADD / TRIM / EXIT` proposal, keep the formal audit path: run `holding-review-run` / `holding-review-audit`, resolve current evidence with `committee-input-resolve`, and use `committee-plan` / `committee-decide` when a formal decision is needed. For a durable registered result, retain `codex-run-init --require-registered-output` and `codex-run-audit`; ordinary unchanged `HOLD` reviews do not need to repeat the entire formal chain.
7. If the review produces a simulated order, preserve the account/order/fill lifecycle. Do not update the Markdown position as bought/sold merely because an order was submitted; update it after replay via `local-portfolio-sync-paper`.
8. If the user asks about aggregate concentration, correlation, drawdown, or allocation, route the same current position set to `$portfolio-manager`.

## Workflows

- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)

## Output

Keep it compact. For each position, normally one line is enough: **标的 — HOLD/ADD/TRIM/EXIT — one main reason — one condition that would change the action**. Explain unfamiliar finance/statistics jargon in a short parenthesis on first use.

## Prohibitions

- Do not perform a full company re-research when only incremental facts changed.
- Do not infer an author's holding rule from selection-only content.
- Do not treat an unfilled paper order as a position change.
- Do not submit a real brokerage order.
