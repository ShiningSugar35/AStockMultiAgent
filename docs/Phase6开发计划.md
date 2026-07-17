# Phase 6 委员会与选择性反方开发计划

计划日期：2026-07-17

状态：已完成并通过终验。本文保留开发前冻结的范围和顺序，实际交付及验证证据见 `docs/Phase6验收报告.md`。Phase 5 的确定性蒸馏内核虽已实现，但线上覆盖及人工审核仍为 `PARTIAL`，不会因 Phase 6 完成而被提前标记完成。

## 1. 阶段目标

Phase 6 要交付一条可审计、可恢复、完全基于冻结工件的投资判断链：

```text
冻结输入核验
  -> 不可覆盖的硬门槛
  -> 覆盖度与证据缺口
  -> 确定性决策矩阵
  -> 选择性反方
  -> DecisionPack
  -> TradeProtocol
  -> 可选的 Codex 单次文字说明
```

这里的“委员会”不是多个 Agent 投票，也不负责重新找资料。它只读取已经通过 Schema、引用、PIT 和 Policy 校验并登记到 ArtifactStore 的工件。相同输入哈希、规则版本和请求必须产生相同的 `decision_id`、决定内容和对象哈希。

## 2. 明确不做

- 不联网，不调用 API、MCP、Browser/Chrome，不打开完整 PDF/DOCX，不重抓公告或知乎。
- 不让 Codex、外部 LLM 或自然语言字段修改硬门槛、数值结果、仓位上限和 verdict。
- 不因缺证据而“降低语气后继续”；关键缺口只能是 `NEEDS_INFO`。
- 不让社区内容成为关键事实的唯一证据。
- 不直接创建模拟订单、修改账本或连接券商；TradeProtocol 只是冻结规则工件。
- 不实现真实下单、杠杆、动态权重、上下文赌博机或强化学习。
- Phase 5 未经人工批准的候选 Skill 不能进入委员会的生产 Skill 版本集合。
- 不把私有 PDF、DOCX、Cookie、浏览器 Profile、原文或 `runtime/` 提交到 Git。

## 3. 冻结输入边界

### 3.1 允许的工件

首版只接受已登记、可重算哈希且类型在白名单中的工件：

- `FrozenEvidencePack`
- `BaseCasePack`
- `SpecialistDelta`
- `SpecialistDiagnosticReport`
- `ResearchMemoArtifact`
- `FinancialIntegrityEvidencePack`
- 已审核的人工补证工件
- `PositionMonitoringPlan`
- `HoldingEvidenceUpdate`
- `HoldingReviewPack`
- `PositionActionProposal`
- `CounterCasePack`
- 结构化的组合状态与市场状态快照

输入引用固定 `artifact_id`、`artifact_type`、`object_sha256`、角色和是否必需。服务必须同时核对 ArtifactRegistry、对应安全索引、ObjectStore 实际字节哈希和领域 Schema。未知 ID、类型不符、索引哈希不符、对象损坏、公司不一致、`as_of` 穿越或未批准 Skill 一律拒绝，不能留下半个委员会运行。

### 3.2 CommitteeAccessPolicy

每个委员会请求都固化现有 `CommitteeAccessPolicy`：network、API、MCP、browser、全文读取和新研究全部只能是 `false`；缺证据动作只能是 `NEEDS_INFO`，且必须生成调查任务。该 Policy 同时进入请求哈希和审计结果，不能只存在于文档里。

### 3.3 冻结集合

新增版本化 `CommitteeInputBundle`，保存排序后的输入引用、公司、决策范围、`as_of`、规则版本、Skill 版本、访问策略和输入集合哈希。完整内容进入 ObjectStore；SQLite 只保存 ID、类型、状态、数量、哈希和时间等安全索引。

## 4. 结构化决策输入

委员会不从自由文本里猜数值。新增 `CommitteeDecisionRequest`，要求调用方显式提供并用冻结 evidence/artifact 引用支持以下信号：

- 决策范围：新候选、现有模拟仓位或用户声明的只读监控仓位；
- 预期收益区间、下行区间、置信度和持有期；
- 数据、证据、PIT、专家覆盖和流动性分数；
- 可交易性、数据质量、核心来源冲突、社区唯一证据、财务硬阻断；
- 专家分歧、重大新公告、证伪条件接近、组合风险变化；
- 当前仓位、计划仓位和持仓建议；
- TradeProtocol 所需的入场、仓位、退出、复核和版本化成本/成交规则。

所有比例使用有界 Decimal，所有时间使用带时区时间，所有列表排序去重。收益、下行、仓位和信号必须引用冻结集合中的证据或父工件；缺引用不能变成投资结论。

## 5. 不可覆盖的硬门槛

新增 `configs/committee_rules.yaml`，配置带 `rule_set_id`、`version`、`effective_from` 和内容哈希。首版硬门槛及固定优先级为：

1. `REJECT`：不可交易、明确禁止标的、手动紧急停止、杠杆请求、严重财务硬阻断或确定的核心失效条件。
2. `NEEDS_INFO`：数据质量失败、关键 PIT 不安全、核心来源冲突、关键事实只有社区证据、必需证据/专家覆盖不足、触发反方但缺少合格 CounterCasePack。
3. `PAPER_EXIT`：现有仓位触发已冻结的退出或 thesis invalidation；不得被较乐观收益区间覆盖。
4. `PAPER_HOLD`：现有仓位无退出/减仓硬信号且通过前述门槛；只表示模拟持有/继续观察，不自动成交。
5. `PAPER_ELIGIBLE`：新候选通过全部门槛，覆盖、流动性、风险收益和仓位均满足版本化阈值。
6. `WATCH`：研究材料完整但没有达到模拟候选阈值，或已有减仓/复核信号但不构成强制退出。

规则引擎只读取结构化字段。可选 narrative 即使给出相反建议，也只能产生审计 warning，不能修改 verdict、hard_blocks、needs_info、max_position 或 TradeProtocol 状态。

首版组合级风险闸门也固化在同一版本化规则中：计划后总暴露不高于 80%、单一行业暴露不高于 25%、与现有组合的最大绝对相关性不高于 80%；组合回撤达到 20% 或连续亏损达到 5 次时冻结新增风险。重大公告待核验或数据异常冻结进入 `NEEDS_INFO`。这些是可审计的首版保守默认值，不是收益承诺；以后只能创建新规则版本，不能回写历史决定。

## 6. 选择性反方

CounterCase 只在下列任一条件发生时触发，不对每个候选无条件增加成本：

- 计划仓位超过配置阈值；
- BaseCase、专家或持仓建议存在实质冲突；
- 财务异常或证据覆盖接近最低线；
- 潜在收益高但关键假设仍弱；
- 多专家严重分歧、重大新公告、证伪条件接近；
- 组合风险显著变化。

新增 `CounterCaseTriggerReport` 和 `CounterCasePack`。CounterCasePack 只能挑战冻结集合里的 claim/evidence，列出替代解释、下行路径、未决问题、引用和估算成本，不能引入未登记事实。触发后没有合格反方包时，结果固定为 `NEEDS_INFO` 并生成调查任务；未触发时不要求构造“形式上的反方”。

## 7. NEEDS_INFO 闭环

新增通用、委员会专用的 `CommitteeInvestigationTask`，字段覆盖 task_id、关联 decision/bundle、reason code、重要性、决策影响、优先级、预估时间、建议来源、搜索词、步骤、所需材料、支持/反驳信号、停止条件、替代证据、状态和解决工件引用。

流程为：

```text
委员会发现缺口
  -> 冻结 NEEDS_INFO DecisionPack
  -> 创建 OPEN 调查任务
  -> 上游或人工取得新证据并单独冻结
  -> 创建新的 CommitteeInputBundle
  -> 产生新的 decision_id
```

旧决定和旧任务不回写历史。解决任务只登记新的解决工件 ID/哈希；委员会不会自行执行调查步骤。

## 8. DecisionPack 与 TradeProtocol

### 8.1 DecisionPack

新增不可变 `DecisionPack`，至少包含：decision_id、bundle_id、冻结输入哈希、规则/Skill 版本、verdict、预期收益区间、下行区间、置信度、hard_blocks、needs_info、反方触发结果、最大仓位、复核时间、结构化 rationale code、evidence IDs、决定哈希和创建时间。

`decision_id` 由规范化请求、冻结输入集合哈希、规则版本和引擎版本确定，不含运行时随机数。相同请求幂等；相同 ID 对应不同内容时按身份冲突拒绝。

### 8.2 TradeProtocol

每个 DecisionPack 都生成一份版本化 TradeProtocol：

- `PAPER_ELIGIBLE`、`PAPER_HOLD`、`PAPER_EXIT` 可生成对应的模拟资格/持有/退出协议；
- `WATCH`、`NEEDS_INFO`、`REJECT` 仍生成 `BLOCKED` 协议，明确为什么不可执行，避免“没有协议就被旁路”；
- 包含 signal_time、earliest_executable_time、持有期、入场与仓位规则、价格/波动/移动/时间止损、thesis invalidation、止盈、复核事件、最大持有期、成本模型、成交模型和 evidence snapshot；
- 所有协议 `requires_user_confirmation=true`，不得携带真实券商命令，不写订单、成交、持仓或账本表；
- 修改规则只能创建新 protocol_id 和有效时间，不能覆盖旧对象。

## 9. 上下文预算与可选 Provider 降级

在现有 `ContextBudgetReport` 上增加委员会预算结果，实际统计冻结工件字节、估算文本 Token、重复输入和可选 narrative 成本。委员会的 full_documents、browser、MCP 和 API 步数必须恒为 0。

预算超限按固定顺序降级：

1. 禁用可选 Codex/Provider narrative；
2. narrative 只读取 DecisionPack 和必要引用摘要，不读取所有父工件；
3. 外部 Provider 未启用、探针失败或估算成本超过 ceiling 时保持 `DETERMINISTIC_ONLY`；
4. 必需确定性输入不能为了省预算被静默删除；若必需结构化工件本身不完整则 `NEEDS_INFO`。

首版不主动调用任何外部 LLM Provider，只交付配置、能力状态和确定性降级证明。Codex 可通过 P4.5 已有的严格运行清单包装已登记的 CounterCasePack、DecisionPack 或 TradeProtocol，不能导入一个未登记的替代决定。

## 10. 持久化与恢复

新增 migration `0027_committee.sql`，预计包含：

- `committee_bundle_index`
- `committee_bundle_input_index`
- `counter_case_index`
- `committee_decision_index`
- `committee_trade_protocol_index`
- `committee_investigation_task_index`

SQLite 不保存 thesis、全文 rationale、原始摘录、搜索词、规则正文或私有内容；这些只进不可变 ObjectStore。各表使用外键、唯一约束、状态检查和父哈希，支持在“对象已写、索引未写”的故障后恢复。恢复只能根据已存在的请求/对象补登记，不能重新研究或换输入。

## 11. CLI、审计与 Repo Skill

计划增加稳定命令：

- `committee-plan`：只校验输入、展示预算和反方触发，不落决定；
- `committee-decide`：生成/幂等读取 bundle、DecisionPack、TradeProtocol 和必要调查任务；
- `committee-status`：读取安全索引状态；
- `committee-audit`：核对 registry、ObjectStore、父哈希、决定矩阵、硬门槛和协议；
- `committee-recover`：补齐可证明的中断写入；
- `committee-task-status`：只读调查任务状态；
- `probe`：公开委员会规则版本、冻结输入类型、Provider/narrative capability 和无网络策略。

更新 `$astock-research-orchestrator`、`$company-deep-research`、`$holding-monitor` 和 `$evidence-investigation` 的真实命令路由。Repo Skill 只能调用稳定 CLI，不能直接写 SQLite、绕过委员会或声称已成交。

## 12. 分段实现顺序

### P6.1 冻结合同与配置

交付 Schema、规则配置、严格输入解析、bundle 哈希、migration 和仓储；先证明委员会无法获得联网 capability。

### P6.2 硬门槛与选择性反方

交付确定性 hard gate、覆盖度门槛、决策矩阵、CounterCase 触发和缺失反方的 `NEEDS_INFO`。

### P6.3 决策、协议与调查闭环

交付 DecisionPack、BLOCKED/ACTIVE TradeProtocol、调查任务、幂等写入、Codex 严格包装和不可覆盖性测试。

### P6.4 CLI、预算、恢复与全链终验

交付预算降级、status/audit/recover、Repo Skill、真实库 migration 和 Phase 4 → Phase 6 合成全链验收。

## 13. 自动化测试与验收

1. CommitteeAccessPolicy 任一联网/全文/新研究字段为 true 时 Schema 直接拒绝。
2. 未登记、类型/哈希不符、对象损坏、公司不一致、未来工件、未批准 Skill 和不在白名单的输入全部拒绝且无半写。
3. 相同冻结输入、请求、规则和引擎版本重复运行，bundle、decision、protocol 与对象哈希完全相同。
4. 不可交易、紧急停止、杠杆、严重财务硬阻断不能被乐观 narrative、收益区间或高置信度覆盖。
5. 关键证据缺失、核心冲突、社区唯一关键事实、低 PIT/覆盖和必需反方缺失固定为 `NEEDS_INFO`，并生成可审计 OPEN 任务。
6. EXIT 优先级高于 HOLD/高收益；现有仓位与新候选分别走正确 verdict 分支。
7. CounterCase 只在配置触发条件出现时要求；包内引用超出冻结集合、引入未知 claim/evidence 或公司不一致时拒绝。
8. 每个 DecisionPack 都有 TradeProtocol；非可执行 verdict 必须是 BLOCKED，所有协议必须人工确认且不能写账本。
9. 预算超限禁用可选 narrative，但确定性决定保持相同；Provider disabled/探针失败/超成本不影响主链。
10. 故障注入后 recover 能补齐安全索引；坏对象、变更规则、输入不全时明确不可恢复。
11. audit 可发现输入索引、ArtifactRegistry、对象、决定、协议或任务的篡改；不会通过重新联网修复。
12. P4 冻结 Evidence → BaseCase → 专家/诊断/memo → 可选持仓复核 → P6 bundle/decision/protocol 合成链实际跑通。
13. SQLite migration、`PRAGMA integrity_check`、外键检查、隐私字段检查通过；私有原料和 runtime 仍被 Git 忽略。
14. 全仓 `uv run pytest`、`uv run ruff check .`、`uv run pyright` 通过；live/昂贵测试继续由明确环境开关控制。

## 14. 可回滚点

- 每个 P6 子阶段独立提交；规则配置和 Schema 先于服务代码提交。
- 新表只追加，不修改既有 Phase 1～5 表语义；回滚代码时旧 Artifact/ObjectStore 仍可读取。
- 规则升级创建新版本，不改旧规则文件对应的历史决定。
- 错误 DecisionPack 通过新输入/新版本重算，不删除或覆盖旧决定。
- 调查任务解决或取消只改变安全状态索引并引用新工件，不擦除历史对象。

## 15. 预算与完成定义

工期参考 10～15 个开发日；本地自动化主路径不产生外部模型费用，网络调用预算为 0。可选 Provider 默认关闭，只有未来用户主动配置并通过探针后才可能计费。

冻结输入、不可覆盖硬门槛、选择性反方、NEEDS_INFO 闭环、DecisionPack、每决定一份 TradeProtocol、预算降级、Codex 严格包装、恢复审计、真实 migration 和全仓验收均已由代码与测试证明，Phase 6 于 2026-07-17 标记完成。计划文件、Schema 存在或单个 happy-path 测试不能代替终验；详细证据见 `docs/Phase6验收报告.md`。
