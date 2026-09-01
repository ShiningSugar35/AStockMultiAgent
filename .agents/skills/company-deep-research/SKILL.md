---
name: company-deep-research
description: Build a cited current or historical deep A-share company research case and final committee decision. Use for deep analysis of a named company, business quality, valuation, industry position, risks, whether the stock is worth a paper position, what evidence would change the view, or how to interpret entry and exit rules.
---

# 公司深度研究

1. Resolve the company to one six-digit A-share identity. For a **current** opinion, run `uv run astock research-acquire-current <company_id> --market <market>` first; do not begin with `probe` or the user's question-time cutoff. Historical/recorded research uses an explicit `research-plan <company_id> --as-of <timestamp>`.
2. Exhaust policy-driven automatic recovery before asking the user for data. Use the active policy budget from `current-research-policy`; retry transient failures, route a `ProviderRecoveryProposal` only through deterministic validation, and treat unknown drift through raw-first `SchemaRepair` rather than guessed facts. Then use authoritative Web research. Aggregate any unavoidable manual request and ask once; provider/debug/artifact details remain internal and never belong in the investor answer.
3. Reuse audited evidence, financial, BaseCase, specialist and memo artifacts while their company/source lineage remains applicable. Current research freezes one decision snapshot after acquisition; historical/prospective work keeps strict source-availability/PIT boundaries.
4. Run `$financial-integrity-audit` before treating a low multiple as an opportunity. Secondary structured feeds may locate facts, but material financial claims return to official reports or equivalently authoritative sources.
5. For a formal current opinion, build the institutional fundamental path: evidence sufficiency → industry/company economics → driver tree → Bull/Base/Bear forecast → valuation/sensitivity → decision context. Keep explicit Bull/Base/Bear scenario analysis; forecast and valuation are deterministic calculations over evidence-bound assumptions.
6. Build BaseCase once. Route only bounded incremental specialists and audited Knowledge Skills. Community/OCR material remains methodology or lead evidence, never sole support for a material company fact.
7. Separate research readiness from execution readiness. Missing current trading-rule/company-action detail may block a simulated order without blocking a months/years conclusion about company quality and valuation.
8. The committee receives frozen artifacts only. Use `committee-schema`, `committee-plan`, relevant audits and the formal outcome states. The committee cannot browse.
9. When exact entry/exit/stop mechanics are needed, use `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`. Never invent an exact price. If the formal result permits simulation, the current entry condition is satisfied, and local `auto_ai_paper_order_on_approved_entry=true`, proceed into the existing paper account/order-confirmation flow; do **not** call it a position until replay records a fill.
10. A direct user instruction to buy/sell overrides the model's opinion, not execution mechanics. Do not veto the user's simulated order because research is bearish; route it to the paper-order path immediately while preserving cash/position availability, lot size, verified tradability, price-band, account-confirmation and fill checks.
11. Require `research-chain-audit` and final runtime/committee audits for a formal certified chain. For durable internal provenance use `uv run astock codex-run-init <request> --artifact-id <artifact_id> --require-registered-output`, `codex-run-import`, and `codex-run-audit`; these identities stay internal.
12. After a formal current opinion is complete, invoke `$continuous-investment-monitor`: enroll the resolved stock as `ANALYZED`, and persist only evidence-backed typed entry/review/exit thresholds that already exist in structured outputs. This enrollment is required even when the conclusion is WATCH or the stock is not held, so later price, disclosure, news-lead and catalyst changes can be reviewed incrementally.
13. Build one canonical `ResearchNarrativeBundle` and render public output through `ResponseGateway`. The stable machine commands `research-investor-view` and `research-acquisition-investor-view` remain JSON contracts; use `research-public-view` or `research-acquisition-public-view` for audited public presentation. Never delete the subject, conclusion strength, valuation/odds, largest risk, change condition, data cutoff, required citations, or safe report reference merely to meet a length budget.
14. After a completed deep-research task, publish a formal report from the same frozen narrative and registered input artifacts. Default to DOCX, use Markdown only as deterministic fallback, and generate PDF only when an available converter succeeds validation. Return only the safe report reference in the investor answer; report generation must never become a second research fact source.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-committee-trade-plan.md`](../../../docs/workflows/workflow-committee-trade-plan.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)
- [`docs/workflows/workflow-adaptive-edge.md`](../../../docs/workflows/workflow-adaptive-edge.md)
- [`docs/architecture/public-response-contract-v1.md`](../../../docs/architecture/public-response-contract-v1.md)
- [`docs/architecture/formal-reporting-v1.md`](../../../docs/architecture/formal-reporting-v1.md)

## Output

Keep the investor answer compact: **主体 → 结论与强度 → 估值/赔率 → 2–4个决定性理由 → 最大风险 → 改变判断的条件 → 数据时间与必要引用**. Add catalyst detail only when it changes the decision. Avoid saying the same thing again under “总结”. If a technical term, empirical paper result or formula is necessary, explain it once in a short parenthesis, e.g. “CVaR（最差那部分行情里的平均亏损）”. Translate unresolved evidence into investment language rather than backend terminology. Length reduction may remove only non-critical reasons; mandatory fields must survive or the answer uses a no-echo safe fallback. Before sending, apply `research-investor-answer-audit` and the canonical public response contract.

## Prohibitions

- Do not stop current research after the first provider fails if another automatic or authoritative source can answer the same bounded question.
- Do not use community content as sole support for a key company fact.
- Do not invent BUY/SELL prices to hide uncertainty.
- Do not treat a submitted but unfilled paper order as a holding.
- Do not bypass paper-account mechanics merely because the user overrode the research opinion.
- Do not submit a real brokerage order.

## Same-request automatic continuation contract

For a current-company investment question, an acquisition gap is an internal workflow state,
not a user-facing answer. The Agent must keep the original request active and drive the
continuation until it reaches one of the explicit terminal states below.

1. Start or recover the durable `CurrentResearchContinuation` for the investor request with `uv run astock research-current-continuation-start REQUEST.json` / `uv run astock research-current-continuation-status <continuation_id>`; the underlying direct acquisition entry remains `uv run astock research-acquire-current <company_id> --market <market>` for diagnostics and compatibility.
2. While the status is `AUTO_RESOLUTION_REQUIRED`, inspect unresolved external tasks and use
   approved Web/Search or authoritative-source adapters as discovery only. Fetch the exact
   official document, freeze it through the official capture/ObjectStore path, emit a typed
   `CurrentResearchAutomaticResolution`, submit it with `uv run astock research-current-continuation-resolve RESOLUTION.json`, and resume the same continuation. Public captures are validated and bound atomically; `research-current-continuation-bind` is reserved for later user-supplied private material. Never ask the user to perform public Web research on the Agent's behalf.
3. While the status is `TEAM_RESEARCH_REQUIRED`, read `uv run astock research-team-status <plan_id>` and execute every ready non-gate task in the frozen research-team DAG. Route Macro/Policy to `$macro-policy-regime`, Industry to `$industry-value-chain`, Governance to `$governance-management-quality`, Catalyst to `$catalyst-event-research`, the independent Reviewer to `$investment-red-team`, and model/backtest validation to `$model-risk-backtest-validation`; register every typed output/result and advance the same continuation. Do not skip fundamental, financial-integrity, valuation, independent Bull/Bear, reviewer, model-risk, red-team, or committee dependencies merely to improve latency.
4. `READY_FOR_INVESTOR_VIEW` permits a formal recommendation only when every readiness gate
   passes. `OBSERVATION_ONLY_FOR_INVESTOR_VIEW` permits an explicitly labelled observation
   view after the whole team has completed but a formal gate remains unsatisfied; it must not
   be presented as a formal buy/sell recommendation.
5. `NEEDS_USER_INPUT` is allowed only when all approved automatic public channels have
   exhausted their bounded attempts, or when the required formal material is genuinely
   private. Material later supplied by the user must bind to the same continuation lineage
   and resume from the interrupted task rather than opening a new research chain.
6. Intermediate statuses must set `investment_conclusion_blocked=true`; they are not an
   excuse to return an incomplete conclusion. Broker execution remains forbidden in every
   state.

The runtime may use `CurrentResearchContinuationService.run_to_terminal` with an Agent-owned
external resolver and team executor. The CLI-equivalent loop uses
`research-current-continuation-resolve`, `research-current-continuation-resume`, typed
`research-team-role-output` / `research-team-task-result`, and
`research-current-continuation-advance`; `research-current-continuation-bind` resumes later
private-material input on the same lineage. Python owns deterministic budgets, PIT, artifact
lineage, verified same-request acquisition reuse, state validation and safety gates; the Agent
owns discovery and research judgement.
