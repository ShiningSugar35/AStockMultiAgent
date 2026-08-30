# Workflow — Catalyst & Event Research

## When to use

Use when the ready Research Team `company-catalyst` task requires a `CatalystRiskPack`, or when a company decision depends on dated approvals, tenders, capacity, products, earnings, refinancing, lockups, litigation, policy or regulatory events.

Primary skill: `$catalyst-event-research`; it consumes frozen macro/policy, industry and governance profiles and feeds independent Bull/Bear, Reviewer and Committee tasks.

## Flow

1. Read `research-team-status <plan_id>` and consume only the ready CATALYST task. Reuse existing event captures before searching for deltas.
2. Discover with news/calendars only as needed, then return to exchange/CNINFO/issuer/regulator/court/procurement primary records and freeze exact event lineage.
3. Build one event ledger: window, preconditions, measurable outcome, probability/impact range, dependencies, downside path, status and next evidence checkpoint.
4. Compare with relevant base rates where defensible; disclose sample and transfer limitations. Detect event interactions and prevent double counting against forecast/valuation assumptions.
5. Register `CatalystRiskPack` through `research-team-role-output`, then complete through `research-team-task-result`. Leave `CATALYST_RISK=false` for rumor-only, unbounded or unsupported material events.
6. Downstream Bull/Bear Agents receive the same frozen event ledger but use independent contexts.

## Stop conditions

- Stop or abstain when material events lack primary lineage, bounded timing or explicit preconditions.
- Do not use search absence as negative proof.
- Do not manufacture precise probabilities or expected values.
- Do not turn an event into recommendation or order authority by itself.
