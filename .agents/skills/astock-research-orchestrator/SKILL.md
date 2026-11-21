---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research across company analysis, candidate discovery, holdings, portfolio decisions, paper trading and evidence recovery. Use for stock recommendations, buy/sell research, planned-purchase portfolio completion, holding changes or requests spanning project skills.
---

# A股研究总控

## Workflow

1. Recover existing user state: `local-portfolio-sync-paper` when a paper account exists, then `local-portfolio-status`, `portfolio-local-snapshot` and `continuous-monitor-status`. Consume material deltas through `$holding-monitor` / `$continuous-investment-monitor`; queued work is not completed analysis. Record an explicitly reported completed external trade through the existing account workflow before continuing, asking only for genuinely missing execution facts.
2. Route the actual intent. Named company → `$company-deep-research`; broad recommendations → Research Team and `$candidate-scan`; `PLANNED_PURCHASE_PORTFOLIO_COMPLETION` → `$portfolio-manager`; `HELD_POSITION_REBALANCE_REVIEW` → `$holding-monitor` plus `$portfolio-manager`; evidence gaps → `$evidence-investigation`. Load only the relevant capability contract, linked Workflow and required professional Skills. Use the active execution/reasoning/reviewer classification: let code validate and calculate; retain independent model judgment where it changes the research. A capability manifest is not evidence that the host has executed it.
3. For current research use `uv run astock research-acquire-current` or recover its same-request continuation. The user's question timestamp/question-time is not a cutoff; `current-research-policy` freezes the latest verified facts after bounded acquisition. Historical/OOS work has its own strict availability contract.
4. Reuse valid evidence and request-bound artifacts. Automatic recovery uses allowlisted `ProviderRecoveryProposal` and raw-first `SchemaRepair` / Schema Repair; then perform authoritative Web recovery via the evidence Skill. Only after allowlisted automated/provider paths **and** authoritative Web search are exhausted may public unavailability be terminal. A manual request is allowed only for private input/authorization; provider/retry/artifact details are internal and never become the investor answer. Do **not** run `uv run astock probe` before every investment question.
5. For broad discovery retain `research-seeds --live` → `research-seeds-promote` and the pure blind cohort. `JuglarCycleStageSkill` and other methods may change bounded research priority only. Validate Universe coverage through its existing proof, deepen each selected company, and expand unresearched candidates within policy when the requested count is unmet; never fill quotas with unsupported BUY opinions.
6. Resolve planning capital/return objectives per field from the explicit request, verified user history, then active policy defaults. Reuse registered constraints; do not infer account assets from a planning example. Goals cannot relax financial, governance, liquidity, concentration, valuation or publication checks. Planned purchase completion first verifies the anchor's current formal research, then measures CURRENT/ANCHOR/TARGET risk gaps.
7. Complete the applicable canonical DAG: institutional fundamentals and deterministic forecasts/valuation; financial integrity, industry, governance and catalysts; independent Bull/Bear; Reviewer/Red Team; Committee; Portfolio; mandatory readiness and `RecommendationResearchReceipt` / Publication Gate. A Candidate, partial acquisition or unfinished team is not a final investment answer. Read `ClassifiedTradeProtocol` through `trade-plan-view` for exact mechanics instead of inventing prices.
8. Use `uv run astock codex-run-init --require-registered-output` and `uv run astock codex-run-audit` for registered outputs. Committee consumes the frozen case via `committee-input-resolve / committee-plan / committee-decide / committee-audit`. **The committee never performs the search itself**. A new material fact returns to acquisition/research, not an untracked committee browse.
9. Keep research proposals separate from execution. **A submitted order is never called a position until the fill ledger confirms it**. Explicit user simulated-trade instructions override model opinion, while cash, available shares, instrument rules, confirmation and replay remain binding. Default replay is raw 60m; use 5m only for ambiguity. ETF execution remains default-off under its independent rule; diversification is not `EXPLICIT_HEDGE`.
10. Publish one complete investment report from `ResearchNarrativeBundle` through `ResponseGateway` and `research-investor-answer-audit`; preserve stable machine JSON views. Formal WATCH/APPROVE_SIMULATION targets enroll the monitor with reason `RECOMMENDED`. Register one task observation without inventing evaluation labels.

## Same-request closure

Continue the same request when closure says `CONTINUE_AUTOMATICALLY`; retain validated artifacts and resume affected nodes. Only complete, permitted lineage yields `READY_FOR_INVESTOR_VIEW`; public exhaustion yields `PUBLIC_DATA_UNAVAILABLE`, while private input can yield `NEEDS_USER_INPUT`. Do not end with a list of routine research this Skill can still execute.

Follow observed host deadlines/interruption and active resource budgets through `configs/chat_invocation_policy_v1.yaml`, retaining request/round/checkpoint/lease. Elapsed project time alone does not stop useful work. Verify a real continuation binding; never claim an imagined scheduler. Save the exact request/operation/input hash, budget and recovery cursor before an external wave. Reconcile unknown completions before any redispatch; late public originals remain reusable evidence. Save internal progress without presenting unfinished research as a report. A long-lived continuation watcher survives a single round.

Prospective isolation remains `ELIGIBLE_RULE_STATE_MACHINE_RESEARCH` → `AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL` → explicit rule-research approval. **Do not change weights or the paper ledger** from shadow/adaptive results.

## Workflows

- [Company research](../../../docs/workflows/workflow-current-company-research.md)
- [Full market research](../../../docs/workflows/workflow-full-market-research-team.md)
- [Planned purchase and hedging](../../../docs/workflows/workflow-portfolio-transition-and-hedging.md)
- [Holding decisions](../../../docs/workflows/workflow-holding-rebalance-decision.md)
- [Paper trading](../../../docs/workflows/workflow-paper-trading.md)
- [Prospective evaluation](../../../docs/workflows/workflow-prospective-evaluation.md)
- [Public response contract](../../../docs/architecture/public-response-contract-v1.md)

## Output

Give the investment judgment, strength, price/valuation, earnings drivers, financial/governance quality, industry context, catalysts, decisive disagreement, portfolio/risk allocation, entry/exit/invalidation conditions, data cutoff and citations. Explain material uncertainty and rejected alternatives. Do not reduce a complete decision to two generic reasons. For a simple factual question, answer directly.

Planning budgets, suggested allocations and public facts may be shown. Existing-account private values stay implicit unless the user asks; verified requested values may be answered. Bind important narrative facts to their entity, metric, period, unit, value, qualifier and citation before editing. Refine prose from those frozen facts without changing numbers, direction, probabilities, sources or risk. Style repair cannot become a new research gate.

## Prohibitions

- Do not bypass identity, provenance, arithmetic, financial/governance vetoes, same-request publication or paper-account mechanics.
- Do not replace blind discovery, a portfolio constraint or a formal financial claim with a model's confidence or community popularity.
- Do not repeat full acquisition after a local gap, treat public-source failure as missing private input, or demand user research that available tools can perform.
- Do not expose internal commands, hashes, errors or rejected drafts in ordinary investor output.
- Do not invent filled positions, admitted production qualifications, real-broker execution, or a formal hedge from low historical correlation.
