# Workflow — Catalyst & Event Research

## When to use

Use when the ready Research Team `company-catalyst` task requires a `NewsEventResearchPack`, or when a company decision depends on dated approvals, tenders, capacity, products, earnings, refinancing, lockups, litigation, policy or regulatory events.

Primary skill: `$catalyst-event-research`; it consumes macro/policy, industry and governance profiles and feeds independent upside/downside, reviewer and Committee tasks.

## Flow

1. Read `research-team-status <plan_id>` and consume only the ready CATALYST task. Reuse existing event captures before searching for deltas.
2. For current discovery, use the shared `news.discovery.lead` lane to collect every available independent structured lead source within the bounded budget (currently GDELT + Finnhub when configured). A success from one source must not suppress the others, and duplicate syndicated stories remain one lead family rather than independent confirmation.
3. Regardless of structured-lead success, continue authoritative Web search for material company/policy events and return to exchange/CNINFO/issuer/regulator/government/court/procurement primary records before admitting a fact. Finnhub/GDELT are discovery-only and can never be the sole policy or material-event authority.
4. Build one event ledger: window, preconditions, measurable outcome, probability/impact range, dependencies, downside path, status and next evidence checkpoint.
5. Compare with relevant base rates where defensible; disclose sample and transfer limitations. Detect event interactions and prevent double counting against forecast/valuation assumptions.
6. Register `NewsEventResearchPack` through `research-team-role-output`, then complete through `research-team-task-result`. Leave `CATALYST_RISK=false` for rumor-only, unbounded or unsupported material events.
7. Downstream independent upside/downside analyses receive the same event ledger but use independent contexts.

## Stop conditions

- Stop or abstain when material events lack primary lineage, bounded timing or explicit preconditions.
- Do not use search absence as negative proof.
- Do not manufacture precise probabilities or expected values.
- Do not turn an event into recommendation or order authority by itself.
