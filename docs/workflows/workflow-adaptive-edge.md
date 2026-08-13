# Workflow — Adaptive Edge Diagnostics

## When to use

适用问题：用户明确要求调试系统、排查 provider/schema drift、查看当前研究为什么选择某条数据路径，或要求 Agent 在不放松安全门的情况下调整研究计划。普通“某股票现在能不能买”不进入本 Workflow，而继续走 `workflow-current-company-research.md` 的 INVESTOR_MODE。

## Flow

### 架构原则

本 Workflow 固定遵守 **Adaptive Edge / Deterministic Core**：Agent 可以改变“先做什么、用哪个 allowlisted provider、如何解释未知 schema、哪些 optional module 值得运行”，不能改变“什么算正式事实、PIT、数学/会计约束、ObjectStore 不可变、账本人工确认和 broker 禁用”。

## 1. 只读诊断优先

1. `research-capability-status <company_id> --market <market>`：查看 active `current-research-policy` 生成的 capability schedule、provider candidates、health-degraded candidates、lookback 和 worker 预算；不写 CurrentResearchSchedule。
2. `provider-dialect-status`：查看 Provider Registry、health、transport profile、dialect version/response shape；不调用外网、不修改 provider config。
3. `adaptive-edge-status`：查看 Planner、Schema Repair、Specialist、Portfolio allocator 与 Current Research policy 的活动版本及硬安全边界。
4. `adaptive-edge-schema`：导出 ResearchPlanner / ProviderRecovery / SchemaRepair 的严格 JSON Schema，供 Agent 生成 proposal。

## 2. Research Planner

Agent 根据用户意图生成 `ResearchPlannerProposal`，只声明 requested modules、requested acquisition capabilities、optional module 跳过理由和 specialist budget。

执行 `adaptive-plan-validate <proposal.json>` 后：

- proposal 先冻结为 `PROPOSED` artifact；
- deterministic validator 自动补回 Evidence、PIT、Financial Integrity、Fundamental Model 等 mandatory modules及其依赖；
- active Current Research core acquisition 永远自动补回；
- specialist budget 只能落在 active `specialist-resource-policy`；
- proposal 不能开启 paper ledger write 或 broker execution；
- 通过后冻结 `ValidatedResearchPlan`。

只有 frozen `ValidatedResearchPlan` 才能作为 `research-acquire-current --planner-plan-artifact-id ...` 的输入。它可以裁剪 optional acquisition，但不能删除 core capability。

## 3. Provider Recovery

Provider 连续失败时，Agent 可以根据 failure class、retryable、health、capability 和 transport profile 生成 `ProviderRecoveryProposal`，然后运行 `adaptive-recovery-validate`。

Validator 只允许：

- Provider Registry 中已经注册的 adapter；
- adapter 声明 requested capability；
- health 不是 UNAVAILABLE/CORRUPT；
- 不把 non-retryable 原 provider 当作无条件重试路径；
- authority fallback 仍遵守强官方优先与 Manual-last。

未知 provider、capability mismatch、transport-profile drift 或无自动/权威路径会得到 `REJECTED` validation，而不是让 Agent 自行请求任意 URL/插件。

## 4. Schema Repair

Unknown dialect/schema drift 必须 raw-first：先保存真实 SourceSnapshot，再由 Agent 生成 `SchemaRepairProposal`。严禁“模型看一眼 JSON 就直接改正式 parser”。

`adaptive-schema-repair-validate` 要求：

1. base dialect/version 与 active registry 一致；
2. candidate mapping 只能映射到已存在 canonical financial field；
3. 至少达到 active policy 要求的多样本 raw SourceSnapshot；
4. 至少一个允许类型且 ObjectStore 校验通过的官方交叉验证 artifact；
5. contract test id 必须真实指向仓库 `tests/` 下的测试文件。

只有全部通过才形成 `VALIDATED`。之后仍必须显式运行：

`adaptive-schema-repair-admit <validation-id> --approve`

才生成 `ADMITTED` **candidate dialect release**。Admission 仍不修改 active `provider_dialects.yaml`、不写正式事实；它只是可审计候选版本。使用 `adaptive-artifact-audit` 检查 lineage/ObjectStore，使用 `adaptive-dialect-rollback` 记录拒绝/回滚，active dialect 继续保持原版本。

## Stop conditions

- Manual remains last：自动 provider/authority 路径仍可继续时，不向用户索要材料。
- Proposal 无法通过 deterministic validator 时，不通过自然语言“变通”绕门。
- Schema Repair 缺 raw/official/contract 证据时保持 REJECTED，不猜值。
- 任何真实 broker execution 都不可用；任何模拟账本写入仍需要独立人工确认。
- INVESTOR_MODE 不展示 proposal/schema/provider/artifact 细节；内部答案发送前继续执行 investor-answer audit。
