---
name: portfolio-manager
description: Evaluate portfolio risk, compare constrained allocation methods, and construct an A-share portfolio only from committee-approved research. Use for portfolio review, allocation, diversification, risk contribution, current holdings, or requests to recommend several researched stocks as one portfolio.
---

# 组合评估与构建

1. Distinguish the task first: existing-portfolio review uses `uv run astock portfolio-paper-evaluate` for the paper account or `portfolio-evaluate REQUEST.json` for read-only external holdings; a new portfolio uses the candidate/research/committee chain before `portfolio-construct REQUEST.json`.
2. For a recommendation request, obtain a bounded discovery set through `uv run astock research-seeds --live`, then close complete Candidate evidence with `$candidate-scan`. Research Seeds may originate from existing candidates, market liquidity/scale, or expert Skill domains; none is a buy recommendation. A `RESEARCH_READY` candidate is still only worth researching. Run `$company-deep-research` for each shortlist member and retain only a current `ClassifiedTradeProtocol` whose final outcome is `APPROVE_SIMULATION` and whose audit passes.
3. Before constructing a portfolio, align every candidate to one point-in-time `as_of`. Never combine stale and current classifications. Missing daily history, expired classification, unresolved corporate action, or incomplete research is `NEEDS_INFO`, not an assumed neutral input.
4. Run `uv run astock portfolio-construct REQUEST.json`. Read all four proposals: constrained equal weight, inverse volatility, hierarchical risk, and Ledoit-Wolf shrinkage minimum variance. Treat `EQUAL_WEIGHT_CONSTRAINED` as the default benchmark until real forward evidence supports another default.
5. Apply the frozen committee limits before explaining weights: no leverage, maximum single position, maximum total exposure, correlation/drawdown gates, and the currently available group-exposure constraint. Residual allocation stays cash rather than being forced into stocks.
6. For an existing portfolio, explain annualized volatility and downside deviation, beta, tracking error, max drawdown, historical 95% VaR/CVaR/CDaR, concentration HHI/effective position count, pair correlations, and marginal risk contribution in plain language. Separate historical risk diagnostics from forecasts.
7. Use industry/group exposure only when its provenance is clear. Current construction accepts a caller-supplied risk group and therefore reports `RISK_GROUP_IS_CALLER_SUPPLIED`; do not present that group limit as a fully certified industry taxonomy.
8. Run `uv run astock portfolio-audit <PortfolioAnalysisReport_or_PortfolioConstructionReport_artifact_id>` before using a durable report in a final answer. Open only the compressed report and exact referenced artifacts; do not reread every company document.
9. When the user asks “which stocks should I buy as a portfolio?”, summarize the single-stock committee outcomes first, then the portfolio interaction: why the names diversify or overlap, which risk dominates, what cash remains, and why the default allocation differs from the alternative methods.

## Workflows

- [`docs/workflows/workflow-portfolio-construction.md`](../../../docs/workflows/workflow-portfolio-construction.md)

## Output

For portfolio review, return a plain-language risk diagnosis plus the immutable `PortfolioAnalysisReport` identity. For construction, return the admitted/rejected company list, constrained equal-weight default, three comparison proposals, cash residual, binding constraints, model-risk warnings, and the immutable `PortfolioConstructionReport` identity. Explain percentages as portfolio weights, not certainty or expected return.

## Prohibitions

- Do not turn a candidate-scan signal directly into a portfolio holding.
- Do not maximize Sharpe from an unconstrained mean-return estimate or present one optimizer as ground truth.
- Do not hide residual cash when constraints prevent full investment.
- Do not treat unverified industry labels as official classifications.
- Do not write the paper ledger, place a simulated order, or create a brokerage order from portfolio analysis.
- Do not use retrospective returns or optimization output to override an individual company `REJECT`, `WATCH`, or `NEEDS_INFO` decision.
