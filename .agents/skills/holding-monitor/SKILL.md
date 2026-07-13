---
name: holding-monitor
description: Review what changed for open paper positions or user-declared real monitoring positions. Use when the user asks which holdings need attention, whether a thesis strengthened or failed, whether to add, trim, exit, review stops, or compare current evidence with the last frozen review.
---

# 持仓监控

1. Run `uv run astock paper-status` for paper positions; treat real positions as read-only user input.
2. Synchronize raw 5m data and inspect `quality-report` before using price triggers.
3. Load the position's `PositionMonitoringPlan` and last frozen evidence snapshot.
4. Gather only evidence added since the last review.
5. Distinguish no new evidence from refutation.
6. Write a `HoldingReviewPack` and import it through the Codex run service.

## Output

Produce `HoldingReviewPack`. Any `PositionActionProposal` is advisory and must set `requires_user_confirmation=true`.

## Prohibitions

- Do not repeat full company research unless a material-change rule triggers it.
- Do not infer an author's holding rule from selection-only content.
- Do not submit a real order or write the paper ledger directly.
