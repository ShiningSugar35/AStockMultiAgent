# AStockMultiAgent 文档治理与导航

> 状态：CURRENT
> 版本：documentation-governance-v1
> 更新日期：2026-09-07
> 适用范围：仓库内所有人类文档、Agent Skill、Workflow、机器合同、运行状态与验收记录

## 1. 结论

本项目继续采用 Markdown，但不再让 Markdown 独自承担“系统真相”。专业的多 Agent 项目需要同时维护六类事实：

1. **目标与约束**：为什么做、不能做什么；
2. **架构决策**：为什么选择这一方案、放弃了什么；
3. **可执行合同**：字段、状态机、路由、阈值和失败行为；
4. **运行事实**：账户、账本、工件、数据和任务的当前状态；
5. **验收证据**：测试、审计、基准和受控运行结果；
6. **历史**：过去版本、已替代方案与发布记录。

Markdown 适合第 1、2、部分第 5 类；第 3、4 类必须由代码、Schema、配置、数据库和测试承担。任何 Markdown 叙述都不能越过机器合同宣布能力已完成。

## 2. 单一事实源优先级

发生冲突时，按下表从上到下裁决：

| 优先级 | 事实类型 | 权威位置 | 不应承担的职责 |
|---:|---|---|---|
| 1 | 安全、交互与开发硬规则 | `AGENTS.md`、确定性 guardrail | 具体业务状态、历史流水 |
| 2 | 运行事实 | SQLite、Parquet、ObjectStore、manifest、正式 release | 架构动机、开发说明 |
| 3 | 可执行合同 | `src/astock/schemas/`、migration、versioned config、CLI、状态机 | 以自然语言替代校验 |
| 4 | 自动验收 | `tests/`、审计命令、记录式/受控 live 证据 | 用 fixture 冒充真实生产状态 |
| 5 | 架构决策 | `docs/adr/`、当前 `docs/architecture/` | 活跃任务清单、运行事实 |
| 6 | 跨能力流程 | `docs/workflows/` | 重复 Schema 或业务实现 |
| 7 | Agent 方法合同 | `.agents/skills/*/SKILL.md` | 建立第二套路由、账本或证据模型 |
| 8 | 当前待实施路线 | `planning/work_packages_v1.yaml`（机器索引）+ `开发计划.md`（可读计划） | 已完成历史、长期运行样本门 |
| 9 | 最近验证快照 | `进度验收.md` | 累积所有历史验收流水 |
| 10 | 历史与背景 | Git、tag/release、冻结设计文档、`docs/scouting/` | 作为当前实现依据 |

若机器合同和架构文档不一致，先按机器合同运行，同时把文档差异登记为缺陷；不得反过来让 Agent 依据过时文档绕过代码。

## 3. 各类文档的固定职责

### 3.1 `README.md`

只保留：产品定位与永久安全边界、最小启动/健康检查/测试入口、当前能力概览及明确限制、指向本索引的导航。

不得再堆叠完整命令清单、阶段历史、长篇架构论证或验收日志。

### 3.2 `AGENTS.md`

只维护所有 Agent 都必须遵守的仓库级规则：真相层级、证据/PIT、写入边界、开发工作流、投资者输出边界和永久禁止事项。稳定规则才进入该文件，单个任务的临时步骤不得写入。

### 3.3 `docs/adr/`

一个 ADR 只记录一项重要决策，包含状态、上下文、决策、后果、替代方案、合规检查与替代关系。旧 ADR 不删除；后续决策使用 `Superseded by` 连接。

### 3.4 `docs/architecture/`

记录当前架构边界和版本化领域设计。每份文档必须声明：

- `状态`：CURRENT / PROPOSED / HISTORICAL / SUPERSEDED；
- `是否已经实现`；
- 对应机器合同、配置、migration、CLI 与测试；
- 不属于本设计的能力。

没有这些声明的旧文档只能作为历史背景。

### 3.5 `docs/workflows/`

描述多个 Skill/Service 如何协同，重点是顺序、并行条件、停止条件、恢复点和写入副作用。Workflow 不复制字段定义和阈值，只引用机器合同。

### 3.6 `.agents/skills/`

Skill 是单个专业角色的方法与工具合同，不是自由人格提示词。一个 Skill 应明确触发条件、输入、输出、命令、证据要求、弃权条件和禁止事项。跨 Skill 编排应进入 Workflow 或代码级 Orchestrator，不能不断把所有流程追加到总控 Skill。

### 3.7 `开发计划.md`

仅保存**当前尚未完成且已批准实施**的工作包。每个工作包必须有目标/范围/非目标、依赖与写入边界、交付物、可机器验证的验收门、回滚方式和当前状态。

`planning/work_packages_v1.yaml` 是工作包 ID、顺序、依赖、状态、优先级和唯一写入 lane 的机器索引；`开发计划.md` 负责解释目标、验收和回滚。两者必须由合同测试保持一致，禁止再靠人工同时维护两套无校验状态。Agent 派工和并发冲突检查优先读取机器索引，再打开 Markdown 获取完整语义。

完成后从机器索引和计划中同时移除，其稳定事实迁入架构文档，最近证据进入 `进度验收.md`，历史由 Git/发布记录保留。

长期样本累积、外部授权、生产资格和自然场景观察不是开发 backlog；对应能力应保持 `NOT_ADMITTED`、`SHADOW` 或 fail-closed。

### 3.8 `进度验收.md`

只回答三个问题：当前仓库最近验证到什么程度；本次变更实际交付了什么、通过了哪些检查；哪些关键能力仍未实现或未验证。

不保存跨版本长篇流水，不重复整个架构，也不把未完成计划写成验收结果。

### 3.9 `.ai-bridge/`

只作为单次执行的**瞬时交接与运行传输层**，不是项目计划、完成状态或架构事实源。`current-plan.md`、`agent-status.md` 必须写明生成时间、canonical plan、durable run 与是否已被替代；新任务开始时若其内容与 `planning/work_packages_v1.yaml`、`开发计划.md`、Git 或运行事实冲突，必须先标记 `SUPERSEDED`，不得按旧“唯一计划”继续执行。

## 4. 当前文档导航

### 项目入口

- [项目 README](../README.md)
- [全局 Agent 规则](../AGENTS.md)
- [当前开发计划](../开发计划.md)
- [工作包机器索引](../planning/work_packages_v1.yaml)
- [最近进度验收](../进度验收.md)

### 当前与拟议架构

- [当前研究性能与 Agent Skills](architecture/current-research-performance-and-agent-skills-v1.md)
- [组合与持仓决策](architecture/portfolio-holding-decision-v1.md)
- [连续投资研究](architecture/continuous-investment-research-v1.md)
- [全市场研究团队](architecture/full-market-research-team-v1.md)
- [公共回复合同](architecture/public-response-contract-v1.md)
- [投资请求统一编排蓝图](architecture/investment-request-orchestration-v1.md)
- [市场状态与风险预算总控蓝图](architecture/market-regime-control-v1.md)
- [ChatGPT / Codex 定时投研适配蓝图](architecture/scheduled-research-orchestration-v1.md)

### 验收设计

- [业务问题—能力链验收矩阵](acceptance/business-question-capability-matrix-v1.md)

### 决策记录

- [ADR 索引](adr/README.md)
- [ADR-0001：文档即代码，但机器合同拥有执行权](adr/0001-documentation-as-code-with-machine-contracts.md)
- [ADR-0002：市场状态是风险预算覆盖层，不是个股结论替代器](adr/0002-market-regime-as-risk-overlay.md)

### 跨能力流程

- [Workflow 索引](workflows/README.md)

### 历史研究

- `低成本A股多Agent投研系统方案.md`：冻结的总体设计背景，不再承担当前计划或验收；
- `docs/architecture/AStockMultiAgent系统体验与投研能力深度研究报告.md`：2026-08-31 时点审查，部分缺口已被后续实现；
- `docs/scouting/`：外部能力和技术侦察记录，不直接取得生产资格。

## 5. 状态与生命周期

架构文档和 ADR 使用以下状态：

| 状态 | 含义 |
|---|---|
| `PROPOSED` | 已研究并形成方案，但尚未完成实现/验收 |
| `ACCEPTED` | 决策已批准，后续实现必须遵守 |
| `CURRENT` | 与当前机器合同一致且仍有效 |
| `SHADOW` | 只允许研究/对照，不参与正式投资判断 |
| `SUPERSEDED` | 已被具名新文档或 ADR 替代 |
| `HISTORICAL` | 仅作为历史背景，不能作为当前完成证明 |
| `REJECTED` | 评审后明确不采用 |

一次能力的正常生命周期是：

```text
PROPOSED architecture/ADR
  → machine schema/config/state machine
  → unit/contract/recorded integration tests
  → controlled-live evidence（适用时）
  → shadow/prospective gate（影响投资决策时）
  → CURRENT
```

文档从 `PROPOSED` 改为 `CURRENT` 必须绑定实际代码、配置、migration、测试和运行证据，不能只改状态文字。

## 6. 多 Agent 修改规则

1. 开始工作前先读 `AGENTS.md`、本索引、工作包机器索引、当前计划和相关架构/Workflow，并核对 `.ai-bridge/` 瞬时交接是否仍指向当前 canonical plan。
2. 重要架构取舍先写 ADR；单纯实现细节不滥用 ADR。
3. 新增机器行为时，按 `Schema → repository/state → service → CLI → Skill/Workflow → tests` 顺序闭合。
4. 不让多个 Agent 同时写同一权威文档；通过任务/worktree 或明确唯一写入者隔离。
5. 任何“完成”必须引用测试/审计/运行证据；模型自述不算证据。
6. 变更后检查内部链接、文档状态、命令存在性、Git diff 和项目外产物。
7. 用户可见投资回复与开发诊断继续分层；内部编排、状态码和工件身份不得泄露到普通投资回复。

## 7. 文档反模式

以下情况视为缺陷：

- README 复制完整命令目录或阶段历史；
- 开发计划继续保存已完成阶段；
- 进度验收累积成第二份架构说明；
- Skill 通过自然语言复制 Python 阈值；
- 架构文档写“已支持”，但没有对应命令、Schema 或测试；
- 用测试 fixture 或历史 replay 宣称当前 live 能力可用；
- 同一事实分别存在两套 Markdown 且无权威关系；
- 工作包机器索引与 Markdown 计划的 ID、依赖、状态或 owner lane 漂移；
- 过期 `.ai-bridge/current-plan.md` 或 `agent-status.md` 继续自称当前唯一计划；
- 删除已被替代的决策，导致后续无法解释为何采用当前方案。

## 8. 外部方法参考

- AWS, *Architectural decision record process*: https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/adr-process.html
- OpenAI Agents SDK, *Agent orchestration*: https://openai.github.io/openai-agents-python/multi_agent/
- NIST AI RMF 1.0: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf

这些资料只支持治理方法；本仓库的安全和投资边界仍以 `AGENTS.md` 与机器合同为准。
