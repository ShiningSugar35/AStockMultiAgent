# AStockMultiAgent

AStockMultiAgent 是一套**本地优先、可审计、可恢复**的 A 股多 Agent 投研与模拟交易系统。自然语言模型负责研究意图、证据解释与专业协作；Python 3.12 的 `astock` CLI 负责确定性数据、PIT、财务计算、风险、状态机和账本。

## 永久边界

- 不承诺收益，不连接真实券商自动下单；真实交易只能由用户在券商端执行。
- 所有正式历史输入必须 point-in-time safe，未来可见事实不得倒灌。
- 社区内容和搜索结果只能作线索；公告、交易所、财报和权威政策来源承担关键事实。
- 候选、研究意见、组合建议和模拟订单互不等价；只有模拟成交才改变模拟持仓。
- 实际账户与模拟账户永久分 lane，不能互相写入或混成一份伪账本。
- 缺数据、证据冲突或能力未准入时 fail closed，不用叙述伪装完成。

## 当前能力概览

| 领域 | 当前边界 |
|---|---|
| 数据与证据 | ObjectStore、Parquet、SQLite、PIT、官方公告/PDF/DOCX、Claim–Evidence、Provider resilience 已实现 |
| 公司与行业研究 | 单公司 Research Runtime、机构级基本面、财务审计、行业/产业链、治理、催化剂、独立多空与投委会已具备 |
| 全市场与组合 | 三市场候选链、正式荐股门、组合风险/构建/迁移、ETF 深度事实与对冲评估已具备；不以初筛冒充推荐 |
| 账户与模拟交易 | 外部真实账户 append-only 事件、多账户投影、模拟订单/成交/T+1/恢复已具备；ETF 模拟执行默认关闭 |
| 持续研究 | Watch Universe、行情/公告/news lead、Catalyst、typed rule、持久任务和已确认模拟订单回放已具备 |
| 定时语义研究 | 本地 Monitor 已具备确定性轮询；ChatGPT / Codex 的定时唤醒、语义复核、幂等通知和 missed-run 恢复尚未接入 |
| 用户输出 | Investor Mode、公共回复审计与内部诊断分层已具备 |
| 宏观 current data | NBS/PBOC/MOF/NDRC 当前仍是 recorded-first；正式 live 数据面尚未闭合 |
| 市场状态总控 | 现有 `market-regime-v1` 仅用于 shadow 分层；多维状态、风险预算与荐股数量联动仍是拟议能力 |
| 统一投资请求编排 | “每问先恢复实际/模拟持仓、强制能力覆盖回执、历史研究标的登记”已完成设计，尚未实现为统一运行时硬门 |
| Phase 7/8 | 前向样本仍在积累，自适应准入保持关闭；历史回放不能替代真实前向证据 |

能力的详细实现状态以 Schema、配置、CLI、测试、SQLite/Parquet/ObjectStore 和正式审计结果为准，不以本表单独证明。

## 快速开始

```powershell
uv sync --all-groups
uv run astock init
uv run astock probe
uv run astock --help
```

常用只读入口：

```powershell
# 当前公司研究
uv run astock research-acquire-current 600519 --market XSHG
uv run astock research-plan 600519 --mode LIVE
uv run astock research-run-company 600519 --mode LIVE --institutional-research-required

# 实际账户 / 本地投影 / 模拟盘
uv run astock external-account-list
uv run astock local-portfolio-status
uv run astock paper-status

# 组合与持续监控
uv run astock portfolio-schema
uv run astock continuous-monitor-status
```

完整命令以 `uv run astock --help` 和各子命令 `--help` 为准；README 不复制整个 CLI 目录。

## 架构概览

```text
自然语言投资问题
  → Repo Skill / Workflow / Orchestrator
  → Python Schema + Service + CLI + deterministic guardrail
  → ObjectStore / Parquet / SQLite / versioned config
  → typed research / risk / account / paper artifacts
  → Response Gateway
  → 投资者可读结论
```

拟议的下一版统一入口将固定为：

```text
Request
  → actual/paper/monitor/latest-regime availability preflight
  → required/conditional/prohibited capability DAG
  → typed outputs + coverage receipt
  → investor answer audit
```

该入口尚未实现，不能把架构设计当成当前运行事实。

## 事实源

- 原始响应与不可变对象：`runtime/objects/sha256/`
- 分析事实：Parquet；DuckDB 只建视图
- 任务、游标、工件注册、真实账户事件和模拟账本：SQLite
- 可执行合同：`src/astock/schemas/`、migration、versioned config、状态机和 CLI
- Agent 草稿：`runtime/codex_runs/<run_id>/`，校验后才能进入 ArtifactStore
- 本地用户投影：`user_state/`，永久 Git-ignore
- 设计、计划与验收：Markdown；没有机器证据时不得宣称能力完成

## 文档入口

1. [AGENTS.md](AGENTS.md)：所有 Agent 必须遵守的安全、证据、交互和开发规则。
2. [docs/README.md](docs/README.md)：文档治理、单一事实源优先级、ADR 与完整导航。
3. [开发计划.md](开发计划.md)：当前已批准但尚未完成的工作包。
4. [进度验收.md](进度验收.md)：最近一次任务的验证结果与未完成边界。
5. [docs/workflows/README.md](docs/workflows/README.md)：跨 Skill 工作流。
6. [skills/README.md](skills/README.md)：人类可见 Skill 目录；canonical Skill 位于 `.agents/skills/`。
7. [低成本A股多Agent投研系统方案.md](低成本A股多Agent投研系统方案.md)：冻结的历史总体设计背景，不再承担当前状态或开发计划。

下一阶段重点设计：

- [统一投资请求编排](docs/architecture/investment-request-orchestration-v1.md)
- [市场状态与风险预算总控](docs/architecture/market-regime-control-v1.md)
- [ChatGPT / Codex 定时投研适配](docs/architecture/scheduled-research-orchestration-v1.md)
- [68 个业务问题能力链验收矩阵](docs/acceptance/business-question-capability-matrix-v1.md)

## 工程检查

```powershell
uv run pytest
uv run ruff check .
uv run pyright
```

新增或修改能力时，按 `Schema → repository/state → service → CLI → Skill/Workflow → tests/audit` 闭合；文档中的 `PROPOSED`、`SHADOW`、`CURRENT` 等状态由 [文档治理规则](docs/README.md)解释。
