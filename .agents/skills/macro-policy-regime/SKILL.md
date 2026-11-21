---
name: macro-policy-regime
description: Build point-in-time macro and policy regime profiles and trace their transmission into an A-share industry or company. Use for Research Team MACRO/POLICY tasks, cycle positioning, liquidity conditions, or policy-dependent theses.
---

# 宏观与政策传导

## Inputs and authority

Use the [shared team execution contract](../../../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) for task dispatch, registration and recovery. Load this role's method only; reuse the coordinator's ready-task snapshot.

- Consume only a ready `macro-regime` or `policy-regime` task from the existing `ResearchTeamPlan`; do not create a parallel plan, router, state store, or evidence model.
- Preserve the plan's company/full-market scope, `as_of`, acquisition report, upstream task lineage, and point-in-time availability boundary.
- Primary authority is official first: PBOC, NBS, MOF, NDRC, State Council, CSRC, official ministries/regulators and exchanges. Secondary research may explain mechanisms but cannot replace a material official policy or statistic.

## Procedure

1. Freeze every release vintage and distinguish announcement date, effective date, observation period, revision vintage and availability time. Historical/prospective work rejects releases unavailable at `as_of`.
2. Build an explicit transmission chain: policy or macro shock → rates/credit/liquidity/demand/input cost/FX/regulation → industry economics → company driver. Mark every link as observed, supported inference, or unresolved.
3. Produce a base regime plus upside/downside alternatives, leading indicators, lag assumptions, invalidation conditions and the next authoritative release that can change the state.
4. Reconcile material conflicts with another authoritative series or document. Do not jump from a headline to a stock direction.

## Required output contract

- Produce `MacroResearchOutcome` for `macro-regime` and `PolicyRegimeProfile` for `policy-regime`; preserve the admitted task's scope and point-in-time evidence.
- Set `MACRO_REGIME=true` or `POLICY_REGIME=true` only when authoritative vintage, regime facts, company/industry transmission, scenarios and falsifiers are frozen.

## Output

Return the registered typed regime artifact IDs, the frozen current/base/upside/downside state, the 2–4 transmission links that matter, next release checkpoints, unresolved conflicts and falsifiers. Keep broad commentary out unless it changes the named industry/company decision.

## Gates and abstention

First recover material public gaps through the shared contract while independent work continues. Abstain from the affected formal conclusion and leave its check false when release vintage is unclear, policy text is unavailable, material data conflict is unresolved, the transmission path is only narrative, or the requested horizon lies outside the evidence. An unresolved public-source gap returns to the continuation/evidence workflow; it is not an investor conclusion.

## Verification

- Verify no later vintage entered a historical decision and no macro/policy fact was inferred from search snippets.
- This Skill produces macro/policy research and does not write the paper ledger.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)

## Prohibitions

- Do not use future releases, revised vintages unavailable at `as_of`, or later policy outcomes in historical work.
- Do not let community commentary satisfy `MACRO_REGIME` or `POLICY_REGIME`.
- Do not infer a company BUY/SELL result directly from macro direction.
- Do not create a real order or change portfolio weights.
