---
name: company-deep-research
description: Build a cited current or historical deep A-share company research case and final committee decision. Use for deep analysis of a named company, business quality, valuation, industry position, risks, whether the stock is worth a paper position, what evidence would change the view, or how to interpret entry and exit rules.
---

# 公司深度研究

1. Resolve the company to one six-digit A-share identity. For a **current** opinion, run `uv run astock research-acquire-current <company_id> --market <market>` first; do not begin with `probe` or the user's question-time cutoff. Historical/recorded research uses an explicit `research-plan <company_id> --as-of <timestamp>`.
2. Exhaust policy-driven automatic recovery before asking the user for data. Use the active policy budget from `current-research-policy`; retry transient failures, route a `ProviderRecoveryProposal` only through deterministic validation, and treat unknown drift through raw-first `SchemaRepair` rather than guessed facts. Then use authoritative Web research. Aggregate any unavoidable manual request and ask once; provider/debug/artifact details remain internal and never belong in the investor answer.
3. Reuse audited evidence, financial, BaseCase, specialist and memo artifacts while their company/source lineage remains applicable. Current research freezes one decision snapshot after acquisition; historical/prospective work keeps strict source-availability/PIT boundaries.
4. Run `$financial-integrity-audit` before treating a low multiple as an opportunity. Secondary structured feeds may locate facts, but material financial claims return to official reports or equivalently authoritative sources.
5. For a formal current opinion, build the institutional fundamental path: evidence sufficiency → industry/company economics → driver tree → Bull/Base/Bear forecast → valuation/sensitivity → decision context. Keep explicit Bull/Base/Bear scenario analysis; forecast and valuation are deterministic calculations over evidence-bound assumptions.
6. Build BaseCase once. Route only bounded incremental specialists and audited Knowledge Skills. Community/OCR material remains methodology or lead evidence, never sole support for a material company fact.
7. Separate research readiness from execution readiness. Missing current trading-rule/company-action detail may block a simulated order without blocking a months/years conclusion about company quality and valuation.
8. The committee receives frozen artifacts only. Use `committee-schema`, `committee-plan`, relevant audits and the formal outcome states. The committee cannot browse.
9. When exact entry/exit/stop mechanics are needed, use `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`. Never invent an exact price. If the formal result permits simulation, the current entry condition is satisfied, and local `auto_ai_paper_order_on_approved_entry=true`, proceed into the existing paper account/order-confirmation flow; do **not** call it a position until replay records a fill.
10. A direct user instruction to buy/sell overrides the model's opinion, not execution mechanics. Do not veto the user's simulated order because research is bearish; route it to the paper-order path immediately while preserving cash/position availability, lot size, verified tradability, price-band, account-confirmation and fill checks.
11. Require `research-chain-audit` and final runtime/committee audits for a formal certified chain. For durable internal provenance use `uv run astock codex-run-init <request> --artifact-id <artifact_id> --require-registered-output`, `codex-run-import`, and `codex-run-audit`; these identities stay internal.

## Workflows

- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-committee-trade-plan.md`](../../../docs/workflows/workflow-committee-trade-plan.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)
- [`docs/workflows/workflow-adaptive-edge.md`](../../../docs/workflows/workflow-adaptive-edge.md)

## Output

Keep the investor answer compact: **结论（1–2句）→ 2–4个决定性理由 → 最大风险/改变判断的条件**. Add valuation or catalyst detail only when it changes the decision. Avoid saying the same thing again under “总结”. If a technical term, empirical paper result or formula is necessary, explain it once in a short parenthesis, e.g. “CVaR（最差那部分行情里的平均亏损）”. Translate unresolved evidence into investment language rather than backend terminology. Before sending, apply the same rules as `research-investor-answer-audit`.

## Prohibitions

- Do not stop current research after the first provider fails if another automatic or authoritative source can answer the same bounded question.
- Do not use community content as sole support for a key company fact.
- Do not invent BUY/SELL prices to hide uncertainty.
- Do not treat a submitted but unfilled paper order as a holding.
- Do not bypass paper-account mechanics merely because the user overrode the research opinion.
- Do not submit a real brokerage order.
