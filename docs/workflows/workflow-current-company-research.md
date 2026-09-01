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
   - Current daily OHLCV uses the existing canonical/local chain first and capability-routed structured fallback. The latest key trading day is shadow-checked against an independent source when available; normalized value disagreement is `CONFLICTED` and must not silently publish/overwrite the canonical release.
   - If secondary financial providers are unavailable, a named-company run may recover from the exact official annual/interim/quarterly report. CNINFO report discovery must exhaust pagination (`search_all`) before treating the exact report as unavailable; first-page search is never negative proof. A CNINFO `ProviderError` is recorded once for that recovery attempt, then the workflow moves to an approved exact-item source rather than repeatedly hitting the same failed capability.
   - The resulting financial release must carry typed official lineage. `CNINFO_EXHAUSTIVE_ENUMERATION` may support all-page proof; `OFFICIAL_WEB_EXACT_ITEM_ADMISSION` proves only the admitted known document and must keep `official_exhaustive_proof_allowed=false`.
   - Retry only retryable transient failures; use one acquisition-boundary retry layer, provider+capability circuit breaking and the versioned total elapsed-time budget rather than indefinite waiting. OPEN or an active HALF_OPEN claim immediately moves this task to an approved fallback without contaminating other capabilities of the same Provider.

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
   - A research-safe `NEEDS_INFO / PARTIAL` financial pack may support a bounded qualitative view, but it cannot set `FINANCIAL_INTEGRITY=true`, cannot open precise `VALUATION=true`, and cannot enable formal recommendation. Those checks require an ObjectStore-verified typed `FinancialIntegrityEvidencePack` with `status=SUCCEEDED / coverage_status=COMPLETE`.

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
   - INVESTOR_MODE is the default for a stock question even if repo-skill discovery or one tool call fails. A system error string and a negated request such as “不要看日志” must not switch the mode.
   - Build one canonical `ResearchNarrativeBundle`, then project it through `ResponseGateway`; do not construct a parallel response object in a Workflow or Skill.
   - Default structure: **主体 → 结论与强度 → 估值/赔率 → 2–4个决定性理由 → 最大风险 → 改变判断的条件 → 数据时间与必要引用**. Do not add a second summary that repeats the same conclusion.
   - Length reduction may remove only non-critical reasons. It must never delete the subject, conclusion, conclusion strength, valuation/odds, largest risk, change condition, data cutoff, required citation or safe report reference; if mandatory content still exceeds the budget, return the safe fallback without echoing the draft.
   - Explain unfamiliar finance/statistics terms briefly on first use, then continue in plain language.
   - Keep provider paths, internal Agents/committee stages, protocol/schema/class names, machine states, reason codes, artifact IDs/hashes, SQL/SQLite, CLI logs and developer meta commentary in diagnostics only.
   - Preserve the stable machine contracts `research-investor-view` and `research-acquisition-investor-view`. Public rendering uses `research-public-view` or `research-acquisition-public-view`.
   - Before sending, apply `research-investor-answer-audit` and `docs/architecture/public-response-contract-v1.md`. A failed audit means **safe rewrite/fallback**, not “show the audit”, “show the backend blockers”, or echo the rejected draft.

10. **Publish the formal report from the same frozen facts**
   - A completed deep-research request builds `ReportRequest` from the same `ResearchNarrativeBundle` and registered input artifacts; the report layer does not refetch or reinterpret research facts.
   - DOCX is the default renderer, Markdown is the deterministic fallback, and PDF is optional only when the configured converter succeeds output validation.
   - Bind citation/asset manifests, privacy level, input hashes, renderer/converter version and output SHA-256 in `ReportManifest`; unsupported asset rights are recorded and excluded rather than silently embedded.
   - Publish through staging, integrity validation and atomic replace. Matching idempotency checkpoints recover the existing report instead of republishing it.
   - The investor answer returns only the safe report reference together with the core conclusion and limitations; private absolute report paths remain internal.

## Stop conditions

- Automatic provider/endpoint/Web paths must be exhausted before requesting user material.
- If authoritative evidence is sufficient for a useful research view but not for precise execution rules, give the research view and explain the investment-relevant uncertainty only.
- Never fabricate BUY authority, target price, position weight, stop, or entry range to hide uncertainty.
- Never turn a backend state into the headline of a normal investor answer.
- **No real brokerage execution**.

## Same-request automatic continuation

The current-company workflow is one durable request, not a sequence of user prompts.

- Start or recover it with `uv run astock research-current-continuation-start REQUEST.json` and
  `uv run astock research-current-continuation-status <continuation_id>`.
- `AUTO_RESOLUTION_REQUIRED`: the Agent automatically discovers and freezes exact official
  evidence, submits a typed automatic-resolution artifact through
  `uv run astock research-current-continuation-resolve RESOLUTION.json`, and resumes. Public
  captures are validated and bound atomically; search snippets and secondary summaries are never
  accepted as the fact artifact.
- `TEAM_RESEARCH_REQUIRED`: the Agent runs all ready research-team tasks in DAG waves and
  advances the gate without returning an interim investment conclusion.
- `READY_FOR_INVESTOR_VIEW`: the complete lineage supports a formal recommendation.
- `OBSERVATION_ONLY_FOR_INVESTOR_VIEW`: the complete lineage supports an explicitly labelled
  observation view, but at least one formal recommendation gate failed.
- `NEEDS_USER_INPUT`: only bounded automatic-channel exhaustion or genuinely private source material may trigger a user request. User-supplied material resumes the same continuation.

Every internal continuation status must preserve `investment_conclusion_blocked=true`,
`same_request_continuation_required=true`, and `broker_execution_allowed=false`; normal investor answers must not expose those backend fields. Public-source
work must never be delegated back to the user merely because one provider or page failed.

## Required specialist Skills

The team DAG must dispatch the following canonical Repo Skills when their corresponding roles
become ready; role labels alone are not an implementation:

- `.agents/skills/macro-policy-regime/SKILL.md` for `macro` and `policy`.
- `.agents/skills/industry-value-chain/SKILL.md` for `industry`.
- `.agents/skills/catalyst-event-research/SKILL.md` for `catalyst`.
- `.agents/skills/governance-management-quality/SKILL.md` for `governance`.
- Existing `bull` and `bear` tasks remain separate, independent contexts; `.agents/skills/investment-red-team/SKILL.md` is dispatched only for the downstream `reviewer` after both complete.
- `.agents/skills/model-risk-backtest-validation/SKILL.md` for `model-risk`.

Each Skill returns the exact typed role output, freezes its evidence lineage, records
contradictions and abstains when its formal gate cannot be supported. The committee may consume
only registered typed outputs from completed dependencies.
