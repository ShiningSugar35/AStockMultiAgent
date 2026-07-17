# Phase 4 P4.1 冻结证据与 BaseCase 验收报告

验收日期：2026-07-17

验收结论：通过。该结论证明冻结证据和一次性共性 BaseCase 的合同、PIT 门禁、引用边界、幂等存储和隐私边界可用；不表示已对任何真实公司形成投资结论，也不表示 Phase 4 其余专家/持仓内核已经完成。

## 1. 已交付

- `FrozenEvidencePack`：冻结 company_id/as_of 下的 Claim、Evidence、冲突、证据等级和 PIT 元数据；保存输入哈希、覆盖状态和降级码。
- `BaseCasePack`：固定 12 个共性研究 section、带证据的判断、结构化缺口、专家标签、逐 section 覆盖、置信度上限和总引用集合。
- 关键判断必须至少有一个 `PRIMARY_OFFICIAL` 证据；社区线索不能独立支撑关键 BaseCase 判断。
- `configs/research_core.yaml`：`base-case-v1` 和 COMPLETE/PARTIAL/INSUFFICIENT 置信度上限，完整列举 12 个 section。
- migration 0022：`frozen_evidence_pack_index` 与 `base_case_pack_index`，只保存安全元数据和对象哈希，不保存 statement、原文或本地路径。
- `ResearchCoreService`：证据冻结、BaseCase 构建、对象/索引/Artifact/PIT/引用审计和 checkpoint。
- CLI：`research-evidence-freeze`、`research-base-case-build/status/audit`；输出只含 ID、哈希、计数、覆盖、置信度和降级码。
- `CodexRunService` 已登记 `FrozenEvidencePack` 与 `BaseCasePack` Schema，继续沿用 evidence_id citation 校验。

## 2. 硬门禁

- Claim 或 Evidence 晚于 as_of：拒绝。
- 正式历史模式缺 PIT：拒绝。
- 正式历史模式使用 `NOT_PIT_SAFE`：拒绝；`APPROXIMATED` 必须显式允许且保留降级码。
- Claim 属于其他 company：拒绝。
- BaseCase 引用冻结包以外 evidence_id：拒绝。
- 关键判断只有社区/私有方法材料、没有官方证据：拒绝。
- 开放 EvidenceConflict：不会丢失，自动生成 blocking gap，BaseCase 变为 `INSUFFICIENT`，置信度上限 0.40。
- section 缺失或材料不足：生成 `EMPTY_REQUIRED_SECTION`/结构化 gap 并降低置信度，不补写事实。

## 3. 幂等、恢复和安全

- FrozenEvidencePack ID 由规范化 as_of、解析后的 Claim/Evidence/PIT 集合和模式生成；相同输入重复运行返回同一 ID 和对象哈希。
- BaseCase ID 由 frozen pack、kernel version 和 draft hash 生成；共性分析重复提交不新增逻辑重复工件。
- 并发/崩溃重试若发现同一 ID 已登记，以已登记对象为准；后来产生的未索引对象只可能成为可审计孤儿，不覆盖正式工件。
- BaseCase 正文只在 ObjectStore；SQLite 索引和 CLI 不含合成测试 statement。
- 审计同时检查 FrozenEvidencePack/BaseCase 对象、PIT 状态、Evidence 存在性、冻结范围、未来信息、关键证据等级、SQLite 计数和 Artifact registry。

## 4. 自动化验收

- 完整合成官方链：PDF → SourceSnapshot/SourceDocument → Evidence → Claim → PIT → FrozenEvidencePack → 12-section BaseCase → audit `PASS`。
- 开放冲突案例：保留冲突并自动降级为 `INSUFFICIENT`，置信度从请求的 0.85 限制到 0.40。
- 社区证据案例：关键判断被拒绝。
- `NOT_PIT_SAFE` 与未来 Claim 案例：正式冻结被拒绝。
- 冻结范围外引用案例：BaseCase 被拒绝。
- CLI 非法草稿包含秘密标记时，只输出通用错误码，不回显标记或输入路径。
- 全仓 `pytest`：174 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。

## 5. 真实运行库

- migration 0001～0022 已应用；`integrity_check=ok`，外键违规 0。
- 真实 FrozenEvidencePack 0、BaseCasePack 0：当前没有用户指定的真实公司研究请求，因此不制造示例结论冒充真实研究。

## 6. 后续边界

P4.2 将实现版本化通用 Skill manifest、最多 3 个专家的确定性路由和只写差分的 `SpecialistDelta`。P4.1 不包含专家结论、研究 memo、持仓动作或委员会决策。
