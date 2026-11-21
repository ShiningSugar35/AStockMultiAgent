# AStockMultiAgent Skills Catalog

本目录是**人类可见的 Skill 索引**，不是第二套 Skill 实现。

AStockMultiAgent 的 canonical Agent Skills 位于 [`../.agents/skills/`](../.agents/skills/)；这是 ChatGPT/Codex 等 Agent 实际发现与加载的目录。不要把同一份 `SKILL.md` 再复制到本目录，否则会形成两套真相。

## Skill 与 Workflow 的边界

- **Skill**：一个稳定能力域的使用契约，回答“这个能力什么时候触发、应该调用哪些确定性命令、有哪些硬边界”。
- **Workflow**：多个 Skill/CLI 跨步骤协作的用户任务，回答“为了完成一个完整目标，先做什么、哪些步骤可以并行、何时 fallback、何时停止”。
- **代码/Schema/Config**：仍是系统事实来源；Skill/Workflow 不能绕过代码门禁，也不能创造代码不存在的能力。

完整 Workflow 索引见 [`../docs/workflows/README.md`](../docs/workflows/README.md)。

## Canonical Skills

| Skill | 主要用途 | Canonical 文件 | 编排或执行合同 |
|---|---|---|---|
| `$astock-research-orchestrator` | 自然语言总控与多步骤路由 | [SKILL.md](../.agents/skills/astock-research-orchestrator/SKILL.md) | current company / candidate / portfolio / prospective |
| `$candidate-scan` | 低成本 Research Seed → Candidate 研究池 | [SKILL.md](../.agents/skills/candidate-scan/SKILL.md) | [candidate discovery](../docs/workflows/workflow-candidate-discovery.md) |
| `$company-deep-research` | 当前或历史单公司完整研究 | [SKILL.md](../.agents/skills/company-deep-research/SKILL.md) | [current company research](../docs/workflows/workflow-current-company-research.md) |
| `$evidence-investigation` | 决策相关证据缺口与自动 fallback | [SKILL.md](../.agents/skills/evidence-investigation/SKILL.md) | [evidence recovery](../docs/workflows/workflow-evidence-recovery.md) |
| `$financial-integrity-audit` | 财报口径、勾稽、异常与审计 | [SKILL.md](../.agents/skills/financial-integrity-audit/SKILL.md) | [financial integrity](../docs/workflows/workflow-financial-integrity.md) |
| `$macro-policy-regime` | 宏观/政策状态、数据 vintage 与公司传导 | [SKILL.md](../.agents/skills/macro-policy-regime/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$industry-value-chain` | 行业价值链、盈利池与非循环可比公司 | [SKILL.md](../.agents/skills/industry-value-chain/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$catalyst-event-research` | 主来源催化剂/风险事件时间线 | [SKILL.md](../.agents/skills/catalyst-event-research/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$governance-management-quality` | 控制、激励、资本配置与披露质量 | [SKILL.md](../.agents/skills/governance-management-quality/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$investment-red-team` | 独立多空后的反证、脆弱性与重复计入审查 | [SKILL.md](../.agents/skills/investment-red-team/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$model-risk-backtest-validation` | 泄漏、多重检验、校准、成本与鲁棒性验证 | [SKILL.md](../.agents/skills/model-risk-backtest-validation/SKILL.md) | [共享团队合同](../docs/architecture/research-specialist-skills-crosswalk.md#shared-execution-contract) |
| `$holding-monitor` | 外部持仓事实恢复、事件增量与目标带再平衡 | [SKILL.md](../.agents/skills/holding-monitor/SKILL.md) | [holding rebalance](../docs/workflows/workflow-holding-rebalance-decision.md) |
| `$continuous-investment-monitor` | 已分析/推荐/持仓/开放模拟订单的持续监控、事件队列与增量研究路由 | [SKILL.md](../.agents/skills/continuous-investment-monitor/SKILL.md) | [continuous investment monitoring](../docs/workflows/workflow-continuous-investment-monitoring.md) |
| `$portfolio-manager` | 组合风险、四种稳健构建、风险缺口预筛、transition/no-trade band 与 hedge 语义 | [SKILL.md](../.agents/skills/portfolio-manager/SKILL.md) | [portfolio transition](../docs/workflows/workflow-portfolio-transition-and-hedging.md) |
| `$paper-trading-recovery` | 会话式模拟账户、订单/成交恢复、默认 60m replay + 5m fallback | [SKILL.md](../.agents/skills/paper-trading-recovery/SKILL.md) | [paper trading](../docs/workflows/workflow-paper-trading.md) |
| `$knowledge-ingest` | 批准本地文件摄取、存量覆盖与知识审计；线上作者采集退役 | [SKILL.md](../.agents/skills/knowledge-ingest/SKILL.md) | [knowledge ingest](../docs/workflows/workflow-knowledge-ingest.md) |
| `$research-tech-scout` | 外部 GitHub/投研平台/社区技术侦察与去重 | [SKILL.md](../.agents/skills/research-tech-scout/SKILL.md) | 按需侦察与来源资格合同 |
| `$source-qualification-auditor` | 外部能力 M-06 资格、撤销与退出审计，不授予来源权威 | [SKILL.md](../.agents/skills/source-qualification-auditor/SKILL.md) | 按需侦察与来源资格合同 |
| `$report-visual-qa` | 正式报告渲染页、引用、隐私、资产权利与版式 QA | [SKILL.md](../.agents/skills/report-visual-qa/SKILL.md) | [current company research](../docs/workflows/workflow-current-company-research.md) |
| `$schema-drift-recorder` | raw-first Schema 漂移记录、候选修复验证与回滚 | [SKILL.md](../.agents/skills/schema-drift-recorder/SKILL.md) | [adaptive edge](../docs/workflows/workflow-adaptive-edge.md) |

## 维护规则

1. 新增一个稳定用户能力域时，优先新增/扩展 canonical Skill；不要为每个 CLI 命令创建 Skill。
2. 仅当用户目标确有跨能力依赖、并行、恢复或副作用时更新 Workflow；单角色方法直接使用 Skill，不为目录对齐复制一份步骤。
3. Skill 可链接实际需要的 Workflow 或现行机器/架构合同。相关引用供按需读取，不要求每次加载整条导航链。
4. Workflow 不得重复粘贴完整 Skill 规则，只描述编排、数据依赖、并发、fallback、输出和 stop condition。
5. 每次新增/删除 Skill 或 Workflow 都必须同步更新本索引、`docs/workflows/README.md` 和相关测试。
