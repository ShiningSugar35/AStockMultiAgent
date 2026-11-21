# Research specialist Skills crosswalk

> CURRENT · 2026-11-21

The existing ResearchTeam DAG owns dependencies, input identity and typed output admission.
Each canonical Skill owns one research method. A Skill is not another worker or model call.
The coordinator passes the ready task and frozen inputs; roles load their own method on demand.

| Task | Canonical Skill | Typed output | Readiness evidence |
|---|---|---|---|
| `macro-regime`, `policy-regime` | [macro-policy-regime](../../.agents/skills/macro-policy-regime/SKILL.md) | `MacroResearchOutcome`, `PolicyRegimeProfile` | `MACRO_REGIME`, `POLICY_REGIME`: official vintage, transmission, scenarios |
| `industry-value-chain` | [industry-value-chain](../../.agents/skills/industry-value-chain/SKILL.md) | `IndustryResearchOutcome` | `INDUSTRY_PROFILE`: operating economics, comparable scope or a proven structural exception |
| `company-catalyst` | [catalyst-event-research](../../.agents/skills/catalyst-event-research/SKILL.md) | `NewsEventResearchPack` | `CATALYST_RISK`: primary event lineage, timing, preconditions, interaction and downside |
| `governance-management-quality` | [governance-management-quality](../../.agents/skills/governance-management-quality/SKILL.md) | `GovernanceAssessment` | `GOVERNANCE_QUALITY`: control, audit, related parties, capital allocation |
| `investment-red-team` | [investment-red-team](../../.agents/skills/investment-red-team/SKILL.md) | `ChallengerAssessment` | `INDEPENDENT_REVIEW`: separate Bull/Bear contexts, common evidence, concrete defects |
| `model-risk-validation` | [model-risk-backtest-validation](../../.agents/skills/model-risk-backtest-validation/SKILL.md) | `ModelRiskValidationReport` | `MODEL_RISK_VALIDATION`: reproduction, leakage, testing, costs and limits |

## Shared execution contract

1. The coordinator reads `uv run astock research-team-status <plan_id>` once per changed DAG wave and dispatches dependency-ready tasks. A role may reuse that snapshot; it need not repeat the same status call before and after every local step.
2. Reuse exact admitted input artifacts and preserve plan/task/company/scope/version identity. The active CURRENT request uses the acquisition snapshot; historical/OOS research uses the actual availability boundary. Load only method references needed by this task.
3. Register the method's typed member artifact, then bind it with `uv run astock research-team-role-output REQUEST.json`. Register `ResearchRoleResult` through `uv run astock research-team-task-result REQUEST.json`. These are distinct machine contracts, not duplicate model reviews. The result references the registered role output and its exact evidence union.
4. Python validates member types, immutable objects, dependencies, evidence and context identity at their existing admission boundaries. Reuse valid receipts; do not add a prompt-only gate or extra LLM call to repeat deterministic checks.
5. Bull and Bear use different `independent_context_id` values and do not read each other's drafts. Reviewer uses neither author's context and does not browse. Committee consumes only frozen admitted outputs.
6. On a recoverable gap, request evidence or corrected inputs in the same continuation. Preserve successful nodes, continue unrelated ready work, and invalidate only affected descendants. Unknown or partial facts keep the affected formal readiness check false; abstention is not completion of the user's research request.
7. After applicable public routes or the configured resource budget are exhausted, record the unresolved impact honestly. Only private input or required authorization can require the user. No generic missing-input label forces all research to stop, and no narrative upgrades an unsupported formal conclusion.
8. Inspect status again when coordinating the next wave or when registration changes task state. Only the existing closure/publication service decides whether a formal investor view is ready. Register one task observation at the owning task's end; individual status reads do not create observations.

## Workflow boundary

Company and market research, evidence recovery, Committee and portfolio decisions retain their cross-capability Workflows. A single-role method does not need a duplicate Workflow. The role tables and executable DAG are not a request to preload every linked Skill.

## Method sources

The existing source catalogue remains [authoritative research methods](../scouting/2026-08-29-authoritative-agent-investment-research.md). Official vintages, issuer filings, industry economics, governance records and reproducible model checks remain evidence requirements. External methodology and search results are context, not company facts or source authority.
