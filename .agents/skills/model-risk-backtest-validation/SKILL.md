---
name: model-risk-backtest-validation
description: Independently validate models, forecasts, factors, backtests and quantitative claims used in A-share research. Use for the Research Team MODEL_RISK task, point-in-time/leakage review, multiple testing, calibration, robustness, transaction costs, capacity, model change governance, or champion-challenger comparison.
---

# 模型风险与回测验证

## Inputs and authority

Use the [shared team execution contract](../../../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) for task dispatch, registration and recovery. Load this role's method only; reuse the coordinator's ready-task snapshot.

- Consume only the ready `model-risk-validation` task from the existing `ResearchTeamPlan`; do not create a parallel plan or validate a model you developed in the same independent context.
- Preserve frozen FinancialIntegrity, Fundamental, Valuation, Bull and Bear lineage plus the typed `ModelRiskValidationReport` contract.
- Inventory every decision-relevant model, formula, heuristic and external score with version, purpose, inputs, code/data hash, training/evaluation window and authority boundary. “Available” is not “executed”.

## Procedure

1. Reproduce the decision-relevant result from frozen code/data/input versions or record non-reproducibility as a formal defect.
2. Audit source availability and point-in-time boundaries, label construction, survivorship/selection leakage, train/test separation, repeated tuning, multiple testing, benchmark choice, missing data and corporate actions.
3. For trading claims, test transaction costs, slippage, turnover, liquidity, capacity and realistic execution timing. For forecasts/probabilities, test calibration, scenario coherence, sensitivity and out-of-distribution conditions.
4. For valuation, test terminal-value dependence, discount-rate/driver sensitivity and market-implied expectations.
5. Compare against simple baselines and, where supported, a champion-challenger. A backtest improvement alone does not grant production or trading authority.

## Required output contract

- Set `MODEL_RISK_VALIDATION=true` only when reproducible lineage, leakage/multiple-testing review, realistic transaction costs and capacity, calibration/robustness results, limitations and promotion boundary are frozen.

## Output

Return the registered typed `ModelRiskValidationReport` artifact ID, model inventory, reproducibility status, leakage/multiple-testing findings, baseline comparison, calibration/robustness results, transaction-cost/capacity realism, confidence downgrade and promotion boundary.

## Gates and abstention

Request reproducible inputs or correction from the owning task, preserving reviewer independence. Continue unrelated research; abstain from the affected model claim and leave `MODEL_RISK_VALIDATION=false` when code/data lineage is unavailable, an executed model cannot be reproduced, a test set was reused for tuning, future data entered the result, or transaction costs/liquidity/capacity are omitted from a trading claim.

## Verification

- Confirm test-family size, multiple-comparison treatment, baseline comparison, failure modes and monitoring/fallback boundary are explicit.
- Proposed model or weight changes remain research-only and cannot mutate the paper ledger here.

## Workflows

- [`docs/workflows/workflow-prospective-evaluation.md`](../../../docs/workflows/workflow-prospective-evaluation.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)

## Prohibitions

- Do not use one backtest, p-value, leaderboard or Sharpe ratio as production proof.
- Do not repair missing lineage by guessing code, data or parameters.
- Do not let the validator modify paper/real ledgers or production weights.
- Do not create real brokerage authority.
