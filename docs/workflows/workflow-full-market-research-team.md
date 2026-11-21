# Workflow — Full-Market Research Team

## When to use

Use for broad **current** stock-picking questions such as “有什么好买的股”“推荐几只股票”“现在全市场哪些公司值得买”。 This workflow is the only route that may produce a market-wide formal buy ranking. A candidate list by itself is never a recommendation.

Architecture/acceptance contract: [`../architecture/full-market-research-team-v1.md`](../architecture/full-market-research-team-v1.md).

## Execution model

- Default backend: `CHAT_ORCHESTRATED`.
- No background LLM daemon or daily pre-sync is assumed.
- User invocation starts one on-demand run. Current market/reference/evidence data are fetched in that run and immutable raw responses are reused through existing ObjectStore lineage.
- **Same-request completion is mandatory for recommendation intent.** Seed/Candidate shortlist, acquisition recovery, partial company fan-out, unfinished debate/committee, and unfinished portfolio are internal checkpoints only. The Agent keeps the originating logical request active until the deterministic investment-closure/recommendation gate permits an investor view, or a truthful documented stop condition applies. Follow the active `configs/chat_invocation_policy_v1.yaml`: hand off for an observed platform deadline/interruption, explicit cancellation or an exhausted resource budget, preserving the same request/checkpoint/lease. Elapsed time alone is not a stop signal. OTHER_AGENT does not inherit Chat scheduling. Per-call/batch/resource limits remain bounded. Never substitute another request's historical receipt or declare a Scheduled task created without its actual creation receipt. Do not answer with “下一步可以继续深研/估值/配仓”。
- Hardware budget comes from `research-runtime-profile`; lightweight laptops use bounded parallelism rather than maximum fan-out.
- `AGENT_RUNTIME` is an optional future executor only; it must reuse the same task graph and readiness gate.

## Flow

1. **Refresh local investor state and investment expectation**
   - Sync/read the local paper portfolio and unresolved continuous-monitor items.
   - Resolve principal and target annual return independently before Full Research: current explicit request value first, then the newest verifiable recorded value in recent formal recommendation history, then the current versioned policy defaults. Model DEFAULT values never hide older user values; malformed objects and other-account entries are skipped. Restore from registered request logs as well as formal recommendation/holding-research contracts, then freeze the small context before capability execution.
   - The annual target is a planning objective, never a promised return or authority to relax mandatory research, concentration, liquidity, risk, margin/shorting or broker constraints. If current approved candidates cannot support the target path under hard limits, retain cash and expose the objective gap.
   - This state restore does not create research authority.

2. **Create the durable team plan**
   - Run `uv run astock research-team-plan`.
   - Read `research-team-status` and execute only currently ready tasks.
   - Each semantic role works in its own context and emits a registered `ResearchRoleOutput`, followed by `ResearchRoleResult`.

3. **Stage 1 — current environment and Universe, safely parallel**
   - CIO intent freezes scope/horizon.
   - Macro, Policy, Liquidity/Risk and current A-share Universe acquisition may run in parallel after CIO intent.
   - XSHG/XSHE/BJSE market snapshots are fetched on demand with `market_fetch_workers` selected from the hardware profile. **证券全集与行情 numerator 分权**：SSE/SZSE/BSE official Instrument Master 分别作为三市场首选 denominator；行情/流动性字段由独立 quote families 提供。EastMoney/Sina 失败或覆盖不足时，使用官方 master 的精确 symbol set 驱动 Tencent batch quote fallback，Tencent 不得自己定义 Universe。
   - One provider/market failure must not cancel independent tasks. Denominator router 按 official → complete secondary master 有界回退；secondary master 只允许维持 observation/research 可用性。`coverage_ratio >= 99.5%` is only the engineering high-coverage threshold. Formal FULL requires a typed `UniverseCoverageProof` whose XSHG/XSHE/BJSE market reconciliations each reach `OFFICIAL_DENOMINATOR_RECONCILED` with verified denominator/numerator hashes and attributable source snapshots. For CURRENT research, the cutoff is the decision snapshot frozen **after** bounded acquisition/recovery completes; it is not the user's question timestamp. A secondary source's self-reported 100% remains `ENGINEERING_HIGH_COVERAGE`.
   - PARTIAL and engineering-high-coverage Universes may continue into observation/research discovery, but `formal_full_market_coverage_allowed=false`; a complete high-coverage scan that yields zero eligible candidates is a valid zero-result state and must not be rewritten as Universe unavailable.

4. **Stage 2 — blind discovery first**
   - Run `research-seeds --live`, promotion and Candidate Scan.
   - Treat Candidate/Promotion `NEEDS_INFO` as an internal recovery signal: rebuild/refetch inconsistent canonical artifacts, exhaust allowlisted provider fallbacks, and use bounded authoritative Web capture for unresolved public facts before rerunning the same lineage. Corporate actions and official announcement hits continue as research signals rather than terminating discovery.
   - Preserve the blind market tranche before Expert overlays.
   - Expert-domain admission uses an **absolute audited Skill count only**. Author-relative `skill_share` is diagnostic and must not gate or score admission.
   - Expert overlay is bounded and never creates recommendation authority.
   - If current market Universe/market Seeds cannot be established after allowed recovery, stop the formal market-wide path. Web/news may investigate the failure, but may not supply hand-picked replacement stocks.

5. **Stage 3 — sector comparison**
   - Combine Macro/Policy/Liquidity context with the blind shortlist.
   - Resolve each candidate into an internal Industry Research Archetype where possible; unresolved mappings remain `UNCLASSIFIED` instead of guessing.
   - Compare industry structure, demand/capacity, pricing, competition, cycle/technology, regulation and company-specific Alpha vs industry/macro Beta.

6. **Stage 4 — bounded company fan-out**
   - Only the bounded shortlist proceeds.
   - Fundamental, Financial Integrity, Catalyst/Disclosure and Market Context work may run in parallel per company after their common evidence scope is frozen.
   - The canonical institutional path remains evidence sufficiency → industry/company economics → driver tree → Bull/Base/Bear forecast → valuation/sensitivity → decision context.
   - Official financial recovery must preserve typed lineage and exact authority semantics. A frozen exact-item report may restore a limited `NEEDS_INFO / PARTIAL` pack, but cannot self-upgrade to COMPLETE.

7. **Stage 5/6 — valuation and independent debate**
   - Precise valuation follows the canonical forecast/model artifacts only after an ObjectStore-verified typed `FinancialIntegrityEvidencePack` proves `status=SUCCEEDED / coverage_status=COMPLETE`. With PARTIAL financial coverage, only an explicitly observation-only valuation result may be registered and the `VALUATION` readiness check remains false.
   - Bull and Bear read the same frozen inputs but **must use different `independent_context_id` values and must not read each other's draft**.

8. **Stage 7/8 — reviewer and committee**
   - Independent Reviewer starts only when Bull and Bear are both complete.
   - Committee starts only after Reviewer and remains offline from new evidence acquisition.

9. **Stage 9 — portfolio**
   - Portfolio construction may use only committee-approved names.
   - Concentration/sector/risk constraints are evaluated here; no candidate/expert seed bypasses this stage.
   - Freeze annual and explicit-horizon profit targets from the resolved principal/annual target; compare them with the weighted valuation return and downside scenario. The annual target may tighten the maximum acceptable entry price through the target-return hurdle only when the valuation horizon is known. Unknown/mixed horizons yield a disclosed non-comparable status and no invented numeric gap. Entry costs/slippage are deducted from notional-based scenario P&L; exit costs remain explicitly unknown. It never increases leverage or overrides hard risk limits.

10. **Stage 10 — deterministic Full Research input gate**
    - Build `FullResearchInputReadinessRequest` only after required work is complete.
    - Run `research-full-research-readiness`.
    - `TEAM_DAG_COMPLETE` is derived by Python and cannot be asserted by the Agent.
    - `READY` means only `full_research_input_ready=true`; this gate never authorizes a formal recommendation.
    - Any failed/missing check means `OBSERVATION_ONLY` and blocks the downstream Full Research recommendation gate.
    - Formal buy ranking, price and position sizing require the separate final `RecommendationResearchReceipt + Publication Gate`.

## Role-output contract

Apply the [shared team execution contract](../architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) once per ready DAG wave. Preserve registered typed members, exact evidence union and independent contexts. Reuse frozen macro/policy, industry, governance and event artifacts across downstream company/Bull/Bear tasks; load each role's Skill only when dispatched. Use `research-coverage-score` for coverage diagnostics. Private Skill remains edge-only and never replaces core evidence or publication.

## Stop conditions

Missing Universe proof, blind lineage, independent contexts or required typed artifacts blocks the dependent formal recommendation. It does not terminate recoverable acquisition or unrelated ready tasks. Continue the same request while an applicable route can add material evidence within the active budget; reuse successful nodes. The existing closure service distinguishes completion, public-route exhaustion, private input, budget exhaustion and interruption. Never relabel an intermediate gate failure as completed research.

## Fail-closed rules

- No current market Universe → no formal market-wide recommendation.
- Zero usable market Seeds → no manual Web/news hand-pick substitution.
- Missing macro/sector/company/valuation/debate/reviewer/committee/portfolio work → observation only.
- Bull/Bear same context → reject completion.
- Missing/unregistered/wrong-contract output → reject completion.
- Candidate/Seed/Skill itself → never BUY authority.
- Real broker execution remains unavailable.

## Performance on light office laptops

- Read concurrency, CPU and shortlist budgets from `research-runtime-profile` and the active policy; do not duplicate numeric defaults in prompts.
- Network-bound independent calls may run concurrently; CPU/memory-heavy work stays bounded.
- Same-run raw snapshots and registered artifacts are reused rather than refetched.
- No GPU, Redis, Kafka, Temporal or background daemon is required.
- The active request/resource policy owns budgets; a fixed duration or completion fraction is not a quality verdict.

## Recovery and optional source supplement

Before returning any public-data `NEEDS_INFO`/unavailable result, the Agent must attempt the existing canonical acquisition, applicable alternate providers, and authoritative Web recovery within the current request budget. Use `research-supplemental-schema` and `research-supplemental-acquire <request.json> --live` for `market.reference.hint` (AKShare/Tushare) or `news.global.lead` (Finnhub) when relevant. Missing optional keys are a source availability issue, not missing investor input. BaoStock stays on the established reference route. Register the returned capture, inspect original-source provenance, and bring verified facts back through the failed canonical node; hint captures are not financial certification, independent EASTMONEY cross-checks, complete Universe proof, or publication permission.

After public recovery is genuinely exhausted, `PUBLIC_DATA_UNAVAILABLE` records a bounded unresolved attempt with no private-material demand. Preserve acquired evidence and describe only the remaining uncertainty. Ask for user input only when it is actually private or requires their authorization. Do not let an unavailable source, incomplete intermediate artifact or exhausted budget trigger an endless full-graph rerun. LLM query/source/parse proposals remain allowed; truth, accounting, historical PIT and execution checks remain deterministic.

Current implementation, validation scope and known limitations: `docs/architecture/adaptive-recovery-and-validation-v1.md`.
