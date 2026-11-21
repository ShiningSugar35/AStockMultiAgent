---
name: industry-value-chain
description: Build an evidence-linked industry value chain and defensible comparable-company profile for A-share research. Use for Research Team INDUSTRY tasks, sector comparison, competitive position, cycle structure, unit economics, market share, pricing power, or valuation peer selection.
---

# 行业价值链与可比公司

## Inputs and authority

Use the [shared team execution contract](../../../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) for task dispatch, registration and recovery. Load this role's method only; reuse the coordinator's ready-task snapshot.

- Consume only the ready `industry-value-chain` task from the existing `ResearchTeamPlan`; do not create a parallel plan or second taxonomy service.
- Preserve company identity, `as_of`, acquisition lineage, upstream `company-intent`, the point-in-time availability boundary, and the typed `IndustryResearchOutcome` output contract.
- Primary authority is issuer/exchange/regulator/official ministry and statistics first, then established industry associations and independently sourced peer filings. Community rankings and broker labels are discovery only.

## Procedure

1. Resolve the active internal research archetype with `uv run astock industry-research-resolve QUERY`; treat it as a research method, not a certified official industry label. Then load the public-core methodology bundle with `uv run astock industry-research-methodology QUERY`. The bundle supplies source-audited questions, KPIs, valuation lenses and red flags from professional/regulatory/industry-standard methodology sources; it is methodology only and cannot certify any current company fact. If the sector-specific bundle cannot be resolved, continue with its `ARCHETYPE_FALLBACK` + `GENERIC_COMPETITIVE_CORE` for a known-but-not-yet-mapped archetype, or `GENERIC_FALLBACK` for a fully unclassified query and record the degradation internally—do **not** return `NEEDS_INFO` solely because a methodology pack or private/blogger Skill is absent. Record official taxonomy separately when available.
2. Map the value chain: upstream inputs, production/service bottlenecks, channels, customers, substitutes, regulatory constraints and profit-pool distribution. Apply the resolved public-core methodology as a question/metric checklist, but source every company/industry conclusion from current admissible Evidence. Connect every material link to an observable driver or mark it unresolved.
3. Select peers before observing the desired valuation conclusion. Use business mix, geography, customer/end-market, scale, capital intensity and accounting scope; show exclusions and retain a valid “no comparable peer” outcome.
4. Normalize period, currency, consolidation scope and one-off items. Produce cycle state, unit-economics drivers, base rates, competitive advantages/disadvantages and falsifiers.

## Required output contract

- Set `INDUSTRY_PROFILE=true` only when value-chain evidence, profit-pool position, normalized peer methodology and falsifiers are frozen.

## Output

Return the registered typed `IndustryResearchOutcome` artifact ID, the value chain and profit-pool position, a defensible peer set or an explicit no-peer result, normalized comparison, cycle/base-rate context and falsifiers.

## Gates and abstention

Recover missing segmentation and period/scope evidence before abstaining from the affected formal conclusion. A missing internal taxonomy or optional methodology is not itself a stop condition. If peers are structurally inapplicable, record the evidence-bound `peer_set_exception_reason` using the existing `STRUCTURAL_NOT_APPLICABLE:` contract; an ordinary data gap does not qualify. Do not force peers or a peer multiple to produce a target value. Unresolved material evidence keeps `INDUSTRY_PROFILE=false`; independent research continues.

## Verification

- Confirm peer rules were fixed before valuation, exclusions are retained, and downstream tasks reuse the frozen profile rather than rebuilding a preferred peer set.
- This Skill produces industry research and does not write the paper ledger.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)

## Prohibitions

- Do not certify an internal archetype as an official industry classification.
- Do not choose peers after seeing which set creates the preferred valuation.
- Do not treat sector momentum, popularity or a broker label as company evidence.
- Do not create recommendation, portfolio, or execution authority.
