---
name: company-deep-research
description: Build a cited point-in-time deep A-share company research case and final committee decision. Use for deep analysis of a named company, business quality, valuation, industry position, risks, whether the stock is worth a paper position, what evidence would change the view, or how to interpret entry and exit rules.
---

# 公司深度研究

1. Resolve the company to one six-digit A-share identity, then run `uv run astock probe` and `uv run astock research-plan <company_id> --as-of <timestamp>`. Use the plan as the gap list; do not start by rereading all documents.
2. Reuse audited PIT-safe Evidence/Financial/BaseCase/Specialist/Memo artifacts when their `company_id`, `as_of`, source hashes, and parent lineage match. Fetch only incremental official disclosures, reference data, and missing evidence through the fixed source priority.
3. If the evidence chain is missing, build it through the validated research request/task/run/pack/freeze commands. Run `$financial-integrity-audit` before interpreting “cheap valuation” as opportunity; unresolved official financial facts remain `NEEDS_INFO`.
4. For a new current company opinion, build the institutional fundamental layer before the committee. Use `institutional-research-schema`, then freeze `EvidenceSufficiencyReport → IndustryProfile → CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle`. Forecast and valuation numbers are recomputed deterministically in Python from evidence-bound assumptions. If expected return or reverse-DCF is needed, supply a PIT `MarketPriceAnchor` that binds the price to one registered artifact/hash; a naked price must not be used. Run `fundamental-model-audit`, then freeze `InstitutionalDecisionContext` with the decision horizon, thesis, variant perception, 3–5 key drivers and competing hypotheses.
5. Build BaseCase once. Route no more than three specialists and use published knowledge Skills only through `KnowledgeSkillProvider`; specialists return Delta rather than rewriting the full company case. The active Serenity registry is `research-skills-v3`: use typed Industry Bottleneck/Event-to-Alpha/Growth Probability/Growth Valuation/Daily Trend/Juglar Cycle contracts only when their required frozen evidence is present. `JuglarCycleStageSkill` must separate industry fixed-asset cycle, company operating stage, and stock pricing stage, preserve eight evidence-bound dimensions, five-stage probabilities, counter-evidence and migration signals, and remain report-only. Keep community/OCR material as methods or leads, never as sole support for key company facts.
6. Compose one `ResearchMemoArtifact` with the union of cited evidence, open gaps, the institutional model/context, BaseCase, specialist deltas, counterarguments, valuation/trend evidence, and thesis invalidation conditions. Resolve material conflicts before asking the committee to decide.
7. The committee receives only frozen artifacts. Build the assessment/request from `committee-schema`, run `committee-plan`, then run the generic Research Runtime with `institutional_research_required=true` and the exact `FundamentalModelBundle` / `InstitutionalDecisionContext` artifacts. Require the DecisionPack/committee audit, PIT `TradingClassification`, and classification audit to pass before accepting a final `ClassifiedTradeProtocol`. Historical recorded Phase-6 runs remain readable as legacy compatibility, but they are not the default for a new company opinion.
8. Interpret final outcomes strictly: `REJECT` means the current frozen case does not qualify; `NEEDS_INFO` means decision-relevant evidence is missing; `WATCH` means monitor without a new simulated position; `APPROVE_SIMULATION` means eligible for paper-only planning, not a real brokerage order.
9. If the user asks “什么位置进/卖、止盈止损如何设”, run `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`. Explain the frozen protocol rules and committee scenario ranges. Do not label scenario-price arithmetic as a target-price forecast, and do not invent exact entry/exit prices when the view says exact ranges are unavailable.
10. Require `research-chain-audit` and the final runtime/committee audits to pass. For a durable registered explanation, run `uv run astock codex-run-init <request> --artifact-id <DecisionPack_or_ClassifiedTradeProtocol_artifact_id> --require-registered-output`, import the exact registered artifact with `codex-run-import`, and require `codex-run-audit` PASS.
11. Open the final `ResearchRunReport`, `TradePlanView`, and only the precise evidence locators needed to explain material claims. This is the default low-token path; raw document reopening is exception-only.

## Output

Start with a concise plain-language verdict: what the company does, why the investment case may work, the strongest risk, current evidence quality, and current state (`REJECT / NEEDS_INFO / WATCH / APPROVE_SIMULATION`). Then provide the professional layer: official facts and citations, financial-integrity findings, BaseCase, specialist/knowledge deltas, valuation and return/downside scenarios, counter-case, invalidation conditions, committee confidence and max paper position, trade-plan rules, and open information gaps. Always distinguish scenario prices from guaranteed targets.

## Prohibitions

- Do not use community content as the sole support for a key company fact.
- Do not bypass point-in-time, corporate-action, suspension, data-quality, or financial-integrity gates.
- Do not let a narrative replace the registered DecisionPack or ClassifiedTradeProtocol.
- Do not infer an exact entry or sell price from free-text protocol rules.
- Do not write the paper ledger or send a real brokerage order from research output.
