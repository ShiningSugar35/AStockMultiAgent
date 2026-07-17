# Phase 4 P4.4 持仓生命周期与增量复核开发计划

计划日期：2026-07-17

状态：开发前计划。系统尚未获得自动改账本或向券商发单的权限；本阶段只生成可审计、必须由用户确认的动作建议。

## 1. 目标

为模拟持仓和用户主动声明的真实监控持仓建立同一套版本化通用生命周期内核。每个持仓先登记 `PositionMonitoringPlan`，以后只读取上次复核边界之后新增、变化、失效或冲突的证据，生成 `HoldingEvidenceUpdate`、`HoldingReviewPack` 和 `PositionActionProposal`。

本阶段不执行交易、不修改模拟账本、不连接券商。`PositionActionProposal.requires_user_confirmation` 在 Schema 和服务两层固定为 `true`，不存在绕过入口。

## 2. 版本化规则与动作优先级

新增 `generic-position-lifecycle-v1` 配置，固定优先级：

1. `EXIT`：已登记的论点失效或不可交易硬阻断触发。
2. `REVIEW`：证据冲突、基线证据失效、数据质量冻结、重大披露待核或人工信息到期；同时禁止 ADD。
3. `TRIM`：风险上升或估值减仓条件触发，且没有更高优先级动作。
4. `ADD`：显式加仓条件触发、存在窗口内新增支持证据、没有任何硬阻断或待复核条件。
5. `HOLD`：没有更高优先级条件；“没有新证据”只表示 HOLD，不表示论点增强。

动作置信度使用版本化基础值并受研究覆盖上限约束；缺证据或冲突只会下调，不得靠规则数量上调到满置信度。

## 3. 严格 Schema

### 3.1 监控计划请求

`PositionPlanCreateRequest` 必须包含 position/company、decision 引用及其来源状态、BaseCase、route、memo、as_of、持有期限、论点摘要、入场假设、价值驱动、验证指标、监控来源/频率、显式条件、人工信息需求和下一复核时间。

每个 `LifecycleCondition` 固定 rule_id、signal_code、动作、来源类型、说明、是否要求新证据和是否为硬阻断。rule_id/signal_code 唯一；至少有一条 EXIT/失效条件，防止建立“只能加仓、永不退出”的计划。

### 3.2 增量复核请求

`HoldingReviewRequest` 固定 plan_id、from_as_of、to_as_of、新增 evidence_id、变化 claim_id、失效 evidence_id、开放 conflict_id 和触发的 `HoldingRuleSignal`。时间窗必须递增，集合必须唯一；信号只能引用计划中已登记 rule_id。

`HoldingRuleSignal` 只记录 rule_id、观测值、发生时间和 evidence_id；动作由冻结计划查出，调用方不能临时把一个 HOLD 规则改成 EXIT。

## 4. 父子一致性和 PIT 门禁

1. 创建计划时核对 BaseCase、route、memo 的 company、as_of、FrozenEvidencePack 和父子关系；memo 未列出的新事实不得进入计划基线。
2. 首次 review 的 from_as_of 必须等于计划基线时间；后续必须等于上次成功 review 的 to_as_of，不允许跳过或重叠窗口。
3. 新增 Evidence 必须存在、entity_ids 包含计划 company、`available_to_system_at` 严格晚于 from_as_of 且不晚于 to_as_of；未来信息和其他公司证据拒绝。
4. changed claim 必须存在且属于同一 company；signal evidence 必须包含在本次新增 evidence_ids 中。
5. 失效 evidence 和冲突不会被静默删除，分别形成 REVIEW/硬阻断输入。

## 5. Artifact、存储和恢复

- 完整 `PositionMonitoringPlan`、`HoldingEvidenceUpdate`、`HoldingReviewPack`、`PositionActionProposal` 只进入 ObjectStore。
- migration 0025 只保存规则版本、ID、父子引用、时间窗、动作、确认标志、哈希、计数、覆盖和状态；不保存 thesis、原因正文、监控路径或私密仓位数量。
- 同一输入和规则版本生成稳定 ID；重复执行返回已登记对象。对象已写、索引未写时可重跑，不覆盖旧对象。
- 每次成功 review 写 checkpoint；status/audit 从安全索引恢复，不直接编辑 SQLite。

## 6. CLI

- `position-lifecycle-schema`：列规则版本、优先级、动作置信度和安全门。
- `position-plan-create <request.json>`：登记计划，只输出 ID、哈希、计数、覆盖和时间。
- `position-plan-status <position_id>`：返回最新安全索引。
- `holding-review-run <request.json>`：生成 update/review/proposal，只输出 ID、动作、置信度、硬阻断、规则码和计数。
- `holding-review-status <position_id>` 与 `holding-review-audit <position_id>`：检查窗口连续性、对象、父子、Evidence/PIT、确认标志、索引计数和 Artifact registry。
- 无效请求只返回固定错误码，不回显 thesis、条件说明、文件路径或私密数量。

## 7. 自动化验收

1. BaseCase/route/memo 父子不一致时计划拒绝；计划缺 EXIT 条件拒绝。
2. 初始无新证据/无信号为 HOLD，不能写成 STRENGTHENED。
3. ADD 必须同时有已登记 ADD 信号和窗口内新增支持 Evidence；缺支持证据改为 REVIEW，冲突/失效/数据冻结时 ADD 被硬阻断。
4. EXIT、REVIEW、TRIM、ADD、HOLD 同时触发时严格按固定优先级；调用方不能伪造动作。
5. 未来 Evidence、其他公司 Evidence、窗口外 Evidence、未知 rule_id、非连续窗口均拒绝。
6. proposal 显式传 `requires_user_confirmation=false` 在 Schema 层拒绝；任何服务路径都不能修改 paper ledger。
7. 重复计划/review 幂等；完整对象和安全索引、Artifact/checkpoint 对账；SQLite 不含合成 thesis/condition 文字或私密数量。
8. 全仓 pytest、ruff、pyright、真实 migration、完整性、外键和 Git 私密文件扫描通过。

## 8. 完成定义

只有五类动作路径、窗口增量、Evidence/PIT、人工确认、不改账本、CLI、恢复、审计和验收报告全部由实际代码与测试证明后，P4.4 才标为完成。文档或已有宽松 Schema 本身不算实现。
