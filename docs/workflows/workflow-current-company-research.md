# Workflow — Current Company Research

## When to use

Use for a named A-share investment question. Start with `$astock-research-orchestrator`, then `$company-deep-research`; evidence and financial integrity are bounded subflows.

## Flow

1. **Recover state and identity.** Restore the account/holdings/monitor projection, review material deltas and resolve one company/market. Reuse verified same-request acquisition and registered evidence; do not repeat every held-company analysis.
2. **Acquire current facts.** Use `uv run astock research-acquire-current <company_id> --market <market>` or the continuation below. `current-research-policy` owns lookback, capability DAG, retry and resource budgets. The question timestamp is not the current research cutoff. Freeze the decision snapshot after acquisition; historical/OOS uses its own real availability boundary.
3. **Recover only missing capabilities.** `SourceAccessRouter` uses registered capability/authority/health/cost policy, not a hardcoded provider order. Execute independent tasks by DAG wave. Check the latest actually disclosed financial period; do not infer disclosure from the calendar. Preserve raw source responses and independent-source disagreements rather than silently overwriting facts.
4. **Recover official evidence.** Route gaps through `$evidence-investigation` and [evidence recovery](workflow-evidence-recovery.md). Use authoritative Web and observed host tools; read `web-tool-schema` / `src/astock/schemas/web_tools.py` only when needed. Reuse body content, use configured backup only after an explicit host gap, and never relabel search/Markdown as a PDF. Exact official documents prove their admitted item; only exhaustive pagination/terminal proof supports enumeration or negative claims. Preserve `official_exhaustive_proof_allowed=false` for exact-item admission.
5. **Build one research case.** Complete `EvidenceSufficiencyReport → IndustryProfile / CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`. Python computes evidence-bound assumptions and sensitivity. Incomplete financial evidence can support a clearly bounded qualitative observation, but cannot assert financial integrity, precise valuation or formal recommendation.
6. **Dispatch specialists and independent review.** Use registered BaseCase and incremental deltas. Route macro/policy, industry, catalyst, governance, reviewer and model-risk to the corresponding canonical Skills. Bull and Bear use independent contexts; Reviewer follows both. Optional registered technical context reuses `MarketContextPack` / existing factor references, adds no parallel technical Agent and does not block otherwise supported fundamentals.
7. **Freeze before Committee and Portfolio.** Committee cannot fetch evidence. Register typed role outputs/results, run required audits, then finish committee, trade classification, portfolio and final receipt/publication. A missing execution rule may block simulation without invalidating an unrelated long-term fundamental observation. Submitted orders never imply filled positions.
8. **Publish from the same facts.** Build `ResearchNarrativeBundle`, render through `ResponseGateway` and run `research-investor-answer-audit`. Material decisions cover economic drivers, accounting/governance, valuation, decisive disagreement, catalysts, portfolio risk and entry/exit/invalidation conditions with citations. Keep required content; style repair cannot change facts or add a research gate. Formal reports reuse the same narrative; see [reporting](../architecture/formal-reporting-v1.md).

## Same-request automatic continuation

Start/recover with `research-current-continuation-start REQUEST.json` and `research-current-continuation-status`. Do not create a new request merely because one provider or page fails.

| State | Continuation |
|---|---|
| `AUTO_RESOLUTION_REQUIRED` | Discover and freeze exact official evidence; submit a typed automatic-resolution artifact through `research-current-continuation-resolve RESOLUTION.json`, then resume |
| `TEAM_RESEARCH_REQUIRED` | Execute all ready research-team tasks, register role outputs/results and advance the same DAG |
| `READY_FOR_INVESTOR_VIEW` | Complete lineage plus final receipt/publication permits the formal answer |
| `OBSERVATION_ONLY_FOR_INVESTOR_VIEW` | Complete the team first, then label the bounded observation; no formal buy/sell permission |
| `PUBLIC_DATA_UNAVAILABLE` | Public routes are explicitly exhausted; preserve evidence and unresolved impact, with no private-material demand |
| `BUDGET_EXHAUSTED` | Time/attempt resource limit; preserve the same request, consume already bound evidence locally without new network work, and record unresolved impact without claiming public-route exhaustion |
| `NEEDS_USER_INPUT` | Identified private input or authorization dependency only; independent capabilities continue, and later evidence binds to the same request |

Active recovery and team states keep `same_request_continuation_required=true` and `investment_conclusion_blocked=true`; normal investor answers must not expose those backend fields. Each resolution preserves its original evidence binding. Later evidence uses `research-current-continuation-bind` after the original admission requirements are met. A resource limit does not mark unfinished research complete. Runtime and CLI continuation obey the same lineage and terminal contract.

## Structured supplements

Read `tushare-schema --endpoint ...` and the exact official reference linked by `configs/tushare_access.yaml` for basic/market/financial/macro needs. `tushare-query` is a supplementary structured route, not the existing formal financial_sources statement certification. Retain applicable `research-supplemental-acquire` hints and Finnhub news leads; missing optional credentials are source availability, not missing investor data. BaoStock remains on its established reference route. Do not load an entire provider skill or repeat a satisfied query.

## Stop conditions

- Do not publish a Candidate, acquisition gap or unfinished team as a completed investment decision.
- Exhaust applicable bounded providers and authoritative Web before marking public unavailability; ask for user material only when private or authorization-dependent.
- Never invent a report disclosure, BUY price, target weight, negative proof or completed source enumeration.
- Keep identity, provenance, financial/accounting correctness, final publication and paper-account mechanics deterministic.
- Do not add real brokerage execution or expose backend diagnostics in an ordinary investor report.
