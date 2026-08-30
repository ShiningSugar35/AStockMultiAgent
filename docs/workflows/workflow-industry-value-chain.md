# Workflow — Industry Value Chain

## When to use

Use when a ready Research Team `industry-value-chain` task requires an `IndustryValueChainProfile`, when a company needs defensible peer selection, or when business economics and valuation depend on industry structure.

Primary skill: `$industry-value-chain`. It consumes `$macro-policy-regime` where relevant and feeds `$company-deep-research`, valuation, catalyst, Bull/Bear and Reviewer tasks.

## Flow

1. Read `research-team-status <plan_id>` and consume only a ready INDUSTRY task. Resolve the internal archetype through `industry-research-resolve`, preserving that it is not a certified official taxonomy.
2. Freeze official/issuer/association evidence for value-chain nodes, market structure, supply/demand, customer and supplier concentration, regulation and cycle indicators.
3. Define peer inclusion/exclusion rules before reading the desired valuation result. Normalize period, currency, scope, business mix and one-offs; retain an explicit “no comparable peer” outcome.
4. Build the profit-pool map, company position, unit-economics drivers, cycle/base-rate state, competitive advantages/disadvantages and falsifiers.
5. Register `IndustryValueChainProfile` with `research-team-role-output`, then complete the task with `research-team-task-result`. `INDUSTRY_PROFILE=true` requires frozen evidence and non-circular peer methodology.
6. Downstream tasks reuse the profile and peer set instead of independently rebuilding or cherry-picking them.

## Stop conditions

- Stop or abstain when segmentation, taxonomy, peer comparability or accounting normalization is materially unresolved.
- Do not set `INDUSTRY_PROFILE=true` from sector popularity, broker labels or price momentum.
- Do not certify internal archetypes as official classifications.
- Do not create recommendation, portfolio or execution authority.
