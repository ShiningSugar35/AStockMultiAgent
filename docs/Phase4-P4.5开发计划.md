# Phase 4 P4.5 Codex/Repo Skill 集成、恢复与终验开发计划

计划日期：2026-07-17

状态：开发前计划。P4.1～P4.4 已有实际实现和验收；本文件只定义最后一段集成工作，不把计划写成已完成能力。

## 1. 目标与非目标

P4.5 不增加新的投资判断规则，而是把已完成的冻结 EvidencePack、BaseCase、专家 route/Delta、诊断、memo、PositionMonitoringPlan 和 HoldingReview 串成一条可恢复、可从 Repo Skill 正确调用、可由 Codex 严格引用既有确定性工件的完整链。

本阶段仍不实现委员会、CounterCase、DecisionPack、真实交易或自动写模拟账本。Codex 只验证和包装已经由确定性服务登记的 Phase 4 工件；不能用一段新生成的文字冒充已运行的 Python 研究链。

## 2. 冻结 Codex 输入清单

新增 `CodexArtifactReference` 和 `CodexRunInputManifest`：

- reference 固定 artifact_id、artifact_type、object_sha256 和 role；object hash 必须为 64 位小写 SHA-256。
- manifest 固定版本、selected_skills、注册工件引用、仅供本地预算使用的旧路径列表，以及 `require_registered_output` 安全门。
- artifact_id、object hash 和 Skill 必须去重；严格模式至少有一个注册工件引用，不能只传本地路径。
- 初始化时逐项核对 Artifact registry、类型、对象哈希和 ObjectStore 完整性；任一不一致时不创建 run。
- `RunManifest.input_hashes` 必须等于清单中冻结对象哈希的稳定集合；完整清单只保存在 runtime，SQLite 不保存路径或正文。

migration 0026 新增安全索引：

- `codex_run_input_index`：run_id、artifact_id、type、role、object hash；
- `codex_run_output_index`：run_id、Codex 验证工件 ID/类型/哈希、原确定性工件 ID/哈希、输入数、引用数、严格门和状态。

两张表只保存 ID、哈希、计数和状态，不保存自然语言请求、citation locator、输入路径或正文。

## 3. 严格“只包装已登记输出”门禁

严格模式支持 P4.1～P4.4 的确定性工件：FrozenEvidencePack、BaseCasePack、SpecialistRoutePlan、SpecialistDelta、SpecialistDiagnosticReport、ResearchMemoArtifact、PositionMonitoringPlan、HoldingEvidenceUpdate、HoldingReviewPack 和 PositionActionProposal。

Codex 导入时：

1. 仍先执行领域 Schema、evidence_id citation 和禁止命令 Policy；
2. 从工件主 ID 推导其确定性 Artifact registry ID；缺主 ID、未登记、类型不符、对象哈希不等或 ObjectStore 损坏均拒绝；
3. PositionActionProposal 继续由 Schema 强制 `requires_user_confirmation=true`；
4. Codex 验证包装对象显式包含 frozen input hashes 和 source artifact ID，避免相同正文在不同输入下发生身份碰撞；
5. Artifact registry 的 input_hashes 写入真实冻结输入，不再一律为空。

非严格兼容路径只用于既有 M1 通用工件测试和非 Phase 4 草稿；Repo Skill 的正式 Phase 4 路径必须开启严格模式。

## 4. Codex run 状态、审计与恢复

新增服务和 CLI：

- `codex-run-status <run_id>`：只返回状态、清单版本、输入/输出 ID 和计数；
- `codex-run-audit <run_id>`：核对 run 行、RunManifest、输入清单、输入安全索引、Artifact registry、ObjectStore、validated_result、citations、输出索引和状态；
- `codex-run-recover <run_id>`：若已有合法 result_draft 但导入在中途失败，幂等重跑导入并补齐文件、Artifact、输出索引和 run 状态；没有 draft 时只报告不可恢复，不伪造输出。

导入写入顺序固定为：验证输入和 draft → 写不可变包装对象 → 幂等登记 Artifact → 原子写 validated/citations/summary/manifest → 同一 SQLite 事务写输出索引并更新 run。故障注入覆盖 Artifact 已登记但输出索引未写、文件已写但 run 未完成两类边界。

## 5. Phase 4 全链状态与审计

新增 `Phase4ChainService` 和：

- `research-chain-status <company_id> [--position-id]`；
- `research-chain-audit <company_id> [--position-id]`。

服务从最新 BaseCase 开始，读取而不重跑研究，组合 P4.1 core audit、P4.2 route/Delta audit、P4.3 diagnostic/memo audit 和可选 P4.4 lifecycle audit。输出只包含阶段状态、父 ID、覆盖、计数和 finding code；任何子审计 NOT_RUN/PARTIAL 都不能被总状态写成 PASS。

该服务不联网、不新增 Evidence、不调用专家、不修改账本。它解决“每个模块各自正常，但整条链是否真的接上”的终验问题。

## 6. CLI 与 Repo Skill 落地

- `probe` 增加已支持的严格 Phase 4 工件类型、输入清单版本和 `registered_output_required` 能力说明。
- `context-plan` 与 `codex-run-init` 增加 `--artifact-id`；注册对象按实际字节计入上下文预算，输出只列 artifact ID，不暴露 ObjectStore 路径。
- `codex-run-init` 增加 `--require-registered-output`；严格模式拒绝只有路径、没有冻结 artifact reference 的初始化。
- 更新 `$astock-research-orchestrator`、`$company-deep-research` 和 `$holding-monitor`：写明实际 research/position/chain 命令、严格 Codex 初始化和导入顺序，删除“尚未实现时假装 DecisionPack”的旧表述。
- 其他 Repo Skill 只在需要时更新能力说明；不扩大其触发范围，也不允许 Skill 直接编辑 SQLite。

## 7. 自动化验收

1. 输入清单去重、未知 artifact、类型/哈希不符、坏 ObjectStore 和严格模式路径-only 全部拒绝，且不留下半个 run。
2. 严格导入十种 Phase 4 工件的主 ID/registry/hash 门禁；伪造 payload、未登记 payload 和 `requires_user_confirmation=false` 拒绝。
3. citation 缺失/空定位、requested_commands、run_id 路径穿越继续拒绝。
4. 相同 run 重复导入幂等；相同输出绑定不同 frozen inputs 时身份不碰撞；Artifact input_hashes 精确等于 RunManifest。
5. 故障注入后 `codex-run-recover` 补齐输出；无 draft、坏 draft 或输入损坏时明确不可恢复。
6. 全链合成案例实际跑通 Evidence → BaseCase → route → diagnostic/Delta → memo → plan → review → strict Codex validation，四个子审计和总审计均 PASS。
7. 删除早期父 Artifact、篡改安全索引或制造子阶段 PARTIAL 时，总审计必须 PARTIAL 并给出阶段化 finding code。
8. Repo Skill 测试验证命令真实存在、严格门已写入正式流程、禁止联网委员会/直接账本/真实订单仍保留。
9. SQLite 0026 表中不出现合成请求、citation、thesis、规则说明、路径或私密数量；真实库 migration、integrity、外键和对象核验通过。
10. 全仓 `pytest`、`ruff check .`、`pyright` 通过，私有 PDF/DOCX、runtime、Cookie 和浏览器 Profile 不进 Git。

## 8. 完成定义

只有当冻结输入、严格输出、Codex 中断恢复、Phase 4 全链审计、Repo Skill 可执行路由、真实 migration 和全仓验收全部由代码与测试证明后，P4.5 和 Phase 4 才标记完成。计划文件、宽松 Schema 或单模块 PASS 都不能替代终验。
