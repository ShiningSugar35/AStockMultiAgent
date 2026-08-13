---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research requests across candidate discovery, company research, trade-plan explanation, portfolio analysis, holding monitoring, evidence, and paper recovery. Use for natural-language questions about a company, whether a stock is worth buying, entry or exit planning, portfolio review, stock recommendations, or any task spanning more than one project skill.
---

# A股研究总控

1. Interpret the user's investment question first; never ask them for internal artifact IDs, failure codes, provider names, or database details. Route a named current-company question or “现在能不能买” to `$company-deep-research`; portfolio/risk/allocation to `$portfolio-manager`; broad discovery to `$candidate-scan`; an existing position to `$holding-monitor`; accounting credibility to `$financial-integrity-audit`; unresolved evidence to `$evidence-investigation`; paper recovery to `$paper-trading-recovery`.
2. Do **not** run `uv run astock probe` before every investment question. `probe` is a lightweight diagnostic command, not a research prerequisite. Use it only for capability/debug checks. Run `uv run astock state-integrity-audit` only when a full SQLite audit is explicitly needed; it may be slow.
3. For broad market discovery rather than a named company, retain the existing deterministic funnel: start with `uv run astock research-seeds --live`, then use `uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live` before Candidate Scan. Expert-domain methods such as `JuglarCycleStageSkill` remain research-prioritization/context tools only; they do not create BUY authority.
4. For a new **current** named-stock opinion, first run `uv run astock research-acquire-current <company_id> --market <market>`. Its lookback, core/optional capabilities, stages, worker budget and automatic-resolution budget come from the active `current-research-policy`, not from this Skill. For a bounded non-default research question, the Agent may submit a `ResearchPlannerProposal` through `adaptive-plan-validate` and pass the resulting frozen plan into current acquisition; deterministic validation always restores core capabilities and mandatory research gates. The user's question timestamp is not the cutoff; the decision snapshot freezes after acquisition.
5. If current acquisition still has unresolved capabilities, treat them as **automatic research work**, not a user-facing stop. Use the active policy budget and policy-driven source routing/provider health first; retry only transient faults. When adaptive recovery is useful, generate a `ProviderRecoveryProposal` but execute only paths admitted by `adaptive-recovery-validate`. Unknown schema drift remains raw-first and may enter `SchemaRepairProposal`, never direct formal facts. Continue authoritative Web research for bounded needs where provider paths are insufficient.
6. Only after allowlisted automated/provider paths **and** authoritative Web search are exhausted may the user be asked for help. Manual remains last regardless of Agent proposal. Aggregate every remaining manual requirement into one concise request; never interrupt repeatedly for one missing item at a time. Use `$evidence-investigation` internally for bounded unresolved claims. Provider names, retry paths, proposal IDs, error codes and unresolved machine states stay in diagnostics and never become the investor answer.
7. After current acquisition and any web fallback, invoke `uv run astock research-plan <company_id> --mode LIVE` or `uv run astock research-run-company <company_id> --mode LIVE --institutional-research-required` without a question-time `--as-of`; the command freezes its current invocation time. Historical/recorded research, backtests, and Phase 7 prospective studies still require explicit point-in-time cutoffs and source-availability checks to prevent future leakage.
8. For a new current investment opinion, require the institutional fundamental path where formal certification is requested: `EvidenceSufficiencyReport → IndustryProfile / CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`. Build the BaseCase once, use the published `KnowledgeSkillProvider`, and keep specialist work incremental. Canonical Forecast/Valuation owns numeric forecasts; Serenity remains evidence-bound context, not a parallel valuation book.
9. Separate **research sufficiency** from **execution readiness**. Missing corporate-action/trading-classification details must block paper execution when relevant, but must not by themselves prevent a months/years fundamental investment analysis. Only surface execution-readiness gaps when the user asks for an executable trade plan, simulation, entry/exit mechanics, or order eligibility.
10. Before a formal decision, resolve frozen inputs with `committee-input-resolve`, validate with `committee-schema` / `committee-plan`, and require relevant audits. `FundamentalModelBundle` and `InstitutionalDecisionContext` are shared PRIMARY inputs, not extra voting Agents. `REJECT`, `WATCH`, and `NEEDS_INFO` remain hard formal states. The committee never performs the search itself.
11. When the user asks for precise entry/exit/stop rules, use `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`. Never invent an exact price when the formal view says an exact range is unavailable. A current investor-facing research judgement may discuss valuation/risk/reward in plain language before execution classification, but it must not masquerade as a certified order instruction.
12. For formal prospective comparison, keep the existing Phase 7 frozen study. Run `uv run astock adaptive-research-status [--study-id <study_id>]`; `ELIGIBLE_RULE_STATE_MACHINE_RESEARCH`, `AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL`, and explicit rule-research approval remain mandatory before any Phase 8 adaptive research. Do not change weights or the paper ledger from shadow results.
13. For a durable registered explanation, use `uv run astock codex-run-init <request> --artifact-id <final_artifact_id> --require-registered-output`, `codex-run-import`, and `codex-run-audit`. This audit trail is internal; do not dump it into the investor answer unless the user asks for diagnostics.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-candidate-discovery.md`](../../../docs/workflows/workflow-candidate-discovery.md)
- [`docs/workflows/workflow-portfolio-construction.md`](../../../docs/workflows/workflow-portfolio-construction.md)
- [`docs/workflows/workflow-prospective-evaluation.md`](../../../docs/workflows/workflow-prospective-evaluation.md)
- [`docs/workflows/workflow-adaptive-edge.md`](../../../docs/workflows/workflow-adaptive-edge.md)

## Output

Lead with a real investor answer, never a backend status report. Use: **结论 / 为什么 / 估值与赔率 / 关键催化 / 最大风险 / 什么情况会改变结论**. Explain evidence maturity only in ordinary investment language (for example “最新年报已核实”“半年报仍需和公司正式披露核对”). Do not mention provider names, internal Agents, committee/runtime stages, protocol/schema/class names, reason codes, artifact/hash, SQL/SQLite, CLI commands, migrations, or how the system works. Before sending a normal investment answer, apply the same rules as `research-investor-answer-audit`; if it fails, rewrite the answer rather than exposing the audit details.

If some information is still unresolved after all automatic acquisition and authoritative Web fallback, give the strongest supportable investment interpretation and describe only the **investment-relevant uncertainty**. Ask the user for material only when automatic channels are exhausted, and then ask once. Do not surface internal formal states unless the user explicitly switches to developer/debug mode or specifically asks about simulation eligibility.

## Prohibitions

- Do not stop at the first provider failure when an automatic fallback or bounded authoritative Web search is available.
- Do not expose internal error codes/artifact identities in a normal investor-facing answer.
- Do not use the user's question timestamp as the cutoff for a current live investment consultation.
- Do not remove source availability/PIT controls from historical replay, formal prospective evaluation, or backtests.
- Do not turn `RESEARCH_READY` candidates directly into BUY recommendations.
- Do not ask the committee to fetch, browse, or create new evidence.
- Do not backfill a formal shadow assignment after its outcome is visible.
- Do not change weights or the paper ledger from a shadow evaluation or Phase 8 admission alone.
- Do not create or send a real brokerage order. Real trades remain user-executed at the broker.
