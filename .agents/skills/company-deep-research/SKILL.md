---
name: company-deep-research
description: Build a cited current or historical deep A-share company research case and final committee decision. Use for deep analysis of a named company, business quality, valuation, industry position, risks, whether the stock is worth a paper position, what evidence would change the view, or how to interpret entry and exit rules.
---

# 公司深度研究

1. Resolve the company to one six-digit A-share identity. For a **current** opinion, do not begin with `probe` or a question-time cutoff. Run `uv run astock research-acquire-current <company_id> --market <market>` first. For historical/recorded research only, use an explicit `research-plan <company_id> --as-of <timestamp>`.
2. The current acquisition layer must exhaust deterministic local/provider fallback before escalating. Treat unresolved capabilities as automatic work for up to the current 1800-second recovery budget: diagnose the fault, retry only transient failures, switch provider or a more reliable endpoint, then automatically use authoritative Web search (exchange/CNINFO/issuer IR/regulator first, then other high-quality public sources). Prefer sources with no account/API-key requirement and cross-check material facts where practical. Do not ask the user to do a lookup the Agent can do itself.
3. If authoritative Web research still cannot close every decision-relevant gap, collect all truly unavoidable manual requirements and ask the user **once**. A provider failure is not itself an investment conclusion. Provider names, exceptions, retry paths, reason codes, artifact identities and command logs are developer diagnostics only and must not enter the investor response.
4. Reuse audited Evidence/Financial/BaseCase/Specialist/Memo artifacts when their company/source lineage remains applicable. For current research, the acquisition-ending decision timestamp replaces the user's question timestamp as the common current snapshot. Historical and prospective paths still enforce exact source availability and no-future-data rules.
5. Run `$financial-integrity-audit` before treating cheap valuation as opportunity. Secondary structured providers are locating/cross-check hints; material financial facts must ultimately return to official reports or other authoritative evidence. A working backup source may continue the research even if the preferred provider is down.
6. For a new current company opinion, build the institutional fundamental layer where formal certification is needed: `EvidenceSufficiencyReport → IndustryProfile → CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`. Forecast/valuation is deterministic Python over evidence-bound assumptions; preserve explicit Bull/Base/Bear scenario analysis and sensitivity to the assumptions that matter most. Current market data acquired during the run may be frozen as the market anchor after acquisition; do not reject it merely because it arrived minutes after the user's question.
7. Build BaseCase once. Route only bounded incremental specialists and use published knowledge Skills through `KnowledgeSkillProvider`. Do not let Agents reread the same evidence independently. Community/OCR material remains methodology/lead evidence, not sole support for material facts.
8. Separate fundamental-research readiness from execution readiness. Missing current corporate-action or TradingClassification details can block a paper order, but do not by themselves block a months/years company-quality/valuation conclusion. Only require `ClassifiedTradeProtocol` when the user asks for executable entry/exit/order eligibility.
9. The committee receives only frozen artifacts. Use `committee-schema`, `committee-plan`, and the relevant audits. The committee cannot browse. `REJECT`, `NEEDS_INFO`, `WATCH`, `APPROVE_SIMULATION` retain their formal meanings; a provisional evidence-backed research stance is not automatically one of these committee states.
10. When the user asks for exact entry/exit/stop rules, run `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`. Do not invent an exact price when the frozen protocol does not contain one.
11. Require `research-chain-audit` and final runtime/committee audits for a formal certified chain. For durable internal provenance use `uv run astock codex-run-init <request> --artifact-id <artifact_id> --require-registered-output`, `codex-run-import`, and `codex-run-audit`; these identities stay out of the normal investor answer.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-committee-trade-plan.md`](../../../docs/workflows/workflow-committee-trade-plan.md)

## Output

Write only for the investor. Start with a concise judgment and confidence, then explain business quality, valuation/risk-reward, earnings/cash-flow drivers, catalysts, main downside cases, and what would change the view. Translate unresolved evidence into investment language such as “半年报中的利润率还需要和公司正式披露核对”. Do **not** display provider names, internal Agent/committee stages, protocol/schema/class names, machine states, reason codes, artifact/hash, database terms, stack traces, or CLI transcripts. Before sending, apply the same rules as `research-investor-answer-audit`; rewrite any answer that fails.

If automatic provider and authoritative Web fallback still leave uncertainty, give the strongest supportable research interpretation and explain only how that uncertainty affects valuation/odds. Do not tell a normal investor that a backend chain is incomplete. Do not invent BUY/SELL/price targets to hide uncertainty, and only ask for user material after all automatic channels are exhausted.

## Prohibitions

- Do not stop current research after the first provider fails if another provider or authoritative Web source can answer the same bounded question.
- Do not use the user's question timestamp as a current-live cutoff.
- Do not disable PIT/source-availability safeguards for historical replay or formal prospective evaluation.
- Do not use community content as sole support for a key company fact.
- Do not let a narrative replace a registered DecisionPack or ClassifiedTradeProtocol where formal execution is requested.
- Do not infer exact entry/sell prices from free-text rules.
- Do not write the paper ledger or send a real brokerage order from research output.
