# Workflow：持续投研、入场跟踪、持仓复核与模拟订单恢复

## When to use

把一次性的公司研究/荐股结果转化为可恢复的生命周期管理：持续观察行情、公告、新闻线索、Catalyst/KPI、复核期限和开放模拟订单，在变化真正影响投资判断时只重跑受影响研究模块，并把入场、持有、加减仓和退出条件保持为可审计状态。

## 前置边界

- 真实券商执行永久禁用；真实交易只能由用户在券商端人工执行。
- Continuous Monitor 只执行确定性采集、事件判断、typed rule 和已确认模拟订单回放；不会从自然语言猜交易阈值。
- 新闻/社媒只作线索。公司关键事实必须回到交易所、CNINFO、监管机构、发行人公告/财报等更强来源。
- 语义研究任务必须由可用 Research Agent 消费。若机器当前没有独立 Agent worker，任务持久排队，不能冒充已分析。

## Flow

1. **会话恢复**：同步 paper account → 读取 `continuous-monitor-status` → 优先处理当前持仓/目标标的的 material 未解决事件与 pending task。
2. **新单股研究**：`$company-deep-research` 完成正式链 → `$continuous-investment-monitor` 以 `ANALYZED` enroll → 仅把已有结构化证据支持的价格/回撤/复核条件写成 typed rule。
3. **荐股**：`$candidate-scan` 完成 seed → promotion → candidate → 单股深研/投委会；只有正式 WATCH / APPROVE_SIMULATION 集合以 `RECOMMENDED` enroll。
4. **常驻循环**：60m canonical 行情、CNINFO、GDELT lead、Catalyst、scheduled review、paper replay 按各自 cadence 执行；每 target/source 独立 cursor、backoff 和失败隔离。
5. **事件分流**：
   - 普通价格更新：只更新观察状态；
   - typed entry/drawdown/exit：创建最小研究复核任务；
   - 财报/业绩/重大公告：Evidence / Financial Integrity / Fundamental / BaseCase / Committee 按 affected modules 重跑；
   - 新闻 lead：先 `$evidence-investigation`，验证后才允许进入事实层；
   - Catalyst 到期/变化：只重跑 Catalyst 声明的 affected modules；
   - 开放 paper order：只对既有已确认订单运行 deterministic replay，fill 后再形成持仓变化事件。
6. **研究闭环**：Research Agent 消费 pending task → 复核证据和受影响模块 → 更新正式冻结研究/交易计划 → `continuous-monitor-reviewed` → ack 已处理事件。
7. **组合闭环**：持仓发生 fill、material thesis delta 或风险阈值变化时，调用 `$holding-monitor` / `$portfolio-manager`；组合层不能覆盖单股 REJECT/WATCH/NEEDS_INFO。

## Stop conditions

- 单一 provider 故障：该 target/source 有界退避，其余来源和标的继续。
- 官方事实无法验证：保留 NEEDS_INFO/不确定性，禁止用新闻替代。
- 60m 无法确定小时内成交路径：升级到 5m fallback；仍不确定则不伪造 fill。
- Agent worker 不可用：持续采集/规则/paper replay 继续，语义研究任务保持 PENDING，下一次 Agent 会话优先消费。
- daemon lease stale：新实例允许接管；未 stale 时第二 owner 必须拒绝。

## 验收

- 同一事件不会重复入库或重复生成研究任务。
- 单 source 失败不会阻塞同 cycle 其他 source/target。
- “某标的怎么样”正式研究后成为 ANALYZED watch target；后续即使未持仓仍跟踪 entry/catalyst/thesis invalidation。
- “请进行荐股”只把完成正式深研的推荐集合纳入 RECOMMENDED；seed/candidate 不直接买入。
- 已确认开放模拟订单会在 daemon 中持续回放；daemon 从不自行创造订单或绕过账本形成持仓。
- material event 只触发必要模块，不做无差别全文重研。
- 正常投资者回答不暴露内部运行术语。
- 全仓 pytest、ruff、pyright、diff-check 通过后才能上线。
