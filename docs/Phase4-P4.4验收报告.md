# Phase 4 P4.4 持仓生命周期与增量复核验收报告

验收日期：2026-07-17

验收结论：通过。`generic-position-lifecycle-v1`、监控计划、连续增量证据窗口、五级动作合并、人工确认建议、恢复、CLI 和审计均有实际代码及测试。该结论只证明系统能形成可审计建议；它不承诺收益、不批准真实交易，也不会修改模拟账本或连接券商。

## 1. 版本化规则与严格输入

- 新增 `configs/position_lifecycle.yaml`，固定 `EXIT > REVIEW > TRIM > ADD > HOLD`，五种动作均有基础置信度，并受研究 memo 覆盖状态上限约束。
- `LifecycleCondition` 不允许把 HOLD 登记为触发规则；ADD 必须声明需要新证据；硬阻断只允许落到 EXIT 或 REVIEW。
- `PositionPlanCreateRequest` 必须引用同一 company/as_of 下的 BaseCase、route 和 memo，并至少有一条 EXIT 条件，防止建立只能加仓、不能退出的计划。
- `HoldingReviewRequest` 固定 from/to 窗口、新增 Evidence、变化 Claim、失效 Evidence、开放冲突和已登记 rule_id；调用方不能临时把某条规则改成另一种动作。
- `PositionActionProposal.requires_user_confirmation=false` 在 Schema 层直接拒绝。

## 2. 监控计划和父子门禁

- `PositionLifecycleService.create_plan` 核对 BaseCase、route、memo 的 company、as_of、父子 ID、冻结 EvidencePack 和 memo 引用范围。
- 验证指标只能引用 memo 已列出的 evidence_id；不能把 memo 之外的新事实偷偷加入持仓基线。
- 用户在系统外形成的决策使用 `USER_DECLARED_EXTERNAL` 明示；若声明是已登记工件，则必须实际存在于 Artifact registry。
- 完整 thesis、入场假设、价值驱动、规则说明和人工信息需求只保存到 ObjectStore；migration 0025 的 SQLite 索引仅保存 ID、版本、时间、哈希、计数、覆盖和状态。

## 3. 增量窗口与动作实测

使用有完整 Evidence/PIT、BaseCase、IndustryBottleneck Delta 和 ResearchMemo 的合成公司案例，连续跑通五个不重叠窗口：

1. 无新增证据、无触发规则得到 HOLD，`thesis_strength_change=UNCHANGED`，没有把沉默误写成增强。
2. 已登记 ADD 规则、新增同公司且在窗口内可得的 Evidence、无硬阻断时得到 ADD。
3. ADD 与 TRIM 同时触发时得到 TRIM。
4. 开放 EvidenceConflict、基线 Evidence 失效且 ADD 缺少信号引用时得到 REVIEW，并同时记录三个硬阻断码。
5. EXIT、REVIEW、TRIM、ADD 同时触发时严格得到 EXIT。

非连续首窗、未知 rule_id、晚于窗口的 Evidence 和其他公司 Evidence 均在落复核索引前拒绝。新增 Evidence 必须满足 `from_as_of < available_to_system_at <= to_as_of`；变化 Claim 必须属于同一公司且发生在窗口内；信号只能引用本窗口声明的新增 Evidence。

## 4. 幂等、崩溃恢复和审计

- 计划和 review 的身份由冻结父对象、规则对象和请求内容共同计算；重复请求返回相同 ID、对象哈希和索引，不制造逻辑重复。
- 故障注入实测了“update/review 已入索引、proposal 尚未写入”时进程中断；相同请求重跑可补齐 proposal、Artifact 和 checkpoint，而不会被连续窗口门禁误判为第二次 review。
- audit 从计划 as_of 开始逐段核对全部历史 from/to 边界，并核对最新 update/review/proposal 对象、元数据计数、Evidence 可得时间、公司范围、Artifact hash 和人工确认标志。
- `HoldingEvidenceUpdate`、`HoldingReviewPack`、`PositionActionProposal` 均纳入 Codex 工件类型校验；完整对象只在 ObjectStore，安全索引可用于恢复。

## 5. 账本隔离、CLI 和隐私

- 生命周期服务没有 Ledger 依赖；完整五动作案例前后 `ledger_account`、`ledger_entry`、`order_record`、`fill`、`position` 和 `position_settlement` 记录数完全相同。
- 新增 `position-lifecycle-schema`、`position-plan-create/status`、`holding-review-run/status/audit`。输出只含 ID、哈希、时间、动作、置信度、规则码、硬阻断和计数。
- 无效计划/review 请求只返回固定错误码；测试中的秘密 thesis 和输入路径均不回显。
- SQLite 生命周期五张表的合成隐私扫描未发现 thesis 或 condition 正文。
- 私有 PDF、DOCX 和 `runtime/` 继续由 `.gitignore` 命中，没有进入 Git。

## 6. 自动化与真实运行库

- 全仓 `pytest`：195 passed / 8 skipped；跳过项仍是需显式环境开关的真实 OCR benchmark 和低频 live provider smoke。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实运行库 migration 0001～0025 已应用，`integrity_check=ok`，外键违规 0。
- 真实规则索引 1 条；真实监控计划、EvidenceUpdate、HoldingReview 和 Proposal 均为 0。当前没有用户指定的真实持仓决策与冻结研究链，因此没有制造示例持仓建议。

## 7. 后续边界

P4.5 将完成 Phase 4 的 Repo Skill/Codex 端到端路由、全链恢复故障矩阵和阶段终验。统一委员会、CounterCase、DecisionPack 与硬风险裁决仍属于 Phase 6；P4.4 不提前实现或宣称这些能力。
