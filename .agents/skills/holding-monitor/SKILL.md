---
name: holding-monitor
description: Review what changed for open paper positions or user-declared real monitoring positions. Use when the user asks which holdings need attention, whether a thesis strengthened or failed, whether to add, trim, exit, review stops, or compare current evidence with the last frozen review.
---

# 持仓监控

1. Run `uv run astock paper-status` for paper positions; treat real positions as read-only user input, then load `position-plan-status <position_id>`.
2. Synchronize raw 5m data and inspect `quality-report` before using price triggers.
3. Gather only evidence after the latest review boundary and create a strict `HoldingReviewRequest`; distinguish no new evidence from refutation.
4. Run `uv run astock holding-review-run <request.json>` and then `holding-review-audit <position_id>`; never construct an action outside the registered lifecycle rules.
5. Resolve the frozen BaseCase/memo/financial/plan/update/review/proposal artifacts with `uv run astock committee-input-resolve --artifact-id <id>...`; run `committee-plan <request.json>`, then `committee-decide <request.json>` and `committee-audit <decision_id>`.
6. Run `uv run astock context-plan --artifact-id <DecisionPack_artifact_id>` and initialize `codex-run-init <request> --artifact-id <DecisionPack_artifact_id> --require-registered-output`.
7. Import the exact registered `DecisionPack` or final `ClassifiedTradeProtocol` through `codex-run-import`, then require `codex-run-audit` PASS.
8. If the user asks about aggregate concentration, correlation, beta, drawdown, or whether the whole set of holdings is balanced, route the frozen position set to `$portfolio-manager`; do not infer portfolio risk by inspecting holdings one at a time.

## Output

Produce `HoldingReviewPack`, a committee `DecisionPack`, and one TradeProtocol. Any `PositionActionProposal` and TradeProtocol remain advisory and require user confirmation; the service never writes an order or ledger entry.

## Prohibitions

- Do not repeat full company research unless a material-change rule triggers it.
- Do not infer an author's holding rule from selection-only content.
- Do not submit a real order or write the paper ledger directly.
