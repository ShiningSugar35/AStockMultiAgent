# AStockMultiAgent 文档治理与导航

> 状态：CURRENT｜更新：2026-11-21

Markdown 说明目标、边界和取舍；机器合同与运行证据决定实际行为。每个主题保留唯一现行文档，按任务需要读取，不把导航当作必须全量加载的清单。

## 单一事实源优先级

| 层级 | 权威位置与职责 |
|---|---|
| 仓库规则 | `AGENTS.md`：全局不变量、交互/写入边界、开发流程；≤8 KiB |
| 运行事实 | SQLite、Parquet、ObjectStore、正式 release/manifest；Markdown 不能改写事实 |
| 机器合同 | `src/astock/schemas/`、migration、versioned config、状态机/CLI |
| 验收证据 | `tests/`、审计、recorded/live 记录；fixture 不冒充 live |
| 现役设计 | `docs/architecture/` 解释当前边界，ADR 保存关键取舍 |
| 方法与协作 | `.agents/skills/` 定义专业角色，`docs/workflows/` 说明跨能力续接 |
| 当前开发 | `planning/work_packages_v1.yaml` 机器索引与 `开发计划.md` 一致；只留未完项 |
| 最近验收 | `进度验收.md` 只写最近任务的已验证事实，历史交给 Git/发布记录 |

机器合同与文档不符时按合同运行并登记文档缺陷。`.ai-bridge/` 是瞬时交接/质量回执，不是第二份计划或架构事实源。

## 维护规则

1. 根 README 只做产品入口，AGENTS 只放所有任务必需的规则；命令参数、字段、预算按需查 schema/config/CLI，不跨文档复制。
2. Skill 保存触发条件、输入/输出、专业判断与停止条件；Workflow 保存依赖、并发、恢复和副作用。总控不再复述全部专业步骤。
3. 当前架构原位更新，明确实现、未实现、验证与准入差别。完全被吸收的旧审计/方案实际删除，不只加 SUPERSEDED 标签留重复正文。
4. ADR 是历史决策例外，不删除旧决策；有效审计/许可证/来源绑定记录按真实引用保留。Git 保存源码/配置/文档历史，不替代业务数据库。
5. 工作包通过独立 Review 与适用测试后，计划/机器索引同时移除；稳定事实迁入现役文档，最近证据写进度验收。自然样本/资格仅作运行 enablement。
6. 同一路径唯一 writer；只暂存本任务路径，保留他人未提交内容。文档合同按实际语义和命令存在性验证，不为保留旧文本而恢复退役行为。
7. 维护修复不自动发产品版本；发布与收尾规则见 AGENTS。未发生的 SHA、发布、clean 状态或成功测试不得预写。

## 按需入口

| 任务 | 文档 |
|---|---|
| 项目/开发 | [README](../README.md)、[规则](../AGENTS.md)、[计划](../开发计划.md)、[机器索引](../planning/work_packages_v1.yaml)、[验收](../进度验收.md) |
| 投资请求/输出 | [统一编排](architecture/investment-request-orchestration-v1.md)、[公共回复](architecture/public-response-contract-v1.md)、[正式报告](architecture/formal-reporting-v1.md) |
| 当前数据补证 | [恢复与验证](architecture/adaptive-recovery-and-validation-v1.md)、[Provider](architecture/provider-resilience-v1.md)、[Web线索](architecture/web-discovery-lead-v1.md) |
| 公司与全市场 | [研究团队](architecture/full-market-research-team-v1.md)、[候选发现](architecture/skill-driven-candidate-discovery-v1.md)、[广度/入场/事件](architecture/recommendation-breadth-entry-event-intelligence-v1.md) |
| 专业方法 | [角色映射](architecture/research-specialist-skills-crosswalk.md)、[Serenity方法](architecture/serenity-method-layer-v1.md)、[技术研究](architecture/technical-research-quality-v1.md) |
| 组合与市场状态 | [组合/持仓](architecture/portfolio-holding-decision-v1.md)、[状态控制](architecture/market-regime-control-v1.md)、[状态研究内核](architecture/market-regime-research-kernel-v1.md) |
| 持续研究/资源 | [持续研究](architecture/continuous-investment-research-v1.md)、[定时研究](architecture/scheduled-research-orchestration-v1.md)、[资源生命周期](architecture/operations-slo-storage-lifecycle-v1.md)、[性能/Skills](architecture/current-research-performance-and-agent-skills-v1.md) |
| 外部能力资格 | [依赖韧性](architecture/external-dependency-resilience-v1.md)；来源配置/正式资格报告拥有准入结论 |
| 执行工作流 | [Workflow索引](workflows/README.md)、[Skill目录](../skills/README.md)、[业务问题矩阵](acceptance/business-question-capability-matrix-v1.md) |
| 架构决策 | [ADR索引](adr/README.md)、[文档即代码](adr/0001-documentation-as-code-with-machine-contracts.md)、[市场状态风险覆盖层](adr/0002-market-regime-as-risk-overlay.md) |
| 本轮外部实践核查 | [优化来源证据](scouting/2026-11-21-system-optimization-evidence.md)，其余 scouting 仅在具体方法/审计引用时读取 |

## 代码合同导航

- Web 工具：`src/astock/schemas/web_tools.py`、`configs/web_tools.yaml`；运行时 `web-tool-schema` 展示合同。宿主能力观测与费用类别分开，未知不等于付费备用许可。
- Tushare：`configs/tushare_access.yaml` 的官方 repo/SDK/逐端点链接，`tushare-schema --endpoint ...` 与 `integrations/tushare/README.md`。只读所需端点；结构化补充尚不替换正式财报回源。
- Mootdx：`configs/mootdx_fallback.yaml`、`integrations/mootdx/README.md` 与 THIRD_PARTY_NOTICES；隔离依赖、raw 60m/5m、明确 SDK/协议/单位归因。
- 资源与验证：`configs/storage_lifecycle.yaml`、`scripts/run_local_quality.py`、`scripts/plan_validation.py`。真实进程身份、引用闭包、输入指纹及终态回执拥有清理与 PASS 事实。
- 本地知识：`configs/knowledge_sources.yaml` 与 [知识工作流](workflows/workflow-knowledge-ingest.md)；线上作者采集已退役，旧名称仅可承担存量证据兼容。

CURRENT 表示文档当前有效，不保证所有部署已取得生产资格；PROPOSED/SHADOW/NOT_ADMITTED 由相应机器合同解释。任何“已完成”都需可定位的测试、审计或运行证据。
