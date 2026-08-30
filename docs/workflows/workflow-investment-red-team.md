# Workflow — Investment Red Team

## When to use

Use when separate Bull and Bear Research Team tasks are frozen and the ready `investment-red-team` task requires an `InvestmentRedTeamReport` before Committee or formal recommendation.

Primary skill: `$investment-red-team`. It consumes frozen evidence, Fundamental, Valuation, Catalyst, Governance and independent Bull/Bear outputs; evidence repair remains `$evidence-investigation`.

## Flow

1. Read `research-team-status <plan_id>`. Require both Bull and Bear COMPLETE and verify different `independent_context_id` values.
2. Freeze the common input set. Reviewer performs no browsing and did not help produce either case.
3. Reconstruct an assumption ledger and test counterevidence, base rates, circular peers, reverse valuation, terminal-value dependence, scenario calibration, event/forecast double counting and threshold fragility.
4. Classify findings as fatal defect, material uncertainty or ordinary disagreement. Attach concrete remediation or confidence downgrade; generic criticism does not pass.
5. Register `InvestmentRedTeamReport` through `research-team-role-output`, then complete through `research-team-task-result`. `INDEPENDENT_REVIEW=true` requires both independent cases, common evidence, assumptions, counterevidence and unresolved defects.
6. New evidence requests return to `$evidence-investigation`; affected Bull/Bear/Reviewer tasks rerun rather than silently mutating frozen outputs.

## Stop conditions

- Stop or abstain if Bull/Bear independence, frozen lineage or required upstream artifacts cannot be verified.
- Reviewer cannot browse, self-repair evidence or share an independent context with Bull/Bear.
- Do not mark review complete with stylistic objections or narrative reassurance.
- Do not override Committee or create execution authority.
