---
name: holding-monitor
description: Review what changed for open paper positions or user-declared real monitoring positions. Use when the user asks which holdings need attention, whether a thesis strengthened or failed, whether to add, trim, exit, review stops, or compare current evidence with the last frozen review.
---

# 持仓监控

1. Run `uv run astock paper-status` for paper positions; treat real positions as read-only user input, then load `position-plan-status <position_id>`.
2. Synchronize raw 5m data and inspect `quality-report` before using price triggers.
3. Gather only evidence after the latest review boundary and create a strict `HoldingReviewRequest`; distinguish no new evidence from refutation.
4. Run `uv run astock holding-review-run <request.json>` and then `holding-review-audit <position_id>`; never construct an action outside the registered lifecycle rules.
5. Run `uv run astock context-plan --artifact-id <review_artifact_id>` and initialize `codex-run-init <request> --artifact-id <review_artifact_id> --require-registered-output`.
6. Import the exact registered `HoldingReviewPack` or `PositionActionProposal` through `codex-run-import`, then require `codex-run-audit` PASS.

## Output

Produce `HoldingReviewPack`. Any `PositionActionProposal` is advisory and must set `requires_user_confirmation=true`.

## Prohibitions

- Do not repeat full company research unless a material-change rule triggers it.
- Do not infer an author's holding rule from selection-only content.
- Do not submit a real order or write the paper ledger directly.
