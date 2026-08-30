# Workflow — Model Risk & Backtest Validation

## When to use

Use when the ready Research Team `model-risk-validation` task requires a `ModelRiskValidationReport`, or when a formal research conclusion relies on a forecast, valuation model, factor, anomaly detector, optimizer, backtest or externally supplied score.

Primary skill: `$model-risk-backtest-validation`; it is independent of model development and feeds Reviewer, Committee and Recommendation Gate.

## Flow

1. Read `research-team-status <plan_id>` and consume only the ready MODEL_RISK task. Freeze model/code/data/input versions and intended authority.
2. Inventory executed decision-relevant models and heuristics. Verify reproducibility and distinguish executed results from merely available capabilities.
3. Audit PIT/source availability, leakage, survivorship/selection bias, train/test separation, repeated tuning, multiple testing, benchmarks, missing data and corporate actions.
4. Test calibration, simple baselines, parameter/structural sensitivity, out-of-distribution conditions, transaction costs, liquidity, turnover and capacity. Valuation additionally checks terminal-value dependence and market-implied expectations.
5. Register `ModelRiskValidationReport` through `research-team-role-output`, then complete through `research-team-task-result`. `MODEL_RISK_VALIDATION=true` requires reproducible lineage, leakage/multiple-testing review, realistic execution assumptions, robustness and explicit limitations.
6. Proposed model/weight changes remain prospective or shadow-gated; validation does not mutate production or paper ledgers.

## Stop conditions

- Stop or abstain when code/data lineage or reproducibility is missing, future data is used, tuning contaminated the test set, or costs/liquidity are omitted for a trading claim.
- Do not promote a model from one backtest, p-value, leaderboard or Sharpe ratio.
- Do not let validator and developer share an “independent” context for the same formal check.
- Preserve `broker_execution_allowed=false`.
