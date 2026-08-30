---
name: industry-value-chain
description: Build an evidence-linked industry value chain and defensible comparable-company profile for A-share research. Use for Research Team INDUSTRY tasks, sector comparison, competitive position, cycle structure, unit economics, market share, pricing power, or valuation peer selection.
---

# 行业价值链与可比公司

## Inputs and authority

- Consume only the ready `industry-value-chain` task from the existing `ResearchTeamPlan`; do not create a parallel plan or second taxonomy service.
- Preserve company identity, `as_of`, acquisition lineage, upstream `company-intent`, the point-in-time availability boundary, and the typed `IndustryValueChainProfile` output contract.
- Primary authority is issuer/exchange/regulator/official ministry and statistics first, then established industry associations and independently sourced peer filings. Community rankings and broker labels are discovery only.

## Procedure

1. Inspect `uv run astock research-team-status <plan_id>` and confirm the INDUSTRY task is dependency-ready.
2. Resolve the active internal research archetype with `uv run astock industry-research-resolve QUERY`; treat it as a research method, not a certified official industry label. Record official taxonomy separately when available.
3. Map the value chain: upstream inputs, production/service bottlenecks, channels, customers, substitutes, regulatory constraints and profit-pool distribution. Connect every material link to an observable driver or mark it unresolved.
4. Select peers before observing the desired valuation conclusion. Use business mix, geography, customer/end-market, scale, capital intensity and accounting scope; show exclusions and retain a valid “no comparable peer” outcome.
5. Normalize period, currency, consolidation scope and one-off items. Produce cycle state, unit-economics drivers, base rates, competitive advantages/disadvantages and falsifiers.

## Required output contract

- Register a typed `IndustryValueChainProfile` through `uv run astock research-team-role-output REQUEST.json`.
- Complete `industry-value-chain` through `uv run astock research-team-task-result REQUEST.json`.
- Set `INDUSTRY_PROFILE=true` only when value-chain evidence, profit-pool position, normalized peer methodology and falsifiers are frozen.

## Output

Return the registered typed `IndustryValueChainProfile` artifact ID, the value chain and profit-pool position, a defensible peer set or an explicit no-peer result, normalized comparison, cycle/base-rate context and falsifiers.

## Gates and abstention

Abstain and leave `INDUSTRY_PROFILE=false` when business segmentation is materially missing, taxonomy is uncertain, peer periods/scopes cannot be reconciled, or no sufficiently comparable peer set exists. Do not force a peer multiple merely to produce a target value.

## Verification

- Re-run `uv run astock research-team-status <plan_id>` and verify the typed output, COMPLETE checkpoint and readiness evidence.
- Confirm peer rules were fixed before valuation, exclusions are retained, and downstream tasks reuse the frozen profile rather than rebuilding a preferred peer set.
- Preserve `broker_execution_allowed=false`; this Skill creates no recommendation or order authority.

## Workflows

- [`docs/workflows/workflow-industry-value-chain.md`](../../../docs/workflows/workflow-industry-value-chain.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)

## Prohibitions

- Do not certify an internal archetype as an official industry classification.
- Do not choose peers after seeing which set creates the preferred valuation.
- Do not treat sector momentum, popularity or a broker label as company evidence.
- Do not create recommendation, portfolio, or execution authority.
