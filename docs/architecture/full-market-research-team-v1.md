# 全市场专业投研团队工作流与正式荐股治理 v1

> 状态：RELEASED（软件实现提交 `7bb7288`；External Dependency Resilience v0.1.0 安全门增强已正式发布）
>
> 日期：2026-08-24；现行集成校正：2026-08-28
>
> 适用范围：AStockMultiAgent 当前主线；默认运行入口为 ChatGPT/Codex Chat 会话唤醒，不依赖后台常驻 LLM 服务。
>
> 核心边界：`broker_execution_allowed=false` 永久不变；本方案只提升研究、筛选、审查、模拟交易前置判断的专业性与可审计性。

## 1. 本轮问题与根因

2026-08-24 的一次“帮我看看有什么好买的股？”真实运行暴露出三个系统级问题：

1. 全市场 Research Seed 获取失败后，顶层会话仍自行挑选三只股票继续分析，属于 **fail-open 候选补位**；
2. 三只股票只完成了当前资料采集和 Web 补证，没有跑完项目已经具备的机构级基本面链，却仍被渲染为正式买入排序；
3. Repo 中存在多个专业 Skill 和确定性研究模块，但没有一条不可绕过的“宏观 → 行业 → 盲筛 → 公司深研 → Bull/Bear → Review → Committee → Portfolio → Recommendation Gate”全市场编排合同。

因此，本轮目标不是用“运行至少 30/60 分钟”伪装深度，而是把**必须完成的研究工作量、独立性和证据门槛写成机器可校验合同**。任务可并行，所以耗时不是质量标准；完成度、独立视角、证据质量、估值闭环和 fail-closed 才是。

## 2. 外部方案调研与取舍

### 2.1 专业投研流程

CFA Institute 的 2026 Equity Valuation reading 将估值过程概括为：理解业务 → 预测经营 → 选择适当估值方法 → 将预测转化为估值 → 用于建议与结论；其 top-down 预测明确从宏观预测进入行业，再进入公司。Industry and Competitive Analysis 又要求先定义行业边界，再分析行业规模/增长/盈利、市场份额、竞争结构（Porter Five Forces）及 PESTLE 外部驱动，并判断公司增长究竟来自宏观/行业 Beta 还是公司 Alpha。

本项目取舍：

- 把 Macro Regime 与 Sector Comparison 升格为全市场荐股的正式前置层；
- 保留项目既有 `IndustryProfile / CompanyEconomics / DriverTree / Forecast / Valuation` 作为公司层 canonical 主链；
- Macro 不直接产出 BUY，只提供情景、风险溢价和行业传导假设。

参考：
- https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/equity-valuation-applications-and-processes
- https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/industry-and-competitive-analysis
- https://www.cfainstitute.org/programs/cfa-program/candidate-resources/practical-skills-modules/practical-macro

### 2.2 多 Agent 编排

OpenAI Agents SDK 将多 Agent orchestration 区分为 LLM 自主编排和代码编排，并支持 manager/agents-as-tools、handoff、guardrails 和 tracing。Anthropic 的 Building Effective Agents 强调：确定流程优先使用 composable workflow；独立子任务和多视角判断适合并行；高价值复杂输出适合 evaluator-optimizer 反复审查。

本项目取舍：

- **默认代码编排 DAG，不让一个顶层模型自行决定哪些硬门可以跳过**；
- LLM 只在角色内部做开放式研究；确定性 Python 决定任务依赖、证据门、完成状态和推荐权限；
- Bull 与 Bear 必须彼此独立，Reviewer 必须在二者完成后工作；
- 每个角色输出结构化结果并留下独立上下文标识；
- 当前 Chat-only 环境默认 `CHAT_ORCHESTRATED`，将任务图交给 ChatGPT/Codex 逐角色执行并登记结果；未来若显式配置 API，可在不改合同的情况下增加 `AGENT_RUNTIME` 执行器。

参考：
- https://openai.github.io/openai-agents-python/multi_agent/
- https://openai.github.io/openai-agents-python/handoffs/
- https://openai.github.io/openai-agents-python/guardrails/
- https://www.anthropic.com/engineering/building-effective-agents

### 2.3 金融多 Agent 前沿实现

TradingAgents 采用 Fundamental / Sentiment / Technical / Trader / Risk / Portfolio 等角色，并使用 Bull/Bear research debate；其 2026-07 v0.3.1 进一步补入 look-ahead filtering、graph-router crash safety、graph-shape-aware checkpoint resume、LLM retry budget，此前版本已具备 structured outputs、persistent decision log、verified data-access contract 与 CI gate。FinRobot 强调金融 Agent、定量/数据层、LLMOps/DataOps 与多源金融数据的分层，并在 Equity Research 路径中采用多专业 Agent、财务预测、DCF、peer comparison 与专业报告生成。Microsoft RD-Agent(Q) 展示了 factor/model 协同迭代的 data-centric quant multi-agent 研究范式；Qlib 则提供松耦合的数据、workflow、模型、回测、风险、组合与执行研究基础设施。

本项目不整体引入 TradingAgents/LangGraph/FinRobot，原因是：

- 当前系统已经有完整 PIT、ObjectStore、Financial Integrity、Institutional Fundamental、Committee、Portfolio 和模拟账本；
- 直接引入会形成第二套事实源和第二套交易权限；
- 当前主要交互入口没有独立 API key 常驻服务。

本项目只吸收其**角色分工、正反方辩论、checkpoint/restart、structured result 和数据访问合同**，继续让 AStock 的确定性内核掌握事实、数学、PIT、门禁和账本。

参考：
- https://github.com/TauricResearch/TradingAgents
- https://github.com/AI4Finance-Foundation/FinRobot

### 2.4 AI 治理与测试

NIST AI RMF / GenAI Profile 强调将可信度与风险管理纳入设计、开发、使用和评估全生命周期，并用 Govern / Map / Measure / Manage 建立可重复治理。

本项目取舍：正式推荐权限必须来自机器可审计的 Recommendation Readiness Gate；普通 Agent 文本不能覆盖该状态。

参考：
- https://www.nist.gov/itl/ai-risk-management-framework
- https://airc.nist.gov/airmf-resources/airmf/

### 2.5 轻薄办公本运行约束

用户常在 HP ProBook 450 G8 一类轻薄办公本上运行系统，因此不能用高端工作站作为前提。DuckDB 官方建议内存受限环境限制线程；复杂 workload 实际常需约 1–4 GB/线程，SSD/NVMe 更适合 out-of-core。SQLite WAL 允许 reader 与 writer 并发，但仍是单 writer 语义。

本项目取舍：

- 启动时动态侦测 CPU/RAM，选择 LOW_RESOURCE / STANDARD / HIGH_RESOURCE；
- 轻薄本默认 2 个 provider worker、2 个逻辑 Agent worker、2 个 DuckDB thread、最多 2 家公司同时深研；
- 网络 I/O 可小规模并行，CPU/内存密集工作严格限并发；
- 不新增 Redis/Kafka/Temporal/常驻 Agent 服务；继续 SQLite + ObjectStore + Parquet + checkpoint；
- 不新增强制 GPU 依赖。

参考：
- https://duckdb.org/docs/lts/guides/performance/environment
- https://duckdb.org/docs/lts/guides/performance/my_workload_is_slow
- https://www.sqlite.org/wal.html

## 3. 运行模式：按需现抓，不依赖后台预同步

### 3.1 明确撤销“必须后台 Persistent Market Master”的前提

当前产品事实是：ChatGPT/Codex Chat 收到用户消息后才唤醒研究工作。正式荐股不得依赖“昨晚一定已经自动同步”的假设。

所以本轮使用 **On-Demand Acquisition**：

1. 用户发起全市场荐股；
2. 同一研究 run 内并行获取 XSHG / XSHE / BJSE 当前 Universe；
3. 同时抓宏观/政策/市场风险材料；
4. 每个 raw response 仍立即进入 ObjectStore，形成同一 run 的不可变 session cache；
5. 后续 Candidate、行业分析、公司深研复用本轮快照，不重复抓同一数据；
6. provider 失败时在本轮预算内切换 allowlisted fallback / authoritative Web；
7. 若全市场 Universe 仍无法证明，正式荐股 **fail closed**，不允许人工脑补候选。

默认自动解决总预算提升到最多 7200 秒；这只是上限，不要求人为等待。只要所有硬门提前完成即可提前结束。

### 3.2 提速策略

- XSHG / XSHE / BJSE Universe I/O 并行，但按硬件预算限制在 2–3 workers；
- 宏观、政策、流动性/风险三个角色并行；
- Candidate 先做低成本 blind scan，再只对有限候选抓昂贵财务/公告/行业证据；
- 同一公司 Fundamental / Financial Integrity / Catalyst / Market Context 在证据冻结后安全并行；
- Bull/Bear 并行，Reviewer 等待二者；
- 只在 shortlist 收敛后运行估值/Committee；
- 全部任务可 checkpoint，崩溃后只恢复未完成节点。

## 4. P0：正式荐股必须 fail closed

### 4.1 Recommendation Readiness Gate

以下检查全部 PASS 才允许 `formal_recommendation_allowed=true`：

1. `UNIVERSE_COVERAGE`
2. `MACRO_REGIME`
3. `SECTOR_COMPARISON`
4. `BLIND_CANDIDATE_SCAN`
5. `INDUSTRY_PROFILE`
6. `COMPANY_ECONOMICS`
7. `FINANCIAL_INTEGRITY`
8. `DRIVER_TREE`
9. `FORECAST_BULL_BASE_BEAR`
10. `VALUATION`
11. `MARKET_PRICE_ANCHOR`
12. `CATALYST_RISK`
13. `BULL_CASE`
14. `BEAR_CASE`
15. `INDEPENDENT_REVIEW`
16. `COMMITTEE`
17. `PORTFOLIO_CONSTRUCTION`
18. `TEAM_DAG_COMPLETE`

缺任何一个：只能输出 `OBSERVATION_ONLY`，禁止出现正式 BUY/加仓排序。Role 文本或自报布尔值不能独立证明 readiness：Universe 必须绑定一个 ObjectStore 可验证、`formal_full_market_coverage_allowed=true` 的 typed `ResearchSeedReport`；Financial 必须绑定 ObjectStore 可验证且 `status=SUCCEEDED / coverage_status=COMPLETE` 的 typed `FinancialIntegrityEvidencePack`。当 `FINANCIAL_INTEGRITY=false` 时，精确 `VALUATION=true` 也必须被确定性拒绝。

### 4.2 Candidate fail-open 禁止

- Seed/Candidate 本身永远没有推荐权；
- `research-seeds --live` 得到 0 个有效 market seed 或无法证明 Universe 时，不允许聊天 Agent 手工挑股票接着跑正式荐股；
- Web 搜索只能修复证据缺口，不能绕过 Universe/Candidate lineage；
- 若用户明确点名股票，允许进入单公司研究，但它不能被伪装成“全市场最优”。

## 5. P1：投研团队 DAG

### 5.1 全市场路径

```text
CIO / Research Manager
├─ Macro Agent ───────────────┐
├─ Policy Agent ──────────────┤
├─ Liquidity & Risk Agent ────┤ Stage 1
└─ Universe Acquisition ──────┘
                ↓
Blind Market Candidate Scan ── Stage 2
                ↓
Sector / Industry Comparison ─ Stage 3
                ↓
Bounded Shortlist
   ├─ Fundamental Agent ──────┐
   ├─ Financial Integrity ────┤
   ├─ Catalyst / Disclosure ──┤ Stage 4 fan-out / company
   └─ Market Context ─────────┘
                ↓
Valuation Agent ────────────── Stage 5
                ↓
   ┌────────────┴────────────┐
Bull Analyst              Bear Analyst   Stage 6 (independent)
   └────────────┬────────────┘
             Reviewer                    Stage 7
                ↓
             Committee                   Stage 8
                ↓
          Portfolio Manager              Stage 9
                ↓
     Recommendation Readiness Gate       Stage 10
```

### 5.2 独立性

- Bull 与 Bear 不得读取彼此的草稿；只读相同冻结 evidence/model bundle；
- Reviewer 只在 Bull/Bear 均完成后运行；
- Committee 只读冻结结果，不联网补证；
- Portfolio 只能使用 Committee 已批准的标的；
- 每个任务必须登记 `independent_context_id`，防止“同一段草稿复制成多个 Agent”。

### 5.3 当前两种执行后端

**CHAT_ORCHESTRATED（默认，当前可用）**

- Python 创建持久 DAG 和任务状态；
- ChatGPT/Codex 根据任务图逐角色执行；
- 每个角色以独立 task result 登记；
- Python 检查 dependency、独立上下文和 gate；
- 不依赖后台 LLM daemon。

**AGENT_RUNTIME（可选扩展）**

- 只有未来用户显式配置 API/本地模型执行器后才启用；
- 可接 OpenAI Agents SDK / 其他兼容 runner；
- 必须复用相同任务图和 Recommendation Gate；
- 不得形成第二套事实源或账本。

## 6. P2：行业公平、Skill Edge 与专业覆盖

### 6.1 删除作者 Skill 占比门槛

用户明确要求删除：

> `Skill 占该作者全部 Skill ≥ 1.5%`

新规则只保留绝对证据门：

> 某行业至少命中 `minimum_domain_skill_count`（默认 3）条 audited active Skills。

`skill_share` 仍可作为诊断指标展示，但**不再参与准入**。

### 6.2 Blind Scan 与 Expert Overlay 分离

正式候选发现必须先保留不依赖私有 Skill 的 blind market tranche，再允许 Expert Skill 作为 overlay：

- Blind tranche 由统一市场/质量条件产生；
- Expert overlay 只能增加研究优先级，不得创造 BUY；
- Expert seed 必须仍满足市场 Universe / 流动性 / 市值等基础资格；
- 默认 `max_market_seeds=20` 的 blind tranche 在最终 `max_total_seeds=40` 中优先保留，不得被 Skill seed 全部挤出；
- Expert overlay 对优先级的额外加成设硬上限，防止知识库覆盖密集行业垄断 shortlist。

### 6.3 行业分析能力与私有 Edge 分离

新增内部 `Industry Research Archetype Registry`，覆盖主要一级行业研究框架。它不是“官方申万/GICS 分类”的冒充，而是项目自己的研究模板。每个模板定义：

- 核心经营驱动；
- 行业结构问题；
- 关键 KPI；
- 常用估值方法；
- 主要会计/周期/监管风险；
- 必做反证问题。

私有 Skill 只决定额外 Edge，不决定系统能不能分析该行业。

### 6.4 Coverage 四维度

每个正式公司研究记录四个独立覆盖维度：

- Universal Research Coverage
- Industry Specialist Coverage
- Private Skill Coverage
- Evidence Coverage

Private Skill Coverage 低时：降低 Edge 置信度/增加行业研究预算，但不把通用研究能力判为不可用。已实现 `research-coverage-score`：Universal / Industry / Evidence 默认最低分别为 90 / 80 / 90；Private Skill Coverage 明确为 edge-only，即使为 0 也不得单独阻断一个基础研究充分的标的。

Coverage 报告只描述“研究覆盖是否足够”，不自行产生 BUY 权限；正式荐股仍必须通过完整 Recommendation Readiness Gate。

## 7. 轻薄本自适应预算

### LOW_RESOURCE

适用于 ≤4 核或 <16 GiB RAM 等保守环境：

- provider workers: 2
- logical agent workers: 2
- DuckDB threads: 2
- parallel deep companies: 2
- initial deep-research shortlist: 6

### STANDARD

- provider workers: 3
- logical agent workers: 3
- DuckDB threads: 4
- parallel deep companies: 3
- shortlist: 8

### HIGH_RESOURCE

- provider workers: 4
- logical agent workers: 6
- DuckDB threads: 8
- parallel deep companies: 4
- shortlist: 10

所有值都属于 versioned policy，不写死在业务 Service；运行时只做硬件分类。

## 8. 数据权威性

正式公司事实仍优先：

1. 交易所 / 监管 / 法定披露平台；
2. 发行人 IR；
3. 已审计/正式财报；
4. 结构化二级源只作定位与交叉；
5. 新闻/社区只作 lead。

A 股公告优先入口包括上海证券交易所最新公告和巨潮资讯。当前按需模式允许本轮 Web 补证，但补证材料必须回写不可变 evidence/snapshot lineage 后才能改变正式事实。

参考：
- https://www.sse.com.cn/disclosure/listedinfo/announcement/
- https://www.cninfo.com.cn/

## 9. 验收标准

### P0 — 正式荐股安全门

- [x] 缺任一 required readiness check，`formal_recommendation_allowed` 必须为 false；
- [x] 0 market seed / Universe 未证明时禁止人工手挑候选形成正式荐股；
- [x] Seed/Candidate/Research Plan 本身 recommendation authority 永远为 false；
- [x] 完整 required checks（当前 18 项，含 `TEAM_DAG_COMPLETE`）全部 PASS 才能得到 `READY`；
- [x] recommendation-required Role 必须绑定已注册且 ObjectStore 可验证的 member artifact；
- [x] Role 声明的 `evidence_id` 必须真实存在、excerpt object 可验证，Result Evidence 必须与 Role Outputs 并集一致；
- [x] final answer workflow 明确要求读取 READY 结果后才可输出正式买入排序。

### P1 — 团队编排

- [x] 可生成持久、版本化 full-market Research Team Plan；
- [x] Macro / Policy / Liquidity 可并行；
- [x] company 深研以 shortlist fan-out；
- [x] Bull/Bear 必须是不同 `independent_context_id`；
- [x] Reviewer 在 Bull/Bear 后；Committee 在 Reviewer 后；Portfolio 在 Committee 后；
- [x] task result 依赖不满足时拒绝登记 COMPLETE；
- [x] Chat-only 环境无需 API key 即可建立/恢复任务图；
- [x] `AGENT_RUNTIME` 仅作为可选执行后端，仍复用同一 DAG / Recommendation Gate，当前默认不启用。

### P2 — 行业与 Skill 偏置治理

- [x] `minimum_domain_skill_share` 从 schema、代码和测试中删除；
- [x] 只有绝对 Skill 命中数参与 expert-domain gate；
- [x] 大量无关 Skill 不得稀释一个已满足绝对命中数的行业；
- [x] blind market tranche 在 expert overlay 前得到保留；
- [x] Expert Overlay bonus 从版本化 policy 注入 Request，业务 Service 不再复制 `0.15` 阈值；
- [x] Expert Skill 不直接创建 recommendation；
- [x] 行业 Archetype registry 覆盖 22 类主要 A 股行业研究框架；
- [x] 行业分类无法可靠解析时显式 `UNCLASSIFIED`，禁止凭空套模板；
- [x] `research-coverage-score` 分离 Universal / Industry / Private Skill / Evidence Coverage，Private Skill 始终 edge-only。

### 性能与硬件

- [x] LOW_RESOURCE 配置 provider/agent/DB 并发均不得超过政策上限；
- [x] XSHG / XSHE / BJSE market snapshot 在允许预算内并行获取；
- [x] 本轮相同快照通过 ObjectStore/session lineage 复用；
- [x] 不要求后台 daemon、定时任务或 GPU；
- [x] 单个 provider 失败不会阻塞其他市场/独立任务，Universe 总覆盖不足则 fail closed；
- [x] 当前实机 `8 logical CPU / 15.73 GiB RAM` 被正确识别为 `LOW_RESOURCE`：provider=2、agent=2、DuckDB=2、parallel companies=2、deep shortlist=6。

### 工程质量

- [x] 新增/修改 unit tests 全通过；
- [x] `uv run pytest -q` 全仓通过：`929 passed / 18 skipped / 0 failed`，918.16s；
- [x] `uv run ruff check .` 通过；
- [x] `uv run pyright` 通过：`0 errors / 0 warnings / 0 informations`；
- [x] `git diff --check` 通过（仅 CRLF→LF 提示，无 diff error）；
- [x] SQLite integrity 审计通过：`PASS / integrity_check=ok / read_only=true`；
- [x] 无真实券商执行能力被引入；
- [x] 未回退工作区既有 Continuous Monitor 等未提交实现，最终作为同一集成发布树共同回归。

### 人工 / 显式 live 验证（本次按用户授权跳过，不伪装为 PASS）

- `SKIPPED_MANUAL`：30-document 真实 OCR benchmark；
- `SKIPPED_MANUAL`：approved local-only private-book visual coverage；
- `SKIPPED_MANUAL`：Candidate/CNINFO/行情/reference provider 的显式 live probe；
- `SKIPPED_MANUAL`：Continuous Monitor 长时间 daemon、真实自然语言场景与 crash/source-degradation 观察。

上述项目只影响真实环境长期观察结论，不阻断本次软件代码、Schema、CLI、Workflow 与确定性安全门发布。

## 10. 实施顺序与状态记录

| 阶段 | 工作 | 状态 |
|---|---|---|
| P0.1 | Recommendation Readiness schema/service/gate | COMPLETE |
| P0.2 | Candidate fail-open 禁止与 blind tranche | COMPLETE |
| P0.3 | On-demand acquisition / hardware-aware I/O | COMPLETE |
| P1.1 | Research Team policy + DAG planner | COMPLETE |
| P1.2 | durable task/result/checkpoint | COMPLETE |
| P1.3 | Chat/Codex Skill/Workflow 集成 | COMPLETE |
| P2.1 | 删除 Skill share gate | COMPLETE |
| P2.2 | Industry Archetype registry | COMPLETE |
| P2.3 | Coverage/Skill bias 审计接口 | COMPLETE |
| REVIEW | 两轮安全门返工 + 配置漂移/产品合同终审 | PASS |
| TEST | 定向 + 全仓 + Ruff + Pyright + diff + integrity | PASS |
| RELEASE | CLI/Workflow 启用、验收文档迁移 | RELEASED |

软件实现发布提交 `7bb7288` 已成功推送 `origin/main`。发布状态以本节历史测试证据为准；当时的人工 / live 长时间观察保持 `SKIPPED_MANUAL`，不得反向改写为该次发布已经验收。

## 11. External Dependency Resilience 现行集成校正

- 全市场正式 FULL 现以每个 XSHG / XSHE / BJSE 市场可审计 `coverage_ratio >= 99.5%` 为必要条件；旧 row floor 只抓明显截断。PARTIAL Universe 可继续 observation/research discovery，但不能形成全市场正式推荐。
- `UNIVERSE_COVERAGE` 由 typed `ResearchSeedReport` 派生，`FINANCIAL_INTEGRITY` 由 typed `FinancialIntegrityEvidencePack` 派生；任意 Role 文本、自报 check 或错误 artifact type 都不能提升 gate。
- CNINFO、交易所 exact-item、secondary financial 的降级链遵循 typed official lineage。Official Web exact-item 只能恢复已知报告，不能证明 exhaustive enumeration；恢复为 PARTIAL 时精确估值和正式推荐继续关闭。
- Provider health/breaker 按 capability 隔离；EastMoney 5m 的真实 NETWORK 失败不污染 Sina 5m 的 HEALTHY 状态，也不允许聊天 Agent手工补位成为正式候选。
- 2026-08-27 的真实 CNINFO、官方日历、5m fallback 与组合故障注入证据记录于《验收报告》和唯一 durable run `lr_mtb4gekw_ff5540ff05d2`；这些增强已随 External Dependency Resilience v0.1.0 通过独立冻结树与远端 Release 门，target 为 `c764e842d3eb1922bc206b7f3cffdd9759c8f1cc`，且不改写 `7bb7288` 的历史发布身份。
