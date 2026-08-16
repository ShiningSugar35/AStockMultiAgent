---
name: paper-trading-recovery
description: Paper-trading recovery for the deterministic local paper account, open orders, fills, and session-on-demand hourly replay. Use after startup or interruption, for paper account status, open orders, fill simulation, journal integrity, positions, NAV, settlement, or optional 5-minute high-fidelity replay.
---

# 模拟账户与会话恢复

1. This is **not** a continuously running trading bot. The default operating model is session-on-demand: when an Agent investment task starts, inspect the local paper account, refresh missing market data, replay the interval since the last checkpoint, and then mirror the result into Git-ignored `user_state/*.md`.
2. Run `uv run astock paper-status` and reject mutation if the ledger is unbalanced or otherwise corrupt. The SQLite ledger remains the deterministic source for cash, orders, fills and settlement; `user_state/portfolio.md`, `orders.md`, and `trades.md` are the human/Agent-facing local mirror.
3. Default replay resolution is **60m**. Run `uv run astock sync-hourly <symbol> --market <market>` for each held symbol or open order, then `uv run astock paper-replay <symbol> --cursor <time> --market <market>`. Hourly OHLC may establish that a limit price was touched, but cannot prove intrabar ordering or queue priority; preserve the `PROVIDER_1H_APPROX` qualification.
4. Keep `--resolution 5m` as an explicit higher-fidelity fallback when an hourly bar is ambiguous, the user asks for a more precise fill reconstruction, or a stop/limit interaction depends materially on the path inside the hour. Do not require 5m storage/replay by default for multi-month or multi-year strategies.
5. Preserve the account/order/fill model. A submitted paper order is **not** a position until replay records a fill. Partial fills, cash reservation, T+1 settlement, fees and order status remain deterministic account facts.
6. After replay or any order/fill change, run `uv run astock local-portfolio-sync-paper`. Agent-facing portfolio reviews should read the local Markdown mirror first and use the SQLite ledger only for deterministic reconciliation or diagnostics.
7. Preserve the previous canonical data and checkpoint when providers or quality gates fail. Never manufacture a fill from missing bars.
8. Shadow/prospective studies and adaptive research remain analytical only. They must not mutate this account unless a separate paper order is explicitly created under the interactive portfolio workflow.

## Workflows

- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)

## Output

Return only the user-relevant account result: current holdings, pending orders, whether each order was simulated as filled/partially filled/unfilled, and any material execution uncertainty. Explain `PROVIDER_1H_APPROX` once in plain language when it matters.

## Prohibitions

- Do not edit SQLite directly.
- Do not treat an hourly price touch as proof of real-world queue execution.
- Do not replace missing hourly or 5m bars with invented data.
- Do not make continuous background runtime a prerequisite for the portfolio workflow.
- Shadow studies and their observations must never initialize, replay, repair, or mutate this account.
- Do not write shadow-study results, arm returns, or adaptive weights into the paper ledger.
- Do not use `adaptive-research-status` as a paper-account recovery or ledger-write command.
- Do not connect to or submit orders to a real broker.
