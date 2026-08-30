---
name: catalyst-event-research
description: Build an evidence-linked catalyst and risk event timeline for an A-share company. Use for Research Team CATALYST tasks, earnings or product milestones, approvals, tenders, capacity, lockups, refinancing, litigation, regulation, policy events, or any thesis that depends on dated outcomes.
---

# 催化剂与风险事件研究

## Inputs and authority

- Consume only the ready `company-catalyst` task from the existing `ResearchTeamPlan`; do not create a parallel plan or free-form news workflow.
- Preserve company identity, `as_of`, acquisition lineage and upstream macro, policy, industry and governance artifacts.
- Primary authority is exchange/CNINFO/issuer/regulator/court/procurement or another formal event record. News, social posts and calendars are discovery leads until the exact official document is frozen.

## Procedure

1. Inspect `uv run astock research-team-status <plan_id>` and confirm all declared dependencies are COMPLETE.
2. Build one event ledger with event type, earliest/latest date or window, official source, preconditions, measurable outcome, thesis direction, probability range, impact range, dependency and status.
3. Separate scheduled, contingent, rumored, cancelled and already realized events. Search absence is not negative proof.
4. Use base rates or comparable historical events where defensible and disclose sample/transfer limits. Do not manufacture a precise probability merely to make expected value look quantitative.
5. Model both catalyst and adverse-event paths, interaction/dependency, next evidence checkpoint and what would cancel or delay each event.
6. Detect and remove double counting where an event is already embedded in forecast or valuation assumptions.

## Required output contract

- Register a typed `CatalystRiskPack` through `uv run astock research-team-role-output REQUEST.json`.
- Complete `company-catalyst` through `uv run astock research-team-task-result REQUEST.json`.
- Set `CATALYST_RISK=true` only when material events have official lineage, bounded timing, explicit preconditions, impact ranges, downside coverage and falsifiers.

## Output

Return the registered typed `CatalystRiskPack` artifact ID, ranked event timeline, probability/impact ranges, dependencies, adverse paths, next evidence checkpoints and thesis-changing falsifiers. Avoid a raw news dump.

## Gates and abstention

Abstain and leave `CATALYST_RISK=false` when a material event exists only in rumor/headlines, timing cannot be bounded, negative proof relies on Web-search absence, or the same event remains double counted in forecast, valuation and event expected value.

## Verification

- Re-run `uv run astock research-team-status <plan_id>` and verify the typed artifact, COMPLETE result and readiness evidence.
- Confirm each material event points to a frozen formal object and every probability/impact range states its basis and uncertainty.
- Preserve `broker_execution_allowed=false`; this Skill never turns an event into order authority.

## Workflows

- [`docs/workflows/workflow-catalyst-event-research.md`](../../../docs/workflows/workflow-catalyst-event-research.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)

## Prohibitions

- Do not convert headline sentiment or rumor into a formal catalyst.
- Do not treat absence from Web search as proof an event did not occur.
- Do not double count one event across forecast, valuation and catalyst expected value.
- Do not create recommendation, portfolio, or execution authority.
