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
   - After/alongside bounded identity resolution, acquire current daily market data, corporate-action evidence, latest annual financial hints and the **latest actually disclosed** interim/quarterly period; do not guess the latest report solely from the calendar month.
   - A preferred-provider failure must not stop independent downstream collection. Diagnose transport/schema/data-quality failure classes internally and switch to a more reliable provider or endpoint for the same capability.
   - Retry only retryable transient network failures; use bounded retry/circuit breaking rather than indefinite waiting.

4. **Resolve unresolved capabilities automatically**
   - Use an automatic resolution budget of up to 1800 seconds for the current user request before considering manual help.
   - Route each unresolved bounded question to `$evidence-investigation`: local/API diagnostics → provider/endpoint fallback → exchange/CNINFO/issuer IR/regulator Web research → reputable public corroboration.
   - Prefer sources requiring no API registration or user interaction and cross-check material facts across independent authoritative sources where practical.
   - Only after automatic provider and authoritative Web paths are exhausted may the user be asked for help, and all remaining actions must be consolidated into one checklist.

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

9. **Render and audit the investor answer**
   - INVESTOR_MODE is the default for a stock question even if repo-skill discovery or one tool call fails. Prefer `research-acquisition-investor-view` / `research-investor-view` for state translation.
   - Lead with: **结论 / 为什么 / 估值与赔率 / 催化 / 最大风险 / 什么情况会改变结论**.
   - Keep provider paths, internal Agents/committee stages, protocol/schema/class names, machine states, reason codes, artifact IDs/hashes, SQL/SQLite, CLI logs and developer meta commentary in diagnostics only.
   - Before sending, apply `research-investor-answer-audit` semantics. A failed audit means **rewrite**, not “show the audit” and not “show the backend blockers”.

## Stop conditions

- Automatic provider/endpoint/Web paths must be exhausted before requesting user material.
- If authoritative evidence is sufficient for a useful research view but not for precise execution rules, give the research view and explain the investment-relevant uncertainty only.
- Never fabricate BUY authority, target price, position weight, stop, or entry range to hide uncertainty.
- Never turn a backend state into the headline of a normal investor answer.
- **No real brokerage execution**.
