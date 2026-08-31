---
name: holding-monitor
description: Persist complete user-declared external trades, restore holding positions across sessions, and make evidence- and portfolio-aware HOLD/ADD/TRIM/EXIT decisions with target bands and event-driven incremental research. Use for any current-holding question or automatically alongside another investment task when the local portfolio is non-empty.
---

# 持仓事实、增量复核与再平衡

1. **先恢复而不是重新询问**。若 paper account 存在先 `local-portfolio-sync-paper`，随后读取 `local-portfolio-status`、`portfolio-local-snapshot` 和 `continuous-monitor-status`。`trades.md` 记录外部既成交易与 paper fills 的来源事实，`portfolio.md` 由它确定性回放；不要依赖聊天记忆恢复购买时间、数量或成本。
2. 当用户第一次明确陈述一笔**已经发生的外部真实交易**时，若市场/证券身份、BUY/SELL、数量、价格和实际成交时间完整且无歧义，同一轮生成 typed capture 并调用 `portfolio-import-declared-trade`。完整事实 exactly-once 落入本地 trade lane；重复陈述必须去重。缺字段时只询问真正缺失的成交事实，不得用当前价、提问时间或模型猜测补齐。
3. 外部既成交易不得写入 SQLite paper ledger；paper position 仍只来自确认订单后的 fill。若同一经济交易同时出现在 external import 与 `PAPER_FILL`，必须冲突阻断，不能双计。
4. 每次投资类 Agent 任务都对已有持仓做**增量**复核。先消费 material unresolved monitor event/task，从 `last_review_at` 到当前检查新增官方披露、财务/KPI、估值、竞争格局、政策/治理、催化剂、执行状态和组合暴露；没有 material delta 是有效结果，不为显得活跃重复整份公司研究。
5. 新闻/社媒只允许进入 `UNVERIFIED_LEAD`。在交易所、CNINFO、监管机构或发行人正式来源验证之前，只能触发 REVIEW/证据调查，不能直接产生 ADD/TRIM/EXIT。
6. 事件语义固定为：`THESIS_INVALIDATING / THESIS_WEAKENING / THESIS_STRENGTHENING / VALUATION_ONLY / PORTFOLIO_RISK_ONLY / TEMPORARY_NOISE / UNVERIFIED_LEAD`。前四类 material 语义必须绑定本轮可见正式证据；调用方单独写一个 severity 没有交易权威。
7. 单股主动作仍恰好一个：`HOLD / ADD / TRIM / EXIT`。`ADD` 必须有新增支持证据；冲突/失效证据优先 REVIEW；`THESIS_INVALIDATING` 在正式证据成立时可进入 EXIT；`PORTFOLIO_RISK_ONLY` 可在公司逻辑未变时提出 TRIM。
8. 每个 material 动作同时检查整个组合：风险贡献、单股/行业集中、相关性、beta/因子、回撤、流动性、现金和替代机会。公司逻辑增强但组合已过度集中时不能机械 ADD；公司逻辑未变但风险贡献超预算时可以 TRIM。需要组合迁移时调用 `$portfolio-manager`。
9. 成本价只用于真实盈亏、执行成本、税费/行为锚定提示，不替代价值与赔率判断。不得因为跌破成本价自动 ADD，也不得因为“回本”自动 EXIT。
10. 对 material action 使用 target band，而不是点目标：当前权重、目标下限/中枢/上限、目标数量区间、成本、执行前提和 reversal conditions 一起进入 formal review。处于 no-trade band 的小偏离保持 HOLD，降低无意义换手。
11. 正式持仓链继续使用 `holding-review-run` / `holding-review-audit`、需要时的 Committee/Codex registered output，并将 event severity、portfolio effect 与 target band 写入 `HoldingReviewPack / PositionActionProposal`。普通未变化 HOLD 不需要重跑全套公司研究。
12. 完成复核后使用 `local-portfolio-review` 保存 action/thesis/note 作为下一次增量边界。若随后产生模拟订单，`portfolio.md` 只能在 replay 形成 fill 后通过 sync 更新；proposal/order 都不是 position。

## Workflows

- [`docs/workflows/workflow-holding-rebalance-decision.md`](../../../docs/workflows/workflow-holding-rebalance-decision.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)
- [`docs/workflows/workflow-portfolio-transition-and-hedging.md`](../../../docs/workflows/workflow-portfolio-transition-and-hedging.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)

## Formal command compatibility

- Material reviews continue through `uv run astock holding-review-run` / `uv run astock holding-review-audit`, with `committee-input-resolve / committee-plan / committee-decide` when Committee validation is required.
- Durable formal outputs continue to use `uv run astock codex-run-init --require-registered-output` and `uv run astock codex-run-audit`; the new target-band/event fields do not bypass this binding.

## Output

默认每个持仓一行：**标的 — HOLD/ADD/TRIM/EXIT — 最主要单股原因 — 最主要组合原因（若有）— 目标区间/数量条件 — 什么会改变动作**。若没有 material delta，明确“核心判断未改变”。不要把内部 event code、artifact、CLI 或数据库状态写进投资者回复。

## Prohibitions

- Do not ask again for a purchase time/price/quantity already persisted in local user state.
- Do not invent missing external trade facts or silently duplicate a paper fill.
- Do not let an unverified news lead directly create ADD/TRIM/EXIT.
- Do not use average cost as a valuation signal.
- Do not rerun full company research when only incremental facts changed.
- Do not treat a proposal or unfilled order as a position change.
- Do not create or send a real brokerage order.
