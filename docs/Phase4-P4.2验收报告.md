# Phase 4 P4.2 专家路由与增量研究验收报告

验收日期：2026-07-17

验收结论：通过。该结论证明版本化通用 Skill 合同、最多三专家的确定性路由、结构化降级、冻结证据边界和增量 `SpecialistDelta` 可用；不证明任何方法具备投资收益，也不批准 Phase 5 的作者候选 Skill，更不表示 P4.3～P4.5 已完成。

## 1. 已交付

- `configs/research_skills.yaml`：`research-skills-v1`，登记 7 个合同。`IndustryBottleneckSkill`、`EventToAlphaSkill`、`GrowthProbabilitySkill`、`GrowthValuationLens`、`DailyTrendHealthSkill`、`HourlySwingSkill` 共 6 个分析合同占专家名额；`ResearchMemoComposer` 不占名额。
- 每个 manifest 固定来源 commit 或本地设计版本、触发标签、期限、必需/可选输入、频率、证据等级、推理步骤、正负信号、失效条件、失败模式、成本、依赖、冲突和输出 Schema。
- 状态 `ENABLED_CONTRACT` 只表示接口和门禁可执行，不表示固定权重或投资表现已经批准。
- `SpecialistRoutePlan`：保存命中理由、稳定分数、READY/DEGRADED/UNAVAILABLE、缺失输入、缺失频率、未选原因、覆盖状态、置信度上限和最多 3 个选择。
- `SpecialistDelta`：只允许增量发现、BaseCase 更正、行业指标、附加证据请求、失败模式、置信度变化、估值/风险调整和逐 section 覆盖变化；Schema 没有完整公司重写字段。
- migration 0023：registry、route plan、delta 三张安全索引，只保存 ID、版本、对象/输入哈希、计数、状态和置信度，不保存 statement、rationale、原文或本地路径。
- CLI：`research-specialist-list/route/status/audit` 与 `research-delta-import`；无效输入只返回固定错误码，不回显私密文字或文件路径。
- `CodexRunService` 已登记 `SpecialistRoutePlan` 与 `SpecialistDelta` Artifact Schema，继续执行 citation 守恒校验。

## 2. 路由和降级门禁

- 首版只使用显式标签、行业、事件、期限和固定排序；不调用 embedding、小模型或外部 LLM，因此相同输入的选择顺序可复现。
- 用户显式点名超过 3 个专家：拒绝，不替用户偷偷删减。
- 自动规则命中超过 3 个：按稳定分数和 skill_id 选前三，所有被截断项记录 `ROUTE_CAPPED_AT_THREE`。
- `ResearchMemoComposer` 固定记录为 `NON_SPECIALIST_COMPOSER`，不能被当作专家路由，也不占名额。
- 缺必需输入或必需频率：对应 Skill 为 `UNAVAILABLE`，不能入选；没有可用专家时总覆盖为 `INSUFFICIENT`。
- 缺一致预期这类可选输入：Skill 可降级运行，但记录 `CONSENSUS_UNAVAILABLE` 并压低覆盖/置信度上限，不用 0 或虚构数据代替。
- `HourlySwingSkill` 只接受独立 `60m` 频率；缺少时记录 `FREQUENCY_UNAVAILABLE`，不得借日线频率或阈值冒充。
- 日线与小时线显式冲突：自动路由保留较高稳定分支并说明排除原因；用户同时点名两者则拒绝歧义请求。

## 3. Delta 证据和隐私门禁

- Delta 必须引用已经登记的 route plan、同一个 BaseCase 和被选中的精确 skill_id/skill_version。
- 所有 finding、metric 和 adjustment 的 evidence_id 必须属于 BaseCase 的 FrozenEvidencePack；范围外引用直接拒绝。
- 关键 finding 至少需要一个 `PRIMARY_OFFICIAL` 证据；社区内容只能提供线索，不能独立支撑关键公司事实。
- Delta 总 evidence_ids 必须等于内部引用并集；对象、SQLite 计数和 Artifact registry 由审计重新核对。
- 审计检查未选 Skill 写入、冻结范围外引用、Evidence 缺失、晚于 BaseCase as_of 的未来信息、关键证据等级、索引计数和对象哈希。
- 合成测试中的 statement 和估值 rationale 已验证不会进入 SQLite；完整载荷只写入不可变 ObjectStore。

## 4. 自动化验收

- registry 精确为 7 个唯一合同、6 个专家合同，最大专家数为 3，memo 不占名额。
- 多规则自动命中稳定选择 3 个，同时出现明确截断与一致预期降级码；重复运行返回同一已登记 route plan。
- 显式 4 专家请求被拒绝；缺 `60m` 的 HourlySwing 为 UNAVAILABLE，未被日线替代。
- 被选中的产业瓶颈专家可以提交带官方证据的增量 finding、行业 metric 和估值 adjustment；重复导入幂等，完整审计 `PASS`。
- 未选中的 Skill、冻结范围外 evidence_id 和完整公司重写字段均被拒绝。
- CLI 私密标记与输入路径不回显。
- 全仓 `pytest`：181 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。

## 5. 真实运行库与 Git 边界

- migration 0001～0023 已应用；`integrity_check=ok`，外键违规 0。
- 已登记 `research-skills-v1` 一份，registry 对象哈希为 `96f58c31f113f196409da819f1b83f1f6f514b3d41f27bb1c918525554fc133d`。
- 真实 route plan 0、SpecialistDelta 0：当前没有用户指定的真实公司 FrozenEvidencePack/BaseCase，因此不制造示例投资结论冒充实测结果。
- 私有 PDF、DOCX 和 `runtime/state.sqlite` 均由 `.gitignore` 命中；本验收报告不含作者原文。

## 6. 下一步

P4.3 将在这些合同之上实现七个确定性诊断接口和各自的输入/输出校验，而不是把 manifest 文字当成已执行分析。P4.4 再实现版本化持仓生命周期与增量复核；P4.5 完成 memo、Repo Skill、恢复和 Phase 4 端到端终验。
