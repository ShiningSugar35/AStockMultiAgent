---
name: company-deep-research
description: Build a cited current or historical deep A-share company research case and final committee decision. Use for deep analysis of a named company, business quality, valuation, industry position, risks, whether the stock is worth a paper position, what evidence would change the view, or how to interpret entry and exit rules.
---

# 公司深度研究

1. Resolve the company to one six-digit A-share identity. For a **current** opinion, do not begin with `probe` or a question-time cutoff. Run `uv run astock research-acquire-current <company_id> --market <market>` first. For historical/recorded research only, use an explicit `research-plan <company_id> --as-of <timestamp>`.
2. The current acquisition layer must exhaust deterministic local/provider fallback before escalating. If `external_research_needs` remain, automatically use authoritative Web search: exchange/CNINFO/issuer IR/regulator first, then other high-quality public sources. Prefer sources with no account or API-key requirement. Do not ask the user to perform a web lookup that the Agent can do itself.
3. If authoritative Web research still cannot close all decision-relevant gaps, collect every remaining manual requirement and ask the user **once**. A provider failure is not itself an investment conclusion. Keep raw provider exceptions, reason codes, hashes, and command logs in diagnostics only.
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

Write for the investor. Start with a concise judgment and its confidence, then explain: business quality, valuation/risk-reward, earnings/cash-flow drivers, catalysts, main downside cases, and what would change the view. Translate evidence gaps into natural language such as “2026 中期报告尚未披露/尚未核实”; do **not** display `CLAIM_IDS_REQUIRED`, `EVIDENCE_PACK_REQUIRED`, provider IDs, stack traces, internal artifact IDs, hashes, database tables, or CLI transcripts unless the user explicitly asks for debugging.

If formal certification is still incomplete after automatic provider and Web fallback, say that formal certification is pending but give the strongest supportable research interpretation. Do not invent a BUY/SELL/price target to hide uncertainty.

## Prohibitions

- Do not stop current research after the first provider fails if another provider or authoritative Web source can answer the same bounded question.
- Do not use the user's question timestamp as a current-live cutoff.
- Do not disable PIT/source-availability safeguards for historical replay or formal prospective evaluation.
- Do not use community content as sole support for a key company fact.
- Do not let a narrative replace a registered DecisionPack or ClassifiedTradeProtocol where formal execution is requested.
- Do not infer exact entry/sell prices from free-text rules.
- Do not write the paper ledger or send a real brokerage order from research output.
