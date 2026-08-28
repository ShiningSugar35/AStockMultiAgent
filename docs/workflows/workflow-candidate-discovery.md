# Workflow — Candidate Discovery

## When to use

Use for broad discovery questions such as “现在有哪些 A 股值得研究”“给我一个观察池”“从全市场筛一批候选”。 This workflow creates **research priority**, not BUY recommendations.

Primary skill: `$candidate-scan`.

## Flow

1. **Low-cost seed discovery**
   - Run `uv run astock research-seeds --live`.
   - Reuse a fresh, ObjectStore/Manifest-verified COMPLETE Instrument Master where possible; refresh through capability-routed providers only when freshness or scope requires it. The registered 2026 official trading calendar is local-official-first and must be consulted before treating a date as an expected trading day.
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
   - `CURRENT_MARKET_SCAN_ZERO_ELIGIBLE_CANDIDATES` means the acquired Universe was usable but no name passed the seed filters; `CURRENT_MARKET_SEED_UNIVERSE_UNAVAILABLE` means the Universe itself could not be established. Never collapse these states.
   - PARTIAL Universe may produce observation/research priority only. Full-market recommendation authority requires every XSHG/XSHE/BJSE `coverage_ratio >= 99.5%`; row-count floors are truncation guards, not FULL proof.
   - The team gate accepts Universe coverage only from the exact ObjectStore-verified typed `ResearchSeedReport` member artifact. Agent prose, a generic artifact, a Search/Web result, or a manually asserted boolean cannot upgrade PARTIAL to FULL.

5. **Deep research only on a bounded shortlist**
   - Pass a small `RESEARCH_READY` shortlist into [Current Company Research](workflow-current-company-research.md), one company at a time or in bounded parallel independent research contexts.
   - Do not perform full institutional research for the entire A-share market.

## Stop conditions

- Momentum/liquidity/community popularity alone can never create `RESEARCH_READY` or BUY authority.
- Incomplete/partial/not-PIT-safe data cannot close or promote a candidate.
- Candidate ranking never bypasses Committee or TradingClassification.
- No paper-ledger or broker write occurs in this workflow.
