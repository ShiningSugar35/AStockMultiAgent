# Workflow — Session Holding Review

## When to use

Run on explicit holding questions **and automatically alongside every investment-related Agent task** when the local portfolio is non-empty. The purpose is to catch up what changed while the Agent was offline, not to keep a daemon running.

Primary skill: `$holding-monitor`; use `$portfolio-manager` for aggregate allocation/risk questions.

## Flow

1. **Refresh account → Markdown mirror**
   - If the paper account exists, run `local-portfolio-sync-paper` and read `local-portfolio-status`.
   - The paper ledger owns orders/fills; Git-ignored `user_state/*.md` is the Agent-facing compact state.

2. **Catch up pending execution first**
   - For held symbols/open orders, synchronize the missing hourly interval and replay to the current boundary.
   - Use 5m only when hourly OHLC cannot answer an execution-sensitive question reliably enough.
   - Never treat a submitted-but-unfilled order as a position.

3. **Research only the delta**
   - Start from `last_review_at` and the previous review note.
   - Check new official disclosures, material financial/KPI change, catalysts, thesis invalidation, valuation movement and relevant market context.
   - “No material new evidence” is a valid result.

4. **Classify the action**
   - Exactly one of `HOLD / ADD / TRIM / EXIT` for each held name.
   - `ADD/TRIM/EXIT` requires evidence and current risk constraints; otherwise default to `HOLD` rather than inventing action.

5. **Use the formal chain only for material action**
   - Material `ADD/TRIM/EXIT` can use `holding-review-run`, `holding-review-audit` and Committee validation.
   - Do not rerun the full original company report merely to say HOLD.

6. **Persist the review boundary**
   - Record the action/thesis state/note with `local-portfolio-review`.
   - If a paper order is created, update `orders.md`/`portfolio.md` only through subsequent account sync after fill/replay.

7. **Portfolio interactions**
   - Concentration, correlation, drawdown and overall allocation belong to [Portfolio Construction](workflow-portfolio-construction.md).

## Output

Normally one line per holding is enough: **标的 — HOLD/ADD/TRIM/EXIT — 核心原因 — 什么会改变动作**. Add one short portfolio-level warning only if material. Define unfamiliar finance/statistics terms briefly on first use.

## Stop conditions

- No synthetic bars/fills.
- No unfilled order reported as a holding.
- No unnecessary full-company rerun.
- No real-broker mutation; real trades remain user-executed outside the system.
