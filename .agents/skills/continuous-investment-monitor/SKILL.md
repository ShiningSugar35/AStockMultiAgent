---
name: continuous-investment-monitor
description: Keep analyzed, recommended, held, catalyst-bound, and open-paper-order A-share targets under durable continuous monitoring. Use after formal stock research/recommendation, before answering a watched target, and when monitor events or research tasks need incremental follow-up.
---

# 持续投研监控

1. 任何正式“某标的怎么样 / 能不能买 / 什么时候买卖”的研究完成后，使用 `uv run astock continuous-monitor-enroll <symbol> --market <market> --company-id <company_id> --name <name> --reason ANALYZED` 纳入持续观察。Watch 不等于 BUY。
2. 荐股链只有已经完成独立深研并进入正式 WATCH 或 APPROVE_SIMULATION 集合的标的，才以 `RECOMMENDED` 纳入；ResearchSeed/Candidate 本身没有推荐权，也不得直接进入模拟下单。
3. 每次投资类会话开始，在同步本地模拟账户后运行 `uv run astock continuous-monitor-status`。如果存在 pending research task，用稳定的 owner id 调用 `continuous-monitor-task-claim --owner-id <owner>` 获取带 lease 的任务；同一任务不得被并发 Agent 重复消费。当前标的若有 material delta，优先消费增量变化，再决定是否需要完整重研；不要默认从零重跑全部链路。
4. Monitor task 的 `requested_modules` 是最小增量研究边界。新闻事件始终只是 lead：先由 `$evidence-investigation` 回到交易所、CNINFO、监管机构或发行人原文；无法验证时保留不确定性，不得把新闻情绪升级成事实或交易信号。
5. 正式研究得到可机器执行的入场、减仓、退出、回撤或复核阈值时，只能把已经结构化且有证据绑定的数字写成 `continuous-monitor-rule-add` 请求；禁止从自然语言 entry/stop/target 文本猜阈值。
6. 价格触发只产生复核/模拟候选动作，不直接改变持仓。AI 主动模拟下单仍必须同时满足正式研究准入、typed entry 已实际满足、本地 `auto_ai_paper_order_on_approved_entry=true` 和既有订单确认规则；成交仍由 paper replay 决定。
7. 开放模拟订单和模拟持仓由 daemon 自动纳入。开放订单的持续回放只能匹配已经确认且有正式规则绑定的订单；daemon 不创建真实券商订单，`broker_execution_allowed=false` 永久保持。
8. 完成一次增量研究后，先 `continuous-monitor-reviewed <target_id>` 更新复核边界，再用 `continuous-monitor-task-complete <task_id> --owner-id <owner>` 完成任务并确认对应事件；若研究失败则 `continuous-monitor-task-fail` 留下可审计失败，不得伪装完成。正常投资者回复只说“什么变了、影响是什么、现在应继续观察/准备买入/持有/减仓/退出的条件”，不得泄露 daemon、SQLite、migration、task id、artifact/hash 等内部术语。
9. 当常驻 daemon 正常但没有可用的独立 LLM/Agent worker 时，确定性行情、公告/news lead、Catalyst、规则和 paper replay 仍持续运行；需要语义判断的 research task 保存在持久队列，下一次可用 Agent 会话必须优先消费。不得把“已排队”冒充“已完成分析”。

## Output

Default investor-facing output is incremental and compact: **最新结论 → 发生了什么变化 → 对入场/持有/加减仓/退出条件的影响 → 下一观察条件**。如果没有 material delta，就明确“核心判断未改变”，不要为了显得活跃而重复整份公司研究。内部 task/event/daemon 状态只用于审计，不进入普通投资者回答。

## Workflows

- [`docs/workflows/workflow-continuous-investment-monitoring.md`](../../../docs/workflows/workflow-continuous-investment-monitoring.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-candidate-discovery.md`](../../../docs/workflows/workflow-candidate-discovery.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)

## Prohibitions

- Do not convert news sentiment directly into BUY/SELL or a paper fill.
- Do not parse natural-language trade rules inside the daemon.
- Do not treat a queued research task as completed research.
- Do not bypass PIT, evidence, trading classification, paper confirmation, or ledger mechanics.
- Do not connect to or send an order to a real broker.
