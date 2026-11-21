# AStockMultiAgent Workflow Catalog

Workflow 是跨 Skill 的**用户任务编排层**。它不替代 `.agents/skills/*/SKILL.md`，也不替代 Python/Schema/Config；它只定义完成一个完整投资研究目标时的步骤、依赖、并发、fallback、停止条件和用户输出。

## 当前 Workflow

| Workflow | 典型用户问题 | 主 Skill | 关键输出 |
|---|---|---|---|
| [Full-Market Research Team](workflow-full-market-research-team.md) | “有什么好买的股？推荐几只股票” | `$astock-research-orchestrator` | On-demand team DAG / FullResearchInputReadinessReport / downstream RecommendationResearchReceipt + Publication Gate |
| [Current Company Research](workflow-current-company-research.md) | “现在买中国海油合适吗？” | `$company-deep-research` | 当前研究判断 / 正式 DecisionPack（若链闭合） |
| [Candidate Discovery](workflow-candidate-discovery.md) | “现在有哪些股票值得深入研究？” | `$candidate-scan` | ResearchSeed / Candidate shortlist |
| [Evidence Recovery](workflow-evidence-recovery.md) | “这个关键事实为什么缺证据？” | `$evidence-investigation` | 冻结证据 / 同请求续接 / 私人输入缺口 |
| [Financial Integrity](workflow-financial-integrity.md) | “财报靠谱吗？现金流质量如何？” | `$financial-integrity-audit` | FinancialIntegrity evidence/report |
| [Committee & Trade Plan](workflow-committee-trade-plan.md) | “研究完成后能不能模拟买？什么条件进出？” | `$company-deep-research` | DecisionPack / ClassifiedTradeProtocol / TradePlanView |
| [Portfolio Construction](workflow-portfolio-construction.md) | “把几只研究过的股票组成组合” | `$portfolio-manager` | Risk report / 4 allocation proposals |
| [Portfolio Transition & Hedge-Aware Completion](workflow-portfolio-transition-and-hedging.md) | “准备买 X，再配什么能降低组合风险？” | `$portfolio-manager` | CURRENT/ANCHOR/TARGET / complement screen / target bands / hedge effectiveness |
| [Holding Rebalance Decision](workflow-holding-rebalance-decision.md) | “我已经持有 X，要不要加仓、减仓或调整组合？” | `$holding-monitor` | persisted user state / event-aware HOLD·ADD·TRIM·EXIT / target band |
| [Holding Monitoring](workflow-holding-monitoring.md) | “持仓有什么变化，要不要加减仓？” | `$holding-monitor` | HoldingReviewPack / action proposal |
| [Continuous Investment Monitoring](workflow-continuous-investment-monitoring.md) | “研究/荐股后继续盯着，什么时候该准备买卖？” | `$continuous-investment-monitor` | Watch target / MonitorEvent / leased research task / incremental review |
| [Paper Trading](workflow-paper-trading.md) | “模拟盘恢复、回放、状态是否正常？” | `$paper-trading-recovery` | paper status / replay checkpoint / NAV |
| [Knowledge Ingest](workflow-knowledge-ingest.md) | “摄取批准的本地书籍/文件或审计存量证据” | `$knowledge-ingest` | coverage / immutable source / reviewed Skills |
| [Prospective Evaluation](workflow-prospective-evaluation.md) | “怎么积累前瞻样本、判断系统是否真的有效？” | `$astock-research-orchestrator` | Phase 7/11/12 prospective evidence |
| [Adaptive Edge Diagnostics](workflow-adaptive-edge.md) | “provider/schema/规划为什么失败，Agent 能否自动适配？” | `$astock-research-orchestrator` / `$evidence-investigation` | Validated plan / recovery validation / candidate dialect |

- `workflow-ten-stock-q3-watch.md`：指定观察池的三季报前后持续研究与组合跟踪。

## 通用编排原则

1. **先复用，后抓取**：先查已冻结且 audit 通过的工件，再做增量采集。
2. **Policy-driven 自动路由，Manual 最后**：SourceAccessRouter 按 officiality/capability/health/freshness/latency/cost/auth/retryability 评分；强官方能力优先 `PRIMARY_OFFICIAL`。普通 provider 失败不能直接等价为 `NEEDS_INFO`，Agent 可提出 Recovery proposal，但只能由 allowlisted deterministic validator 执行。
3. **独立步骤并行、局部缺口局部恢复**：身份确认后，行情、公司行动、年度财务、最新中期财务等可并发；Committee、TradingClassification 只等待其实际依赖。一个辅助来源失败不取消其他 ready 分支；适用补证仍有价值时继续同一请求。
4. **当前咨询与历史评估分开**：current live research 在采集结束时冻结统一 decision snapshot；历史 replay、backtest、Phase 7 prospective evaluation 保留严格 source-availability/PIT 防未来数据边界。
5. **研究充分性与执行资格分开**：长期基本面研究可以在执行层数据不完整时继续；模拟订单/精确交易计划仍需完整的 Committee + TradingClassification/执行门禁。
6. **内部日志与投资者输出分开**：reason code、artifact/hash、SQL、CLI transcript 留在诊断层；投资者默认只看结论、依据、估值/赔率、催化、风险和改变结论的条件。
7. **没有正式证据就不伪装正式结论**：允许给出明确标注的 provisional research view，但不得把它冒充 `APPROVE_SIMULATION`、BUY、目标价或仓位授权。
8. **不引入真实券商执行**：当前产品没有真实券商执行能力或对应权限字段；需要模拟写入时继续走既有 paper confirmation。

## 维护要求

- Workflow 中出现的 CLI 必须存在于 `uv run astock --help`。
- 只有跨能力依赖、并发、恢复或副作用需要 Workflow；单角色方法留在 canonical Skill，无需一对一建文档。所有保留链接必须可解析。
- 新增跨能力用户路径时更新现有 Workflow；共享角色派发/登记合同只在[角色映射](../architecture/research-specialist-skills-crosswalk.md#shared-execution-contract)维护，专业角色不重复全量预读。
- 修改后同步导航与引用，按真实入口、链接和角色输入/输出验证；不以固定文档数量或重复文字作为能力已实现的证明。
