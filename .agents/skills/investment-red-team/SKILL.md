---
name: investment-red-team
description: Independently challenge a frozen A-share research case as the investment red team after separate Bull and Bear outputs are complete. Use for the Research Team REVIEWER task, disconfirming evidence, assumption fragility, reverse valuation, scenario consistency, double counting, pre-mortem analysis, or formal committee preparation.
---

# 投资研究独立红队

## Inputs and authority

- Consume only the ready `investment-red-team` REVIEWER task from the existing `ResearchTeamPlan`; do not create a parallel plan or act as the Bear author.
- Require `bull-case` and `bear-case` to be COMPLETE with different `independent_context_id` values and frozen `IndependentBullCase` / `IndependentBearCase` artifacts.
- Work from the common frozen evidence set and admitted specialist outputs. The Reviewer does not browse, repair evidence, or participate in Bull/Bear production.

## Procedure

1. Inspect `uv run astock research-team-status <plan_id>` and verify dependency completion plus Bull/Bear context independence.
2. Reconstruct a compact assumption ledger: key driver, source, scenario range, valuation sensitivity, catalyst dependency and falsifier.
3. Search the frozen corpus for disconfirming evidence and alternative explanations. Test base-rate neglect, narrative/confirmation bias, selection bias, circular peer choice, scenario probability calibration and terminal-value dependence.
4. Perform reverse valuation/market-implied-expectation checks, event/forecast double-counting checks and threshold-fragility tests.
5. Classify findings as fatal defect, material uncertainty or ordinary disagreement. Attach concrete remediation, confidence downgrade and explicit kill criteria; do not manufacture objections merely to appear independent.

## Required output contract

- Register a typed `InvestmentRedTeamReport` through `uv run astock research-team-role-output REQUEST.json`.
- Complete `investment-red-team` through `uv run astock research-team-task-result REQUEST.json`.
- Set `INDEPENDENT_REVIEW=true` only when both independent cases, the common evidence set, assumption ledger, disconfirming evidence, stress tests, unresolved defects and kill criteria are covered.

## Output

Return the registered typed `InvestmentRedTeamReport` artifact ID, strongest surviving thesis, fatal/material defects, disconfirming evidence, market-implied-expectation and double-counting findings, confidence change, remediation and kill criteria.

## Gates and abstention

Abstain and leave `INDEPENDENT_REVIEW=false` when Bull/Bear independence cannot be verified, required upstream artifacts are missing, the common evidence set is not frozen, or a critical fact requires new evidence. New evidence returns to `$evidence-investigation`; affected Bull, Bear and Reviewer tasks rerun rather than silently mutating frozen outputs.

## Verification

- Re-run `uv run astock research-team-status <plan_id>` and verify the typed Reviewer artifact, COMPLETE checkpoint and readiness evidence.
- Confirm the Reviewer used neither Bull nor Bear context, performed no browsing, and did not overwrite upstream artifacts.
- Preserve `broker_execution_allowed=false`; the Reviewer cannot override Committee or create recommendation/order authority.

## Workflows

- [`docs/workflows/workflow-investment-red-team.md`](../../../docs/workflows/workflow-investment-red-team.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)

## Prohibitions

- Do not share one `independent_context_id` between Bull and Bear.
- Do not browse, add evidence, or self-repair lineage inside the Reviewer task.
- Do not mark review complete with generic criticism or self-reported independence.
- Do not override Committee or create execution authority.
