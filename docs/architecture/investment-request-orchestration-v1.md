# Investment Request Orchestration v1

> 状态：CURRENT
> 实现状态：IMPLEMENTED_ENABLED（`full-research-recommendation-v1`，package v0.4.0 release candidate）。所有可直接形成证券选择、买卖判断、价格、数量或仓位的请求统一进入 `FULL_RESEARCH_RECOMMENDATION`；正式投资建议只允许由不可变 `RecommendationResearchReceipt + Publication Gate` 发布。`FullResearchInputReadinessReport` 仅证明上游输入完整，不具有正式荐股权。
> 当前恢复合同：账户读取只消费 canonical external-account 事件与 paper ledger；0068 revision 与同一只读事务冻结状态，禁止按表名推测事实。快照跨账户拒绝、变更失效、历史不可得阻断、并发复用及原子回滚继续由机器测试约束。生产启用状态不由“实现完成”自动改变。
> 更新日期：2026-09-11
> 关联 ADR：`docs/adr/0001-documentation-as-code-with-machine-contracts.md`、`docs/adr/0002-market-regime-as-risk-overlay.md`
> 关联验收：`docs/acceptance/business-question-capability-matrix-v1.md`

## 1. 问题定义

当前仓库已经具备公司研究、宏观政策、行业价值链、财务审计、治理、催化剂、全市场候选、组合风险、外部真实账户、模拟交易、持续监控和公共回复等能力；但它们主要以独立 Service、CLI、Skill 与 Workflow 存在。自然语言投资问题仍缺少一个机器强制的统一入口来保证：

1. 每次投资请求都恢复真实账户、模拟账户、订单、持仓和重大监控增量；
2. 每类问题都运行所有**与该问题实质相关**的能力，而不是只命中一个 Skill；
3. 能力之间按证据依赖串联，可独立的工作并行；
4. 写入类意图与只读研究严格隔离；
5. 最终回复只使用已验证结果，并遵守空持仓静默规则；
6. 系统能证明自己没有漏掉必需能力，而不是依赖 Agent 自述“已经综合分析”。

“尽可能多地调用能力”在本设计中解释为：**最大化相关覆盖，不进行无关、重复或会污染状态的调用**。盲目运行全部命令会增加延迟、成本和故障面，也可能把只读问题错误升级为写入。

## 2. 当前能力与缺口

### 2.1 已有基础

- `ExternalAccountEvent`：真实账户按 `account_id` 隔离的 append-only 交易、现金、转托管和更正事实；
- SQLite paper ledger：模拟资金、订单、成交、T+1 和恢复；
- `UserPortfolioSnapshot`、组合风险/构建/迁移/ETF/对冲；
- Current Research acquisition/continuation、Research Team、Committee、Financial Integrity；
- Continuous Monitor 的持久任务和事件增量；
- `ResponseGateway` 与投资者输出审计；
- Repo Skills 与跨 Skill Workflow。

### 2.2 已关闭的历史缺口

本节不再列未来 TODO。以下能力均已成为当前机器合同：

- `InvestorSessionPreflightReceipt` 是投资请求进入公共回答前的强制恢复工件；
- 多账户 actual、paper 与兼容投影按 lane 冻结并可复用，不以表名猜事实；
- 常规 preflight 不执行全库 SQLite integrity check，完整体检只在显式诊断/发布门执行；
- `CapabilityExecutionPlan + CapabilityCoverageReceipt` 提供机器可读的 REQUIRED/CONDITIONAL/PROHIBITED 合同与覆盖回执；
- `ResearchSubjectRegistry` 统一登记历史研究、推荐、监控与持仓相关主体；
- 投资者公共输出必须经过 `ResponseGateway` 与输出审计；
- 市场状态作为共享输入进入相关投资研究；
- material investment decision 进一步强制进入 `FULL_RESEARCH_RECOMMENDATION`，上游 capability coverage 不能替代最终 Full Research Publication Gate；进入 Full Research 前本金与目标年化按当前明确值 → 最近已注册用户请求/持仓及荐股研究记录 → 100000 元 / 100% 默认值分别解析并冻结。目标年化只约束收益路径、盈亏预算和更严格的可接受入场价，不授予更高风险权限。

## 3. 当前目标架构

```text
User message
  → InvestorRequestEnvelope
  → InvestorSessionPreflightService
       ├─ external accounts / projections
       ├─ paper ledger incremental snapshot
       ├─ compatibility projection reconciliation
       ├─ monitor material deltas
       └─ latest valid market/regime snapshot identity
  → Intent & Entity Resolution
       └─ material investment decision → FULL_RESEARCH_RECOMMENDATION
  → frozen investment expectation (registered request contract; no account writes)
  → CapabilityPolicyPlanner / CapabilityExecutor
       ├─ REQUIRED / CONDITIONAL / PROHIBITED
       ├─ deterministic services + specialist Agents
       ├─ evidence recovery / checkpoints / reuse
       └─ CapabilityCoverageReceipt
  → FullResearchInputReadiness (upstream only)
  → FullResearchReceiptAssembler
       ├─ Request Contract + principal/target-return expectation (current → history → default)
       ├─ one Point-in-Time source/evidence graph
       ├─ Financial/Governance CriticalVeto
       ├─ multi-model valuation + factor/ranking
       ├─ portfolio/risk/execution + quote freshness
       └─ independent challenger + candidate narratives
  → Mandatory Research DAG
  → immutable RecommendationResearchReceipt
  → Publication Gate
       ├─ PUBLISH / CONDITIONAL_ONLY / BLOCKED
       └─ broker_execution_allowed=false
  → ResponseGateway + public output audit
  → recommendation tracking / append-only reevaluation
```

非 material 的事实查询、监控、账户事实写入与 paper operation 继续使用各自独立 side-effect lane；它们不能借 Full Research 获得额外写入权限。`EA_PROVISIONAL` 仅可冻结不完整持仓声明，真实 `EA_WRITE` 必须独立走 `ACCOUNT_FACT_WRITE`。已有持仓的处置请求仍统一进入 `FULL_RESEARCH_RECOMMENDATION`，但会标记 `EXISTING_HOLDING`：`HOLDING_REVIEW` 成为最终 gate 的必需依赖，其 `HoldingReviewPack` 被投影为绑定 monitoring plan、preflight portfolio revision、artifact/hash 与 Evidence lineage 的不可变 holding decision snapshot；即使没有新增 BUY，最终 receipt 与公共投影也必须保留 HOLD/ADD/TRIM/EXIT/REVIEW 动作，不能退化成空动作的“保留现金”。其中 `ADD` 仍属于新增买入，只能在同标的 candidate admission 仍为 eligible 且 CriticalVeto 未触发时保留；TRIM/EXIT/HOLD/REVIEW 不因该买入准入门被静默删除。路由不得用裸 `持有` 或裸 `多少股` 触发 Full Research，而应识别“现有持仓 + 处置动作”或“买入/配置 + 多少股”等决策语义，避免把股东持股数量等事实查询误路由为投资决策。

只有 Developer Mode 可以显示内部计划、状态码、工件和覆盖回执；Investor Mode 只显示投资判断、证据时间、关键理由、风险、动作和改变判断的条件。

## 4. 当前机器合同

### 4.1 `InvestorRequestEnvelope`

最小字段：

- request id、question time、user timezone、market timezone、locale、mode；
- relative-date resolution、civil-date basis 与时间精度（exact/date-only/month-only/unknown）；
- 原始文本与规范化意图；
- 证券/公司/行业/政策/账户实体及解析置信度；
- 用户明确给出的数量、价格、时间、账户和操作；
- read-only / state-write / paper-operation 分类；
- requested horizon、risk intent、排除条件；
- source conversation id 与幂等键。

实体歧义会影响账本、交易或正式投资结论时必须先解决；只影响补充说明时可降级并明确不确定性。

### 4.2 `InvestorSessionPreflightReceipt`

每次投资请求必须生成且被最终答复引用。字段至少包括：

- `as_of` 与 snapshot revision；
- external account identities、各账户 projection hash 与审计状态；
- paper account snapshot hash、账本 revision、positions/open orders/pending settlements；
- actual/paper economic-duplicate 检查；
- legacy projection reconciliation 状态；
- held/researched/recommended symbols；
- unresolved material monitor events/tasks；
- latest valid market/regime snapshot identity、age 与 availability；无合法快照时显式为 `null/UNKNOWN`，不得伪造 current state；
- freshness、degradation 和失败原因；
- `empty_holdings` 与 `material_holding_change_present`；
- receipt object hash。

无合法 receipt 时，系统可继续做通用知识解释，但不得输出基于“当前组合/持仓”的精确动作或正式个性化仓位。合法 preflight receipt 不等于所有后续研究输入都已就绪：纯账户事实写入可以在没有 current regime 的情况下完成；需要买卖、持仓、组合、荐股或宏观判断时，再由 capability planner 把 current regime 列为 `REQUIRED`，缺失则对应决策降级或阻断。

### 4.3 `UnifiedPortfolioContext`

真实账户与模拟账户保持分 lane，不合并成一份伪账本：

```text
actual_accounts[]
  account_id → positions / known cash / unknown cash / event revision
paper_accounts[]
  account_id → positions / open orders / frozen cash / settlements / ledger revision
aggregate_research_view
  economic exposure only, with source lane retained
```

聚合研究视图必须：

- 保留每个账户来源；
- 对 actual trade 与 paper fill 的相似经济暴露做跨 lane 提示；不得据此跨 lane 去重、删除、抵消或把刻意镜像的实际/模拟持仓合并为一笔事实；
- 不把 open order 当持仓；
- 不把 unknown cash 当 0；
- 不把转托管当买卖或收益；
- 允许分别输出实际、模拟和合并风险，但不能互相写回。

### 4.4 `ResearchSubjectRegistry`

用于问题 90 和持续研究。每次成功解析证券/公司时追加事件，而不是覆盖历史：

- `MENTIONED`：用户或系统在正式回答中提及；
- `RESEARCHED`：形成可审计研究工件；
- `RECOMMENDED`：正式建议进入候选；
- `HELD_ACTUAL` / `HELD_PAPER`；
- `REJECTED` / `EXITED`；
- `MONITOR_ENROLLED` / `MONITOR_REMOVED`。

事件包含 request id、available time、instrument identity、研究/建议工件、原因与 source lane。单纯搜索噪声或未解析名称不得自动登记为已研究。

### 4.5 `CapabilityExecutionPlan`

每项能力的状态只能是：

- `REQUIRED`：缺失就不能完成该类答复；
- `CONDITIONAL`：满足具名触发条件才运行；
- `OPTIONAL`：只改善解释，不影响结论；
- `PROHIBITED`：本意图中不得运行，例如只读研究不得写账本。

计划字段包括 capability id、依赖、freshness、输入工件、输出 Schema、预算、并行组、重试/备用路径、停止条件和 side-effect class。

### 4.6 `CapabilityCoverageReceipt`

每个最终答复必须有内部覆盖回执：

- required set / completed set / reused set；
- conditional set、是否触发及理由；
- failed/degraded/skipped set；
- 输出工件与 lineage；
- unresolved conflicts；
- 对最终结论的影响；
- `coverage_complete`；
- policy version。

Agent 文字中说“综合宏观、行业、财务”不算覆盖证据；必须有对应 typed output 或明确的已验证复用。

### 4.7 已落地的认证与只读发布接缝

`output_validation.py` 只读取现有 `StateStore.artifact_registry` 与内容寻址 ObjectStore，不建立第二份事实库。能力完成要求真实注册的预期类型、对象哈希、可得时间和适用账户/实体；任意非空字符串、内联字典、错误类型、损坏对象或未知输出合同均不能认证完成。请求、计划和 preflight 身份必须一致，必需节点、依赖及 side-effect lane 不得被场景覆盖或重新计算哈希后绕过。Coverage 额外绑定 `outputs_verified`、`preflight_receipt_id` 与 `request_fingerprint`，公开层重新核验持久记录和内容，不能只相信调用者的 `coverage_complete=true`。

发布分为执行与展示两步。`VerifiedAnswerProjector`（`answer_projection.py`）只读取已认证请求、preflight、coverage 与真实领域工件，从这些输入确定性生成正文，不接收任意自由草稿。`publish_verified` 生成并冻结答复，绑定全部来源对象及 preflight/coverage 哈希；`publish_registered` 读取原冻结答复时重新生成同一份正文并逐字段比较。工件登记只能证明字节与来源，不能使草稿中虚构的余额、动作或风险成为事实。整个发布过程不重跑经济处理器。公开层统一审计结论、理由、风险、动作、改变条件及持仓段落；错误来源、损坏对象、未知时间、任意字段改写均拒绝或整体降级，不保留未经核验的精确价格和交易指令。

模拟账户余额还必须与同一 preflight 的现金及净值恒等式对账。账户选择来自显式请求；未给账户时仅在恰好存在一个账户时自动解析，多个账户必须明确指定，不能把调用者传入的某个净值工件或未认证的 metadata 默认值当作用户选择。空持仓章节继续静默。非 material 的公司研究与事件研究继续走各自只读呈现；所有 material 投资判断、组合风险动作和持仓处置统一从 `RecommendationResearchReceipt` 确定性投影，旧的独立投资结论/组合/持仓公共分支已删除。

CLI 新增 `investor publish <coverage_receipt_id>` 与 `investor execute-registered <input.json>`，仅消费已注册、类型与来源通过验证的只读结果。`--current` 显式执行既有采集后冻结；缺状态库不自动创建空库，经济权限和安全边界覆盖被拒绝；普通输出只给投资者答复，诊断身份仅在显式 `--diagnostics` 下输出。`ScenarioContractRunner` 未提供草稿时走确定性发布，并把降级答案记为 `ANSWER_NOT_CERTIFIED`、未认证覆盖记为 `COVERAGE_NOT_CERTIFIED`，不能再以 coverage=1 而正文全部降级计作成功。研究标的回执按本次 event 主键核验，要求完整覆盖本次已解析实体，不扫描全部历史。

领域输出已绑定现有模型：行业使用 `IndustryProfile`，财务使用 `FinancialIntegrityEvidencePack`，公司研究使用 `InstitutionalDecisionContext`，组合使用 `PortfolioAnalysisReport`，持仓复核使用 `HoldingReviewPack`，全市场上游准入使用 `FullResearchInputReadinessReport`，最终 material 投资发布使用 `RecommendationResearchReceipt`，ETF研究使用 `ETFResearchMetrics`。`domain_contracts.py` 对完成状态、缺口、证据及角色语义另行检查；治理、催化剂和独立复核即使都通过 `ResearchRoleOutput` 传输，也必须绑定对应的真实 Research Team 计划、角色、完成 checkpoint、成员与依赖哈希，不能互相替代。依赖在单次校验内复用，结束时复核 checkpoint，避免旧结果与新依赖混合。来源数组按注册关系核验，不按两个独立排序数组的位置配对；嵌套可得时间必须通过有界递归检查，预测年份不视为输入可得时间。

当前研究采用唯一的 `DecisionFreezeService`：保留原始 `question_time` 与民用日期解释，在采集完成后冻结独立 `decision_time`，子请求通过 `evidence_cutoff` 读取该截止时间；历史请求不能推进截止时间。`freeze_current_request`、`execute_registered_inputs` 及其薄入口 `execute_registered`／`execute_current_registered` 消费现有注册工件，不自行采集数据，也不调用经济写入处理器。完整请求内容绑定原 request ID；同 ID 换文本或语义被拒绝。子请求工件必须属于已认证冻结清单，禁止能力在发出覆盖回执前即拒绝；稳定重试复用原回执并重新校验输入，不创建第二个调度或事实账本。

正反回归位于 `tests/unit/test_investor_orchestration_guards.py`、`test_investor_domain_contracts.py`、`test_investor_decision_inputs.py`、`test_investor_registered_execution.py` 与 Full Research 专项测试；它们覆盖真实隔离账本净值、机构研究确定性计算、Research Team 登记、错误角色／来源／时间拒绝、最终 receipt seal/replay/tamper、Publication Gate 与重启重试零经济表变化。业务矩阵另由 recorded 68 场景 E2E（含扩展边界用例）验证，不能用单一成功桩、全部降级或守卫通过数替代完整验收。

## 5. 统一请求前置流程

### 5.1 固定顺序

1. 解析 question time、mode 与 write intent；
2. 读取 active external accounts；
3. 对每个账户读取/增量更新 projection，并做事件审计；
4. 读取 paper ledger revision；只有 revision 改变、存在 open order/settlement 或缓存失效时才重建；
5. 幂等刷新 legacy human-readable projection；
6. 形成 lane-separated `UnifiedPortfolioContext`；
7. 读取当前持仓、已研究和已推荐标的的未解决重大监控事件；
8. 只读获取 latest valid `MarketRegimeSnapshotV2` 的 identity、age 与 availability；preflight 不发起宏观/行情网络采集，也不为纯账户事实写入构建新状态；
9. 冻结 `InvestorSessionPreflightReceipt`；
10. 才允许进入具体问题的研究编排。

### 5.2 性能策略

不能每次运行 66 秒级全账本恢复。采用：

- SQLite ledger/event sequence 作为增量游标；
- snapshot hash + revision cache；
- 单进程 request coalescing，多个并发问题共享同一安全时点快照；
- open order/settlement 才触发高成本回放；
- 读路径不执行网络同步，网络更新由具名 acquisition/monitor 能力完成；
- 冷恢复与暖读取分别监控。

拟议性能门：

- 无 revision 变化的本地暖 preflight：100 次基准的 p95 不高于 2 秒；
- 冷恢复允许较慢，但相同 revision 只能计算一次；
- preflight 超时不得伪造空持仓，而应把个性化组合结论降级为不可认证；
- 缓存必须绑定数据库 revision、policy hash 和代码版本，不能仅按时间缓存。

### 5.3 用户可见静默规则

- `empty_holdings=true` 时，最终答复中不得出现“当前没有持仓”“本次不涉及持仓更新”等句子或空持仓章节；
- 有持仓但无重大变化时，不强制增加冗长复核段；只有问题本身涉及持仓时给出正常结论；
- 有重大变化时，在主要结论附近说明变化、影响和动作，不把后台事件列表原样输出；
- 实际与模拟持仓都存在时，必须明确区分，不得合并成用户真实资产。

## 6. 意图到能力 DAG

### 6.1 全局必经能力

所有投资类意图：

```text
REQUEST_TIME
→ ENTITY_IDENTITY
→ SESSION_PREFLIGHT
→ CAPABILITY_PLAN
→ COVERAGE_RECEIPT
→ RESPONSE_GATEWAY
```

`MARKET_REGIME` 对当前买入/卖出判断、持仓动作、组合配置、正式荐股、行业可投资性和宏观政策传导属于 `REQUIRED`；对已发生外部交易的记录/更正、现金划转、转托管、去重检查以及只读订单状态查询属于 `CONDITIONAL` 或 `NOT_APPLICABLE`。其缺失不得阻断 append-only 账户事实提交、幂等检查和写后审计，也不得迫使这些请求联网抓取宏观数据。

写入类意图额外经过：

```text
WRITE_INTENT_VALIDATION
→ IDEMPOTENCY / DUPLICATE CHECK
→ APPEND-ONLY COMMIT OR PAPER CONFIRMATION
→ PROJECTION REFRESH
→ POST-WRITE AUDIT
```

### 6.2 单公司正式投资判断

```text
current acquisition
→ macro/policy + industry + company economics + financial integrity
→ governance + catalyst/event
→ forecast + valuation + market-price anchor
→ independent bull / bear
→ red team + model-risk validation
→ committee/readiness
→ regime risk overlay
→ portfolio impact（存在持仓或计划买入时）
→ entry/exit/monitoring conditions
```

独立模块可并行，但 valuation 依赖 financial/company economics，red team 依赖多空冻结输出，Committee 只读已冻结工件。

### 6.3 全市场荐股与组合构建

```text
official three-market Universe proof
→ broad candidate funnel
→ current market/regime budget
→ bounded deep Research Team per candidate
→ formal admission
→ portfolio construction / constraints / costs
→ regime overlay
→ entry/exit/weight bands
```

“值得现在买”默认要求当前时点完整研究，不能把 seed 或初筛名单当推荐。牛市可以增加**通过全部硬门后**的候选槽位；不能增加未研究证券或降低正式准入门。

### 6.4 持仓复核

```text
preflight positions
→ material monitor deltas
→ incremental company/event/financial research
→ thesis + valuation action
→ portfolio contribution / concentration / correlation / liquidity
→ regime overlay
→ HOLD / ADD / TRIM / EXIT + target band
```

用户成本价影响盈亏、风险承受和实施路径，不替代公司估值。

## 7. 交易与账户语义

### 7.1 已发生真实交易

用户明确陈述已发生交易且字段完整时，写 external account event；不重新要求研究许可。写入后再更新投影和监控。

### 7.2 缺失成交价或成本

不能把某日 K 线价格直接写成“真实成交价”。下一阶段新增独立的 provisional lane：

- `ProvisionalPositionAssertion`：用户确认持有，但交易事实不完整；
- `EstimatedCostBasisRange`：基于指定日期和当日可得未复权 OHLC/VWAP 形成范围与方法；
- `cost_basis_status=ESTIMATED_NOT_LEDGER_TRUTH`；
- 估算只用于区间敏感性，不进入精确已实现收益、税费或账本；
- 用户确认或券商记录导入后，以 append-only replacement/reversal 方式升级为精确事实。

因此，业务问题 13 的“自行根据时间拉 K 线补齐”可以自动完成研究估算，但不能静默伪造执行事实。

### 7.3 多账户、转托管、现金与更正

- 多账户永远按 `account_id` 隔离；只有唯一 active account 或用户已显式配置默认账户时才能自动选择，存在多个账户且无默认时不得猜测；
- “今天/昨天/上次”等相对日期先按请求中冻结的 user timezone 解析为 civil date，再映射到 `Asia/Shanghai` 交易日历；保存解析依据和原始文本，不把 date-only 伪造成具体成交时刻；
- 转托管使用成对 transfer out/in 和 correlation id，数量守恒、成本继承，不产生买卖 P&L；
- 现金转入/转出使用独立 cash event；
- 错误记录通过 reversal/replacement 修正，不 UPDATE/DELETE 历史；
- exact duplicate 通过 idempotency key 和经济指纹返回既有结果；
- 账户写入与模拟盘写入不得共享事务或事件表。

## 8. 失败与降级

| 失败 | 允许的输出 | 禁止的输出 |
|---|---|---|
| 账户投影审计失败 | 通用/单公司研究，明确无法认证组合动作 | 精确总仓位、盈亏、加减仓数量 |
| 当前行情过期 | 中长期基本面观察 | “现在买入价/卖出价” |
| 宏观 live 证据缺失 | 使用有效 recorded/current official capture 并标时点，或中性 overlay | 伪装为当前牛熊确定结论 |
| 财务完整性失败 | 风险观察和缺口 | 正式 BUY/目标价 |
| 能力覆盖不完整 | 降级为观察，列决定性不确定性 | 声称“已全方位研究” |
| 模型/规则冲突 | 更保守 overlay、显示冲突影响 | 任选乐观结果 |
| 写入字段不完整 | provisional 研究估算或一次性请求必要字段 | 写入伪造精确交易 |

## 9. 可观测性

内部记录：

- preflight latency：warm/cold、各子步骤；
- required capability coverage ratio；
- reuse ratio 与重复外部调用；
- conditional trigger precision；
- output guardrail rewrite/fallback；
- account/ledger audit failures；
- empty-holdings silence violations；
- side-effect policy violations；
- recommendation-to-monitor enrollment coverage。

不得把这些字段直接暴露给普通投资者回答。

## 10. 安全边界

- `broker_execution_allowed=false` 永久不变；
- 研究、组合和市场状态服务不得直接写模拟账本；
- 模拟订单继续需要精确规则与人工确认，只有 fill 改变持仓；
- Web/news 不证明全市场 Universe、连续行情、负面不存在或正式财务数字；
- 市场状态不允许绕过财务、治理、估值、流动性和证据门；
- 账户未知字段保持未知；
- 普通回答不输出 CLI、内部 Agent、状态机、artifact/hash 或 provider 故障流水。

## 11. 实施顺序

1. Schema 与 migration：request/preflight/unified portfolio/subject registry/coverage receipt；
2. 增量 `InvestorSessionPreflightService` 与缓存；
3. `ResearchSubjectRegistryService`；
4. machine-readable business scenario/capability policy；
5. typed capability planner/executor；
6. market regime v2 接入为只读共享输入；
7. `InvestorAnswerService` 强制消费 preflight + coverage receipt；
8. Skills/Workflow 变为机器编排的说明和专业方法层；
9. 记录式、受控 live 与 shadow 验收；
10. 保留 feature flag，可回退到现有逐命令路径。

详细工作包和验收门见根目录 `开发计划.md`。

## 12. 非目标

- 不增加真实券商连接或自动下单；
- 不把所有请求都升级为全市场重研究；
- 不因“调用更多能力”重复抓取同一事实；
- 不用大模型生成账本字段、宏观数值或风险指标；
- 不在本轮文档变更中宣称以上 proposed 合同已经实现。

## 13. 方法依据

OpenAI Agents SDK 将多 Agent 编排分为 LLM 驱动与代码驱动，并允许混用。本项目据此把固定安全门、用户态恢复、side effect 和输出审计放在代码，把研究判断与证据解释留给专业 Agent：

- https://openai.github.io/openai-agents-python/multi_agent/
- https://openai.github.io/openai-agents-js/guides/agents/

风险治理采用 NIST AI RMF 的 Govern/Map/Measure/Manage 思路组织文档、场景、量化验收和上线/回滚，但不把该框架当成机械检查表：

- https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf


## 投资预期上下文（WP-25）

状态：CURRENT / IMPLEMENTED。机器入口为 `FullResearchRecommendationService.request_contract`；`InvestorOrchestrationService.prepare` 在能力执行前解析并冻结既有 `FullResearchRequestContract`。`investment_expectations.py` 只处理用户意图与已验证的历史，`investment_objectives.py` 只对已准入研究作 Decimal 目标/盈亏投影，没有新增 Router、估值模型、分配器、账本或迁移。

- 本金和目标年化分别优先本轮明确值，再读同账户已注册请求日志、冻结请求合同和完整研究记录，最后按版本化配置使用 100000 元与 1.00（100%）的模型假设。零年化有效；模型 DEFAULT 不是用户声明，不得遮住更早真实目标。明确为零/负数的本金不能被静默替换成假资金。
- 历史使用只读 SQLite 连接；最多检查最近 1024 条 registry 行、读取其中 16 个相关对象，每个至多 512 KiB；这些上限均在配置。只读缺库不会创建文件，损坏/其它账户/未来记录跳过；历史不可读只作已标注默认分析，不等同于没有持仓。最近记录按可验证语义时间排序，复杂度有界，不遍历 ObjectStore。默认查询窗口外记录不声称已扫描。
- 小型请求合同按 request id 注册在现有 ObjectStore/registry，并在重试中复用；不存在第二套用户资金状态。保存失败保持当前分析可用并记录内部警告。历史助手示例、持仓市值、数据源估值收益不得冒充用户本金/目标。
- 年度目标利润为本金乘目标年化。期间路径按复利折算，优先使用已冻结公司研究的明确期限；未知/混合期限不做伪精确年化比较，不用原先任意的 7.5 个月中点充当估值期。情景盈亏以证券名义金额计算，扣除已估算建仓成本/滑点，卖出税费未知明确说明。
- 条件入场价是基础估值情景按目标折现后的门槛，且只能收紧原有安全边际价格上限；当前价格不满足时给条件方案，不能声称即时可买。目标缺口不提高单股/行业/流动性/风险阈值。止盈、止损、时间退出与基本面失效继续沿用既有正式研究证据；下行情景不是最大亏损保证。
- 新字段缺省为 None 并从旧结构序列化中排除；旧 receipt 内容、语义 hash 和重放不回填新目标。公开报告显示目标年化、来源语义、仓位/收益/风险比例与非承诺说明，不展示本金、账户资产/现金金额、配置金额、持有/建议股数、账户级绝对盈亏或 registry/内部字段；这些数值仅留在内部计算与账本。

权威依据（2026-09-15 检索）：CFA Institute Standard III(C) Suitability 要求在建议前了解风险与收益目标、财务约束并定期复核，且从整体组合评价适配性。采用目标与风险分离原则；不把该标准解释成“100%目标具有可实现保证”。Pydantic 官方 serialization 文档的字段级 `exclude_if` 用于维持旧不可变对象序列化兼容，不新增依赖。

参考：
- https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-iii-c
- https://docs.pydantic.dev/latest/concepts/serialization/

验证按 L2 定向执行：新解析/历史/目标测算边界、Full Research、请求编排、持仓 assembly、公开投影与相关文档合同；不改财报/估值计算、账本/PIT/Universe 或正式 Release，因此不机械重跑全仓。若后续修改上述高风险计算或权限，仍按 AGENTS 升级 L3。
