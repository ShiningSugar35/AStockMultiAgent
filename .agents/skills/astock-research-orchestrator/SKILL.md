---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research requests across local data, evidence, company research, holding monitoring, and paper recovery. Use for requests such as what to research, whether to inspect a company, checking all holdings, updating the paper account, or any task that spans more than one project skill.
---

# A股研究总控

1. Run `uv run astock probe` and treat unavailable capabilities as unavailable.
2. Inspect existing work with `uv run astock research-chain-status <company_id>`; use `research-chain-audit` before reusing a frozen chain.
3. Run `uv run astock context-plan --skill <skill> --artifact-id <artifact_id>` with only the required registered artifacts, then route to the narrowest project Skill.
4. Prefer local/API facts, then MCP, then browser, then a manual investigation task. Run deterministic synchronization and quality commands before reasoning from market data.
5. Build missing Phase 4 nodes only through their validated research/position CLI; do not duplicate a node that already audits PASS.
6. Before a decision, require the relevant financial-integrity pack and run `uv run astock committee-input-resolve --artifact-id <id>...`; create a strict request from `committee-schema`, preview it with `committee-plan <request.json>`, then use `committee-decide <request.json>` and `committee-audit <decision_id>`.
7. If the verdict is `NEEDS_INFO`, route each `committee-task-status <task_id>` to `$evidence-investigation`; after a genuinely new frozen artifact exists, link it with `committee-task-resolve <task_id> <artifact_id>` and create a new committee request/bundle. The committee never performs the search itself.
8. For a prospective frozen-weight comparison, inspect `shadow-schema`, create the study before its effective time, derive each episode key with `shadow-independence-key`, freeze every arm with `shadow-assign`, and persist the signal-time regime with `market-regime-classify`. Record only reconciled, point-in-time observations through `shadow-observation-record`; then run `shadow-evaluate <study_id> --as-of <timestamp>`, `shadow-status --study-id <study_id>`, and `shadow-audit <study_id>`.
9. For any request about dynamic weights or entering Phase 8, first run `uv run astock adaptive-research-status [--study-id <study_id>]`. Its underlying audited admission must be `ELIGIBLE_RULE_STATE_MACHINE_RESEARCH`; a capability result other than `AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL` is a hard stop. Even that status only permits asking for explicit approval of a specific study/report/admission version before rule-state-machine shadow research; it never authorizes an algorithm, online weight changes, a brokerage order, or a write to the main paper ledger.
10. For a durable Codex explanation, run `uv run astock codex-run-init <request> --artifact-id <DecisionPack_or_TradeProtocol_or_ShadowEvaluationReport_or_Phase8AdmissionReport_artifact_id> --require-registered-output`, import that exact registered object with `codex-run-import`, and finish with `codex-run-audit`.

## Output

Produce a frozen-input `DecisionPack` and one TradeProtocol for every committee decision. For a declared shadow study, also produce an immutable `ShadowEvaluationReport` and `Phase8AdmissionReport`. `REJECT`, `NEEDS_INFO`, and `WATCH` protocols must remain `BLOCKED`; no verdict, protocol, shadow report, or admission report implies an executed trade.

## Prohibitions

- Do not invent a buy recommendation when the research pipeline is incomplete.
- Do not write SQLite or canonical Parquet directly.
- Do not ask the committee to search or fetch new evidence.
- Do not backfill a formal shadow assignment after its outcome is visible.
- Do not change weights or the paper ledger from a shadow evaluation.
- Do not implement or start Phase 8 research from admission alone; require the explicit rule-research approval reported by `adaptive-research-status`.
- Do not create or send a real brokerage order.
