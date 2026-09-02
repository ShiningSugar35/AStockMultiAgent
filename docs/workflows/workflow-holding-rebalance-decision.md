# Workflow — Holding Rebalance Decision

## When to use

Use when the investor already holds one or more securities and asks whether to add, hold, trim, exit, or change the surrounding portfolio because price, fundamentals, valuation, events or portfolio risk have changed.

Primary skill: `$holding-monitor`; aggregate allocation uses `$portfolio-manager`; incremental source is `$continuous-investment-monitor`.

## Flow

1. **Persist a newly declared external trade before analysis**
   - If the investor states a completed external trade with exact market/security identity, side, quantity, price and actual transaction time, create a typed declaration and run `portfolio-import-declared-trade` in the same turn.
   - Exactly identical re-statements are idempotent; a collision with a paper fill is blocked.
   - Missing quantity/price/time is not guessed. Ask only for the missing fact and do not create a holding until the fact is complete.

2. **Restore the held-position boundary**
   - Sync the paper account if present, read `local-portfolio-status`, freeze `portfolio-local-snapshot`, and recover open orders and monitor tasks.
   - Use persisted `opened_at`, `last_trade_at`, quantity, average cost and last review. Do not ask again unless the user is correcting the stored fact or audit reports damage.
   - Average cost is an execution/P&L fact, not a valuation signal.

3. **Research only the delta**
   - Start at `last_review_at` and check new official disclosures, financial/KPI changes, valuation movement, competitive/industry changes, governance/policy/catalysts and market/execution context.
   - News/social items are `UNVERIFIED_LEAD` until an authoritative source verifies them; unverified leads may trigger REVIEW only.

4. **Classify event semantics with evidence**
   - `THESIS_INVALIDATING`: core assumption broken.
   - `THESIS_WEAKENING`: evidence reduces value driver or raises downside probability.
   - `THESIS_STRENGTHENING`: new evidence supports the thesis; this only opens ADD evaluation, not automatic ADD.
   - `VALUATION_ONLY`: facts are stable but odds/valuation changed.
   - `PORTFOLIO_RISK_ONLY`: company thesis is unchanged but concentration/risk budget changed.
   - `TEMPORARY_NOISE`: no action-changing evidence.
   - `UNVERIFIED_LEAD`: evidence investigation only.
   Material thesis/valuation severities require current evidence lineage; a caller-supplied label alone has no action authority.

5. **Evaluate the single name and portfolio separately**
   - Single-name layer: thesis, valuation/odds, catalysts, governance, financial integrity, execution/liquidity.
   - Portfolio layer: marginal risk contribution, single-name/industry concentration, correlation/beta/factor exposure, drawdown/tail risk, cash and opportunity cost.
   - A stronger company thesis cannot justify ADD if the portfolio would breach concentration/risk constraints. A stable thesis can still justify TRIM when portfolio risk is excessive.

6. **Choose exactly one primary position action**
   - `EXIT`: formal thesis invalidation or target weight becomes zero.
   - `TRIM`: thesis weakening, valuation deterioration, liquidity/implementation issue, or portfolio-risk-only excess.
   - `ADD`: only with new supporting evidence, acceptable valuation and portfolio headroom.
   - `HOLD`: no material delta, evidence conflict unresolved, or current position remains inside the target/no-trade band.
   - Conflict/invalid evidence may require internal REVIEW; investor-facing output should explain that the evidence is not strong enough to change the position rather than exposing an internal code.

7. **Bind the action to a target band**
   - For material action, pass current quantity/weight, target lower/mid/upper weights, target quantity range, implementation cost, execution preconditions and reversal conditions into the holding review.
   - Do not force a trade for small point-target drift. If current exposure remains within the band, HOLD.
   - Quantity guidance requires a known portfolio NAV/cash boundary, current executable price and exact instrument trading-unit rule; otherwise give weight/condition guidance only.

8. **Persist the review, not a fake fill**
   - Run the formal HoldingReview/PositionActionProposal path when material and audit it.
   - Save the concise review boundary with `local-portfolio-review` so the next session can work incrementally.
   - If a simulated order follows, only `paper-replay` fill + `local-portfolio-sync-paper` changes position quantity. ETF simulation is default-off and is allowed only when a valid independent `ETFInstrumentExecutionRule` covers the instrument/date and the existing confirmation/replay gates all pass; STOCK mechanics must never be reused implicitly.

9. **Batch portfolio re-evaluation for shared events**
   - When one macro/industry event affects multiple holdings, aggregate affected positions and perform one portfolio-level recomputation through `$portfolio-manager` rather than one full matrix calculation per name.

## Output

Per holding: **标的 — HOLD/ADD/TRIM/EXIT — 单股变化 — 组合层影响 — 目标区间/数量条件 — 下一复核条件**. If nothing material changed, say so explicitly. Do not repeat the original full research report.

## Stop conditions

- Required external trade fact remains incomplete or conflicted.
- Authoritative evidence for a material event is unavailable.
- Portfolio NAV/cash is unknown for a requested exact quantity recommendation.
- Current formal company research is invalid/stale and must be refreshed before a material action.
- Proposed action would bypass paper-confirmation, fill or real-broker boundaries.
