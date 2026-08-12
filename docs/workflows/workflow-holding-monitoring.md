# Workflow — Holding Monitoring

## When to use

Use for an open paper position or a user-declared real position when the question is “最近发生了什么”“逻辑有没有变”“要不要加、减、退出”“哪些持仓最需要关注”。

Primary skill: `$holding-monitor`; use `$portfolio-manager` for aggregate portfolio questions.

## Flow

1. **Load the position boundary**
   - For paper positions run `paper-status` and load `position-plan-status <position_id>`.
   - Treat a real brokerage position as read-only user input; the system never connects to the real broker.

2. **Use only incremental evidence**
   - Start after the latest frozen review boundary.
   - Gather new announcements, financial changes, catalysts and validated market data only as needed.
   - Distinguish “no new evidence” from “evidence refutes the thesis”.

3. **Refresh price/quality inputs when trigger rules need them**
   - Synchronize missing canonical market data and inspect quality before applying price/volume triggers.
   - Never replace missing bars with synthetic observations.

4. **Run the lifecycle review**
   - Build a strict `HoldingReviewRequest` and run `holding-review-run`.
   - Run `holding-review-audit <position_id>`.
   - Lifecycle action proposals remain within configured priority/invalidation rules.

5. **Escalate material thesis changes to Committee**
   - Resolve the exact BaseCase/memo/financial/plan/update/review/proposal artifacts.
   - Run committee plan/decision/audit only when the change is material enough to require a formal position verdict.

6. **Avoid full reruns by default**
   - Catalyst/KPI changes should rerun affected modules only.
   - A full company research rerun is reserved for material-change or invalidation conditions.

7. **Route portfolio interactions correctly**
   - Concentration, correlation, beta, drawdown and overall balance belong to [Portfolio Construction](workflow-portfolio-construction.md), not per-position intuition.

8. **Action remains advisory**
   - Any add/trim/exit proposal needs the applicable formal protocol and user confirmation before a paper operation.
   - Real trades remain user-executed outside the system.

## Output

Show: what changed since last review, thesis strengthened/weakened/invalidated, catalyst/KPI status, highest-priority risk, current formal action state, and next review trigger. Avoid repeating the original full research memo when nothing material changed.

## Stop conditions

- No new evidence is a valid “no material change” result.
- Selection-only author content cannot be inferred into a holding/exit rule.
- No direct SQLite, paper-ledger or real-broker mutation from the review itself.
