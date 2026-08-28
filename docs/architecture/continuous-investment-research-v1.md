# 持续投研与模拟交易生命周期架构 v1

> 状态：RELEASED（migration 0059 与 Continuous Monitor 已进入 `main`；External Dependency Resilience v0.1.0 已完成正式集成发布）
> 日期：2026-08-22；现行集成校正：2026-08-28
> 适用范围：AStockMultiAgent 当前主线
> 安全边界：仅自动化投研、监控、模拟账户与模拟调仓；`broker_execution_allowed=false` 永久保持，真实交易继续由用户在券商端人工执行。

## 1. 问题定义

当前系统已经具备候选发现、公司深研、机构级基本面、委员会、交易协议、持仓生命周期、Catalyst/KPI、模拟账本与离线回放，但运行语义仍以“用户发起一次投资会话”为主：

- 分析完成后没有可靠的常驻监控循环；
- 已分析但尚未建仓的标的缺少后续 entry timing 跟踪；
- 持仓只在下一次会话启动时补做 K 线和事实变化复核；
- `PositionMonitoringPlan`、`CatalystRecord` 已能表达“监控什么”，但没有持续执行者；
- 公告、新闻线索、价格异常、Catalyst 到期等变化没有统一事件队列；
- 增量研究没有稳定的事件触发协议，容易退化为下次会话再做一次全量研究；
- 模拟订单虽然有严格账本和回放，但缺少常驻调度来及时检查 entry/stop/take-profit/time-stop 条件。

本轮目标不是再增加一批人格 Agent，而是把现有能力补成“研究团队式生命周期”：

```text
研究/荐股/持仓
   ↓ 自动纳入 Watch Universe
持续数据采集（行情/公告/新闻线索/Catalyst/KPI）
   ↓
事件去重、严重度、PIT 与来源等级
   ↓
确定性触发器
   ├─ 只更新观察状态
   ├─ 触发局部研究模块
   ├─ 触发持仓/交易协议复核
   ├─ 触发组合风险复核
   └─ 触发模拟订单回放/候选模拟调仓
   ↓
新的冻结研究边界/Action Proposal
   ↓
持续记录、可恢复、可审计
```

## 2. 外部投研工程方案调研与取舍

### 2.1 QuantConnect LEAN

参考：
- https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview
- https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/key-concepts

可借鉴：Universe Selection → Alpha → Portfolio Construction → Risk Management → Execution 的职责隔离；持续风险管理不与信号生成耦合。

本项目取舍：保留“研究观点”和“模拟执行”分层；持续监控只能产生事件、研究任务、Action Proposal 或模拟订单请求，不能越过既有风险/确认/成交状态机直接改仓位。

### 2.2 Microsoft Qlib Online Serving

参考：
- https://qlib.readthedocs.io/en/latest/component/online.html

可借鉴：`OnlineManager.routine()` 按日/分钟运行、更新在线预测、准备任务/模型/信号，并保存 online history。

本项目取舍：采用“routine + history”的思想，但不引入 Qlib 训练栈。AStock 的 routine 是低成本事件循环，更新行情/公告/新闻线索/状态，并仅按 affected modules 触发研究。

### 2.3 vn.py / VeighNa

参考：
- https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py

可借鉴：定时事件 + 队列 + handler 的事件驱动模型，timer 与业务处理分离。

本项目取舍：不用新的消息中间件；以 SQLite append-only event index + 单实例 lease/heartbeat 实现可恢复事件总线，避免 Windows 单机引入 Redis/Kafka。

### 2.4 TradingAgents

参考：
- https://github.com/TauricResearch/TradingAgents

可借鉴：Fundamental / News / Technical / Risk / Portfolio 的职责覆盖和持续风险复核。

本项目取舍：不复制 Agent swarm。已有机构基本面、Serenity、Portfolio、Committee 继续作为“深研究层”；新守护进程只做便宜、确定性、可重复的监控与任务编排。新闻只作 lead，必须经过更强来源验证后才能改变事实层。

### 2.5 GDELT DOC 2.0

参考：
- https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/

可借鉴：免费、近实时、跨语言新闻检索，可作为无 API key 的 headline/URL 线索层。

本项目取舍：GDELT 只允许进入 `NEWS_LEAD`，authority 固定低于交易所/CNINFO/监管/发行人原文；新闻情绪本身不能直接触发 BUY/SELL，只能触发 `REVIEW`、证据调查或被交易协议明确允许的观察提醒。

### 2.6 Temporal 类 Durable Workflow

参考：
- https://docs.temporal.io/temporal

可借鉴：任务必须在进程崩溃、网络故障后从持久状态恢复，而不是依赖内存定时器。

本项目取舍：当前 Windows 单机和低成本约束下不部署 Temporal Server。用现有 SQLite + checkpoint + idempotency + lease 模拟必要的 durable semantics；未来多机后再评估外部 workflow engine。

## 3. 最终架构

### 3.1 两层运行时

**层 A：Continuous Monitor（常驻、确定性、低成本）**

职责：
1. Watch Universe 管理；
2. 行情 60m 常规更新，必要时 5m 精查；
3. CNINFO 官方公告增量轮询；
4. GDELT 新闻线索增量轮询；
5. Catalyst 窗口/KPI 状态复核；
6. 时间复核点、entry/exit typed trigger 评估；
7. 模拟账本/开放订单的低成本恢复调度；
8. 写 `MonitorEvent` 与 `MonitorResearchTask`；
9. daemon lease、heartbeat、backoff、crash recovery。

本层不调用大模型，不写真实券商订单。

**层 B：Research Agents（事件触发、按需、高价值）**

职责：
1. 消费 `MonitorResearchTask`；
2. 按 affected modules 做增量研究；
3. 新闻 lead 先寻权威原文或第二独立来源；
4. 更新 Catalyst/KPI、Evidence、BaseCase/Forecast/Valuation；
5. 生成新的 HoldingReview / TradePlan / Portfolio Risk；
6. 只有达到既有准入门槛后才允许 AI 发起模拟订单。

### 3.2 Watch Universe

目标来源：

- `ANALYZED`：用户问“某标的怎么样”后自动纳入；
- `RECOMMENDED`：荐股结果中进入正式深研/观察的标的自动纳入；
- `PAPER_POSITION`：模拟持仓强制纳入；
- `OPEN_PAPER_ORDER`：开放模拟订单强制纳入；
- `MANUAL`：用户显式添加；
- `CATALYST`：存在未关闭 Catalyst 的公司强制纳入。

Watch 不是 BUY。`RECOMMENDED/ANALYZED` 只表示系统继续追踪。

### 3.3 事件类型

最小稳定集合：

- `PRICE_BAR_UPDATED`
- `PRICE_TRIGGER`
- `DRAWDOWN_TRIGGER`
- `OFFICIAL_DISCLOSURE`
- `NEWS_LEAD`
- `CATALYST_DUE`
- `CATALYST_CHANGED`
- `SCHEDULED_REVIEW_DUE`
- `PAPER_REPLAY_DUE`
- `DATA_SOURCE_DEGRADED`
- `RESEARCH_TASK_CREATED`

事件必须含 `event_id / target_id / event_type / severity / observed_at / available_at / source / source_ref / payload_hash / dedupe_key / affected_modules / requires_research`。

### 3.4 typed price / risk trigger

现有 `TradeProtocol.entry_rule` 等仍是解释文本，不能让 daemon 猜数字。Continuous Monitor 新增机器可执行 `MonitorRule`：

- metric：`LAST_PRICE / RETURN_1D / RETURN_5D / DRAWDOWN_FROM_WATCH_HIGH / VOLUME_RATIO / DAYS_SINCE_REVIEW`；
- comparison：`GT / GE / LT / LE / EQ`；
- threshold；
- action：`OBSERVE / REVIEW / ENTER_PAPER_CANDIDATE / ADD_REVIEW / TRIM_REVIEW / EXIT_REVIEW`；
- cooldown；
- affected_modules。

Agent 可从正式研究生成 typed rule，但必须显式落盘；daemon 禁止从自然语言 `entry_rule/stop_rule` 自行解析阈值。

### 3.5 行情策略

- 常驻主频：60m；低成本、满足波段/价值投资；
- 盘中最近价/小时线用于 entry/stop 的粗触发；
- 当小时 OHLC 会导致“先止盈还是先止损/是否成交”的路径歧义时，才调 5m；
- 原始未复权价格用于模拟执行；
- 研究趋势可使用版本化派生序列；
- 失败不覆盖 canonical；双源冲突进入 degradation event。

### 3.6 公告和新闻

**官方公告 lane**：CNINFO `ALL` 分类，按 target 的 disclosure cursor 从上次成功时间到当前 UTC/上海日历窗口增量查询，announcement id 去重；标题、发布时间、官方 URL、raw snapshot 冻结。

**新闻 lane**：GDELT ArticleList，只抓 headline/url/domain/language/seendate 作为 lead。规则：

1. news lead 永远不能直接升级为事实；
2. 同 URL/标题 hash 去重；
3. 高影响关键词（监管调查、立案、处罚、重大合同、事故、停产、并购、业绩预告、回购、减持、诉讼等）提高 task severity，但仍须权威验证；
4. Agent 消费时优先找交易所/CNINFO/监管/发行人原文；找不到时明确 `UNVERIFIED_NEWS_LEAD`。

### 3.7 Catalyst 与基本面

- 未关闭 `CatalystRecord` 在 expected window 内按 cadence 检查；
- 到 `expected_to` 且 KPI 缺失/未达成时生成 due/missed 复核；
- 已有 `catalyst-monitor` 决定 affected modules；
- 财报/业绩预告/重大公告默认影响 `EVIDENCE/FUNDAMENTAL_MODEL/VALUATION`；
- 普通价格变化只影响 `MARKET_TRADE_CONTEXT/RISK`，禁止无意义重跑全文研究。

### 3.8 模拟交易与自动调仓边界

允许：

- 自动回放开放模拟订单；
- 自动发现 entry/stop/take-profit/time-stop typed trigger；
- AI 在已有规则允许、正式研究准入且本地配置 `auto_ai_paper_order_on_approved_entry=true` 时创建模拟订单请求；
- 对组合生成 target/proposal，并在模拟账户内按已有确认与成交状态机推进。

禁止：

- daemon 自行从自然语言推导价格；
- daemon 直接修改持仓；
- 新闻情绪直接 BUY/SELL；
- 任何真实券商连接/下单。

### 3.9 持久化与恢复

新增 migration 0059：

- `continuous_monitor_target`
- `continuous_monitor_rule`
- `continuous_monitor_event`
- `continuous_monitor_task`
- `continuous_monitor_run`
- `continuous_monitor_source_cursor`
- `continuous_monitor_daemon`

关键约束：

- event append-only；
- dedupe key 唯一；
- task 可重试但 task identity 幂等；
- 每 source/target 独立 cursor；
- daemon 单实例 lease + heartbeat；
- 进程崩溃后 stale lease 可被新实例接管；
- 单 target/source 失败不阻断其他 target；
- bounded retry + exponential backoff；
- 每轮完成写 run summary。

## 4. Skills 与工作流改造

新增 `$continuous-investment-monitor`：

- 在任何“某标的怎么样”正式输出后 enroll；
- 在荐股链输出后，将完成深研或正式 WATCH/APPROVE_SIMULATION 的标的 enroll；
- 投资类会话开始时先读 monitor status/unresolved events；
- material event 触发 `$company-deep-research` / `$financial-integrity-audit` / `$holding-monitor` / `$portfolio-manager` / `$evidence-investigation`；
- 输出给用户时只说投资影响，不泄露 daemon/SQLite/任务内部术语。

更新 `$astock-research-orchestrator`、`$holding-monitor`、`$candidate-scan`、`$paper-trading-recovery`，让常驻 monitor 成为默认增量来源，而不是“下一次会话才第一次发现变化”。

## 5. CLI 合同

稳定命令：

```text
continuous-monitor-schema
continuous-monitor-enroll SYMBOL --market XSHG --company-id ... --name ... --reason ANALYZED
continuous-monitor-remove TARGET_ID
continuous-monitor-rule-add REQUEST.json
continuous-monitor-cycle --live
continuous-monitor-daemon --live --interval-seconds N
continuous-monitor-start --live --interval-seconds N
continuous-monitor-stop
continuous-monitor-status
continuous-monitor-events [--unresolved-only]
continuous-monitor-ack EVENT_ID
continuous-monitor-tasks [--pending-only]
```

`start/stop/status` 负责 Windows 本机常驻进程生命周期；daemon 写 PID/heartbeat，但 SQLite lease 是事实源。

## 6. 默认资源预算

当前硬件/免费源下的 v1 默认：

- daemon wake-up：60 秒；
- 行情 due：交易时段每 15 分钟检查一次是否需要补齐 60m canonical；真正 provider 拉取由数据截止判断控制；
- CNINFO：每 15 分钟/target，盘后可放宽；
- 新闻 lead：每 30 分钟/target，最大 20 条/轮；
- Catalyst：每 60 分钟检查窗口，到期点强制检查；
- full fundamental refresh：不定时扫，仅由公告/Catalyst/定期 review 触发；
- 失败 backoff：60s → 5m → 15m → 30m，上限 30m；
- 每轮 target 上限：默认 50；避免低成本环境发生 provider 风暴。

## 7. 当前验收状态

### A. 数据与运行时（确定性实现已验收）

- [x] fresh SQLite migration 从 0001 到 0059 全通过且 checksum 守卫未放宽。
- [x] 同一事件重复抓取不产生第二条 `MonitorEvent`。
- [x] 一个 source 失败不会阻塞其他 source/target。
- [x] daemon stale lease 可被新 owner 接管，不能同时存在两个 active owner。
- [x] 每轮持久化 run summary、duration、target/source 成功失败计数。

### B. 行情与交易条件（确定性实现已验收）

- [x] recorded/integration 路径可更新 60m 数据并产生价格类 event；真实长期 cadence 另列运行观察。
- [x] typed entry/exit rule 只由确定性比较器触发；自然语言 rule 不被 daemon 猜测。
- [x] 路径歧义保留 5m fallback，不以小时 OHLC 冒充精确成交顺序。
- [x] daemon 不直接写 position；模拟成交仍由现有 Paper Ledger/replay 决定。

### C. 公告、新闻与 Catalyst（确定性实现已验收）

- [x] CNINFO announcement id 可增量识别、去重并绑定 raw snapshot。
- [x] 新闻 headline/url 只进入 `NEWS_LEAD`，不能直接成为正式 Evidence fact。
- [x] Catalyst 只返回 affected modules，不全链盲重跑。
- [x] material announcement/news task 能指向正确的 ResearchModule 集合。

### D. 产品行为

- [x] “某标的怎么样？”完成正式分析后可按 Skill/Workflow 合同进入 watch universe。
- [x] “请进行荐股”后只有完成深研或正式 WATCH/APPROVE_SIMULATION 的标的可进入 recommendation watch；Candidate 本身不能直接变成 BUY。
- [ ] 已分析未持仓标的在真实长时间 daemon 中持续检查 entry timing、Catalyst 与失效条件：`SKIPPED_MANUAL`，需自然运行证据。
- [ ] 已持仓标的在真实长时间 daemon 中持续形成 HOLD/ADD/TRIM/EXIT 增量复核输入：`SKIPPED_MANUAL`，需自然运行证据。
- [x] 正常投资者输出合同禁止泄露 daemon、migration、artifact/hash 等内部术语。

### E. 安全与工程门（已验收）

- [x] `broker_execution_allowed=false` 在新增 schema/服务/CLI 中无例外。
- [x] 新闻不能单源直接触发模拟买入/卖出。
- [x] 发布时全仓 pytest、Ruff、Pyright、diff check 与 state-integrity-audit 全部通过；历史数字保留在《验收报告》，不冒充本轮 External 冻结树结果。
- [x] unit/integration tests 覆盖 dedupe、lease、rule evaluator、source degradation、CNINFO/GDELT recorded fixture、restart recovery 与 CLI。

真实长时间 daemon、自然语言场景及 crash/source-degradation 连续观察仍是长期运行义务，不影响 0059 确定性软件架构已经发布，但不得描述为真实无人值守运行已完成。

## 8. 上线验收场景

### 场景 1：`xxx标的怎么样？`

1. 完成当前公司研究；
2. 给出价值区间/赔率、entry 条件、失效/退出条件；
3. enroll 为 `ANALYZED`；
4. daemon 后续抓行情/公告/news lead/Catalyst；
5. 发生 material delta 时建立局部研究任务；
6. 下次用户打开会话时直接消费最新增量，而不是从零重做。

### 场景 2：`请进行荐股`

1. deterministic seed/candidate funnel；
2. 候选逐个独立深研；
3. WATCH / APPROVE_SIMULATION 才能进入正式 recommendation set；
4. recommendation set enroll 为 `RECOMMENDED`；
5. daemon 持续观察 entry timing/公告/news/catalyst；
6. 满足 approved typed entry 后才允许进入模拟订单路径；
7. 建仓后 source reason 增加 `PAPER_POSITION`，继续监控至退出复盘。

## 9. 已实施范围与长期运行断点

已完成并发布：

1. 0059 状态层与 schema；
2. repository + deterministic evaluator；
3. CNINFO/GDELT source adapters；
4. cycle service + task routing + Catalyst integration；
5. daemon lease/start-stop/status；
6. CLI；
7. Skill/workflow/docs；
8. unit/integration tests；
9. code review；
10. 全仓工程门。

仍需真实自然运行积累、不得冒充已完成：

1. 长时间 daemon heartbeat、source degradation 与 crash recovery 观察；
2. “某标的怎么样？”和“请进行荐股”两个真实自然语言场景的持续事件/任务闭环；
3. 持仓与未持仓标的跨日 entry/exit/Catalyst 增量研究证据。

## 10. External Dependency Resilience 现行集成校正

- Continuous Monitor 的 market/CNINFO/GDELT lane 复用现有 ProviderFactory、SourceAccessRouter、capability health/breaker、ObjectStore 与 Evidence；不得自建第二套 provider 状态或事件外证据库。
- 单 source/capability 故障只产生结构化 `DATA_SOURCE_DEGRADED` 与 bounded backoff，不阻塞其他 source/target。OPEN 或有效 HALF_OPEN claim 时不重复撞击同一失败 capability；stale claim 可恢复。
- CNINFO known-item/disclosure discovery 可经正式官方 exact-item 路恢复；公告“没有发生”的 negative proof 仍必须依赖 exhaustive pagination，Search/Web 未命中没有该权限。
- 5m 仅在 paper path ambiguity 时按需使用。单一备用源成功不能覆盖已有双源 canonical，也不能伪造成交；所有 Paper Ledger 与 `broker_execution_allowed=false` 边界保持不变。
- External Dependency Resilience v0.1.0 已通过冻结树、实现提交 `c764e842d3eb1922bc206b7f3cffdd9759c8f1cc`、annotated tag 与 GitHub Release 远端门；本节集成校正随 release-state 文档提交进入 `main`，历史 Continuous Monitor 发布身份不变。
