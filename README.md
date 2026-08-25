# AStockMultiAgent

AStockMultiAgent 是一套本地优先、可审计、可恢复的 A 股多 Agent 投研与模拟交易系统。自然语言入口可以是 Codex、ChatGPT、Gemini 等支持本地 MCP/仓库工具的模型；确定性的数据、计算、证据、风控和账本工作由 Python 3.12 的 `astock` CLI 完成。

系统的长期设计、尚未完成的真实运行义务和已经验收的工程事实分别维护在：

- [低成本A股多Agent投研系统方案.md](低成本A股多Agent投研系统方案.md)
- [开发计划.md](开发计划.md)
- [验收报告.md](验收报告.md)

## 当前产品能力

| 能力 | 当前状态 | 主要入口 |
|---|---|---|
| 数据 / ObjectStore / Parquet / SQLite / PIT | 已实现 | `probe`、`sync-*`、`reference-*` |
| Provider Resilience / 全市场按需油路 | 已实现 | ENV↔DIRECT transport lane、BaoStock→EastMoney→Sina fallback、per-market completeness gate、fresh verified snapshot reuse；无需后台预同步 |
| 官方公告、PDF/DOCX、Claim-Evidence | 已实现 | `disclosure-*`、文档 / evidence CLI |
| 财务可信度与红旗审计 | 已实现 | `financial-*` |
| Research Seeds → Candidate 自动 Promotion | 已实现 | `research-seeds`、`research-seeds-promote`、`candidate-audit` |
| 候选研究池 | 已实现 | Promotion 自动组装严格 `CandidateInputRelease`；`candidate-input-run` 保留为手工/诊断入口 |
| 全市场专业投研团队 / 正式荐股门 | 已实现 | `research-team-plan/status`、`research-recommendation-readiness`；Macro→行业→公司→Bull/Bear→Reviewer→Committee→Portfolio 全链，缺任一硬门只能观察 |
| 行业研究覆盖与 Skill Edge 治理 | 已实现 | `industry-research-archetypes/resolve`、`research-coverage-score`；22 类内部研究框架，Private Skill 仅作 Edge |
| 单公司 Research Runtime | 已实现 | `research-plan`、`research-run-company`、`research-audit` |
| Institutional Fundamental Research | 已实现 | `institutional-research-schema/finalize`、`fundamental-model-audit`、`institutional-decision-context-freeze` |
| Serenity typed specialists | 已实现 / v4 上游审计 | `research-skills-v3`：6 个 Serenity 方法 + 独立 Hourly Swing；含 Juglar 周期阶段 |
| Knowledge Skill audited registry | 已发布 / 237 active | historical 653 只保留审计身份；426 RETIRE Skill 从活动表/ObjectStore 物理压缩，Provider 只读取 237 条 active Skills |
| Knowledge Storage Lifecycle | 已实现 / 0057 | 1,066,886 行历史 Semantic/Distillation/Reviewed 流水冷归档；现役 FK 父级闭包保留；单行 knowledge Parquet 已分区合并；archive/restore/Parquet/VACUUM 均有显式审计命令 |
| Agent Observability | 已实现 / 0058 | Repo Skill selection/execution hit rate、仅标注样本 routing precision/recall、任务耗时、ResearchRun stage/provider/cache、双源数据对齐质量统一报表 |
| External Research Tech Scout | 已实现 | `$research-tech-scout`；GitHub/投研平台/社区发现 → 去重 → ADAPT/SHADOW/WATCH/REJECT，不自动改变生产权重 |
| PIT Temporal Validity | 已实现 | `pit-temporal-audit`：availability/reference time 分离、O(V+E) temporal non-interference；truncation property tests；`pit-knowledge-cutoff-diagnostic` 仅作跨时期衰减诊断 |
| 投资委员会 | 已实现 | `committee-*`；委员会只消费冻结工件 |
| PIT TradingClassification | 已实现 | `trading-classification-*` |
| 最终模拟研究协议 | 已实现 | `ClassifiedTradeProtocol`、`trade-plan-view` |
| 组合评估 | 已实现 | `portfolio-paper-evaluate`、`portfolio-evaluate` |
| 组合构建 | 已实现 | `portfolio-construct`、`portfolio-audit` |
| 持续投研与 Watch Universe | 已实现 / 0059 | 60m 行情、CNINFO、GDELT lead、Catalyst、typed rule、事件/研究任务、lease/heartbeat、恢复式 daemon |
| 模拟账户生命周期 | 已实现 | 保留账户/订单/成交/确认；默认 60m，5m 高精度 fallback；已确认开放模拟订单可由 monitor 持续 deterministic replay |
| Phase 7 前向影子评测 | 程序完成 / 真实样本采集中 | 正式 6-arm study，当前 0/100 |
| Phase 8 自适应准入 | 程序完成 / `NOT_ADMITTED` | 真实 Phase 7 证据达标前全部关闭 |

项目**没有真实券商自动下单接口**。真实交易只能由用户在券商端自行执行。

### Knowledge Storage Lifecycle

当前热库只保留 Research Runtime 所需状态与现役 provenance。旧 Semantic/Distillation/Reviewed/Book/Private 生产流水通过 `knowledge-cold-archive-run --confirm` 写入 `runtime/archive/knowledge-history/<digest>/` 的 zstd Parquet，并把 manifest 同步写入 ObjectStore；`knowledge-cold-archive-audit` 可逐文件校验 hash/行数，`knowledge-cold-archive-restore` 可完整恢复。所有仍被 Direct/Visual/Audited 热表引用的 FK 父级行自动保留，原始 SourceSnapshot/Evidence/Zhihu version/ObjectStore 不随归档删除。

历史 `knowledge_comments` / `knowledge_content` 的单行 Parquet 由 `knowledge-parquet-compact --confirm` 按已有 author/content_type/year 分区合并；兼容 additive schema 演进，旧记录缺失的新列补 `NULL`。全部 archive 与 Parquet audit 通过后，才允许 `state-vacuum --confirm` 把 SQLite free pages 一次性返还文件系统。

### Agent Observability 与技术自由人

`agent-observation-register` 为一次项目 Agent 任务冻结 eligible / selected / completed Repo Skills 与端到端耗时；普通任务不允许自行填写“正确答案”，只有人工标注、fixture 或独立评测才填写 `expected_skill_ids`，因此 routing precision/recall 只对真实标注子集计算。`agent-observability-report` 还直接聚合既有 `SkillUsageEvent`、ResearchRun wall/stage time、provider calls/cache hits 与 canonical 双源行情的 timestamp/OHLC/volume 对拍，不维护第二份性能事实源。

`$research-tech-scout` 负责持续扫描 GitHub、量化/投研平台、论文/官方文档及实践社区。外部发现先与当前能力去重，再标记为 `ADAPT_PATTERN / SHADOW_EXPERIMENT / WATCH / REJECT`；量化模型、因子、执行规则仍必须经过既有 PIT 和 prospective/shadow 门，社媒只作发现线索。

### PIT Temporal Validity

`pit-temporal-audit` 对一个明确 decision time 下的 source/window/resample/as-of join/retrieval/transform/decision 依赖图做 temporal non-interference 审计。每个节点同时保存 `reference_time` 与 `available_at`；真正决定“当时能不能用”的是 availability。只对 value-independent availability fragment 给出可证明检查，使用 active output dependency closure + topological propagation，复杂度为 `O(V+E)`；未知依赖、依赖环、节点早于依赖可用、未来节点污染当前决策或 value-dependent availability 都 fail closed。报告和原始请求均进入 ObjectStore，但该审计本身没有生产准入、模拟盘写入或券商权限。

`truncation_invariance_probe` 为 row-aligned 时间序列变换提供“截断未来后重算，当前前缀必须不变”的可复用检查；≤64 行默认全量检查，长序列默认最多取 64 个均匀覆盖 cutoff 并显式返回 `exhaustive=false`，需要全量时可显式传入全部 cutoffs，避免验证工具自身随历史长度形成近似二次写法。Hypothesis property tests 会随机追加未来后缀并验证 causal transform 不漂移，同时用显式 peeking transform 证明检查能抓到未来依赖。`pit-knowledge-cutoff-diagnostic` 仅把已冻结时期指标按模型 knowledge cutoff 分成 cutoff 前 / 后，并计算项目自定义的 `pre_alpha - post_alpha` 与 retention ratio；跨 cutoff 时期被排除，缺任一侧样本时返回 `NOT_EVALUABLE`。这些数值只用于识别可能的 parametric look-ahead / 泛化衰减，不构成泄漏定论，更不能直接改变 Phase 8、Committee 或 Paper 权重。

### 本地用户态、持续监控与持仓复核

系统将低成本 deterministic monitor 与高价值 Research Agent 分层：常驻 monitor 负责 Watch Universe、行情/公告/news lead/Catalyst、typed rule、已确认开放模拟订单回放和持久事件队列；需要语义判断的增量研究由可用 Agent worker/会话消费。每次投资类会话启动时，先把模拟账户同步到 Git 忽略的 `user_state/portfolio.md`、`orders.md`、`trades.md`，再读取 monitor 的 material delta/pending task 并做增量复核；若当前没有独立 LLM worker，任务会可靠排队而不会冒充已完成分析。

- `portfolio.md`：当前持仓、平均成本、最近复核动作与投资逻辑状态；
- `orders.md`：尚未完全成交的模拟订单；
- `trades.md`：已确认的模拟成交/用户记录；
- 三者均位于 `user_state/`，不会 push 到 Git。

默认用 **60 分钟 OHLC** 做持续/离线补回放：它可以判断限价是否在某个小时内被触及，但不能证明盘口排队与小时内先后路径，所以状态明确标为近似成交模拟。只有小时线存在实质歧义时才使用 `--resolution 5m` 做更精细复核。开放模拟订单可由 Continuous Monitor 周期性调用同一 deterministic replay；订单和成交仍严格分开：**下单不等于持仓，只有回放确认成交后才更新持仓。**

## 自然语言用法

仓库内 `.agents/skills/` 是网页端/Agent 的主要任务路由。总控 Skill 为 `$astock-research-orchestrator`。

### “这家公司怎么样？”

路由到 `$company-deep-research`：

```text
官方证据
→ 财务可信度 / Evidence Sufficiency
→ IndustryProfile / CompanyEconomics
→ DriverTree → Bull/Base/Bear Forecast → Valuation / Market-Implied Expectations
→ FundamentalModelBundle / InstitutionalDecisionContext
→ BaseCase
→ Serenity / Knowledge Skill Delta
→ 反方和证据缺口
→ 投资委员会
→ TradingClassification
→ ClassifiedTradeProtocol
→ 以 ANALYZED 纳入 Continuous Monitor / 写入已验证 typed rule
→ 面向普通用户的解释
```

先运行：

```powershell
uv run astock research-plan 300750 --as-of 2026-08-11T10:00:00+08:00
```

只补齐计划中真正缺失的冻结工件，不重复读取整个资料库。新当前公司意见默认先运行 `institutional-research-schema`，构建并 audit `FundamentalModelBundle`，再冻结 `InstitutionalDecisionContext`；随后 `research-run-company` 以 `institutional_research_required=true` 消费 exact bundle/context。Forecast、DCF/reverse-DCF 和 sensitivity 都由 Python 复算，模型只能给 evidence-bound assumption。要计算 expected return 或市场隐含预期时，价格必须通过 `MarketPriceAnchor` 绑定注册 artifact/hash 与 PIT 时间；裸价格不会进入计算。

当前 Serenity 方法注册表为 `research-skills-v3`：在原 Industry Bottleneck、Event-to-Alpha、Growth Probability、Growth Valuation、Daily Trend Health 基础上新增 `JuglarCycleStageSkill`，用于固定资产投资周期的需求/ASP/利润率/Capex/库存/产能/客户行为/资本市场反应八维证据、五阶段概率、反证和迁移信号；它只形成 SpecialistDelta，不直接给目标价或仓位。Serenity Growth/Valuation 只提供增量方法上下文，canonical `ForecastPack / ValuationPack` 是数值基本面预测与估值的唯一主账本，避免并行假设集。

### “这只股票能不能买？什么位置进？预期卖到哪？”

只有最终 `ClassifiedTradeProtocol` 才能进入交易计划解释：

```powershell
uv run astock trade-plan-view <ClassifiedTradeProtocol-artifact-id>
```

`TradePlanView` 汇总委员会的收益/下行情景、置信度、最大模拟仓位、entry rule、止损/移动止损/时间止损、take-profit、论文失效条件、复核事件和交易制度分类。

如果底层没有结构化的精确入场/退出价格证据，系统会明确返回：

- `exact_entry_zone_available=false`
- `exact_exit_target_available=false`

以参考价换算出的委员会收益区间只是**情景价格区间**，不是目标价预测。

### “评估我的投资组合”

模拟账户可直接：

```powershell
uv run astock portfolio-paper-evaluate --account-id paper --live
```

外部只读组合使用严格 `PortfolioAnalysisRequest`：

```powershell
uv run astock portfolio-schema
uv run astock portfolio-evaluate portfolio-analysis.json
```

组合报告覆盖：

- 年化波动与下行波动；
- Beta 与 Tracking Error；
- 最大回撤；
- 历史 VaR / CVaR / CDaR；
- HHI 与有效持仓数；
- 两两相关性；
- 边际风险贡献；
- 现金 / 总敞口；
- 行业或风险组暴露；
- 与委员会硬风险上限的冲突。

### “推荐几只股，组成一个组合”

推荐链先做**低成本 Research Seeds**，再做完整候选证据。Research Seeds 只回答“哪些标的值得花更高成本继续查”，**不能直接产生 BUY**。

```text
已有 RESEARCH_READY Candidate
        +
Market Seeds（流动性 / 规模 / 换手，仅用于研究优先级）
        +
Expert Seeds（当前已发布大 V Skills → 动态擅长领域 → 当前行业板块成分）
        ↓
ResearchSeedReport
        ↓
research-seeds-promote
        ↓
CandidateInstrumentUniverseProof + 官方公告 / 财务 / PIT / 质量 / 公司行动证据
        ↓
自动 CandidateInputRelease → Candidate Scan
        ↓
每只股票独立完成公司研究和投委会
        ↓
WATCH / APPROVE_SIMULATION 进入 RECOMMENDED 持续观察集
        ↓
只有当前 APPROVE_SIMULATION 的 ClassifiedTradeProtocol 进入组合构建
        ↓
Portfolio Construction
```

Expert Seeds 不硬编码“某作者擅长什么”。系统从当前 composite Knowledge registry 的 `skill_name / decision_question / core_principle / applicable_conditions / required_evidence / positive/negative signals` 重新统计每位作者对当前公开行业板块的 Skill 支持密度；知识 registry 更新后领域画像也随之更新。行业匹配只决定研究范围，任何公司事实仍需回到官方证据。

网页/MCP Agent 的第一步：

```powershell
uv run astock research-seeds --live
uv run astock research-seeds-status
uv run astock research-seeds-audit <ResearchSeedReport-artifact-id>
uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live
uv run astock research-seeds-promote-status
uv run astock research-seeds-promote-audit <SeedPromotionReport-artifact-id>
```

Promotion 只对 Seed 小集合自动补完整 Candidate 输入；已有 `RESEARCH_READY` Candidate 直接复用，证据未闭合的 Seed 返回结构化 task，不会要求网页模型手工拼大 JSON。`candidate-input-schema / candidate-input-run` 仅保留为手工或诊断入口。

组合构建：

```powershell
uv run astock portfolio-construct portfolio-construction.json
```

系统固定输出四套受约束提案：

1. `EQUAL_WEIGHT_CONSTRAINED`：默认稳健基准；
2. `INVERSE_VOLATILITY`；
3. `HIERARCHICAL_RISK`；
4. `SHRINKAGE_MIN_VARIANCE`：Ledoit-Wolf 协方差收缩 + long-only minimum variance。

默认不会用未校准的预期收益去最大化 Sharpe。单股、总敞口、组暴露等约束先应用，无法安全分配的资金保留现金。除非真实 Phase 7 样本外证据支持改变默认方法，否则受约束等权保持默认。

当前行业/风险组由调用者提供，报告会明确标记 `RISK_GROUP_IS_CALLER_SUPPLIED`；在接入可审计的正式行业 taxonomy release 前，不把该字段冒充官方行业分类。

## Repo Skills

当前主要 Skills：

- `$astock-research-orchestrator`：自然语言总控；
- `$candidate-scan`：候选研究池；
- `$company-deep-research`：单公司完整投研；
- `$financial-integrity-audit`：财务可信度；
- `$evidence-investigation`：关键证据补全；
- `$holding-monitor`：持仓增量复核；
- `$portfolio-manager`：组合评估与受约束构建；
- `$paper-trading-recovery`：模拟盘恢复；
- `$knowledge-ingest`：批准来源的知识采集。

人类可见能力目录见 [`skills/README.md`](skills/README.md)；canonical Skill 仍位于 `.agents/skills/`，避免复制第二套 `SKILL.md`。跨 Skill 的完整任务链统一记录在 [`docs/workflows/README.md`](docs/workflows/README.md)，当前覆盖单股研究、候选发现、证据补全、财务审计、投委会/交易计划、组合、持仓、模拟盘、知识采集和前瞻评估。

Agent 的默认低 token 工作方式是：先读最终压缩工件，再按 evidence locator 精确打开必要证据。不要让多个 Agent 重复读取同一批原文。

## Adaptive Edge / Deterministic Core

系统现在把“灵活”限定在可审计边界内：Provider/endpoint、transport、dialect、Current Research capability graph、Specialist 预算和 Portfolio allocator 都由版本化 policy/registry/plugin 描述；外部 Agent 可以提交 ResearchPlanner / ProviderRecovery / SchemaRepair proposal，但只能先形成 `PROPOSED` artifact，再由 Python deterministic validator 决定能否进入 `VALIDATED`。核心的官方事实认证、PIT、数学/会计、ObjectStore 不可变、模拟账本人工确认和 broker 禁用不会交给模型自由判断。

Schema drift 采用 raw-first：未知结构先保存原始 SourceSnapshot，再允许 Agent 提出 candidate mapping；只有多样本、官方交叉验证和仓库真实 contract test 全部通过后，才允许显式批准生成 `ADMITTED` candidate dialect。Candidate 不会自动覆盖 active dialect，也不会直接写正式事实，并支持 artifact audit 与 rollback。

正常股票咨询仍默认是投资者模式，不展示上述内部协议；这些能力只通过开发诊断命令或显式系统调试请求暴露。

## 常用确定性命令

```powershell
uv sync --all-groups
uv sync --extra semantic
uv run astock init
uv run astock probe

# Provider / reference
uv run astock provider-list
uv run astock sync-instruments --live
uv run astock sync-calendar --exchange XSHG --start 2026-08-01 --end 2026-08-11 --live
uv run astock sync-daily 600519 --market XSHG --start 2026-04-01 --end 2026-08-11 --live

# Research Seeds / Candidate
uv run astock research-seeds --live
uv run astock research-seeds-promote <ResearchSeedReport-artifact-id> --live
uv run astock research-seeds-promote-audit <SeedPromotionReport-artifact-id>
uv run astock candidate-status --scan-id <scan-id>
uv run astock candidate-audit <scan-id>

# 当前单股研究：先采集，再在采集结束时冻结 current decision snapshot
uv run astock research-acquire-current 600519 --market XSHG
uv run astock research-plan 600519 --mode LIVE
uv run astock research-run-company 600519 --mode LIVE --institutional-research-required
uv run astock research-status <research-run-id>
uv run astock research-audit <research-run-id>
uv run astock trade-plan-view <classified-protocol-artifact-id>

# Adaptive Edge / developer diagnostics (read-only first)
uv run astock research-capability-status 600519 --market XSHG
uv run astock provider-dialect-status
uv run astock adaptive-edge-status
uv run astock adaptive-edge-schema

# Agent proposal validation; these only freeze/validate internal artifacts, not facts/orders
uv run astock adaptive-plan-validate planner-proposal.json
uv run astock adaptive-recovery-validate recovery-proposal.json
uv run astock adaptive-schema-repair-validate schema-repair-proposal.json
# Candidate dialect admission requires explicit approval and still does not mutate active dialect
uv run astock adaptive-schema-repair-admit <validation-id> --approve
uv run astock adaptive-artifact-audit <artifact-id>
uv run astock adaptive-dialect-rollback <candidate-release-id>

# 历史/recorded 研究仍显式提供 --as-of，保持防未来数据边界
uv run astock research-plan 600519 --as-of 2026-08-11T10:00:00+08:00

# 本地用户态 / 会话式模拟账户
uv run astock local-portfolio-init
uv run astock local-portfolio-sync-paper
uv run astock local-portfolio-status
uv run astock sync-hourly 600519 --market XSHG
uv run astock paper-replay 600519 --market XSHG --cursor <ISO时间>
# 小时线存在成交路径歧义时才切 5m
uv run astock paper-replay 600519 --market XSHG --cursor <ISO时间> --resolution 5m

# 组合
uv run astock portfolio-paper-evaluate --account-id paper --live
uv run astock portfolio-evaluate portfolio-analysis.json
uv run astock portfolio-construct portfolio-construction.json
uv run astock portfolio-audit <portfolio-report-artifact-id>

# Phase 7 / 8
uv run astock phase7-study-ensure
uv run astock shadow-status --study-id <study-id>
uv run astock shadow-audit <study-id>
uv run astock phase8-status --study-id <study-id>

# 工程门
uv run pytest
uv run ruff check .
uv run pyright
```

## 数据和安全边界

- 所有正式历史输入必须 point-in-time safe；未来可见事实不得倒灌。
- 社区内容只作方法和研究线索，关键公司事实回到公告、交易所和财报。
- 委员会不联网、不搜索，只读冻结工件；缺证据返回 `NEEDS_INFO`。
- 候选排名不是交易指令。
- 组合优化器不能覆盖单股 `REJECT / WATCH / NEEDS_INFO`。
- 未复权日线用于真实成交价格和风险诊断时，会显式暴露公司行动跳变风险；不会静默把复权价当成交价。
- `runtime/`、Cookie、浏览器 Profile、私有资料和密钥不进入 Git。
- Phase 7 历史回放和 fixture 永远不能增加正式 forward-event count。
- Phase 8 在真实前向证据达到门槛前保持 `NOT_ADMITTED`；任何自适应研究都需要后续显式、版本化批准。
