---
name: macro-policy-regime
description: Build point-in-time macro and policy regime profiles and trace their transmission into an A-share industry or company. Use for Research Team MACRO/POLICY tasks, cycle positioning, liquidity conditions, or policy-dependent theses.
---

# 宏观与政策传导

## Inputs and authority

- Consume only a ready `macro-regime` or `policy-regime` task from the existing `ResearchTeamPlan`; do not create a parallel plan, router, state store, or evidence model.
- Preserve the plan's company/full-market scope, `as_of`, acquisition report, upstream task lineage, and point-in-time availability boundary.
- Primary authority is official first: PBOC, NBS, MOF, NDRC, State Council, CSRC, official ministries/regulators and exchanges. Secondary research may explain mechanisms but cannot replace a material official policy or statistic.

## Procedure

1. Inspect `uv run astock research-team-status <plan_id>` and confirm the exact ready task, dependencies, output type and required readiness check.
2. Freeze every release vintage and distinguish announcement date, effective date, observation period, revision vintage and availability time. Historical/prospective work rejects releases unavailable at `as_of`.
3. Build an explicit transmission chain: policy or macro shock → rates/credit/liquidity/demand/input cost/FX/regulation → industry economics → company driver. Mark every link as observed, supported inference, or unresolved.
4. Produce a base regime plus upside/downside alternatives, leading indicators, lag assumptions, invalidation conditions and the next authoritative release that can change the state.
5. Reconcile material conflicts with another authoritative series or document. Do not jump from a headline to a stock direction.

## Required output contract

- Register a typed `MacroRegimeProfile` and/or `PolicyRegimeProfile` through `uv run astock research-team-role-output REQUEST.json`.
- Complete the matching task through `uv run astock research-team-task-result REQUEST.json` with independently identified artifact lineage.
- Set `MACRO_REGIME=true` or `POLICY_REGIME=true` only when authoritative vintage, regime facts, company/industry transmission, scenarios and falsifiers are frozen.

## Output

Return the registered typed regime artifact IDs, the frozen current/base/upside/downside state, the 2–4 transmission links that matter, next release checkpoints, unresolved conflicts and falsifiers. Keep broad commentary out unless it changes the named industry/company decision.

## Gates and abstention

Abstain and leave the formal check false when release vintage is unclear, policy text is unavailable, material data conflict is unresolved, the transmission path is only narrative, or the requested horizon lies outside the evidence. An unresolved public-source gap returns to the continuation/evidence workflow; it is not an investor conclusion.

## Verification

- Re-run `uv run astock research-team-status <plan_id>` and verify the task is COMPLETE, the expected typed artifact is registered, and the corresponding readiness check is true only when supported.
- Verify no later vintage entered a historical decision and no macro/policy fact was inferred from search snippets.
- Preserve `broker_execution_allowed=false`; this Skill never creates recommendation, portfolio, or order authority.

## Workflows

- [`docs/workflows/workflow-macro-policy-regime.md`](../../../docs/workflows/workflow-macro-policy-regime.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)

## Prohibitions

- Do not use future releases, revised vintages unavailable at `as_of`, or later policy outcomes in historical work.
- Do not let community commentary satisfy `MACRO_REGIME` or `POLICY_REGIME`.
- Do not infer a company BUY/SELL result directly from macro direction.
- Do not create a real order or change portfolio weights.
