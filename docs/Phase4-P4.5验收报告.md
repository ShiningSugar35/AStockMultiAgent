# Phase 4 P4.5 Codex/Repo Skill 集成、恢复与终验报告

验收日期：2026-07-17

验收结论：通过。Phase 4 的冻结输入、严格 Codex 输出、全链状态/审计、中断恢复、Repo Skill 路由、CLI、真实 migration 和全仓回归均已有实际代码与测试。因此 P4.1～P4.5 和 Phase 4 标记完成。该结论不包含委员会、DecisionPack、真实交易、自动写模拟账本或收益承诺。

## 1. Codex 冻结输入清单

- 新增 `CodexArtifactReference`、`CodexRunInputManifest` 和 `codex-run-input-v2`。
- 每个输入固定 Artifact registry ID、类型、ObjectStore SHA-256 和角色；ID、哈希、Skill、旧路径均不得重复。
- 严格运行至少需要一个已登记工件，只有本地路径时 Schema 直接拒绝。
- 初始化前实际核对 registry 类型/哈希和 ObjectStore 完整性；未知、类型/哈希篡改和损坏对象均不创建 run。
- `RunManifest.input_hashes`、runtime 输入清单与 migration 0026 的 `codex_run_input_index` 三方一致；SQLite 只保存 ID、哈希、角色和时间，不保存请求、正文或路径。

## 2. 严格已登记输出

严格模式已覆盖十种实际 Phase 4 工件：

- FrozenEvidencePack、BaseCasePack；
- SpecialistRoutePlan、SpecialistDelta、SpecialistDiagnosticReport、ResearchMemoArtifact；
- PositionMonitoringPlan、HoldingEvidenceUpdate、HoldingReviewPack、PositionActionProposal。

导入顺序为领域 Schema → evidence citation → 禁止命令 Policy → 主 ID/registry/type/object hash → 输出必须已列入本 run 冻结输入。伪造主 ID、改 payload、输出未登记、输出不在冻结输入和 `requires_user_confirmation=false` 均不能进入 ArtifactStore。

Codex 验证包装对象显式写入 frozen input hashes 和 source artifact ID。实测相同 Proposal 加载不同附加冻结上下文时得到不同包装哈希，不发生 Artifact 身份碰撞；registry 的 input_hashes 与 RunManifest 精确一致。

## 3. 状态、审计和中断恢复

- 新增 `codex-run-status/audit/recover` 和 migration 0026 `codex_run_output_index`。
- 输出索引保存验证工件 ID/类型/哈希、原确定性工件 ID/哈希、draft hash、输入数、citation 数、严格门和状态，不保存 citation locator 或正文。
- audit 核对 request hash、RunManifest、Policy 版本、输入文件/索引/registry/ObjectStore、draft hash、validated/citations 文件、输出索引、source artifact 和 run 状态。
- 模拟“对象和验证文件已写、输出索引与 run 完成态尚未写”后，`recover` 使用原 draft 幂等补齐；重复恢复返回同一输出哈希。
- 无 draft 时恢复明确失败；完成后改 draft、改输入安全索引、改父 Artifact 或损坏输入对象都会使审计为 `PARTIAL`，不会被最新文件掩盖。
- 领域载荷无效时 CLI 只返回固定错误码，测试中的私密 payload 和输入路径不回显。

## 4. Phase 4 全链审计

- 新增只读 `Phase4ChainService`、`research-chain-status` 和 `research-chain-audit`。
- 服务从最新 BaseCase 开始，组合 core、route/Delta、diagnostic/memo 和可选 lifecycle 审计；不联网、不重跑研究、不新增 Evidence、不修改账本。
- 合成完整官方 Evidence/PIT 案例实际跑通 Evidence → BaseCase → route → Industry diagnostic/Delta → memo → monitoring plan → HOLD review → 十种 strict Codex validation，四段子审计与总审计均 PASS。
- 研究链完成但持仓尚未复核时，研究级 audit 可 PASS；指定 position 后总状态为 PARTIAL 并返回 `HOLDING_REVIEW_NOT_RUN`。
- position 与 company 不一致会返回明确 mismatch；删除早期 BaseCase Artifact 时总审计返回带阶段前缀的 finding code。

## 5. CLI 和 Repo Skill

- `probe` 现在列出 `codex-run-input-v2`、`registered_output_required` 和十种严格工件类型。
- `context-plan`、`codex-run-init` 支持 `--artifact-id`；注册对象按实际字节计入预算，严格运行使用 `--require-registered-output`。
- `$astock-research-orchestrator`、`$company-deep-research`、`$holding-monitor` 已改为真实存在的 Phase 4 命令，并要求 `research-chain-audit`/`holding-review-audit` 与 `codex-run-audit`。
- 公司研究 Skill 不再把尚未实现的 DecisionPack 写成当前产物；DecisionPack 明确留到 Phase 6。
- Repo Skill 自动测试核对其所列命令均真实注册，并保留不得联网委员会、不得直接写账本、不得真实下单的禁令。

## 6. 自动化与真实运行库

- Pytest collection：212 项。
- 因机器同时运行既有量化采集进程，单一全仓命令超过桌面命令时限；相同 collection 已穷尽分组执行：unit 80 passed，integration 106 passed，contract/property 18 passed，acceptance/live 8 skipped，总计 **204 passed / 8 skipped**。
- 跳过项仍是需显式环境开关的 30 文档真实 OCR benchmark、巨潮 live probe 和 6 个行情 provider live smoke；没有把失败测试写成 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实运行库 migration 0001～0026 已应用，`integrity_check=ok`，外键违规 0。
- 真实 `run=0`、`codex_run_input_index=0`、`codex_run_output_index=0`；当前没有用户指定的真实公司/持仓研究链，因此没有制造 Codex 结论。
- 真实 `research-chain-audit company:not-run` 返回 `NOT_RUN/CORE_NOT_RUN`，证明缺链不会伪装成 PASS。
- 私有 PDF、DOCX、runtime、Cookie 和浏览器 Profile 继续不进入 Git。

## 7. Phase 4 完成边界

Phase 4 现在能可靠完成“冻结证据—一次共性研究—最多三专家差分—引用保真 memo—增量持仓复核—人工确认建议—严格 Codex 包装—全链审计”。统一委员会、CounterCase、DecisionPack、TradeProtocol 和 `NEEDS_INFO` 调查闭环属于 Phase 6，下一步按先计划后编码进入 Phase 6。
