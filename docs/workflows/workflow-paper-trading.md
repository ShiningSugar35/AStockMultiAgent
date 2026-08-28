# Workflow — Session-on-Demand Paper Account

## When to use

Use for simulated account/order/fill state, catch-up after an Agent session was offline, user- or AI-initiated paper orders, settlement and NAV. This workflow is intentionally **not** a continuously running trading daemon.

Primary skill: `$paper-trading-recovery`.

## Flow

1. **Refresh local user state**
   - The paper ledger remains the deterministic source for cash, orders, fills, settlement and positions.
   - `user_state/portfolio.md`, `user_state/orders.md` and `user_state/trades.md` are Git-ignored, human-readable mirrors for the Agent.
   - On session start, run `local-portfolio-sync-paper` when the paper account exists, then read `local-portfolio-status`.

2. **Check account integrity before mutation**
   - Use `paper-status`; reject mutation on an unbalanced journal or inconsistent account.
   - Full SQLite integrity checking remains a separate developer/recovery operation, not a prerequisite for every investment question.

3. **Catch up offline intervals at hourly resolution by default**
   - For each held symbol or open order, run `sync-hourly` over the missing interval.
   - Run `paper-replay` at its default `60m` resolution.
   - Hourly OHLC can show that a limit price was touched, but cannot prove queue priority or the exact intrahour price path. The replay therefore remains `PROVIDER_1H_APPROX` and uses conservative fill pricing.
   - If the hourly bar is materially ambiguous, use `paper-replay --resolution 5m` as the explicit higher-fidelity fallback. The 5m providers are isolated by the `market.raw_5m` capability breaker: one provider failure may fall back to the approved peer without poisoning unrelated capabilities. A surviving single source remains `SINGLE_SOURCE_5M`; when a previous canonical exists and any provider fails, the degraded run must preserve that canonical rather than overwrite it.

4. **Keep order and fill semantics separate**
   - An accepted order is not a holding.
   - Cash reservation, partial fills, fees, T+1 settlement and order status remain deterministic ledger facts.
   - After replay, mirror confirmed fills/open orders/positions back to the local Markdown files.

5. **User-directed simulated order**
   - If the user explicitly says to buy/sell/add/trim a symbol, their instruction overrides the Agent's investment opinion.
   - Enter the simulated order flow without re-litigating whether the Agent likes the trade.
   - Mechanical constraints still apply: account cash/available shares, board lot, verified tradability, price band, order confirmation, and later fill simulation.

6. **AI-initiated simulated order**
   - Only an audited formal result that permits simulation **and** a currently satisfied entry rule may trigger an AI-initiated order.
   - Local `auto_ai_paper_order_on_approved_entry=true` is standing permission to proceed into the existing paper-order confirmation flow, not permission to declare a fill.
   - The replay layer determines whether the submitted order actually fills.

7. **Settle and mark**
   - Settlement uses the verified trading calendar and exact lots.
   - NAV marking uses valid unadjusted market references.

8. **Keep experimental research isolated**
   - Shadow/prospective/adaptive research remains analytical. It may motivate a separately created paper order but cannot directly mutate account state.

## Output

Show only decision-relevant account state: holdings, open orders, filled/partially filled/unfilled status, cash impact, and any material execution uncertainty. Explain hourly approximation once when relevant; do not dump internal ledger mechanics unless asked.

## Stop conditions

- No direct SQLite edits.
- No invented bars or fills. Search/Web discovery and an exact-item official document cannot substitute for continuous OHLCV or prove execution/fill paths.
- No accepted-but-unfilled order may be reported as a position.
- No real brokerage connection or real order submission.
