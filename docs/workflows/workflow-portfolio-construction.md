# Workflow — Portfolio Construction

## When to use

Use when the user asks about aggregate portfolio risk, diversification, concentration, or wants several already-researched stocks combined into one constrained portfolio.

Primary skill: `$portfolio-manager`.

## Flow

1. **Accept only eligible research inputs**
   - Each candidate must come from the approved research/Committee chain required by the portfolio request.
   - Candidate ranking, ResearchSeed or unreviewed company narrative cannot become portfolio weight directly.

2. **Evaluate the current portfolio**
   - For paper holdings use `portfolio-paper-evaluate --account-id <account> --live`; otherwise use a frozen portfolio request with `portfolio-evaluate`.
   - Reuse common-session market data and validated instrument lineage.

3. **Measure portfolio-level risk**
   - Evaluate concentration, correlations, beta/factor exposure where available, drawdown, ES/CVaR/CDaR, liquidity, implementation cost and stress scenarios.
   - Group/industry constraints are only as authoritative as their taxonomy provenance; caller-supplied tags must remain labelled as such.

4. **Construct four comparable proposals**
   - Run `portfolio-construct` and preserve:
     1. `EQUAL_WEIGHT_CONSTRAINED` — default robust baseline;
     2. `INVERSE_VOLATILITY`;
     3. `HIERARCHICAL_RISK`;
     4. `SHRINKAGE_MIN_VARIANCE` — Ledoit-Wolf + long-only minimum variance.
   - Apply hard single-name/total exposure/group constraints before optimizing.
   - Unallocatable capital remains cash.

5. **Do not optimize on uncalibrated expected returns**
   - No LLM-expected-return → Max Sharpe default path.
   - Alternative methods are comparisons until prospective evidence justifies changing the default.

6. **Audit and explain trade-offs**
   - Run `portfolio-audit` on persisted reports.
   - Explain how each proposal changes concentration, volatility/tail risk, liquidity and turnover rather than simply ranking by backtest return.

7. **Feed monitoring/paper layers separately**
   - A construction proposal does not write orders or the paper ledger.
   - If the user chooses a simulated action, continue through the validated paper-operation flow with explicit confirmation.

## Output

Show current risk first, then the four allocation proposals, why they differ, binding constraints, cash residual and the recommended **comparison baseline**. Do not present an optimizer output as an automatic trade authorization.

## Stop conditions

- No unapproved company can enter via portfolio optimization.
- No model can override a single-stock Committee rejection or missing hard input.
- Default equal-weight changes only after formal prospective evidence, not one historical backtest.
- No real brokerage execution.
