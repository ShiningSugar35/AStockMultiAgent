# AStockMultiAgent Workflow Catalog

Workflow 是跨 Skill 的**用户任务编排层**。它不替代 `.agents/skills/*/SKILL.md`，也不替代 Python/Schema/Config；它只定义完成一个完整投资研究目标时的步骤、依赖、并发、fallback、停止条件和用户输出。

## 当前 Workflow

| Workflow | 典型用户问题 | 主 Skill | 关键输出 |
|---|---|---|---|
| [Current Company Research](workflow-current-company-research.md) | “现在买中国海油合适吗？” | `$company-deep-research` | 当前研究判断 / 正式 DecisionPack（若链闭合） |
| [Candidate Discovery](workflow-candidate-discovery.md) | “现在有哪些股票值得深入研究？” | `$candidate-scan` | ResearchSeed / Candidate shortlist |
| [Evidence Recovery](workflow-evidence-recovery.md) | “这个关键事实为什么缺证据？” | `$evidence-investigation` | 冻结证据或一次性人工清单 |
| [Financial Integrity](workflow-financial-integrity.md) | “财报靠谱吗？现金流质量如何？” | `$financial-integrity-audit` | FinancialIntegrity evidence/report |
| [Committee & Trade Plan](workflow-committee-trade-plan.md) | “研究完成后能不能模拟买？什么条件进出？” | `$company-deep-research` | DecisionPack / ClassifiedTradeProtocol / TradePlanView |
| [Portfolio Construction](workflow-portfolio-construction.md) | “把几只研究过的股票组成组合” | `$portfolio-manager` | Risk report / 4 allocation proposals |
| [Holding Monitoring](workflow-holding-monitoring.md) | “持仓有什么变化，要不要加减仓？” | `$holding-monitor` | HoldingReviewPack / action proposal |
| [Paper Trading](workflow-paper-trading.md) | “模拟盘恢复、回放、状态是否正常？” | `$paper-trading-recovery` | paper status / replay checkpoint / NAV |
| [Knowledge Ingest](workflow-knowledge-ingest.md) | “把这个作者/书籍方法沉淀进知识库” | `$knowledge-ingest` | coverage / immutable source / reviewed Skills |
| [Prospective Evaluation](workflow-prospective-evaluation.md) | “怎么积累前瞻样本、判断系统是否真的有效？” | `$astock-research-orchestrator` | Phase 7/11/12 prospective evidence |
| [Adaptive Edge Diagnostics](workflow-adaptive-edge.md) | “provider/schema/规划为什么失败，Agent 能否自动适配？” | `$astock-research-orchestrator` / `$evidence-investigation` | Validated plan / recovery validation / candidate dialect |

## 通用编排原则

1. **先复用，后抓取**：先查已冻结且 audit 通过的工件，再做增量采集。
2. **Policy-driven 自动路由，Manual 最后**：SourceAccessRouter 按 officiality/capability/health/freshness/latency/cost/auth/retryability 评分；强官方能力优先 `PRIMARY_OFFICIAL`。普通 provider 失败不能直接等价为 `NEEDS_INFO`，Agent 可提出 Recovery proposal，但只能由 allowlisted deterministic validator 执行。
3. **并行只用于无依赖步骤**：身份确认后，行情、公司行动、年度财务、最新中期财务等可并发；Committee、TradingClassification 等依赖上游冻结输入的阶段必须串行。
4. **当前咨询与历史评估分开**：current live research 在采集结束时冻结统一 decision snapshot；历史 replay、backtest、Phase 7 prospective evaluation 保留严格 source-availability/PIT 防未来数据边界。
5. **研究充分性与执行资格分开**：长期基本面研究可以在执行层数据不完整时继续；模拟订单/精确交易计划仍需完整的 Committee + TradingClassification/执行门禁。
6. **内部日志与投资者输出分开**：reason code、artifact/hash、SQL、CLI transcript 留在诊断层；投资者默认只看结论、依据、估值/赔率、催化、风险和改变结论的条件。
7. **没有正式证据就不伪装正式结论**：允许给出明确标注的 provisional research view，但不得把它冒充 `APPROVE_SIMULATION`、BUY、目标价或仓位授权。
8. **永不真实券商下单**：所有 Workflow 都受 `broker_execution_allowed=false` 全局边界约束。

## 维护要求

- Workflow 中出现的 CLI 必须存在于 `uv run astock --help`。
- 每个 Repo Skill 至少链接一个与其主要任务对应的 Workflow。
- 新增跨 Skill 用户路径时优先补 Workflow，而不是把总控 Skill 无限拉长。
- 修改 Workflow 后应同步更新 `skills/README.md`、相关 Skill 和 `tests/unit/test_repo_skills.py` 的文档合同。
