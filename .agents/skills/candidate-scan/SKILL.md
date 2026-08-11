---
name: candidate-scan
description: Build an evidence-bounded A-share research candidate or observation registry from one immutable CandidateInputRelease. Use when the user asks what companies deserve research, what may be worth observing, for a candidate list, or to refresh an existing watchlist.
---

# 候选扫描

1. Run `uv run astock probe` and confirm the local candidate capability is available.
2. For a broad current-market discovery request, first run `uv run astock research-seeds --live`. This low-cost stage merges three research-only sources: existing `RESEARCH_READY` candidates, market liquidity/scale Seeds, and Expert Seeds dynamically inferred from the currently published big-V Skills plus current public industry-board constituents. Research Seeds are not CandidateRecords and never imply BUY.
3. Use the bounded Seed set to assemble one immutable `CandidateInputRelease` from versioned instrument/tradability, calendar, unadjusted daily data, corporate-action hints, data-quality report, canonical announcement events, closed financial-integrity evidence, and optional user watchlist or holding review. Do not perform full evidence/financial collection for the entire A-share market when a smaller Seed set exists.
4. Ensure every decisive artifact carries its artifact id, object hash, coverage, availability, and PIT status. Formal historical scans accept only `CERTIFIED` or `DOCUMENT_RECONSTRUCTED` inputs. Expert Skill/domain matches are research-scope hints only; company facts still come from official evidence.
5. For MCP/web-agent workflows, prefer `uv run astock candidate-input-run RELEASE.json` to stage the immutable release and scan it without copying a second large request through the conversation. `candidate-input-schema` exposes the exact release contract; `candidate-input-stage` remains available when a separate scan request is needed.
6. Otherwise put only the release id and object hash in `CandidateScanRequest`, then run `uv run astock candidate-scan REQUEST.json`.
7. Inspect with `uv run astock candidate-status --scan-id SCAN_ID` or `--company-id COMPANY_ID`, and verify with `uv run astock candidate-audit SCAN_ID`.
8. Treat `NEEDS_INFO` as an evidence/coverage gap. It never means that the scan found no candidates. For a user asking which stocks to buy, pass a bounded research-ready shortlist to `$company-deep-research`; do not answer from candidate ranking alone.

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
