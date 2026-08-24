---
name: candidate-scan
description: Build an evidence-bounded A-share research candidate or observation registry from one immutable CandidateInputRelease. Use when the user asks what companies deserve research, what may be worth observing, for a candidate list, or to refresh an existing watchlist.
---

# 候选扫描

1. Run `uv run astock probe` and confirm the local candidate capability is available.
2. For a broad current-market discovery request, first run `uv run astock research-seeds --live`. The command fetches XSHG/XSHE/BJSE market snapshots on demand with the hardware-aware worker budget. It merges existing `RESEARCH_READY` candidates, a **blind market tranche** from liquidity/scale data, and bounded Expert overlays from audited Skills plus public industry-board constituents. Expert-domain admission uses only the absolute audited-Skill count (`minimum_domain_skill_count`, default 3); the retired author-relative Skill-share threshold must not be restored. `skill_share` is diagnostic only. Expert overlay may add at most the active policy bonus and cannot displace the reserved blind tranche. Research Seeds are not CandidateRecords and never imply BUY.
3. Promote the bounded Seed set with `uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live`. Promotion automatically reuses existing `RESEARCH_READY` candidates, freezes an exact `CandidateInstrumentUniverseProof` from the Seed report and parent instrument master, checks/syncs calendar and unadjusted daily data, freezes data quality and official CNINFO corporate-action absence evidence, enumerates canonical announcements, reuses or runs FinancialIntegrity, builds the immutable `CandidateInputRelease`, and immediately runs Candidate Scan. A blocked Seed becomes a structured retryable evidence task without blocking the rest of the batch. Do not perform full evidence/financial collection for the entire A-share market when a smaller Seed set exists.
4. Ensure every decisive artifact carries its artifact id, object hash, coverage, availability, and PIT status. Formal historical scans accept only `CERTIFIED` or `DOCUMENT_RECONSTRUCTED` inputs. Expert Skill/domain matches are research-scope hints only; company facts still come from official evidence.
5. `candidate-input-schema / candidate-input-stage / candidate-input-run` remain manual/diagnostic fallbacks for an already assembled release. In normal MCP/web-agent discovery, do not ask the model to hand-build the large release JSON when `research-seeds-promote` can derive it from registered Seed/reference/evidence artifacts.
6. Otherwise put only the release id and object hash in `CandidateScanRequest`, then run `uv run astock candidate-scan REQUEST.json`.
7. Inspect with `uv run astock candidate-status --scan-id SCAN_ID` or `--company-id COMPANY_ID`, and verify with `uv run astock candidate-audit SCAN_ID`.
8. Treat `NEEDS_INFO` as an evidence/coverage gap. It never means that the scan found no candidates. For a user asking which stocks to buy, pass a bounded research-ready shortlist to `$company-deep-research`; do not answer from candidate ranking alone. After independent deep research, pass only formal WATCH / APPROVE_SIMULATION names to `$continuous-investment-monitor` as `RECOMMENDED`; raw Seed/Candidate membership must never enroll itself as a recommendation.

## Deterministic policy

- `candidate-scan-v1` uses 20 valid trading days, median turnover of at least CNY 20 million, and a nonzero-turnover ratio of at least 0.90.
- A 20-day absolute price change of at least 15% plus current volume at least 1.5 times the prior-20 median is a weak clue only.
- Quality `FAIL` disables technical and liquidity support; `PARTIAL` is explicitly degraded.
- Only canonical major announcements and closed MEDIUM/HIGH financial findings provide official or financial support.
- Watchlist membership is user intent only. Holding review contributes only new or invalidating evidence.
- `RESEARCH_READY` requires at least MODERATE real evidence, PIT safety, non-failed quality and liquidity gates, and `TRADABLE` status.

## Workflows

- [`docs/workflows/workflow-candidate-discovery.md`](../../../docs/workflows/workflow-candidate-discovery.md)

## Output boundary

The only durable outputs are `CandidateSignal`, `CandidateRecord`, `CandidateUniverseSnapshot`, `CandidateScanReport`, and `CandidateAuditReport`. `CandidateRecord` means “worth further research” or “observation only”; it is never paper eligibility or a trading instruction.

## Prohibitions

- Do not create BUY/SELL direction, target price, quantity, weight, order, position, `TradeProtocol`, committee decision, or paper-ledger mutation.
- If current market Universe/market Seeds are unavailable, do not hand-pick replacement names from Web/news/community material and present them as a market-wide shortlist; fail closed and return a coverage gap.
- Do not turn price momentum, liquidity, or community popularity alone into `RESEARCH_READY`.
- Do not silently use future, missing, partial, duplicate, or `NOT_PIT_SAFE` evidence.
- Do not use an incomplete release to increment lifecycle misses or close a candidate.
- Do not bypass `astock` commands by directly editing SQLite.
