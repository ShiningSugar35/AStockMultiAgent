# Workflow — Portfolio Transition & Hedge-Aware Completion

## When to use

Use when the investor says they plan to buy a formally researched stock and want additional stocks/assets to diversify, reduce a specific risk, or build the position as part of a portfolio.

Primary skill: `$portfolio-manager`. Supporting skills: `$astock-research-orchestrator`, `$candidate-scan`, `$company-deep-research`, `$industry-value-chain`, `$macro-policy-regime`, `$catalyst-event-research`, `$model-risk-backtest-validation`, `$evidence-investigation`.

## Flow

1. **Restore state and constraints**
   - Sync paper state if present, read `local-portfolio-status`, freeze `portfolio-local-snapshot`, and consume material continuous-monitor deltas.
   - Recover known holdings/trade facts instead of asking again.
   - Recover or request only missing portfolio constraints: investable capital/cash, horizon, concentration/risk/liquidity/turnover bounds and locked holdings. Unknown cash/NAV remains unknown; do not invent quantity targets.

2. **Revalidate the planned anchor**
   - The proposed anchor X must have a current formal research result that is still usable. Candidate/ResearchSeed/history of a prior recommendation is insufficient.
   - If material new evidence exists, run incremental company research before portfolio construction.

3. **Create comparable starting states**
   - Build `CURRENT` from the actual portfolio boundary.
   - Build `ANCHOR_ONLY` by adding X within current formal constraints and available capital.
   - Compare concentration, beta/factor/industry/cycle exposure, drawdown/tail risk, liquidity and scenario sensitivity. The difference defines the portfolio risk gaps to solve.

4. **Discover complements from risk gaps, not names first**
   - Convert each material gap into desired candidate properties.
   - Stock discovery must remain inside the proven A-share Universe/Research Team funnel. Use a bounded 6–12 name pre-screen and send only the strongest 2–5 through full company research.
   - Web/news may verify economic mechanisms or facts but cannot hand-pick a stock to replace missing Universe lineage.
   - ETF candidates require an official registered `ETFProductProfile` plus `ETFResearchMetrics` at the same `as_of`; the metrics bind average trading amount, volatility, tracking error and product fee fields. Premium/discount remains unavailable until a formal NAV/iNAV series exists. If the system has no proven ETF product candidate for the gap, say so rather than manufacturing one.

5. **Admit final portfolio members**
   - Every stock must pass the existing current company/Committee/classification chain at the same decision boundary.
   - Compare the active allocator proposals: constrained equal weight baseline, inverse volatility, hierarchical risk, and shrinkage minimum variance.
   - No optimizer may override a single-company reject/watch/missing-hard-input state.

6. **Evaluate hedge language separately**
   - A complement with lower portfolio risk is `DIVERSIFICATION` unless a specified risk is proven to be offset.
   - For a proposed ETF natural hedge, first freeze `portfolio-etf-metrics`, then run `portfolio-hedge-evaluate`. Require official mechanism provenance, normal/stress PIT evidence, a positive risk reduction above the versioned threshold, verified round-trip cost below its independent limit, and disclosed basis/model risk. Never subtract cost from beta/HHI/volatility percentages as though they had the same units.
   - Current long-only stock/ETF support never upgrades a candidate to `EXPLICIT_HEDGE`. Shorting, futures, options and real broker execution remain unavailable.

7. **Construct the transition**
   - Run `portfolio-transition` on the current analysis, target construction and user intent.
   - Output `CURRENT / ANCHOR_ONLY / TARGET`, target weight bands, residual cash, turnover, risk differences, implementation cost and binding constraints.
   - Quantity bands are shown only when NAV, current quantity, executable price and instrument-specific trading unit are all known.
   - Small drift inside the no-trade band remains HOLD; the workflow does not force point-target rebalancing.

8. **Separate recommendation from execution**
   - Transition/hedge reports are read-only and cannot write the paper ledger.
   - If the investor chooses a simulated action, continue to the validated paper workflow with explicit confirmation. Only replayed fills update positions.

## Output

Investor-facing order: **建议组合 → 为什么比只买 X 更稳健/更匹配目标 → 目标区间与现金 → 对冲/分散化的准确性质 → 成本与最大失效条件**. Do not expose internal artifacts or command traces.

## Stop conditions

- Anchor formal research is stale/blocked.
- Current portfolio or critical investor constraints cannot be established for the requested precision.
- Current Universe cannot support stock candidate discovery.
- ETF candidate lacks an official registered product profile.
- A claimed hedge lacks mechanism, stress evidence or verified implementation cost.
- Any proposal would bypass Committee, target-band, paper-confirmation or `broker_execution_allowed=false` boundaries.
