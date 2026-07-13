# 给 Claude 的两阶段任务：验证并制定“低成本 A 股多 Agent 投研、证据审计与模拟交易系统”的可落地开发计划

## 0. 你的角色、本次任务边界与两阶段交付制度

你是一名同时具备以下经验的首席系统架构师：

- Python 数据工程与量化研究；
- A 股财报、审计红旗与公开信息核查；
- 多 Agent / Agent Skills / RAG / LLMOps；
- 事件驱动回测与本地模拟交易；
- Windows 单机低成本部署；
- 数据溯源、时间穿越防护、可复现实验和模型评测。

本任务必须严格分成两次输出。**当前第一次回复只能进行预检与技术验证规划，不得直接输出完整开发计划。**

### 0.1 第一次输出：Preflight / Go-No-Go

第一次只允许输出以下内容：

1. 阻断性问题、关键假设和潜在冲突；
2. 各子系统的初步可行性结论：`GO / CONDITIONAL_GO / NO_GO / NEEDS_TEST`；
3. Phase 0 实测清单，包含具体测试对象、步骤、样本、输出字段和通过标准；
4. 用户需要执行或授权执行的检查，以及统一的结果回填模板；
5. 根据不同测试结果应选择的降级路线。

第一次输出必须重点验证：

- 免费分钟行情的真实历史范围、完整性和字段质量；
- 用户现有 QMT/xtquant 环境可获得的周期、历史深度和权限；
- 小时/日线可靠底座是否成立；
- PDF 原文、表格和页码解析质量；
- 已固定 commit 的开源代码是否能在 Windows/Python 3.11 下独立运行；
- 自有 OpenAI-compatible 反向代理/中转的连通性、结构化输出能力、上下文限制、超时、成本口径和失败降级；
- 本机性能、磁盘和单次运行耗时；
- Point-in-Time 数据可得性；
- 知乎采集的登录、断点和人工验证边界。

第一次输出不得展开：

- 完整仓库目录；
- 全量数据库表；
- 二十多个模块的逐一设计；
- 全量 JSON Schema；
- 七个以上阶段的详细任务拆解；
- 完整测试矩阵；
- 大篇幅总体架构报告；
- 生产代码。

第一次输出应形成一份可执行但紧凑的 `preflight.md` 内容，并在结尾明确停止，等待用户回填实测结果。

### 0.2 第二次输出：完整开发计划

只有当用户提供 Phase 0 实测结果，或明确要求基于已知结果继续时，才进入第二次输出。

第二次输出必须根据真实测试结果，而不是理想假设，生成一份**完整、详细、具体到模块、字段、接口、状态机、测试和验收标准的开发计划**，建议文件名：

`full_development_plan.md`

第二次输出可以给出：

- 总体架构和运行 DAG；
- 开源项目文件级复用矩阵；
- 仓库目录；
- SQLite、Parquet、DuckDB 和内容寻址对象存储设计；
- JSON/Pydantic Schema；
- 模拟账户和回放引擎；
- 财务可信度审计；
- Knowledge/Skill 蒸馏；
- Token/API 成本控制；
- 分阶段开发任务、测试矩阵和验收标准。

以下所有章节共同构成系统需求规格。第一次输出只利用它们生成预检方案；第二次输出再完整展开。

# 1. 项目目标与现实约束

## 1.1 项目目标

开发一套面向个人投资者的、以 A 股为主的：

> **证据驱动、多时间尺度、低成本、多 Agent 投资研究、候选生成、模拟交易和持续复盘系统。**

系统同时支持：

- 数周至数月的波段交易研究；
- 数月至数年的价值与产业趋势研究；
- 自动获取结构化数据和官方公开资料；
- 对用户指定标的进行深度分析；
- 由专业 Skill 生成候选标的；
- 主动识别自动化无法补齐的关键证据；
- 为用户生成低成本、15～45 分钟可完成的人工调查任务；
- 所有重要结论绑定可追溯信息源；
- 生成带明确入场、退出、失效和复核规则的模拟交易协议；
- 在系统关机后重新启动时断点补数并回放模拟交易；
- 持续统计各 Skill、各专家差分和最终委员会的真实效果；
- 后期根据市场状态和历史绩效动态调整策略权重。

系统不是“自动荐股聊天机器人”，也不是高频交易系统。系统必须允许输出：

- `证据不足，暂不形成判断`；
- `仅作为待核验线索`；
- `需要人工补证`；
- `拒绝进入股票池`；
- `维持观察，不建立模拟仓位`。

## 1.2 资金、硬件、模型接入与成本

- 初始实际投资资金约人民币 50,000 元；
- 禁止默认使用杠杆；
- 初期只做模拟交易和人工确认后的半自动研究；
- 本机为 Windows，CPU：Intel i5-1135G7，GPU：NVIDIA MX450；
- 不假设具备高端 GPU；
- 不做大型模型训练或大型模型参数微调；
- 优先零成本或低成本；
- 不购买 X 等社交平台的大规模实时 API；
- 不默认购买昂贵分钟行情、新闻或研报数据库；
- 可以接受后期少量年度数据费用，但必须先完成免费 MVP；
- 用户有 ChatGPT Pro 5x、Codex 额度及可用的自有反向代理/中转方案。

### 模型接入既定前提

本项目已决定允许使用用户自行管理的 **OpenAI-compatible 反向代理、中转或其他兼容端点**。不要把 OpenAI 官方订阅/API 限制作为本项目的阻断性讨论，也不要反复要求用户放弃该方案。

技术设计必须将模型接入抽象为可替换 Provider，并至少支持：

1. `Deterministic Mode`：完全不使用 LLM；
2. `Manual LLM Packet Mode`：导出紧凑任务包，由用户手动交给 Codex/Claude，再导入结构化结果；
3. `OpenAI-Compatible Mode`：连接用户配置的官方 API、反向代理或中转；
4. 可选本地小模型，但不作为 MVP 硬依赖。

模型 Provider 至少配置：

```text
provider_id
base_url
api_key_secret_ref
model_id
request_format
structured_output_support
stream_support
context_limit
max_output_tokens
timeout_seconds
retry_policy
rate_limit_policy
price_table_version
billing_source
health_status
last_probe_at
```

系统不得把任何上游模型、域名或价格写死。反向代理的合法使用、账号和访问权限由用户负责；开发计划只处理技术接入、安全存储、可替换性、预算、故障和审计。

## 1.3 运行特征

- 系统不会 24 小时运行；
- 用户通常不会超过 5 天不启动，但必须支持更长时间停机；
- 主要在收盘后运行；
- 小时级 K 线用于波段研究和市场状态；
- 日线、周线用于中长期趋势与估值；
- 1 分钟和 5 分钟 K 线用于模拟交易路径回放；
- 所有关键状态必须落盘，进程中断后可恢复；
- 任何任务重复执行都应幂等，不得产生重复订单、重复证据或重复分析。

---

# 2. 必须遵守的设计原则

## 2.1 确定性计算优先

以下工作必须由 Python 或其他确定性程序完成，不能让 LLM 心算：

- 三表勾稽；
- 财务比率；
- TTM、同比、环比、CAGR；
- 复权与公司行为处理；
- 同行业分位数；
- 异常检测特征；
- 均线、ATR、波动率；
- 估值情景计算；
- 组合相关性；
- 仓位、费用、滑点；
- 模拟交易回放；
- 数据去重和单位换算；
- 路由中的明确规则；
- 证据引用覆盖率；
- Token 和 API 成本统计。

LLM 只负责：

- 非结构化文本理解；
- 财报附注和公告中的事实抽取；
- 跨文档矛盾识别；
- 因果解释；
- 多种解释的竞争；
- 证据综合；
- 反方论证；
- 紧凑的最终叙述。

## 2.2 采用“DAG + 结构化工件”，不要采用无限增长的群聊

系统的“多 Agent”应优先实现为：

> 中央 Orchestrator 调度一组输入输出严格定义的节点，每个节点读取已版本化工件并写出新的工件。

不要让多个 Agent 共享无限增长的聊天历史。每个阶段通过 JSON/Pydantic 对象、Parquet、Markdown 和证据引用传递状态。

核心工件包括：

- `EvidencePack`
- `BaseCasePack`
- `SpecialistDelta`
- `FinancialIntegrityEvidencePack`
- `ManualInvestigationTaskPack`
- `CounterCasePack`
- `DecisionPack`
- `TradeProtocol`
- `RunManifest`
- `CostLedger`

## 2.3 共性分析只做一次，专家只输出差分

不要让每个“博主 Agent”重复读取：

- 全套财务数据；
- 全套财报；
- 行业介绍；
- 通用估值；
- 通用风险；
- 全套新闻；
- 最终报告。

应采用：

```text
统一证据包
→ 共性投资内核生成 BaseCasePack
→ 代码优先的专家路由器
→ 仅运行命中的专家 Skill
→ 专家只输出 SpecialistDelta
→ 统一委员会消费 BaseCasePack + Delta + 专项风险包
```

“博主”主要保留为：

- 方法论和 Skill 的知识来源；
- 来源归属；
- 历史观点演化；
- 评测标签；
- 可选的人格化展示视图。

运行时应按能力领域组织，而不是按博主姓名启动多个完整 Agent。

## 2.4 所有判断必须可溯源

必须区分：

1. 已核实事实；
2. 管理层说法；
3. 第三方估计；
4. 社区线索；
5. Agent 推断；
6. 尚未验证；
7. 已被后续事实证伪。

任何关键判断必须绑定 `evidence_id`。证据至少包含：

- 原始来源；
- 原始链接；
- 本地快照路径；
- 文档标题；
- 文件哈希；
- 发布日期；
- 系统可获得日期；
- 查询日期；
- 页码、章节或 DOM 定位；
- 原文片段或片段哈希；
- 证据等级；
- 对应主体；
- 权利与使用状态。

## 2.5 时间穿越防护与 Point-in-Time 可信度

所有数据和文档必须区分：

- `period_end`
- `published_at`
- `effective_at`
- `ingested_at`
- `available_to_system_at`
- `revised_at`
- `supersedes_source_id`

仅有这些时间字段仍不足以证明数据是 Point-in-Time。每条可用于历史评测的数据还必须声明：

```text
point_in_time_status:
  CERTIFIED
  DOCUMENT_RECONSTRUCTED
  APPROXIMATED
  NOT_PIT_SAFE

availability_basis:
  ORIGINAL_DOCUMENT_TIMESTAMP
  ARCHIVED_SNAPSHOT
  PROVIDER_METADATA
  CURRENT_PROVIDER_VALUE
  MANUAL_ASSUMPTION
```

定义：

- `CERTIFIED`：数据源原生保留历史版本和可获得时间，且已验证；
- `DOCUMENT_RECONSTRUCTED`：根据当时发布的原始公告、财报、问询回复或归档快照重建；
- `APPROXIMATED`：基于有限元数据作保守近似，只能用于探索性分析；
- `NOT_PIT_SAFE`：当前供应商值、无法确定历史可得时间或已被后续修订覆盖。

硬性规则：

1. `NOT_PIT_SAFE` 不得进入正式历史 Agent 评测、策略比较或模型训练；
2. `APPROXIMATED` 只能进入单独标记的探索性回测，不得与正式样本混合汇报；
3. 系统启用后必须持续快照，逐步建立自己的真实 PIT 数据库；
4. 系统启用前的历史研究，只有找到原始公告、原始版本或可靠归档时，才可标为 PIT 安全；
5. 处罚决定、财报更正和事后监管结论可以作为未来标签，不能进入历史输入；
6. 股票池和基准必须纳入退市股票、历史证券代码、历史 ST 状态和历史指数成分，防止幸存者偏差；
7. 每份回测报告必须披露 `PIT 覆盖率`、各状态样本占比和被排除样本；
8. 当前 Provider 返回的“最新修订后财务值”默认标记为 `CURRENT_PROVIDER_VALUE + NOT_PIT_SAFE`，除非另有证据；
9. 不得因为数据库中存在 `published_at` 字段，就自动声称已消除时间穿越。

历史回测和历史 Agent 评测只能使用当时已公开、且在指定可得性假设下可被系统获得的信息。

## 2.6 风险硬门槛优先于加权平均

以下类型信号不得被“其他 Agent 多数看多”抵消：

- 三表无法勾稽且无法解释；
- 严重非标审计意见；
- 监管立案或已确认重大违法风险；
- 核心来源冲突未解决；
- 数据质量严重不合格；
- 无法交易或流动性不足；
- 关键证据只有未经核验的社区传闻；
- 系统专长覆盖严重不足且计划仓位较高。

---

## 2.7 唯一事实来源与写入所有权

必须消除 SQLite、DuckDB 和 Parquet 各保存一份行情/财务事实导致的漂移：

- **内容寻址对象存储**：原始 PDF、HTML、JSON、截图和原始下载文件，以 SHA-256 为对象键，原始对象不可覆盖；
- **Parquet**：标准化结构化行情、财务、事件、因子和实体关系的唯一分析事实源；
- **DuckDB**：主要读取 Parquet、建立视图、执行研究查询和回测，不再复制同一份事实数据；
- **SQLite**：只保存操作状态、任务游标、事务账本、订单、成交、现金、费用、持仓、配置、锁和幂等键；
- **Artifacts**：保存有版本的 EvidencePack、BaseCasePack、Delta、DecisionPack 和报告，不作为原始事实表替代品。

每类数据必须定义唯一写入者、版本规则和重建路径。不得让多个模块分别修改同一事实表。对象存储建议路径：

```text
objects/sha256/ab/cd/<完整sha256>
```

元数据只引用对象哈希，不依赖容易变化的文件名。

# 3. 需要按固定版本核查并决定如何复用的开源项目

禁止仅审计“当前主分支”，因为主分支随时可能变化。首次审计以以下 commit 为固定起点：

| Repository | Pinned commit SHA | License at commit | 初步定位 |
|---|---|---|---|
| `muxuuu/serenity-skill` | `c2fe93deedfd0d1bd9fe7ef0601ea1b9c20ea24a` | MIT | 方法论与产业瓶颈 Skill |
| `haskaomni/serenity-skill` | `332037ea5f41ce7f150afbedb3517bcd1f1b2833` | MIT | 可拆分的五个投资 Skill |
| `leafpaper/claude-company-analysis` | `d908885640cc7d4ff06f1ee48b1beb2ab13012be` | MIT | 财务规则、PDF、引用和缺口机制代码捐献者 |
| `AI4Finance-Foundation/FinRobot` | `297a8d28d099be328c8a8eb658b4f782b93f3651` | Apache-2.0 | 只借鉴编排、溯源和职责分离 |

AuditAgent 为论文方法论来源，不假设存在可安装仓库。

每次开源审计都必须生成不可变记录：

```text
repository_url
commit_sha
license_at_commit
license_file_hash
audited_files
audited_file_hashes
audit_date
auditor
reuse_decision
local_patch_set
local_patch_hash
upstream_update_policy
```

必须用固定 SHA checkout 后实际阅读 README、LICENSE、目录和核心文件，输出文件级复用矩阵：

```text
项目
commit_sha
文件/模块
许可证
直接复用 / 改造复用 / 只借鉴 / 放弃
理由
依赖
A 股适配问题
Token 成本问题
测试要求
本地补丁
```

未来升级上游版本时，必须重新审计新 commit，并生成差异报告；不能静默跟随主分支。

## 3.1 Serenity 两个仓库

### A. `muxuuu/serenity-skill`

地址：

`https://github.com/muxuuu/serenity-skill`

拟复用内容：

- 产业链研究总流程；
- 从市场故事到系统变化、必要部件、产业链层级、稀缺环节、上市公司、证据、失效条件的推理链；
- 先排产业链层级、后排公司；
- 证据等级；
- 稀缺环节评分卡；
- 研究路由、单公司挑战、候选比较；
- A 股官方证据路径。

建议定位：

> `IndustryBottleneckSkill` 和主题研究的总方法论来源，而不是整个系统的唯一总 Agent。

不得直接沿用未经 A 股历史验证的固定评分权重作为投资结论。

### B. `haskaomni/serenity-skill`

地址：

`https://github.com/haskaomni/serenity-skill`

拟拆用：

- `serenity-alpha`
- `bayesian-intrinsic-growth-valuation`
- `gf-dma-health-index`
- `tam-adj-peg`
- `buy-side-equity-research-memo`

建议映射：

- 新闻或事件到财务传导：`EventToAlphaSkill`
- 3～5 年增长假设概率：`GrowthProbabilitySkill`
- TAM 与成长持续性估值：`GrowthValuationLens`
- 日线趋势与基本面匹配：`DailyTrendHealthSkill`
- 最终研究备忘录结构和引用纪律：`ResearchMemoComposer`

必须做的 A 股改造：

- A 股公司普遍缺少美股式季度指引；
- 免费一致预期和上修数据未必稳定；
- GF-DMA 日线阈值不得机械套到小时线；
- 单独设计 `HourlySwingSkill`；
- 所有缺失数据应降级，不得编造；
- 估值公式和阈值必须通过历史案例与行业分层验证。

## 3.2 AuditAgent

论文：

`https://arxiv.org/abs/2510.00156`

仅借鉴方法论，不假设有可直接安装的实现或公开数据集。

应借鉴：

- 从已确认处罚案例学习会计科目风险先验；
- 关键词稀疏检索 + 向量语义检索；
- 单文档专家；
- 跨年度专家；
- 跨科目关联专家；
- 全局证据聚合；
- 优先定位证据链，而不是只输出二元“是否舞弊”。

必须防止：

- 用未来监管处罚泄漏历史标签；
- 把论文中的风险先验当作永久真理；
- 让所有公司都运行昂贵的全量跨文档 LLM 审计；
- 把“异常”直接写成“造假”。

## 3.3 `leafpaper/claude-company-analysis`

地址：

`https://github.com/leafpaper/claude-company-analysis`

建议只把它当：

> 代码捐献者、规则参考和 MVP 起点，不整套照搬其多写手、多 Reviewer 报告流水线。

重点核查和拆用：

- `scripts/financial_audit.py`
- `scripts/pdf_reader.py`
- `scripts/derived_metrics.py`
- 红旗 Reviewer 规则；
- 数值来源标签；
- 信息缺口闭环；
- 监控和版本比较思路；
- 单元测试。

不要直接照搬：

- 多个串行写手反复生成长报告；
- 固定使用 Tushare 的数据层；
- 未经校准的总评分；
- 估值、技术面、舆情和财务审计耦合在一起；
- 对所有行业运行完全相同的 11 套框架。

需要建立数据 Provider 抽象，允许 AKShare、BaoStock、QMT、Tushare 和手工导入互换。

## 3.4 FinRobot

地址：

`https://github.com/AI4Finance-Foundation/FinRobot`

只借鉴：

- Lead Agent / Orchestrator；
- 数据、分析、建模、综合和报告职责分离；
- 数值由代码计算；
- LLM 只解释和综合；
- provenance；
- Bull / Bear / Judge 的条件化启用；
- 可追踪运行记录。

不要整套 fork 当前全栈系统。原因：

- 体量过大；
- 与本项目 Windows 单机、低成本目标不匹配；
- 桌面端和复杂前端不是 MVP 必需；
- 本项目应优先轻量 DAG、FastAPI/CLI/Streamlit；
- 多空辩论只能在升级条件命中时运行。

## 3.5 PyOD / PyGOD

- PyOD：用于表格和时间序列异常检测；
- PyGOD：用于实体关系图异常检测。

使用原则：

- 输出异常分数和调查线索，不输出“舞弊结论”；
- 先实现可解释的行业分位数、稳健 Z-score、Isolation Forest；
- PyOD 作为第二层补充；
- PyGOD 只有在公司—子公司—股东—客户—供应商—关联方图谱质量达到要求后才启用；
- 必须有行业和商业模式条件化；
- 每个模型记录特征版本、训练窗口、参数、污染率和适用行业。

---

# 4. 总体架构要求

请优先设计为单体模块化系统，而不是微服务集群。

建议总流程：

```text
数据采集与质量控制
        ↓
统一市场数据仓 + 统一证据库
        ↓
确定性市场扫描器
        ↓
专家 Skill 的轻量 screen()
        ↓
候选注册表：合并、去重、硬性初筛
        ↓
共性投资内核：BaseCasePack
        ↓
专家路由器
        ↓
仅运行命中的 analyze(stock) 差分 Skill
        ↓
财务可信度与红旗审计（按条件升级）
        ↓
人工调查任务生成器
        ↓
用户导入补证材料
        ↓
反方/证据审查（按条件升级）
        ↓
统一委员会裁决器
        ↓
程序化风险管理
        ↓
模拟交易协议
        ↓
断点回放、持续跟踪和 Skill 评测
        ↓
市场状态和策略权重更新
```

## 4.1 模块建议

至少规划：

1. `core/orchestrator`
2. `core/run_planner`
3. `core/router`
4. `core/policy_engine`
5. `core/cost_manager`
6. `providers/market`
7. `providers/filings`
8. `providers/manual_import`
9. `data_quality`
10. `evidence_store`
11. `document_pipeline`
12. `candidate_registry`
13. `skills/common`
14. `skills/specialists`
15. `skills/serenity`
16. `skills/distilled`
17. `financial_integrity`
18. `manual_investigation`
19. `counter_case`
20. `portfolio`
21. `paper_trading`
22. `market_regime`
23. `evaluation`
24. `knowledge_ingestion`
25. `ui`
26. `cli`
27. `tests`

---

# 5. 数据层与多时间尺度行情

## 5.1 数据源角色

### 免费优先

- AKShare：公开行情、分钟数据、市场宽度、行业、公告和公开财经数据的运输层；
- BaoStock：日线、交易日和部分财务历史底座；
- QMT/xtquant：如果用户现有券商权限允许，作为可选行情源和交叉验证源；
- 巨潮资讯、上交所、深交所、北交所：法定披露和问询函；
- 证监会及相关失信/处罚平台；
- 各政府、监管和行业官方平台；
- 用户手工上传的 PDF、网页存档、截图和结构化文件。

### 付费备用

- Tushare 只作为可替换备用，不得成为免费 MVP 的硬依赖；
- 商业分钟数据和新闻权限只有在免费数据稳定性被量化证明不足后才考虑。

## 5.2 Provider 能力注册表与 Phase 0 分钟数据硬闸门

每个行情 Provider 必须动态记录：

- 支持市场；
- 支持频率；
- 可请求的起止日期；
- 实际返回的起止日期；
- 历史保留长度；
- 是否复权；
- 是否包含成交额；
- 时间戳语义；
- 上午/下午切片规则；
- 限速；
- 账户或终端权限依赖；
- 最近一次实测；
- 数据质量评分；
- 是否处于可用状态；
- 备用顺序。

系统启动时运行轻量能力探测，不要把“免费接口永远可用”写死。

Phase 0 必须输出唯一的分钟数据闸门结论：

```text
MINUTE_DATA_GO
MINUTE_DATA_PARTIAL
MINUTE_DATA_NO_GO
```

建议判定：

- `MINUTE_DATA_GO`：用户 QMT 或其他合法数据源能为候选股和开放仓位稳定提供所需 1m/5m 增量，字段和时间覆盖通过测试；
- `MINUTE_DATA_PARTIAL`：只能稳定取得近期 1m、有限历史 5m，或仅部分标的/时段可用；允许增强模拟，但必须支持降级；
- `MINUTE_DATA_NO_GO`：无法稳定取得足够分钟数据；MVP 模拟交易使用小时/日线保守模型，基本面系统继续开发。

Phase 0 至少抽样：

- 沪市主板、深市主板、创业板、科创板、北交所；
- 正常交易、停牌、复牌、涨跌停、除权附近标的；
- 高流动性与低流动性标的；
- 近 5 个交易日、近 30 日、近 1 年和更早历史；
- 1m、5m、1h、1d；
- 未复权和可用复权口径。

必须记录“请求范围”和“实际返回范围”，不能只看接口不报错。

## 5.3 K 线可靠底座与积累策略

免费 MVP 的确定性承诺应是：

- 小时或日线作为可靠研究底座；
- 只对候选股、重点观察股、相关指数和开放模拟仓位持续积累 5 分钟数据；
- 1 分钟数据属于可选增强；
- 不承诺回填系统启用前的大量历史分钟数据；
- 分钟数据不足不阻塞公告、财务、产业链和价值投研系统。

### 原始层

- 1 分钟：只在 Provider 实测可得时，用于近期恢复和盘中路径增强；
- 5 分钟：只对有限标的持续本地积累，不默认全市场长期回填；
- 1 小时：优先由可靠 5 分钟数据聚合；若无法积累，则使用已通过质量验证的 Provider 小时线；
- 日线：长期历史、市场状态和公司行为基础；
- 交易日历；
- 未复权真实价格；
- 公司行为和复权因子。

### 派生层

- 60 分钟 K 线由本地 5 分钟数据按统一规则聚合时，记录聚合版本；
- 无5分钟底座时可以使用 Provider 60分钟线，但需记录来源和切片口径；
- 日线、周线和月线由标准化数据派生；
- 研究序列与真实成交序列分离；
- 任何复权序列均可由未复权价格和公司行为重建。

## 5.4 停机后的分级恢复与降级回放

不得假设系统原则上总能找到 1 分钟或 5 分钟历史数据补回停机区间。

恢复逻辑：

1. 读取每个标的最后处理的交易时间；
2. 根据交易日历计算缺失交易时段；
3. 查询 Provider 当前真实可提供的范围；
4. 能获得并通过质量检查的近期区间优先使用 1 分钟；
5. 更早区间仅在5分钟数据已经本地积累或 Provider 实测可靠时使用5分钟；
6. 若分钟数据不足，按 TradeProtocol 指定的降级模型使用小时线、下一交易日开盘、下一交易日收盘或日线保守成交；
7. 若连日线都不完整，标记 `UNREPLAYABLE`，不得虚构成交；
8. 恢复任务允许不同区间使用不同分辨率；
9. 不能把“未获取到”解释为“该分钟无成交”；
10. 不因模拟回放降级而阻断基本面研究模块。

每次回放保存：

```text
requested_resolution
actual_resolution
replay_quality
provider_id
coverage_start
coverage_end
missing_bar_count
data_quality_flags
fill_model_version
fallback_reason
```

`replay_quality` 至少包括：

```text
EXACT_1M
VERIFIED_5M
PROVIDER_1H_APPROX
DAILY_OPEN_MODEL
DAILY_CLOSE_MODEL
DAILY_CONSERVATIVE
UNREPLAYABLE
```

若一分钟数据出现开盘为 0、字段错位或其他已知异常，应尝试备用源或按数据质量规则修复；无法验证时必须降级，不可静默填值。

## 5.5 数据质量

必须检查：

- 请求区间与实际返回区间；
- 缺失交易时段；
- 重复 K 线；
- 时间戳错位；
- 午休跨越；
- 开盘/收盘边界；
- 成交量单位；
- 价格为 0；
- 高低价关系；
- 复权异常；
- 公司行为；
- 停复牌；
- 不同源冲突；
- 涨跌停价格；
- 股票更名、退市和代码映射。

保存原始数据，标准化数据可重建。

# 6. 事实层、证据库与财报文档处理

## 6.1 数据分层

采用四层：

1. **事实原文层**：监管、交易所、公司法定披露、政府记录；
2. **结构化数据层**：AKShare、BaoStock、QMT、Tushare 等运输与整理；
3. **宏观和事件层**：部委、央行、统计局、期货交易所、海外官方源；
4. **社区和作者层**：只做假设生成、方法论蒸馏和盲点提示。

社区内容不得直接升级为强证据。

## 6.2 文档处理

优先：

1. 下载并保存原始文件；
2. 文件哈希与版本识别；
3. 原生 PDF 文本提取；
4. 版面和章节识别；
5. 表格提取；
6. 仅在必要时 OCR；
7. OCR 结果与原始页图绑定；
8. 所有提取结果缓存，避免重复消耗。

重点章节：

- 主表；
- 财务报表附注；
- 收入确认；
- 应收、存货、预付、其他应收；
- 在建工程、固定资产、无形资产；
- 商誉和减值；
- 关联方与子公司；
- 或有事项；
- 审计意见；
- 关键审计事项；
- 内控报告；
- 管理层讨论；
- 重大合同；
- 会计政策和估计变更；
- 前期差错更正。

## 6.3 Claim—Evidence 模型

请设计：

```text
Claim
Evidence
ClaimEvidenceLink
SourceDocument
SourceSnapshot
SourceEntity
EvidenceConflict
```

关键要求：

- 一个 Claim 可由多个 Evidence 支持或反驳；
- 一条 Evidence 可以支持多个 Claim；
- 推断必须引用其依赖事实；
- 关键事实引用覆盖率低于阈值时不得输出高置信度；
- 来源冲突进入人工核查；
- 失效链接由本地快照和哈希补偿；
- 引用必须精确到页码、章节或 DOM 位置。

---

# 7. 财务可信度与红旗审计 Agent

正式名称：

> **财务可信度与红旗审计 Agent**  
> Financial Integrity & Red-Flag Agent

不得输出：

```json
{"is_fraud": true}
```

应输出风险、异常、冲突、正常解释和证据缺口。

## 7.1 内部结构

```text
Financial Integrity & Red-Flag Agent
│
├── 确定性报表校验器
├── 盈余质量规则引擎
├── 跨期异常
├── 同行业异常
├── 财报文本审计
├── 审计师与治理
├── 跨文档证据
├── 实体关系异常
└── 解释竞争与证据缺口
```

## 7.2 确定性校验

至少包括：

- 资产负债表恒等式；
- 三表勾稽；
- 现金变动与现金流；
- 同比、环比、TTM 重算；
- 每股数据和股本变化重算；
- 单位和币种一致性；
- 合并范围变化；
- 报表版本和更正。

## 7.3 盈余质量与红旗

候选规则：

- Beneish M-Score；
- Piotroski F-Score；
- Altman Z-Score；
- Sloan Accrual；
- DuPont；
- 经营现金流/净利润；
- 应收/收入；
- 存货/成本；
- 预付款/采购；
- 其他应收款；
- 在建工程/资本开支；
- 研发资本化率；
- 非经常损益依赖；
- 毛利率与同行偏离；
- 税费、员工和产能与收入变化不一致；
- 货币资金与利息收入不匹配；
- 商誉和减值；
- 前五大客户供应商集中；
- 关联交易；
- 资金占用和担保。

每条规则必须拥有：

```text
rule_id
formula_version
source_reference
applicable_industries
excluded_industries
required_fields
minimum_periods
threshold_source
calibration_status
known_false_positive_modes
severity
output_type
tests
```

## 7.4 行业适用性

禁止把同一组指标机械应用于所有公司。

至少分开：

- 普通制造；
- 周期与资源；
- 工程和政府采购；
- 软件与互联网；
- 消费与零售；
- 医药和生物科技；
- 银行；
- 保险；
- 证券；
- 房地产；
- 公用事业；
- 早期亏损成长公司。

例如：

- Beneish 和 Altman 不应机械用于银行保险；
- 亏损生物科技不应因传统 PE/现金流规则被简单判死刑；
- 周期公司必须使用正常化盈利；
- 高应收可能是商业模式特征，也可能是风险，需要同行和合同结构解释。

## 7.5 AuditAgent 式跨文档审计

按条件升级后运行：

1. 根据历史确认案例建立科目风险先验；
2. 先定位高风险科目；
3. 结构化章节切片；
4. BM25/关键词稀疏检索；
5. 向量语义检索；
6. 单文档内部矛盾；
7. 同科目跨年度变化；
8. 跨科目关联；
9. 财报—问询函—回复—处罚—更正之间的冲突；
10. 全局聚合；
11. 生成可审计证据链。

## 7.6 异常检测

第一层：

- 行业分位数；
- 稳健 Z-score；
- 同公司 3～10 年偏离；
- 规则阈值。

第二层：

- Isolation Forest；
- ECOD/HBOS 等 PyOD 方法；
- 多模型一致性。

第三层：

- 在高质量实体图谱建立后，才考虑 PyGOD。

异常检测只产生：

- 异常程度；
- 对比组；
- 可能原因；
- 建议核查项。

## 7.7 解释竞争

每个重要红旗至少生成：

- 舞弊或粉饰解释；
- 正常经营解释；
- 行业特征解释；
- 数据质量解释；
- 需要什么证据才能区分。

## 7.8 标准输出

设计 `FinancialIntegrityEvidencePack`，至少包含：

```text
company_id
as_of
reporting_periods
raw_source_ids
verified_numbers
recalculated_metrics
rule_findings
time_series_anomalies
peer_anomalies
document_conflicts
governance_findings
regulatory_findings
possible_benign_explanations
evidence_gaps
requested_followups
risk_level
hard_blocks
confidence_components
evidence_conflict_score
rule_versions
model_versions
```

它不直接给股票总评分。

---

# 8. 人工调查任务生成器

正式职责不是继续无限自动搜索，而是：

> 当现有自动化数据和证据库无法解决关键主张时，生成少量、可执行、低成本的人工调查任务。

建议拆成：

1. `EvidenceGapResolver`：代码检查现有数据库、自动 Connector 和用户上传；
2. `ManualInvestigationPlanner`：只在仍缺失时生成任务卡。

每次最多 3～7 项，绝对不超过 10 项。

默认只生成：

- 预计 15～45 分钟能完成；
- 可能改变估值、风险等级或最大仓位；
- 优先免费、权威、可追溯；
- 有明确停止条件。

## 8.1 所有上市公司通用检查

- 最近三年年报；
- 审计意见和关键审计事项；
- 问询函及回复；
- 差错更正和业绩修正；
- 关联交易；
- 担保和资金占用；
- 监管函、处分和处罚；
- 审计机构、财务负责人和高管变更；
- 企业主体、核心子公司和历史名称；
- 执行、限制消费和重大诉讼；
- 客户供应商与股东管理层潜在关联。

优先来源：

- 巨潮资讯；
- 上交所、深交所、北交所；
- 中国证监会及失信记录平台；
- 国家企业信用信息公示系统；
- 中国执行信息公开网；
- 中国裁判文书网；
- 信用中国。

## 8.2 专项任务模板

### 政府采购、工程、政企软件

核查：中标公告、合同公告、项目编号、联合体份额、延期/变更/终止/重新招标、公司披露与采购侧对应。

来源：中国政府采购网、全国公共资源交易平台、各省市公共资源交易中心。

### 制造、化工、能源、矿业和重资产

核查：批准产能、环评、能评、排污许可、项目建设和投产、原料设备能源、矿权土地、环保处罚、在建工程转固、设计产能与实际产量。

### 医药、生物科技和医疗器械

核查：正式批准、受理/审评/临床/上市阶段、申办方、适应症、注册证持有人、突破性治疗、优先审评、抽检、召回和处罚。

来源：国家药监局、药审中心、临床试验登记平台、地方药监部门。

### 软件、互联网和平台

核查：域名和 App 主体、ICP 备案、产品运营状态、专利权人和法律状态、发明专利/实用新型/软著区别、客户官网反向验证。

来源：工信部备案、国家知识产权局、App 商店、客户官网、政府采购。

### 金融机构

核查：金融监管处罚、资本充足/偿付能力/流动性、不良与拨备、大股东和关联交易、分支机构许可、重复处罚。

### 出口制造和跨境贸易

核查：HS 编码、行业进出口趋势、海外客户和进口商、贸易商中转、海关/UN Comtrade/ImportYeti 覆盖边界。贸易数据只能作为旁证。

### 消费、汽车和零售

核查：召回、抽检、门店、渠道折扣、库存、经销和直营、电商价格销量评价。地图和电商只作为低等级观察证据。

## 8.3 任务卡 Schema

每项任务包含：

```text
task_id
待验证主张
为什么重要
影响的投资结论
优先级
预计耗时
推荐免费信息源
可复制搜索词
操作步骤
需要保存的材料
支持信号
反驳信号
信息源局限
完成条件
停止条件
无法获得时的替代证据
主体全称/子公司/历史名称
证据等级
```

规则：

- 未搜索到不等于不存在；
- 必须核对主体；
- 达到足够支持或反驳的证据后停止；
- 不允许任务无限扩张；
- 输出只能是已验证、存在矛盾、尚未验证或公开信息无法验证。

---

# 9. 社区作者和书籍的知识蒸馏

## 9.1 目标来源

优先考虑：MR Dang、黄彦臻、派大星皮皮、寒武纪的鳄鱼、未来新增白名单作者、MR Dang 的《价值投资功法》PDF，以及用户有合法访问权限的其他资料。

## 9.2 不做“大师人格微调”

第一阶段采用：

```text
原始内容
→ 自动清洗
→ 人工审核
→ 观点卡片
→ 方法规则
→ Skill
→ 历史案例评测
→ 生产启用
```

不直接把整本书、所有评论或长文塞进微调。

## 9.3 知乎采集器

使用：

- Playwright；
- 持久化浏览器 Profile；
- 用户手动登录；
- Cookie 本机加密保存；
- Cookie 不写进代码、日志或 Git；
- 单线程或极低并发；
- 可配置随机间隔；
- 403、429、安全验证时暂停；
- 不设计验证码绕过、签名破解或风控规避；
- 支持用户手动保存 HTML/Markdown 作为降级路径；
- 只采集用户有权访问的白名单内容；
- 记录权利和用途状态；
- 只用于个人研究，不默认允许再发布原文。

## 9.4 两阶段采集

目录阶段保存：作者 ID、内容 ID、类型、标题、URL、发布时间、更新时间、评论数、内容哈希、采集状态和游标。

详情阶段保存：正文、问题上下文、图片和引用链接、评论树、编辑时间、原始 HTML、结构化 JSON 和断点状态。

## 9.5 评论线程保留规则

只保留目标博主参与过的线程：

1. 建立完整树；
2. 找出目标博主的所有评论；
3. 向上追溯每条博主评论到根节点；
4. 保留这些路径的并集；
5. 保留理解上下文所需的祖先；
6. 保留博主回复后的必要追问和博主再次回复；
7. 删除与博主无关的其他分支。

## 9.6 断点续爬

保存：

```text
author_id
content_type
content_id
page_cursor
comment_cursor
status
last_success_at
retry_count
last_error
content_hash
```

每一页、每一篇、每个评论分页后写检查点；要求幂等、失败队列、重启继续、内容更新采用版本而非覆盖。

## 9.7 自动和人工清洗

自动降权或删除：重复段落、营销、口号、无关故事、纯情绪、无证据断言、重复案例、过时即时价格、纯修辞。

保留：判断规则、分析步骤、证据偏好、估值方法、反例、失败案例、仓位、风险、退出、失效条件、观点变更。

人工界面支持：保留/删除、拆分/合并、修正作者标的时间、方法论标签、适用行业和周期、已验证/已证伪、补充官方证据、批准进入 Skill、撤回 Skill。

## 9.8 Skill Manifest

每个 Skill 至少包含：

```text
skill_id
name
version
source_authors
source_material_ids
description
triggers
applicable_markets
applicable_industries
applicable_horizons
decision_questions
required_inputs
optional_inputs
required_evidence_types
reasoning_steps
positive_signals
negative_signals
invalidation_conditions
known_failure_modes
output_schema
cost_class
dependencies
incompatible_skills
evaluation_cases
approval_status
```

共性方法抽入通用层，真正分歧保留为条件化规则。

---

# 10. 统一投资内核与专家差分

## 10.1 BaseCasePack

统一内核只运行一次，包含：

```json
{
  "company_id": "",
  "as_of": "",
  "business_model": [],
  "revenue_drivers": [],
  "profit_drivers": [],
  "cash_flow_quality": {},
  "capital_returns": {},
  "reinvestment": {},
  "management_and_governance": {},
  "competitive_position": {},
  "industry_supply_demand": {},
  "valuation_expectations": {},
  "price_and_trend_context": {},
  "known_risks": [],
  "evidence_gaps": [],
  "specialist_tags": [],
  "base_confidence": 0.0,
  "coverage": {},
  "evidence_ids": []
}
```

## 10.2 专家路由器

路由依据：收入来源、主要资产、价值驱动、投资命题、证据缺口、风险暴露、市场和持有周期。

实现顺序：

1. 明确行业和业务规则；
2. 关键词；
3. Skill 描述 embedding 相似度；
4. 低置信度边界案例才调用小模型；
5. 默认最多激活 1～3 个专家；
6. 超过上限需要升级理由。

路由单位是“当前投资命题需要什么知识”，而不是仅看申万行业。

## 10.3 专家接口

```text
screen(universe_context) -> CandidateDelta[]
analyze(base_case, scoped_evidence) -> SpecialistDelta
```

专家差分输出：

```json
{
  "skill_id": "",
  "skill_version": "",
  "incremental_findings": [],
  "base_case_corrections": [],
  "industry_specific_metrics": [],
  "additional_evidence_requests": [],
  "failure_modes": [],
  "confidence_delta": 0.0,
  "valuation_adjustments": [],
  "risk_adjustments": [],
  "evidence_ids": [],
  "coverage_delta": {}
}
```

专家不得重复写完整公司介绍和通用财务分析。

## 10.4 专家覆盖不足

没有匹配 Skill 时标记 `specialist_coverage = insufficient`，并禁止最高置信度、降低最大仓位、显式提示仅通用框架覆盖、生成外部专家需求、在委员会中加入不确定性折扣。

---

# 11. 候选生成和股票池系统

删除昂贵的“股票池 LLM Agent”，保留股票池系统。

候选来源：纯 Python 因子和市场扫描、公告事件、价格成交量异动、财务异常、产业 Skill 的 `screen()`、用户输入、手动观察和现有持仓复核。

候选注册表负责：代码标准化、去重、来源 Skill、推荐原因、首次发现、最近复核、证据强度、数据完整度、流动性、可交易性、行业风格暴露和生命周期。

建议状态：

```text
DISCOVERED
SCREENED
RESEARCHING
NEEDS_INFO
WATCHLIST
PAPER_PENDING
PAPER_OPEN
PAPER_CLOSED
REJECTED
ARCHIVED
```

必须保留一个独立、便宜的市场扫描器，避免完全继承博主历史偏好。

---

# 12. 统一委员会与风险管理

## 12.1 委员会不是多个 Agent 固定投票

采用“共性的决策制度，消费个性的增量证据”。

输入：BaseCasePack、SpecialistDelta、FinancialIntegrityEvidencePack、人工补证、CounterCasePack、市场状态、当前组合、专家覆盖度、数据证据质量和流动性。

决策维度：预期收益、下行损失、证据强度、变体认知、催化剂、时间期限、组合相关性、流动性、覆盖度、未解决假设、财务可信度和市场状态。

优先实现为：硬门槛 → 程序化决策矩阵 → 一个可选 LLM 综合说明 → 仅冲突或高风险时启用反方 Agent。

**委员会不得重新搜索。** 裁决阶段只能读取已经冻结、带版本和哈希的标准化工件，不得自行重新联网、重新读取整份财报、重新抓取新闻或启动无界研究。发现关键缺口时，只能返回：

```text
NEEDS_INFO
```

并生成明确的信息缺口，退回人工调查或上游研究节点。这样保证不同节点使用同一信息截面，并防止成本失控。

## 12.2 升级触发

只有以下情况才做深度审计或多 Agent 辩论：

- 计划仓位超过阈值；
- 基础分析与专家冲突；
- 财务异常；
- 证据不足但潜在收益高；
- 系统低覆盖领域；
- 多个专家严重分歧；
- 重大新公告；
- 关键证伪条件接近触发；
- 组合风险显著变化。

## 12.3 风险引擎

程序化且不可被普通 LLM 绕过：总仓位、单股上限、行业上限、风格暴露、流动性、相关性、最大回撤、连续亏损、数据异常冻结、财务红旗限制、专家覆盖折扣、重大公告冻结、手动紧急停止、禁止杠杆、禁止未经用户确认的实盘订单。

---

# 13. 模拟交易系统

## 13.1 自建模拟账户核心，VeighNa 只作设计参考

核心必须是本地事件驱动、可恢复、可审计的模拟账户，不依赖外部虚拟账户 API。

VeighNa PaperAccount 只允许借鉴：

- OrderData / TradeData / PositionData 等对象思想；
- 委托、成交、持仓事件顺序；
- 到价成交的基本接口；
- 滑点和事件驱动适配思路。

不得把 VeighNa PaperAccount 的账户账本、持久化和绩效核算作为本系统核心。系统必须自己实现：

- 双重记账账户账本；
- 现金、可用现金、冻结资金和应收应付；
- 订单、撤单、拒单和成交事件日志；
- 佣金、印花税、过户费和其他费用；
- 幂等事件 ID；
- 回放游标；
- 公司行为账本；
- 分红、送转、配股和除权处理；
- 持仓成本与已实现/未实现盈亏；
- 组合净值序列；
- 订单、成交、账务事件之间的可重放映射；
- 崩溃恢复和一致性校验。

建议使用 append-only event log + 可重建快照。账本发生不平衡时必须停止回放并报警。

## 13.2 每个投资判断必须生成 TradeProtocol

至少包含：

```text
protocol_id
decision_id
company_id
strategy_id
skill_versions
created_at
signal_time
earliest_executable_time
holding_horizon
entry_rule
entry_order_type
position_size_rule
price_stop_rule
volatility_stop_rule
trailing_stop_rule
time_stop_rule
thesis_invalidation_rule
take_profit_rule
review_events
max_holding_period
cost_model_version
fill_model_version
evidence_snapshot_id
confidence
```

不同投资风格可以使用不同退出逻辑：波段使用价格/ATR/移动止损；长线使用基本面失效、估值极端、事件复核和时间复核。不强制长线价值投资全部使用简单百分比止损。

协议创建后不可回写历史，修改必须创建新版本并记录生效时间。

## 13.3 启动恢复流程

1. 读取开放订单、持仓、协议、账户快照和最后事件序号；
2. 校验双重记账账本平衡和事件连续性；
3. 找到每个标的最后处理时间；
4. 取得交易日历和 Provider 能力；
5. 根据 `MINUTE_DATA_GO/PARTIAL/NO_GO` 及当前实际覆盖，选择1m、5m、1h或日线降级路径；
6. 拉取并验证可获得数据，记录请求范围与实际范围；
7. 按时间顺序回放；
8. 处理公司行为；
9. 处理订单、冻结、成交、费用和现金账；
10. 触发止损、止盈、时间退出和复核；
11. 每根或每批 K 线保存幂等检查点；
12. 更新组合净值和绩效序列；
13. 生成恢复报告；
14. 对分辨率降级、路径不确定和不可回放交易单独标记。

不得因为分钟数据缺失而自动假设所有订单都按理想价格成交。

## 13.4 同一 K 线内路径不确定

即使 1 分钟 K 线也可能同时触及止盈和止损。优先尝试更细数据或备用源；无法确认则使用保守规则；标记 `ambiguous_intrabar_path=true`；同时计算乐观和保守结果，主绩效采用保守结果。

## 13.5 A 股规则

必须按日期配置：T+1、100 股整数、交易时间、停牌、涨跌停、不同板块、风险警示、一字板、佣金最低收费、印花税、过户费、滑点、成交量约束、部分成交、分红、送转、配股、除权、退市和委托有效期。不得使用当前规则覆盖全部历史。

## 13.6 模拟评测账户

至少维护：规则基准、BaseCase-only、BaseCase + 单专家、完整委员会、重要 Skill 影子账户、沪深300或中证全指、简单等权候选基准。

---

# 14. 市场状态、在线学习和强化学习

## 14.1 市场状态

至少区分：趋势牛市、高波动牛市、震荡、趋势熊市和恐慌。

输入：指数 60 分钟与日线趋势、市场宽度、新高新低、成交额、行业扩散、波动率、回撤、风格相对表现和模拟策略绩效。多数策略亏损只能是特征之一。

## 14.2 冻结权重、影子评测与后续训练顺序

在积累足够的独立模拟决策前，所有 Skill、专家和策略权重必须冻结并版本化。MVP 阶段：

- 使用预先声明的固定权重或硬门槛；
- Skill 运行独立影子账户；
- 不根据短期盈亏在线修改权重；
- 不允许人工事后修改历史权重；
- 同一批候选、同一 TradeProtocol、同一成交模型下，对比 `BaseCase-only` 与 `BaseCase + SpecialistDelta`；
- 专家增量价值必须通过 walk-forward 和样本外测试；
- 必须覆盖多个市场状态，而不是只经历单一牛市或熊市；
- 无足够样本时输出“尚不能判断该专家有增量价值”。

后续顺序：

```text
固定权重与影子账户
→ 规则状态机
→ 离线动态权重研究
→ 上下文多臂赌博机影子运行
→ 最后才考虑 PPO/A2C 等强化学习
```

强化学习只调整 Skill 权重、策略权重、总体风险敞口、现金比例、波段和价值策略分配、行业/单股上限，不得直接无约束生成股票订单。

第二次开发计划必须为每次升级定义：

- 最小独立决策数；
- 每个市场状态的最低样本量；
- 样本外窗口；
- 基准账户；
- 置信区间；
- 最大回撤和稳定性阈值；
- 是否优于固定权重；
- 失败后的回滚机制。

MVP 默认不实现在线学习和深度强化学习。

# 15. Token、API 和推理成本控制

## 15.1 三种运行模式

### A. Deterministic Mode

不调用 LLM；数据、筛选、财务规则、异常检测和模拟交易照常运行，只输出模板化结果与待人工处理包。

### B. Manual LLM Packet Mode

系统导出紧凑任务包；用户手动交给 Codex/Claude；模型按 JSON Schema 返回；系统校验导入；保存模型、提示词、工件哈希和结果版本。

### C. OpenAI-Compatible Mode

用户配置官方 API、自有反向代理或中转。系统通过统一 Provider 接口调用，不依赖固定域名。每次运行先估算成本或配额；硬限制单次、每日和每月预算；超预算自动降级，不允许静默超支。

如果中转无法提供精确 Token 价格或缓存账单，应支持：

- 用户维护价格表；
- 按返回 usage 估算；
- 导入中转账单进行事后对账；
- 使用请求次数、输入字符和输出字符作为备用成本指标；
- 成本数据缺失时标记 `COST_UNKNOWN`，不得显示虚假精度。

## 15.2 RunPlanner 和 TokenBudgetManager

运行前输出：将运行哪些节点、为什么触发、每个节点预计输入/输出 Token、预计成本、缓存命中、可降级步骤、深度模式是否需要用户批准。

保存：

```text
provider
model
prompt_version
skill_version
input_tokens
cached_input_tokens
cache_write_tokens
output_tokens
reasoning_tokens
estimated_cost
actual_cost
run_id
artifact_hash
```

## 15.3 工件缓存优先于 Prompt Cache

缓存键建议：

```text
skill_version
prompt_version
model_id
evidence_pack_hash
base_case_hash
question_hash
output_schema_version
```

只有输入事实或 Skill 版本变化时才重新调用。常规监控只分析新增变化，不重复分析整份年报。

## 15.4 Prompt Cache

如果使用支持缓存的 API：固定规则和示例放前部，公司变量数据放后部；保持工具定义稳定；记录缓存写入和读取；不假设缓存必然省钱；按当前模型缓存价格和实际复用次数测算；缓存只是补充，不能替代架构去重。

## 15.5 不使用无限长 previous_response 链

每次研究任务独立保存结构化状态，使用状态压缩、工件引用、摘要和新增证据增量，不依赖长会话隐藏历史成本。

## 15.6 模型分层

- 代码路由：不使用模型；
- 简单分类/抽取：低成本模型或本地模型；
- 财报关键矛盾：中档模型；
- 高风险跨文档综合：强推理模型；
- 最终报告：一次紧凑生成；
- Reviewer：仅条件触发。

请给出零 API、极低预算、深度研究三套成本场景。价格不得写死，应在实施时重新核验。

---

# 16. 评测体系

## 16.1 财务审计评测

使用证监会处罚、问询函、财报更正、非标审计、已确认内控问题和正常同行样本。

评测：问题召回、证据召回、引用支持、年份页码、误报、无害解释、证据不足识别、时间穿越、行业适用性和置信度校准。

## 16.2 投资研究评测

- 5、20、60 个交易日方向；
- 目标区间；
- 置信度校准；
- 催化剂；
- 失效条件；
- 引用覆盖；
- 信息缺口；
- 观点修改；
- 专家 Delta 增量价值。

## 16.3 交易评测

- 扣费后收益；
- 超额收益；
- 最大回撤；
- MFE/MAE；
- 盈亏比；
- 胜率；
- 持有周期；
- 换手；
- 流动性；
- 路径不确定比例；
- 1 分钟与 5 分钟回放差异。

## 16.4 蒸馏 Skill 评测

- 复现方法而非文风；
- 引用原始观点；
- 适用行业；
- 错误场景拒绝触发；
- 失效条件；
- 无未来泄漏；
- 相对于共性内核的增量。

所有历史评测还必须报告：PIT 覆盖率、`CERTIFIED/DOCUMENT_RECONSTRUCTED/APPROXIMATED/NOT_PIT_SAFE` 分布、退市股覆盖、历史成分覆盖和被排除样本。

---

# 17. 存储建议与唯一事实来源

```text
objects/sha256/
    内容寻址原始对象：PDF、HTML、JSON、截图、原始下载文件

data/parquet/
    标准化行情、财务、因子、事件、实体关系；唯一结构化分析事实源

knowledge/raw/
knowledge/clean/
knowledge/cards/
knowledge/skills/

artifacts/
    EvidencePack
    BaseCasePack
    SpecialistDelta
    FinancialIntegrityEvidencePack
    DecisionPack
    TradeProtocol
    Agent报告

state.sqlite
    任务、游标、锁、配置、订单、成交、账本、现金、持仓、检查点、幂等事件

research.duckdb
    外部读取 Parquet、建立视图、执行因子/横截面/回测/分析查询

logs/
    运行日志、错误、成本、审计日志
```

硬性分工：

- 原始文档和网页使用 SHA-256 内容寻址对象存储；
- Parquet 是结构化分析数据的唯一事实源；
- DuckDB 主要建立读取 Parquet 的视图，不再复制同一行情、财务和事件事实；
- SQLite 只保存操作状态和事务账本，不作为分析事实仓；
- 报告和工件引用事实源的版本和哈希，不自行维护另一套数字；
- 原始对象不可覆盖；
- 清洗和派生结果可重建；
- 所有工件有 Schema 版本；
- 迁移可回滚；
- 关键表有索引和唯一约束；
- 明确每类数据的唯一写入模块；
- 定期执行跨存储一致性校验。

# 18. UI 和操作方式

MVP 优先 CLI、Streamlit 或轻量 Web 控制台、Windows 任务计划程序；FastAPI 仅在需要模块接口时使用。

不要优先开发 Tauri 桌面、React 大型前端、Kubernetes、消息队列集群、分布式训练和大型向量数据库。

最小 UI 页面：今日运行、候选注册表、单标的研究、证据引用、人工调查任务、导入用户材料、财务红旗、模拟账户、恢复报告、Skill 绩效、Token/API 成本、数据源健康。

---

# 19. 安全、隐私、版权与合规

## 19.1 秘密管理

Cookie、API Key、QMT 凭据不得进入 Git；本地加密；日志脱敏；`.env.example` 仅写字段；导出任务包前检查敏感信息；支持一键清除会话和 Cookie。

## 19.2 社区内容

仅抓白名单和用户有权访问内容；不绕过验证码和访问控制；不批量再发布原文；记录权利状态；书籍和付费内容默认仅本地个人研究；报告尽量引用摘要和来源，不复制大段原文。

## 19.3 交易

初期禁止自动实盘下单，只做研究、模拟和用户确认；后期接券商前核查最新程序化交易、券商接口和报告要求；实盘适配器与模拟引擎隔离并默认关闭。

---

# 20. 推荐技术栈

请评估并说明取舍：

- Python 3.11；
- uv；
- Pydantic v2；
- pandas 或 Polars；
- NumPy；
- DuckDB；
- Parquet；
- SQLite；
- scikit-learn；
- PyOD；
- PyGOD（后期）；
- PyMuPDF；
- PDF 表格提取工具；
- Playwright；
- FastAPI；
- Streamlit；
- APScheduler 或 Windows Task Scheduler；
- pytest；
- Hypothesis；
- ruff；
- mypy/pyright；
- Git。

Agent 框架可评估轻量自研 DAG、PydanticAI、OpenAI Agents SDK 或其他方案。优先中央 Manager/Orchestrator，不要让 Agent 自由接管整个会话。

---

# 21. 两阶段交付合同

## 21.1 第一次输出：只做阻断项与 Go/No-Go

当前第一次回复必须严格按以下顺序：

### A. 阻断性问题与已采用假设

- 只列真正影响架构选择的问题；
- 能采用保守默认值的问题直接写明假设，不要无限追问；
- 标出相互冲突的需求；
- 不超过 20 项。

### B. 子系统可行性矩阵

至少覆盖：

- 小时/日线行情底座；
- 1m/5m 分钟增强；
- QMT/xtquant；
- 官方公告与PDF；
- PIT 历史评测；
- 财务审计规则；
- PyOD；
- 开源 Skill 复用；
- 自有 OpenAI-compatible 接口；
- 知乎采集；
- 本地模拟账本；
- 单机性能。

每项给出：`GO / CONDITIONAL_GO / NO_GO / NEEDS_TEST`、依据、需要实测的变量和降级方案。

### C. Phase 0 Go/No-Go 实验矩阵

每个实验必须给出：

```text
experiment_id
purpose
preconditions
sample_scope
exact_steps
expected_outputs
pass_criteria
partial_criteria
fail_criteria
estimated_time
estimated_cost
result_artifact
fallback_if_failed
```

至少包含：

1. AKShare 1m/5m/1h 实际历史范围和字段质量；
2. QMT/xtquant 1m/5m/1h/1d 权限与历史深度；
3. `MINUTE_DATA_GO/PARTIAL/NO_GO` 判定；
4. 退市股、历史成分和 PIT 数据可得性；
5. 原生PDF、扫描PDF、表格、页码和引用解析；
6. pinned commit 开源代码的 Windows/Python 3.11 可运行性；
7. 自有 OpenAI-compatible 端点的连通、JSON Schema、超时、重试、usage和成本；
8. 本机批量因子、PDF解析和DuckDB查询性能；
9. 知乎持久化登录、目录抓取、评论树和验证码暂停；
10. 双重记账模拟账本最小原型的确定性与崩溃恢复。

### D. 闸门结果枚举

至少定义：

```text
MINUTE_DATA_GO / MINUTE_DATA_PARTIAL / MINUTE_DATA_NO_GO
QMT_GO / QMT_PARTIAL / QMT_NO_GO
PIT_GO / PIT_PARTIAL / PIT_NO_GO
PDF_GO / PDF_PARTIAL / PDF_NO_GO
LLM_PROVIDER_GO / LLM_PROVIDER_PARTIAL / LLM_PROVIDER_NO_GO
KNOWLEDGE_INGEST_GO / KNOWLEDGE_INGEST_PARTIAL / KNOWLEDGE_INGEST_NO_GO
LOCAL_COMPUTE_GO / LOCAL_COMPUTE_PARTIAL / LOCAL_COMPUTE_NO_GO
```

### E. 用户执行清单和结果回填模板

提供一份紧凑模板，使用户运行测试后直接粘贴结果。第一次输出到此停止，不得继续生成完整计划。

## 21.2 第二次输出的触发条件

用户提供 Phase 0 结果后：

1. 先解释每个闸门结果对架构的影响；
2. 删除无法成立的理想假设；
3. 选择实际数据频率、Provider、LLM 模式和模拟成交降级策略；
4. 再生成完整开发计划。

## 21.3 第二次输出：完整开发计划必须包含

### 21.3.1 结论先行

- 是否可在该硬件和预算上完成；
- 哪些功能属于 MVP；
- 哪些必须推迟；
- 推荐最终架构；
- 最大风险；
- Phase 0 结论如何改变方案。

### 21.3.2 固定版本开源审计

逐个分析两个 Serenity 仓库、AuditAgent、leafpaper、FinRobot、PyOD、PyGOD、VeighNa PaperAccount，给出文件级“直接复用/改造/只借鉴/不使用”，并输出：

```text
repository_url
commit_sha
license_at_commit
audited_files
audit_date
local_patch_set
```

### 21.3.3 模块架构

给出模块依赖图、运行 DAG、单标的分析时序、启动恢复时序、人工补证闭环、模拟交易回放时序和 Token 升级时序。

### 21.3.4 仓库目录

给出可直接创建的目录树，并说明每个目录职责和唯一写入所有权。

### 21.3.5 存储和数据库

给出：

- 内容寻址对象存储布局；
- SQLite 事务状态与账本表；
- Parquet 分区与 Schema；
- DuckDB 外部视图；
- 索引、唯一约束、迁移、保留、哈希和版本；
- 跨存储一致性检查。

### 21.3.6 Schema

至少给出字段级 Schema：

- SourceDocument
- Evidence
- Claim
- EvidencePack
- BaseCasePack
- SpecialistDelta
- FinancialIntegrityEvidencePack
- ManualInvestigationTask
- CandidateRecord
- CoverageScore
- DecisionPack
- TradeProtocol
- LedgerAccount
- LedgerEntry
- Order
- Fill
- Position
- CorporateActionEvent
- PortfolioNAV
- ReplayCheckpoint
- ReplayQuality
- PointInTimeMetadata
- SkillManifest
- RunManifest
- CostLedger
- ModelRun
- DataProviderCapability
- OpenSourceAuditManifest

### 21.3.7 数据源矩阵

逐类列出自动/手工源、权威等级、成本、历史深度、PIT 状态、限速、字段、失败模式、备用源和 MVP 是否接入。

### 21.3.8 财务可信度设计

规则注册表、行业 Profile、AuditAgent 式检索、PyOD、跨文档、解释竞争、硬阻断、评测集和防泄漏。

### 21.3.9 Knowledge/Skill 设计

爬虫、清洗、评论树、断点、观点卡片、Skill Manifest、自动/人工审核、版本、评测和撤回。

### 21.3.10 路由和 Token 预算

路由算法、最大专家数、升级条件、三种 LLM 模式、反向代理 Provider、Token 估算、API 预算、工件缓存、Prompt 缓存、失败降级和状态压缩。

### 21.3.11 委员会和冻结工件

委员会只能消费冻结工件，不得重新搜索；设计 `NEEDS_INFO` 退回路径、决策版本、信息截面和成本上限。

### 21.3.12 模拟交易

1m/5m/1h/日线分级回放、分钟数据闸门、双重记账、事件日志、A 股规则、成交模型、同 K 线路径、断点、公司行为、影子账户和绩效归因。

### 21.3.13 分阶段开发路线

至少拆成：

#### Phase 0：技术验证

数据源能力探测、分钟数据闸门、QMT 权限、PIT 可得性、PDF 解析、pinned commit 脚本测试、反向代理/手动模式、性能和模拟账本原型。

#### Phase 1：数据、对象存储、状态和模拟账户

小时/日线可靠底座、有限标的分钟积累、质量、本地双重记账、回放、基准策略、无 LLM 可运行。

#### Phase 2：证据库和官方文档

公告、PDF、Claim—Evidence、引用、持续快照和 PIT 标记。

#### Phase 3：财务可信度 MVP

确定性规则、行业 Profile、跨期、PyOD、EvidencePack、人工任务。

#### Phase 4：Serenity 和通用投资内核

BaseCase、产业瓶颈、Event-to-Alpha、估值、日线趋势、小时波段、差分输出。

#### Phase 5：知识蒸馏

知乎采集、书籍清洗、卡片、Skill、评测。

#### Phase 6：委员会和选择性反方

冻结工件、决策制度、风险、覆盖度、升级、模拟交易。

#### Phase 7：冻结权重影子评测

固定权重、相同候选集和交易协议的 BaseCase 对照、多个市场状态、walk-forward 和样本外评估。

#### Phase 8：可选自适应权重研究

只有 Phase 7 达到样本门槛后，才研究规则动态权重、上下文赌博机和 RL；默认不进入 MVP。

每阶段必须给输入、任务、产物、验收、测试、可回滚点、预算和明确不做内容。

### 21.3.14 测试计划

至少包含单元、属性、合约、数据源 Schema 漂移、断点恢复、幂等、账本平衡、PIT、幸存者偏差、模拟交易确定性、同 K 线歧义、引用正确性、Token 超预算、Cookie 失效、API失败、数据冲突、行业规则适用性、Skill 路由、影子账户和回归测试。

### 21.3.15 风险清单

至少讨论免费数据源不稳定、历史分钟不足、复权、PDF 表格、PIT 缺失、财务规则误报、监管标签延迟、社区版权、Cookie 和封禁、LLM 幻觉、反向代理中断、Token 失控、过拟合、RL 样本不足、模拟成交乐观、复杂度失控、单机性能、Windows 兼容、开源许可证和版本变化。

### 21.3.16 最终建议

输出：

1. 推荐的 MVP 边界；
2. 第一批 10～20 个具体开发任务；
3. 先审计的固定版本开源文件；
4. 仍需用户实测的决策；
5. 明确推迟的功能；
6. 一份可以直接交给 Codex 创建仓库骨架的后续任务清单，但本轮不要生成全部代码。

# 22. 重要禁止事项

- 不要把所有开源项目整套拼在一起；
- 不要把 FinRobot 全栈直接 fork 为 MVP；
- 不要复制 leafpaper 的多写手长报告流水线；
- 不要跟随未固定的主分支进行审计；
- 不要让每个专家重复读取完整财报；
- 不要让 LLM 计算财务数字；
- 不要把异常等同造假；
- 不要让社区观点成为事实层；
- 不要默认 Tushare 付费数据；
- 不要把任何模型上游、域名、价格或反向代理写死；
- 不要默认免费分钟接口永久稳定或总能回填停机区间；
- 不要因分钟数据失败阻塞基本面投研；
- 不要用当前交易规则覆盖全部历史；
- 不要把系统停机天数简单等同于可获得的分钟数据范围；
- 不要把 VeighNa PaperAccount 当作本系统账本和持久化核心；
- 不要让委员会在裁决阶段重新搜索或重新读取整份原始材料；
- 不要固定运行所有 Agent；
- 不要无限增长对话上下文；
- 不要在没有 Point-in-Time 防护的情况下正式评测历史；
- 不要把 `CURRENT_PROVIDER_VALUE` 当成历史时点值；
- 不要只使用当前仍上市股票和当前指数成分；
- 不要在积累足够样本前在线调整专家或 Skill 权重；
- 不要让 SQLite、DuckDB、Parquet 各保存一份独立事实真相；
- 不要先开发漂亮前端再补数据和模拟正确性；
- 不要在 MVP 接入实盘自动下单；
- 不要为了“完整”而牺牲低成本、可审计和可恢复性；
- 第一次输出不得越过 Preflight，直接生成泛泛的超长开发计划。

# 23. 输出风格

- 使用中文；
- 结论明确；
- 先讲建议，再讲理由；
- 对不确定项标明“需实测”；
- 对开源代码给文件路径；
- 对数据结构给字段；
- 对模块给输入、输出和错误处理；
- 对阶段给验收标准；
- 不要只写概念性口号；
- 不要用虚假的精确收益预测；
- 不要承诺系统可以稳定盈利；
- 不要把研究输出写成直接买卖指令；
- 不要为了显得先进而强行使用强化学习、多 Agent 辩论或大型模型。

---

# 24. 已核验的外部约束与参考地址

以下信息作为 Phase 0 的已知背景，但仍须在用户环境中实测：

1. AKShare 股票分钟接口文档：
   - `https://akshare.akfamily.xyz/data/stock/stock.html`
   - 东财 `stock_zh_a_hist_min_em` 的 1 分钟数据文档说明仅返回近 5 个交易日且不复权，并提示除最近交易日外开盘字段可能为 0。
2. XtQuant 行情模块：
   - `https://dict.thinktrader.net/nativeApi/xtdata.html`
   - 支持 `1m`、`5m`、`1h`、`1d` 等周期和历史下载；实际数据能力与本地 MiniQMT 和其连接的行情服务器一致，必须实测。
3. VeighNa PaperAccount：
   - `https://www.vnpy.com/docs/cn/community/app/paper_account.html`
   - 基于实盘行情；不提供资金计算；订单和成交关闭后不保存；持仓单独持久化；撮合不考虑盘口挂单量。
4. 固定版本仓库：
   - `https://github.com/muxuuu/serenity-skill/commit/c2fe93deedfd0d1bd9fe7ef0601ea1b9c20ea24a`
   - `https://github.com/haskaomni/serenity-skill/commit/332037ea5f41ce7f150afbedb3517bcd1f1b2833`
   - `https://github.com/leafpaper/claude-company-analysis/commit/d908885640cc7d4ff06f1ee48b1beb2ab13012be`
   - `https://github.com/AI4Finance-Foundation/FinRobot/commit/297a8d28d099be328c8a8eb658b4f782b93f3651`
5. AuditAgent 论文：
   - `https://arxiv.org/abs/2510.00156`

Claude 不得把以上文档描述替代用户环境实测，也不得因某接口“理论支持”就直接判定为 `GO`。
