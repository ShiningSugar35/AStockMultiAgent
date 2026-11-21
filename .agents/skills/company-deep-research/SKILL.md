---
name: company-deep-research
description: Build cited deep A-share company research and a final committee decision. Use for a named company's business quality, valuation, industry position, risks, investment case, or evidence that changes the view.
---

# 公司深度研究

1. Resolve one security and market. Start or recover the same investor request; direct acquisition remains `uv run astock research-acquire-current <company_id> --market <market>`. CURRENT freezes after acquisition and does not use the user's question-time cutoff. Explicit historical/OOS work requires genuinely available historical inputs.
2. Reuse valid same-request evidence and read the active policy budget from `current-research-policy`. Use validated `ProviderRecoveryProposal`, raw-first `SchemaRepair` and bounded authoritative Web recovery. Manual help is only for private input/authorization after checking existing records; diagnostic and artifact details remain internal.
3. Run `$financial-integrity-audit`, industry, governance and catalyst research where required. Secondary data and community methods may locate evidence but do not certify material financial claims. Read `tushare-schema --endpoint ...` only for a relevant structured supplement; the formal statement/report lineage remains separate.
4. Build institutional fundamentals once: evidence sufficiency → industry/company economics → driver tree → Bull/Base/Bear forecast → valuation/sensitivity → decision context. Python computes numerical outputs from evidence-bound assumptions. Freeze units, periods, source/counter-evidence, epistemic type, ranges and next verification events with important driver assumptions. Require explicit enterprise-to-equity adjustments and diluted share basis; compare sampled forecasts only at the same metric/year/unit. Use operating-driver sensitivity, then keep valuation, timing and execution eligibility distinct.
5. Reuse BaseCase and route bounded incremental specialists/Knowledge. Private methods are edge-only; their density cannot determine blind candidates or excuse missing core industry/evidence coverage. Do not create a second valuation ledger, technical agent or publication gate.
6. Preserve independent Bull/Bear contexts, downstream Reviewer/Red Team and required model-risk work. Material typed fact/assumption/calculation disputes may use research-team-reviewer-prepare once and reconcile its operation/input hash with research-team-reviewer-complete; an unknown outcome stays reserved, not redispatched. Style differences add no calls. Committee uses `committee-schema / committee-plan` and frozen inputs only; it cannot browse. Run `research-chain-audit` and applicable final runtime/committee audits. For registered provenance use `uv run astock codex-run-init REQUEST --require-registered-output`, `codex-run-import` and `codex-run-audit`.
7. Separate fundamental judgment from executable mechanics. Use `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>` for entry/exit details. User simulated-trade instructions override model opinion, while available cash/shares, instrument rules, confirmation and replay remain binding; only a fill changes a position.
8. On completion invoke `$continuous-investment-monitor`, enroll the target as `ANALYZED`, and register only existing evidence-backed typed thresholds. Queueing a task is not an executed review. Render one `ResearchNarrativeBundle` via `ResponseGateway` and `research-investor-answer-audit`; preserve stable machine JSON views.

## Same-request automatic continuation contract

Read the linked workflow and the relevant local schema on demand; do not copy all fields into a prompt.

| State | Required action |
|---|---|
| `AUTO_RESOLUTION_REQUIRED` | Inspect the bounded gap, obtain exact authoritative evidence, submit `CurrentResearchAutomaticResolution` via `research-current-continuation-resolve`, and resume the same request |
| `TEAM_RESEARCH_REQUIRED` | Execute ready tasks in DAG waves through the appropriate professional Skills; register typed role outputs/results before advancing |
| `READY_FOR_INVESTOR_VIEW` | Publish only after complete lineage, final receipt and Publication Gate permit it |
| `OBSERVATION_ONLY_FOR_INVESTOR_VIEW` | After the whole team completes, clearly label the observation; do not imply formal buy/sell permission |
| `PUBLIC_DATA_UNAVAILABLE` | Record exhausted public attempts and the remaining uncertainty; do not manufacture a demand for private material |
| `NEEDS_USER_INPUT` | Ask once for genuinely required private input/authorization; later input resumes the same lineage |

Start/status use `research-current-continuation-start REQUEST.json` and `research-current-continuation-status`; public resolution uses `resolve/resume/advance`, while `research-current-continuation-bind` is for later user material. Intermediate states keep `investment_conclusion_blocked=true`. Do not stop with routine work that can still be executed. Broker execution remains forbidden; no broker permission field is added to research contracts.

For a revised premise first use research-forecast-revision-impact; invalidate affected current descendants explicitly with research-forecast-invalidate, preserving historical artifacts. Evaluate frozen forecasts with research-forecast-outcome only after the matching official release becomes available. Scenario ranges are not calibrated probabilities.

## Workflows

- [Current company and host evidence continuation](../../../docs/workflows/workflow-current-company-research.md)
- [Committee and trade plan](../../../docs/workflows/workflow-committee-trade-plan.md)
- [Paper trading](../../../docs/workflows/workflow-paper-trading.md)
- [Formal reporting](../../../docs/architecture/formal-reporting-v1.md)
- [Institutional driver/valuation contract](../../../docs/architecture/full-market-research-team-v1.md#12-机构研究的经营估值与复盘合同)
- [Structured data and original sources](../../../docs/architecture/source-consumers-v1.md)

## Output

For an investment decision give a complete report: subject and judgment strength; current valuation/odds; economic and earnings drivers; financial/governance quality; industry and catalysts; decisive disagreement; portfolio/risk implications; entry, exit and invalidation conditions; data cutoff and citations. Simple factual queries can be short. Never compress away required evidence or risks.

Publish DOCX from the same frozen narrative and registered inputs; Markdown is the existing fallback, PDF only after available conversion passes validation. Return a safe report reference and substantive findings, without exposing private paths. Writing repair must preserve numbers, units, dates, direction, probabilities and provenance.

## Prohibitions

- Do not stop on the first provider failure, ask the user to research public sources, or endlessly rerun a completed graph.
- Do not turn a snippet, Markdown fetch, Tushare hint or community/OCR method into formal report evidence.
- Do not invent prices, forecasts, weights, completed research, current ETF execution permission or filled holdings.
- Do not let missing optional technical/execution detail invalidate an unrelated supported fundamental conclusion.
