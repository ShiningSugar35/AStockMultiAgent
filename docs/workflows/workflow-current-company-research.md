# Workflow — Current Company Research

## When to use

Use for a named A-share current-investment question such as “现在买中国海油合适吗”“这家公司现在估值贵不贵”“未来半年到一年赔率如何”。

Primary skills: `$astock-research-orchestrator` → `$company-deep-research`, with `$evidence-investigation` and `$financial-integrity-audit` as bounded subflows.

## Flow

1. **Resolve identity**
   - Resolve one six-digit A-share code and explicit market.
   - Do not ask the user for internal artifact IDs.

2. **Current acquisition first**
   - Run `uv run astock research-acquire-current <company_id> --market <market>`.
   - This is the current-investment acquisition layer, not a historical replay.
   - The user's question timestamp is not the research cutoff; the current decision snapshot is frozen after acquisition.

3. **Parallel acquisition where safe**
   - After/alongside bounded identity resolution, acquire current daily market data, corporate-action evidence, latest annual financial hints and latest interim/quarterly financial hints.
   - A preferred provider failure must not stop independent downstream collection.
   - Retry only retryable transient network failures; use provider fallback rather than indefinite waiting.

4. **Escalate unresolved capabilities automatically**
   - If the acquisition report contains `external_research_needs`, route each bounded question to `$evidence-investigation`.
   - Search exchange / CNINFO / issuer IR / regulator first; then high-quality professional public sources for corroboration.
   - Prefer sources requiring no API registration or user interaction.
   - Only after local/API and authoritative Web fallback are exhausted may the user be asked for help, and all remaining actions must be consolidated into one checklist.

5. **Plan and formalize the research chain**
   - For current live research use `uv run astock research-plan <company_id> --mode LIVE` or `uv run astock research-run-company <company_id> --mode LIVE --institutional-research-required` without a question-time `--as-of`.
   - Reuse valid frozen evidence, FinancialIntegrity, BaseCase, specialist, memo and institutional artifacts when applicable.
   - Build missing evidence, then the institutional chain when formal certification is required:
     `EvidenceSufficiencyReport → IndustryProfile / CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`.

6. **Build one common research case**
   - Build BaseCase once.
   - Route bounded specialist/Knowledge deltas only where applicable; do not let multiple Agents reread the same raw corpus.
   - Preserve Bull/Base/Bear scenario analysis, key-driver sensitivity, counter-case and falsifiers.

7. **Committee only after frozen research inputs**
   - Committee is offline and cannot fetch evidence.
   - Use `committee-input-resolve`, `committee-plan`, decision/audit paths only after decision-relevant research gaps are closed enough for a formal verdict.

8. **Separate research from execution**
   - Missing TradingClassification/corporate-action execution detail may block a simulated executable protocol but does not by itself block a months/years fundamental assessment.
   - If the user asks for exact entry/exit/stop mechanics, continue through [Committee & Trade Plan](workflow-committee-trade-plan.md).

9. **Render the investor answer**
   - Prefer `research-acquisition-investor-view` / `research-investor-view` for state translation.
   - Lead with: **结论 / 为什么 / 估值与赔率 / 催化 / 最大风险 / 什么情况会改变结论**.
   - Keep reason codes, artifact IDs/hashes, provider stack traces, SQL and CLI logs in diagnostics unless the user explicitly asks to debug.

## Stop conditions

- **Formal `NEEDS_INFO`**: only after available automatic provider/Web paths cannot close a decision-relevant formal gap.
- **Provisional view allowed**: when official certification is incomplete but enough authoritative evidence exists for a clearly labelled research interpretation.
- **No invented precision**: never fabricate BUY authority, target price, position weight, stop, or entry range to avoid an incomplete formal chain.
- **No real brokerage execution**.
