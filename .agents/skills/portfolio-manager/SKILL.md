---
name: portfolio-manager
description: Evaluate portfolio risk, compare constrained allocation methods, and construct an A-share portfolio only from committee-approved research. Use for portfolio review, allocation, diversification, risk contribution, current holdings, or requests to recommend several researched stocks as one portfolio.
---

# 组合评估与构建

1. Refresh the Git-ignored local portfolio first with `uv run astock local-portfolio-sync-paper` when the paper account exists. Existing-paper review may use `uv run astock portfolio-paper-evaluate --account-id default --live`; a new portfolio still uses Candidate → company research → formal approval before construction.
2. For recommendations, obtain a bounded discovery set with `research-seeds --live`, close Candidate evidence, and run `$company-deep-research` for each shortlist member. A `RESEARCH_READY` Candidate never creates BUY authority.
3. Align every admitted company to one current point-in-time snapshot. Do not mix stale and current decisions or assume missing corporate-action/execution facts are neutral.
4. Run `uv run astock portfolio-construct REQUEST.json`. Compare the policy-enabled `EQUAL_WEIGHT_CONSTRAINED` benchmark with inverse volatility, hierarchical risk, and Ledoit-Wolf shrinkage minimum variance. Keep constrained equal weight as the default until prospective evidence justifies a versioned change; optimization cannot override an individual company's `REJECT / WATCH / NEEDS_INFO`.
5. Apply hard risk constraints before weights: no leverage, single-name/total exposure, correlation/drawdown, and available group exposure. Unallocated capital stays cash.
6. Explain only the risk measures that materially affect the decision. If you use terms such as CVaR, beta or shrinkage covariance, define them once in ordinary language (for example, “CVaR = 最差那部分历史行情里的平均亏损”). Historical risk statistics are diagnostics, not return forecasts.
7. If a newly constructed portfolio contains names whose formal result permits simulation and whose entry conditions are currently satisfied, local `auto_ai_paper_order_on_approved_entry=true` allows those names to enter the existing simulated account/order-confirmation flow. Portfolio weights determine intended sizing; actual holdings change only after replay records fills.
8. A direct user paper-trade instruction still overrides the portfolio recommendation itself. Do not refuse a simulated user order because it worsens the recommended allocation; record the order request and separately warn, briefly, about the resulting concentration/risk.
9. Run `uv run astock portfolio-audit <PortfolioAnalysisReport_or_PortfolioConstructionReport_artifact_id>` for durable formal output; internal artifact identities stay out of the normal investor answer.

## Workflows

- [`docs/workflows/workflow-portfolio-construction.md`](../../../docs/workflows/workflow-portfolio-construction.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)

## Output

Keep the final answer short. For an existing portfolio: **总体风险一句话 → 最需要处理的1–3个持仓/暴露 → 建议动作**. For construction: **入选标的与权重 → 保留现金 → 1–3个主要组合风险**. Do not dump every calculated metric merely because it exists; explain only decision-relevant metrics and define unfamiliar jargon briefly.

## Prohibitions

- Do not turn a candidate-scan signal directly into a holding.
- Do not maximize Sharpe from an unconstrained mean-return estimate or present one optimizer as ground truth.
- Do not hide residual cash when constraints prevent full investment.
- Do not treat unverified industry labels as official classifications.
- Do not treat a submitted but unfilled paper order as a position.
- Do not use portfolio optimization to override an individual company `REJECT`, `WATCH`, or `NEEDS_INFO` for AI-initiated orders.
- Do not create or send a real brokerage order.
