---
name: candidate-scan
description: Build an evidence-bounded A-share research candidate or observation registry from one immutable CandidateInputRelease. Use when the user asks what companies deserve research, what may be worth observing, for a candidate list, or to refresh an existing watchlist.
---

# 候选扫描

1. Run `uv run astock probe` and confirm the local candidate capability is available.
2. Assemble and register one immutable `CandidateInputRelease` from local versioned instrument/tradability, calendar, unadjusted daily data, corporate-action hints, data-quality report, canonical announcement events, closed financial-integrity evidence, and optional user watchlist or holding review.
3. Ensure every decisive artifact carries its artifact id, object hash, coverage, availability, and PIT status. Formal historical scans accept only `CERTIFIED` or `DOCUMENT_RECONSTRUCTED` inputs.
4. Put only the release id and object hash in `CandidateScanRequest`; then run `uv run astock candidate-scan REQUEST.json`.
5. Inspect with `uv run astock candidate-status --scan-id SCAN_ID` or `--company-id COMPANY_ID`, and verify with `uv run astock candidate-audit SCAN_ID`.
6. Treat `NEEDS_INFO` as an evidence/coverage gap. It never means that the scan found no candidates.

## Deterministic policy

- `candidate-scan-v1` uses 20 valid trading days, median turnover of at least CNY 20 million, and a nonzero-turnover ratio of at least 0.90.
- A 20-day absolute price change of at least 15% plus current volume at least 1.5 times the prior-20 median is a weak clue only.
- Quality `FAIL` disables technical and liquidity support; `PARTIAL` is explicitly degraded.
- Only canonical major announcements and closed MEDIUM/HIGH financial findings provide official or financial support.
- Watchlist membership is user intent only. Holding review contributes only new or invalidating evidence.
- `RESEARCH_READY` requires at least MODERATE real evidence, PIT safety, non-failed quality and liquidity gates, and `TRADABLE` status.

## Output boundary

The only durable outputs are `CandidateSignal`, `CandidateRecord`, `CandidateUniverseSnapshot`, `CandidateScanReport`, and `CandidateAuditReport`. `CandidateRecord` means “worth further research” or “observation only”; it is never paper eligibility or a trading instruction.

## Prohibitions

- Do not create BUY/SELL direction, target price, quantity, weight, order, position, `TradeProtocol`, committee decision, or paper-ledger mutation.
- Do not turn price momentum, liquidity, or community popularity alone into `RESEARCH_READY`.
- Do not silently use future, missing, partial, duplicate, or `NOT_PIT_SAFE` evidence.
- Do not use an incomplete release to increment lifecycle misses or close a candidate.
- Do not bypass `astock` commands by directly editing SQLite.
