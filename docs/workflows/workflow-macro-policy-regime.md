# Workflow — Macro & Policy Regime

## When to use

Use when a ready Research Team task requires `MacroRegimeProfile` or `PolicyRegimeProfile`, or when a current/historical company thesis materially depends on monetary, fiscal, regulatory, liquidity, industrial-policy, demand or cost conditions.

Primary skill: `$macro-policy-regime`. It feeds `$industry-value-chain`, `$catalyst-event-research`, `$company-deep-research` and the existing Research Team; it never creates recommendation authority by itself.

## Flow

1. Read `research-team-status <plan_id>` and consume only `macro-regime` / `policy-regime` when ready. Preserve plan scope, `as_of`, acquisition lineage and task dependencies.
2. Reuse frozen releases first. For new material, capture primary official publications with announcement/effective/observation/availability timestamps; historical work rejects later revisions unavailable at `as_of`.
3. Separate observed state from forecast. Build base/upside/downside regime states and an explicit transmission graph into the relevant industry/company drivers.
4. Cross-check material conflicts against another authoritative series or source. Missing official text, unclear vintage or unsupported transmission leaves the relevant readiness check false.
5. Register the frozen `MacroRegimeProfile` / `PolicyRegimeProfile` through `research-team-role-output`, then register an independently identified `ResearchRoleResult` through `research-team-task-result`.
6. Downstream Agents receive only the profile, assumptions, falsifiers and evidence lineage—not a repeated scrape of the same macro corpus.

## Stop conditions

- Stop with abstention when vintage, effective date, authority or company transmission cannot be established.
- Do not set `MACRO_REGIME` / `POLICY_REGIME=true` from commentary, headline sentiment or a point forecast alone.
- Do not convert a regime state directly into a company recommendation, portfolio weight or order.
- Preserve `broker_execution_allowed=false`.
