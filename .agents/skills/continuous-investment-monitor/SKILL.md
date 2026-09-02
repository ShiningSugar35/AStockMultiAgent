---
name: continuous-investment-monitor
description: Keep analyzed, recommended, held, catalyst-bound, and open-paper-order A-share targets under durable continuous monitoring. Use after formal research/recommendation, before answering a watched target, and when events require evidence-backed holding or portfolio re-evaluation.
---

# 持续投研监控

1. 任何正式“某标的怎么样 / 能不能买 / 什么时候买卖”的研究完成后，使用 `continuous-monitor-enroll` 纳入持续观察。荐股链只有完成正式深研并进入 WATCH/APPROVE_SIMULATION 的标的才以 `RECOMMENDED` 纳入；ResearchSeed/Candidate 没有推荐权。
2. 每次投资类会话开始，在同步 paper/local user state 后先读 `continuous-monitor-status`。持仓必须先恢复 `portfolio-local-snapshot`；daemon 排队的任务不等于已完成研究。
3. 低成本 daemon 只负责确定性变化：行情、官方公告 lead、新闻 lead、Catalyst/KPI、typed 价格/时间规则和已确认 open paper order replay。它不生成投资结论、组合权重、目标价或真实订单。
4. `requested_modules` 是最小增量边界。普通价格变化优先只复核 market/risk；财报、治理、政策、产能、诉讼、监管等 material event 才扩大到受影响模块。不要默认每次从零重跑整家公司。
5. 新闻/社媒始终是 `UNVERIFIED_LEAD`：先由 `$evidence-investigation` 回到交易所、CNINFO、监管机构或发行人原文。无法验证时保留 REVIEW，不得将新闻情绪转换成 ADD/TRIM/EXIT 或 portfolio hedge。
6. 语义 Agent 在取得正式证据后，将事件裁决为 `THESIS_INVALIDATING / THESIS_WEAKENING / THESIS_STRENGTHENING / VALUATION_ONLY / PORTFOLIO_RISK_ONLY / TEMPORARY_NOISE`。severity 必须与本轮 evidence lineage 一起进入 `$holding-monitor`，不能由 daemon 或调用方单独声明后直接交易。
7. material held-name delta 除单股复核外必须检查组合层影响：风险贡献、集中度、相关性、beta/因子、行业/周期共振、流动性和现金。若影响组合，触发 `$portfolio-manager` 的增量 transition；同一事件影响多个持仓时合并成一次组合级重算，避免 N 次重复矩阵计算。
8. 正式研究得到机器可执行的价格、减仓、退出、回撤或复核阈值时，只能把已结构化且证据绑定的数字写成 monitor rule；禁止从自然语言 entry/stop/target 文本猜阈值。
9. 价格触发只产生复核/模拟候选动作，不直接改变持仓。模拟订单仍需正式研究准入、typed 条件满足、本地设置和用户确认；fill 只由 replay 决定。ETF paper order/replay 只在独立 `ETFInstrumentExecutionRule` 对目标证券和当前日期有效、`execution_enabled=true`、费用/lot/tick/limit/settlement 已冻结并完成既有确认链时才可进入；仓库默认关闭，monitor 不得绕过这一边界。
10. 完成增量研究后更新 reviewed boundary，再完成/失败对应持久任务；失败必须保留为可审计失败。正常投资者输出只说“什么变了、影响是什么、现在 HOLD/ADD/TRIM/EXIT 或组合条件如何改变”。
11. 没有可用独立 Agent worker 时，daemon 可以继续更新确定性事件和 replay，但语义 task 必须留在持久队列，下一次 Agent 会话优先消费；不得把“已排队”冒充“已分析”。

## Workflows

- [`docs/workflows/workflow-holding-rebalance-decision.md`](../../../docs/workflows/workflow-holding-rebalance-decision.md)
- [`docs/workflows/workflow-portfolio-transition-and-hedging.md`](../../../docs/workflows/workflow-portfolio-transition-and-hedging.md)
- [`docs/workflows/workflow-continuous-investment-monitoring.md`](../../../docs/workflows/workflow-continuous-investment-monitoring.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)

## Stable task commands

Use `uv run astock continuous-monitor-task-claim --owner-id <owner>` before consuming a semantic task, then `uv run astock continuous-monitor-task-complete` or `continuous-monitor-task-fail`; update the review boundary with `continuous-monitor-reviewed`. `broker_execution_allowed=false` remains permanent.

## Output

默认只输出增量：**最新结论 → 发生了什么变化 → 对入场/持有/加减仓/组合风险的影响 → 下一观察条件**。没有 material delta 时明确“核心判断未改变”。内部 task/event/daemon/artifact 仅用于审计。

## Prohibitions

- Do not convert news sentiment directly into BUY/SELL/ADD/TRIM/EXIT or a paper fill.
- Do not let the daemon classify semantic thesis severity without evidence-backed Agent review.
- Do not parse natural-language trade rules inside the daemon.
- Do not treat a queued research task as completed research.
- Do not trigger one full portfolio recomputation per holding for the same event when a batched recomputation suffices.
- Do not bypass PIT, evidence, target-band, paper confirmation, ETF-execution or ledger mechanics.
- Do not connect to or send an order to a real broker.
