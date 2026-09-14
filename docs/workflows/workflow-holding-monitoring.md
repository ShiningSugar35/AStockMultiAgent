# Workflow — Session Holding Review

## When to use

Run on explicit holding questions **and automatically alongside every investment-related Agent task** when the local portfolio is non-empty. The purpose is to catch up what changed while the Agent was offline, not to keep a daemon running.

Primary skill: `$holding-monitor`; use `$portfolio-manager` for aggregate allocation/risk questions.

## Flow

1. **Restore external accounts + paper state**
   - Read `external-account-list`; for legacy installations run `external-account-migrate-legacy-default` once, then use `external-account-projection <account_id>` / `external-account-audit <account_id>` for authoritative external holdings, cash and corrections.
   - If the paper account exists, run `local-portfolio-sync-paper` and read `local-portfolio-status`.
   - SQLite `external_account_event` owns real-account facts; the paper ledger separately owns simulated orders/fills. Git-ignored `user_state/*.md` is only the Agent-facing projection/compatibility mirror.

2. **Refresh the lightweight holding projection**
   - After the canonical preflight is available, best-effort refresh `user_state/position_tracking/当前持仓.md`, `已结束交易.md` and `主动跟踪计划.md`.
   - Freeze the first verified observation price and any already-sealed research snapshot at first sight; later research must not be backfilled into history.
   - A failed account read, missing account identity, corrupt optional cache, older observation or concurrent projection writer is **unknown coverage**, never evidence of liquidation. Preserve the last known active record and archive.
   - Only a complete authoritative lane that confirms the position is gone may remove it from the active projection. If the actual exit timestamp, fees or net return are not verifiable, archive them as unknown rather than estimating.

3. **Catch up pending execution first**
   - For held symbols/open orders, synchronize the missing hourly interval and replay to the current boundary.
   - Use 5m only when hourly OHLC cannot answer an execution-sensitive question reliably enough.
   - Never treat a submitted-but-unfilled order as a position.

4. **Research only the delta**
   - Start from `last_review_at` and the previous review note.
   - Check new official disclosures, material financial/KPI change, catalysts, thesis invalidation, valuation movement and relevant market context.
   - “No material new evidence” is a valid result.

5. **Classify the action**
   - Exactly one of `HOLD / ADD / TRIM / EXIT` for each held name.
   - `ADD/TRIM/EXIT` requires evidence and current risk constraints; otherwise default to `HOLD` rather than inventing action.

6. **Use the formal chain only for material action**
   - Material `ADD/TRIM/EXIT` can use `holding-review-run`, `holding-review-audit` and Committee validation.
   - Do not rerun the full original company report merely to say HOLD.

7. **Persist the review boundary**
   - Record the action/thesis state/note with `local-portfolio-review`.
   - If a paper order is created, update `orders.md`/`portfolio.md` only through subsequent account sync after fill/replay.

8. **Portfolio interactions**
   - Concentration, correlation, drawdown and overall allocation belong to [Portfolio Construction](workflow-portfolio-construction.md).

## Output

Normally one line per holding is enough: **标的 — HOLD/ADD/TRIM/EXIT — 核心原因 — 什么会改变动作**. Add one short portfolio-level warning only if material. Define unfamiliar finance/statistics terms briefly on first use.

## Stop conditions

- No synthetic bars/fills.
- No unfilled order reported as a holding.
- No unnecessary full-company rerun.
- No real-broker mutation; real trades remain user-executed outside the system.
