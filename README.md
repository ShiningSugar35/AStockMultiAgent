# AStockMultiAgent

AStockMultiAgent 是一套本地优先、可审计、可恢复的 A 股多 Agent 投研与模拟交易系统。自然语言入口可以是 Codex、ChatGPT、Gemini 等支持本地 MCP/仓库工具的模型；确定性的数据、计算、证据、风控和账本工作由 Python 3.12 的 `astock` CLI 完成。

系统的长期设计、尚未完成的真实运行义务和已经验收的工程事实分别维护在：

- [低成本A股多Agent投研系统方案.md](低成本A股多Agent投研系统方案.md)
- [开发计划.md](开发计划.md)
- [验收报告.md](验收报告.md)
- [phase7_status.md](phase7_status.md)

## 当前产品能力

| 能力 | 当前状态 | 主要入口 |
|---|---|---|
| 数据 / ObjectStore / Parquet / SQLite / PIT | 已实现 | `probe`、`sync-*`、`reference-*` |
| 官方公告、PDF/DOCX、Claim-Evidence | 已实现 | `disclosure-*`、文档 / evidence CLI |
| 财务可信度与红旗审计 | 已实现 | `financial-*` |
| Research Seeds → Candidate 自动 Promotion | 已实现 | `research-seeds`、`research-seeds-promote`、`candidate-audit` |
| 候选研究池 | 已实现 | Promotion 自动组装严格 `CandidateInputRelease`；`candidate-input-run` 保留为手工/诊断入口 |
| 单公司 Research Runtime | 已实现 | `research-plan`、`research-run-company`、`research-audit` |
| Institutional Fundamental Research | 已实现 | `institutional-research-schema/finalize`、`fundamental-model-audit`、`institutional-decision-context-freeze` |
| Serenity typed specialists | 已实现 / v4 上游审计 | `research-skills-v3`：6 个 Serenity 方法 + 独立 Hourly Swing；含 Juglar 周期阶段 |
| Knowledge Skill composite registry | 已发布 | `KnowledgeSkillProvider`，653 admitted Skills |
| 投资委员会 | 已实现 | `committee-*`；委员会只消费冻结工件 |
| PIT TradingClassification | 已实现 | `trading-classification-*` |
| 最终模拟研究协议 | 已实现 | `ClassifiedTradeProtocol`、`trade-plan-view` |
| 组合评估 | 已实现 | `portfolio-paper-evaluate`、`portfolio-evaluate` |
| 组合构建 | 已实现 | `portfolio-construct`、`portfolio-audit` |
| 模拟交易 / 5m 回放 | 已实现核心链 | `paper-*`；任何账本写入仍需独立人工确认 |
| Phase 7 前向影子评测 | 程序完成 / 真实样本采集中 | 正式 6-arm study，当前 0/100 |
| Phase 8 自适应准入 | 程序完成 / `NOT_ADMITTED` | 真实 Phase 7 证据达标前全部关闭 |

项目**没有真实券商自动下单接口**。真实交易只能由用户在券商端自行执行。

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
只保留当前 APPROVE_SIMULATION 的 ClassifiedTradeProtocol
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

Agent 的默认低 token 工作方式是：先读最终压缩工件，再按 evidence locator 精确打开必要证据。不要让多个 Agent 重复读取同一批原文。

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

# 单股研究
uv run astock research-plan 600519 --as-of 2026-08-11T10:00:00+08:00
uv run astock research-run-company 600519 --as-of 2026-08-11T10:00:00+08:00
uv run astock research-status <research-run-id>
uv run astock research-audit <research-run-id>
uv run astock trade-plan-view <classified-protocol-artifact-id>

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
