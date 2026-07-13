---
name: candidate-scan
description: Build an evidence-bounded A-share candidate or watchlist from implemented deterministic scans. Use when the user asks what stocks deserve research, what may be worth observing or buying, for a candidate list, or to refresh an existing watchlist.
---

# 候选扫描

1. Run `uv run astock probe` and inspect which candidate and evidence capabilities are implemented.
2. Synchronize required symbols through `uv run astock sync-market` or `uv run astock sync-5m`; reject failed quality batches.
3. Run `uv run astock context-plan` before opening research artifacts.
4. Use only implemented deterministic rules and frozen evidence.
5. Separate “worth further research” from “paper eligible”.

## Output

When candidate research is enabled, produce `CandidateRecord[]` with evidence IDs and quality status. During M1, produce a `RunManifest(status=NEEDS_INFO)` plus `ContextBudgetReport`; list an observation candidate only when deterministic evidence supports it.

## Prohibitions

- Do not turn price momentum or community popularity alone into a recommendation.
- Do not present an unavailable research stage as completed.
- Do not modify the paper ledger.
