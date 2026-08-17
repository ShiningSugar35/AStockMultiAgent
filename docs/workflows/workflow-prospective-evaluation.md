# Workflow — Prospective Evaluation

## When to use

Use when the goal is to determine whether research/portfolio/Skill changes work **out of sample** rather than to answer a single current investment question.

Primary routing skill: `$astock-research-orchestrator`. This workflow spans Phase 7, Phase 11 and Phase 12 governance.

## Flow

1. **Use the formal Phase 7 study**
   - Reuse the existing frozen prospective study and arms.
   - Do not create historical/backfilled assignments after outcomes are visible.
   - Formal forward-event count can increase only from real future research events frozen before outcomes.

2. **Freeze assignment and independence before outcomes**
   - For each real event, freeze exact inputs, arm assignment, independence key and ex-ante market-regime snapshot before forward prices/outcomes are visible.
   - Overlapping stock/theme/catalyst decisions must obey the independence contract rather than being counted as independent by convenience.

3. **Register the full research funnel**
   - Use Phase 11 prospective governance for all-trials accounting: seed rejection, promotion block, candidate rejection, Committee `REJECT / NEEDS_INFO / WATCH / APPROVE_SIMULATION` and formal assignment states.
   - `formal_trade_event` remains false for the all-trials registry; it cannot inflate Phase 7 formal event count.
   - Bind stock/industry/theme/decision-date/shared-catalyst clusters where applicable.

4. **Prove temporal validity before reading outcomes**
   - For any new/changed time-series feature, window, resample, as-of join or agentic retrieval chain, run `pit-temporal-audit` against the exact decision-time dependency graph. Reference time and availability time are separate; only value-independent availability is eligible for the linear-time proof.
   - Row-aligned feature transforms require a truncation-invariance property test: recompute on each prefix (or sampled cutoffs) and require the historical prefix to match the same prefix from the full dataset. The local probe is exhaustive through 64 rows and otherwise uses at most 64 well-spread cutoffs unless the caller explicitly supplies an exhaustive list; the result records whether coverage was exhaustive. A drift is a concrete leakage finding, not a warning to ignore.
   - If an LLM/model has a known training/knowledge cutoff and enough independent periods exist on both sides, use `pit-knowledge-cutoff-diagnostic` to report pre/post weighted alpha, project-defined `pre - post` decay and retention. It is descriptive only; missing either side is `NOT_EVALUABLE` and no cutoff diagnostic grants admission.

5. **Wait for real forward observations**
   - Freeze future market observations only after the defined 5/20/60 trading-day horizons mature.
   - Recompute returns, MFE/MAE, implementation cost and benchmark/sector adjustment from canonical frozen data.

6. **Use predeclared statistics**
   - Preserve primary endpoints and diagnostic endpoints defined by the Phase 11 plan.
   - Use paired/clustered bootstrap and purged ordered folds with embargo where required.
   - DSR/PBO remain diagnostics under repeated selection; they do not grant an automatic pass badge.

7. **Track research-production efficiency prospectively**
   - Phase 12 `SkillUsageEvent` records whether a Skill corrected a claim, found a gap, changed a driver, supplied a falsifier, changed IC state and its token/research cost.
   - Prospective lift requires a real prospective evaluation artifact/hash.
   - Without prospective evidence, the system may flag `INSUFFICIENT_PROSPECTIVE_EVIDENCE` but cannot auto-retire, auto-reweight or rewrite a Skill.

8. **Catalyst/KPI updates are local reruns**
   - Register catalyst window, KPI rule and affected modules before/when appropriate.
   - On state change, rerun only affected modules; do not trigger a full research chain just because new information exists.

9. **Admission remains separate from production mutation**
   - `adaptive-research-status` / Phase 8 admission is read-only governance.
   - Even an eligible admission does not enable online learning, adaptive weights, main paper-ledger writes or broker execution without separate explicit versioned approval.

## Output

Report sample count, maturity, independence/cluster coverage, regime/fold coverage, predeclared endpoint results and uncertainty. Distinguish “software ready”, “data collecting”, “statistically evaluated” and “admitted”.

## Stop conditions

- Never manufacture or backfill prospective samples.
- Never relabel regimes retrospectively to improve results.
- Never change default portfolio method or Skill weights from one backtest/small sample.
- No shadow/prospective result can directly write the main paper ledger or real broker.
