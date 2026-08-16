# Workflow — Current Company Research

## When to use

Use for a named A-share current-investment question such as “现在买中国海油合适吗”“这家公司现在估值贵不贵”“未来半年到一年赔率如何”。

Primary skills: `$astock-research-orchestrator` → `$company-deep-research`, with `$evidence-investigation` and `$financial-integrity-audit` as bounded subflows.

## Flow

1. **Refresh local portfolio, then resolve identity**
   - If the paper account exists, run `local-portfolio-sync-paper`, read the Git-ignored local portfolio, and catch up pending orders before the main research question.
   - Review existing holdings incrementally in parallel with the requested company analysis; do not rerun every held company from scratch.
   - Resolve one six-digit A-share code and explicit market. Do not ask the user for internal artifact IDs.

2. **Current acquisition first**
   - Run `uv run astock research-acquire-current <company_id> --market <market>`.
   - The command reads the active `current-research-policy`; default/min/max lookback, core/optional capabilities, dependency stages, worker count and automatic-resolution budget are policy facts, not CLI/Python constants.
   - For a bounded non-default question, an Agent may first submit a `ResearchPlannerProposal` through `adaptive-plan-validate`, then pass the frozen `ValidatedResearchPlan` with `--planner-plan-artifact-id`. The planner can add/skip optional work but cannot remove active core capabilities.
   - This is the current-investment acquisition layer, not a historical replay. The user's question timestamp is not the research cutoff; the current decision snapshot is frozen after acquisition.

3. **Parallel acquisition where safe**
   - Execute the frozen `CurrentResearchSchedule` stage-by-stage; only same-stage tasks run in parallel.
   - Acquire current daily market data, optional corporate-action evidence, latest annual financial hints and the **latest actually disclosed** interim/quarterly period according to the schedule; do not guess the latest report solely from the calendar month.
   - Provider candidates come from capability registry + active provider health/route policy. A preferred-provider failure must not stop independent downstream collection; UNAVAILABLE/CORRUPT live providers are skipped.
   - Retry only retryable transient failures; use bounded retry/circuit breaking rather than indefinite waiting.

4. **Resolve unresolved capabilities automatically**
   - Use the active Current Research policy's automatic resolution budget (currently 1800 seconds) before considering manual help.
   - `SourceAccessRouter` is policy-driven rather than a fixed API→Browser chain: officiality, capability match, health, freshness, latency, cost/auth friction and retryability are scored, while strong official evidence retains hard priority and Manual remains last.
   - If a provider path needs adaptive recovery, Agent may propose a `ProviderRecoveryProposal`; only `adaptive-recovery-validate` may admit allowlisted capability-compatible paths.
   - Route unresolved bounded evidence questions to `$evidence-investigation` and cross-check material facts across independent authoritative sources where practical.
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
   - If the formal result permits simulation and the entry condition is currently satisfied, local standing settings may continue into the paper order/confirmation workflow; the position changes only after replay confirms a fill.
   - A direct user simulated-trade instruction overrides the research opinion but not cash/lot/tradability/price-band/fill mechanics.

9. **Render and audit the investor answer**
   - INVESTOR_MODE is the default for a stock question even if repo-skill discovery or one tool call fails.
   - Default structure: **结论（1–2句）→ 2–4个决定性理由 → 最大风险/改变判断的条件**. Do not add a second summary that repeats the same conclusion.
   - Explain unfamiliar finance/statistics terms briefly on first use, then continue in plain language.
   - Keep provider paths, internal Agents/committee stages, protocol/schema/class names, machine states, reason codes, artifact IDs/hashes, SQL/SQLite, CLI logs and developer meta commentary in diagnostics only.
   - Before sending, apply `research-investor-answer-audit` semantics. A failed audit means **rewrite**, not “show the audit” and not “show the backend blockers”.

## Stop conditions

- Automatic provider/endpoint/Web paths must be exhausted before requesting user material.
- If authoritative evidence is sufficient for a useful research view but not for precise execution rules, give the research view and explain the investment-relevant uncertainty only.
- Never fabricate BUY authority, target price, position weight, stop, or entry range to hide uncertainty.
- Never turn a backend state into the headline of a normal investor answer.
- **No real brokerage execution**.
