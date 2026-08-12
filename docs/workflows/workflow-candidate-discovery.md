# Workflow — Candidate Discovery

## When to use

Use for broad discovery questions such as “现在有哪些 A 股值得研究”“给我一个观察池”“从全市场筛一批候选”。 This workflow creates **research priority**, not BUY recommendations.

Primary skill: `$candidate-scan`.

## Flow

1. **Low-cost seed discovery**
   - Run `uv run astock research-seeds --live`.
   - Merge existing `RESEARCH_READY` candidates, current market liquidity/scale Seeds, and Expert Domain Seeds derived from published Knowledge Skills and current public industry constituents.
   - ResearchSeed is research scope only and has no trading authority.

2. **Bound the expensive work**
   - Run `uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live`.
   - Promotion freezes the exact instrument-universe proof, reference/quality/company-action/announcement/financial inputs needed for the bounded seed set.
   - A blocked seed becomes an isolated evidence task; it must not stop the rest of the batch.

3. **Candidate scan**
   - Reuse an immutable `CandidateInputRelease` where possible.
   - Manual `candidate-input-schema / candidate-input-stage / candidate-input-run` remains diagnostic fallback; do not hand-build a large input release when promotion can derive it.
   - Run Candidate Scan and inspect `candidate-status` / `candidate-audit`.

4. **Interpret candidate states correctly**
   - `RESEARCH_READY` = worth deeper research, not buyable.
   - Observation-only = keep watching; do not promote via narrative.
   - `NEEDS_INFO` = evidence/coverage gap, not “no candidates found”.

5. **Deep research only on a bounded shortlist**
   - Pass a small `RESEARCH_READY` shortlist into [Current Company Research](workflow-current-company-research.md), one company at a time or in bounded parallel independent research contexts.
   - Do not perform full institutional research for the entire A-share market.

## Stop conditions

- Momentum/liquidity/community popularity alone can never create `RESEARCH_READY` or BUY authority.
- Incomplete/partial/not-PIT-safe data cannot close or promote a candidate.
- Candidate ranking never bypasses Committee or TradingClassification.
- No paper-ledger or broker write occurs in this workflow.
