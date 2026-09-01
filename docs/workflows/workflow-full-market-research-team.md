# Workflow — Full-Market Research Team

## When to use

Use for broad **current** stock-picking questions such as “有什么好买的股”“推荐几只股票”“现在全市场哪些公司值得买”。 This workflow is the only route that may produce a market-wide formal buy ranking. A candidate list by itself is never a recommendation.

Architecture/acceptance contract: [`../architecture/full-market-research-team-v1.md`](../architecture/full-market-research-team-v1.md).

## Execution model

- Default backend: `CHAT_ORCHESTRATED`.
- No background LLM daemon or daily pre-sync is assumed.
- User invocation starts one on-demand run. Current market/reference/evidence data are fetched in that run and immutable raw responses are reused through existing ObjectStore lineage.
- Hardware budget comes from `research-runtime-profile`; lightweight laptops use bounded parallelism rather than maximum fan-out.
- `AGENT_RUNTIME` is an optional future executor only; it must reuse the same task graph and readiness gate.

## Flow

1. **Refresh local investor state**
   - Sync/read the local paper portfolio and unresolved continuous-monitor items.
   - This does not create research authority.

2. **Create the durable team plan**
   - Run `uv run astock research-team-plan`.
   - Read `research-team-status` and execute only currently ready tasks.
   - Each semantic role works in its own context and emits a registered `ResearchRoleOutput`, followed by `ResearchRoleResult`.

3. **Stage 1 — current environment and Universe, safely parallel**
   - CIO intent freezes scope/horizon.
   - Macro, Policy, Liquidity/Risk and current A-share Universe acquisition may run in parallel after CIO intent.
   - XSHG/XSHE/BJSE market snapshots are fetched on demand with `market_fetch_workers` selected from the hardware profile.
   - One provider/market failure must not cancel independent tasks. `coverage_ratio >= 99.5%` is only the engineering high-coverage threshold. Formal FULL requires a typed `UniverseCoverageProof` whose XSHG/XSHE/BJSE market reconciliations each reach `OFFICIAL_DENOMINATOR_RECONCILED` with verified denominator/numerator hashes and source snapshots no later than the research cutoff. A secondary source's self-reported 100% remains `ENGINEERING_HIGH_COVERAGE`.
   - PARTIAL and engineering-high-coverage Universes may continue into observation/research discovery, but `formal_full_market_coverage_allowed=false`; a complete high-coverage scan that yields zero eligible candidates is a valid zero-result state and must not be rewritten as Universe unavailable.

4. **Stage 2 — blind discovery first**
   - Run `research-seeds --live`, promotion and Candidate Scan.
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

10. **Stage 10 — deterministic recommendation gate**
    - Build `RecommendationReadinessRequest` only after required work is complete.
    - Run `research-recommendation-readiness`.
    - `TEAM_DAG_COMPLETE` is derived by Python and cannot be asserted by the Agent.
    - Only `formal_recommendation_allowed=true` may be rendered as a formal buy ranking.
    - Any failed/missing check means `OBSERVATION_ONLY`.

## Role-output contract

For each non-gate task:

1. Register the role output with `research-team-role-output`.
2. `plan_id`, `task_id`, and `output_contract` must match the plan.
3. Every recommendation-required role must reference at least one registered member artifact; the member artifact must already exist and verify in ObjectStore.
4. Every declared `evidence_id` must resolve to a real Evidence record whose excerpt object verifies; task-result Evidence ids must exactly equal the union declared by its registered role outputs.
5. Register completion with `research-team-task-result` using the resulting `ResearchRoleOutput` artifact id.
6. Dependencies and Bull/Bear independence are checked deterministically.
7. Use `research-coverage-score` to record Universal / Industry / Private Skill / Evidence coverage. Private Skill is edge-only and never replaces core research coverage or the final recommendation gate.

An arbitrary registered artifact cannot be used directly to complete a research-team task.

## Stop conditions

Formal market-wide recommendation stops immediately at `OBSERVATION_ONLY` if the current Universe cannot be proven, the blind discovery lineage is unavailable, a required team node is incomplete/blocked, Bull/Bear independence fails, a required output contract cannot be verified, or the deterministic Recommendation Readiness Gate is not READY. Evidence recovery may continue within the on-demand run budget, but no conversational fallback may bypass these conditions.

## Fail-closed rules

- No current market Universe → no formal market-wide recommendation.
- Zero usable market Seeds → no manual Web/news hand-pick substitution.
- Missing macro/sector/company/valuation/debate/reviewer/committee/portfolio work → observation only.
- Bull/Bear same context → reject completion.
- Missing/unregistered/wrong-contract output → reject completion.
- Candidate/Seed/Skill itself → never BUY authority.
- Real broker execution remains unavailable.

## Performance on light office laptops

- LOW_RESOURCE defaults: provider=2, logical Agent=2, DuckDB threads=2, deep-company concurrency=2, shortlist=6.
- Network-bound independent calls may run concurrently; CPU/memory-heavy work stays bounded.
- Same-run raw snapshots and registered artifacts are reused rather than refetched.
- No GPU, Redis, Kafka, Temporal or background daemon is required.
- The full run may consume the configured two-hour resolution budget when data providers or authoritative Web evidence are slow; duration itself is not a quality gate.
