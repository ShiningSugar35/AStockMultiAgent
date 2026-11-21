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

## Execution classification and on-demand inputs

The existing `CapabilityNode` index in `investor_orchestration/capabilities.py` owns required/conditional selection, dependencies, output schema and side-effect class. The following classification explains how to execute those nodes; it does not create a second registry, scheduler or model quota.

| Execution class | Existing capability / task | Inputs loaded on demand | Output and authority |
|---|---|---|---|
| Deterministic execution | REQUEST_TIME, ENTITY_IDENTITY, SESSION_PREFLIGHT, CURRENT_MARKET, MARKET_REGIME, FINANCIAL_INTEGRITY, FORECAST_VALUATION, FULL_RESEARCH_GATE, SUBJECT_REGISTRY, EXTERNAL_ACCOUNT, PAPER | Exact request, registered upstream artifacts, selected versioned policy | Existing typed result and receipt. Validation, arithmetic, identity and ledger operations do not launch an extra LLM. Forecast assumptions still come from separately frozen research inputs. |
| Optional reasoning method | INDUSTRY, COMPANY_RESEARCH, GOVERNANCE, EVENT_RESEARCH, PORTFOLIO, HOLDING_REVIEW, ETF; relevant ResearchTeam role | One ready task, frozen artifact summaries and only the matching Skill references | Existing cited role/profile/decision output. A Skill is a method contract; registration is deterministic. Method metadata never grants external-account or paper-confirm permission. |
| Independent review | RED_TEAM; bull-case, bear-case, investment-red-team, model-risk-validation | Common frozen evidence; separate author/reviewer contexts; reviewer receives admitted outputs after authors finish | Existing independent case / ChallengerAssessment / ModelRiskValidationReport. Independence and required evidence cannot be folded into the author. |
| Cross-capability orchestration | FULL_MARKET, COMMITTEE, RESPONSE_GATEWAY; company and monitoring Workflows | One current DAG/status snapshot and admitted outputs of required predecessors | Existing plan, classified protocol and narrative projection. Orchestration alone is not an extra model call; domain judgment and deterministic publication checks remain distinct. |

The table describes responsibility, not a claim that an entire multi-stage capability is pure Python. For example, Python recomputes a valuation, while its operating hypotheses remain explicit research assumptions. The executable node's actual required inputs and side-effect permission remain authoritative.

No node-folding candidate is enabled in this completion. The saved evaluation interface presently has no matched real model trajectories establishing equal evidence, equal budget, risk discovery, citation preservation, token cost and memory. Bull/Bear/reviewer are independently required, and pure validation already runs in Python. Without a measured repeated-evidence candidate, adding a folding switch would add an unqualified execution path. Reopen only from registered `research-evaluation-report` pairs; missing samples and missing host measurements remain UNKNOWN, not savings.

## Native-host observation boundary

On 2026-11-21 the permitted read-only Windows inspection observed Antigravity processes. The connected desktop bridge returned zero visible windows, and the attempted CLI entry path was unavailable through the supported bridge. This establishes application presence only. Effective cwd, the native instruction chain, triggered Skills, loaded bytes, subscription Web Search/FETCH and interruption hooks were not observable in this session. Repository navigation and versioned host capability contracts are implemented, but these native-host facts must be recorded by a real invocation before enabling a native integration claim. No shell Stop hook or fabricated token reduction is introduced.
