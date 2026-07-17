# Phase 4 P4.3 七个确定性诊断接口开发计划

计划日期：2026-07-17

状态：开发前计划。P4.2 只登记了可执行合同和路由；本计划完成前，不把任何 manifest 描述当作已经运行的研究分析。

## 1. 目标与边界

P4.3 在同一个 FrozenEvidencePack、BaseCasePack 和 SpecialistRoutePlan 上实现 6 个占专家名额的确定性诊断器，以及 1 个不占名额的引用保真 memo 组合器。诊断器只把结构化观测转换为带证据的 `SpecialistDelta`，不重复写完整公司报告，不输出自动买卖指令，不承诺收益。

作者蒸馏得到的 40 个候选 Skill 继续保持 `NOT_RUN/PENDING`，不得进入本注册表。P4.3 的规则状态只表示代码接口和门禁通过，不表示历史收益或固定阈值已得到投资批准。

## 2. 公共接口和存储

1. 为每个诊断器建立严格请求 Schema；`skill_id` 和 `skill_version` 使用固定字面量，防止把一种输入伪装成另一种 Skill。
2. 建立版本化 `research_diagnostics.yaml`，只保存透明阈值和最低样本数；禁止隐藏模型、embedding 或联网推理。
3. `ResearchDiagnosticsService` 先验证 route plan 的精确 Skill 是否已选中，再执行规则，最后调用 P4.2 的 `build_delta` 做冻结证据、官方证据、幂等和 Artifact 门禁。
4. `SpecialistDiagnosticReport` 记录规则版本、PASS/PARTIAL/INSUFFICIENT、信号码、降级码、指标名、证据请求码、Delta ID 和证据并集。完整报告进入 ObjectStore；SQLite 只保存安全索引和计数。
5. `ResearchMemoArtifact` 只引用 BaseCase finding ID、Delta/finding/metric/adjustment ID、gap code 和 evidence_id，并检查引用并集守恒；不得新增事实或 evidence_id。
6. migration 0024 建立诊断报告和 memo 的安全索引，保留父子外键、对象哈希、输入哈希、版本、计数、覆盖和状态，不保存 statement、rationale、标题正文或本地路径。

## 3. 六个专家诊断器

### 3.1 IndustryBottleneckSkill

- 输入链：系统变化、必要环节、稀缺性、可替代性和上市公司价值捕获，每层单独携带 evidence_id。
- 只有五层全部成立且可替代性不超过版本阈值时，才输出正向增量发现。
- 任一层缺失或未验证时返回 `INSUFFICIENT_EVIDENCE` 和对应证据请求，不用总分掩盖断链。

### 3.2 EventToAlphaSkill

- 输入必须含已验证事件、经营指标方向、财务方向、明确时间窗、可证伪条件和各自证据。
- 只有标题、没有传导指标、没有时间窗或没有反证时，返回 `EVENT_TRANSMISSION_INCOMPLETE`，不得生成 Alpha 结论。
- 完整时只输出“可观察的经营/财务传导假设”，不直接生成订单。

### 3.3 GrowthProbabilitySkill

- 输入 2～5 个互斥场景；每个场景显式给出概率、年化增长、持续年限、驱动、失败条件和证据。
- Decimal 概率必须精确合计为 1；不守恒直接拒绝，不在服务内部偷偷归一化。
- 输出概率加权增长与持续期；一致预期缺失只标 `CONSENSUS_UNAVAILABLE`，不以 0 代替。

### 3.4 GrowthValuationLens

- 分开输入市场隐含增长、研究场景增长、稀释、再投资、估值依据和可选一致预期。
- 只比较“市场已经定价的预期”与研究场景之差，并按版本化中性带生成 INCREASE/DECREASE/NEUTRAL 调整；不生成伪精确目标价。
- 一致预期缺失时保持可运行但降级，置信度不得上调。

### 3.5 DailyTrendHealthSkill

- 只接受 `1d`、质量门 `PASS` 且达到独立最低样本数的派生指标。
- 使用版本化日线规则判断 HEALTHY/MIXED/WEAK 背景，输出价格趋势 section 的非关键增量；不得转成独立买入建议。
- 质量失败、频率错误或样本不足时不计算分数，返回明确缺口。

### 3.6 HourlySwingSkill

- 只接受独立 `60m`、质量门 `PASS` 且达到小时线最低样本数的指标。
- 使用独立小时规则和阈值输出 POSITIVE/NEUTRAL/NEGATIVE timing context；不复用日线分数和阈值，不生成订单。
- 缠入 `1d`、缺小时频率或质量失败时返回 `FREQUENCY_UNAVAILABLE`/`QUALITY_GATE_FAILED`，不得降格冒充。

## 4. ResearchMemoComposer

1. 输入一个 BaseCase、同一个 route plan 和属于该 route 的 Delta ID 列表。
2. 逐项核对 Delta 的 base_case_id、route_plan_id、被选 Skill 和 FrozenEvidencePack；范围外或其他公司的 Delta 拒绝。
3. 生成 12 个 BaseCase section 的 finding/evidence 引用，另列每个 Delta 的增量 finding、correction、metric、adjustment 和 evidence 引用。
4. 总 evidence_ids 必须等于 BaseCase 与所列 Delta 引用并集；未运行的已选专家明确进入 `missing_selected_skill_ids`。
5. memo 只组合引用，不重新联网、不重新研究、不生成新 statement，也不修改账本。

## 5. CLI、恢复和审计

- `research-diagnostic-schema`：列出七种请求类型、规则版本和固定 Skill 版本，不输出私密内容。
- `research-specialist-diagnose <request.json>`：严格解析一个诊断请求，返回 report/delta ID、哈希、状态、信号码、降级码和计数。
- `research-memo-compose <request.json>`：生成引用保真 memo，返回 ID、哈希、覆盖、引用计数和缺失专家。
- `research-diagnostic-status/audit <base_case_id>`：检查对象、索引、父子关系、Skill 选择、证据范围、未来信息、引用并集、计数和 Artifact registry。
- 无效请求只返回固定错误码；异常文本、statement、rationale 和输入路径不得回显。

## 6. 自动化验收

1. 产业链任一层断裂返回不足；完整链生成一次幂等 Delta。
2. 只有新闻标题的事件不产生传导结论；完整事件链生成带时间窗与反证的增量。
3. 成长概率不守恒被拒绝；守恒场景的加权结果可复算；无一致预期明确降级。
4. 估值中性带、上调和下调三条路径均可复算；缺一致预期不被写成 0。
5. 日线与小时线分别通过正确频率/质量/样本门；互换频率、失败质量和不足样本均拒绝或降级。
6. 所有诊断只允许被 route 选中的精确 Skill；冻结范围外证据和关键判断缺官方证据被 P4.2 门禁拒绝。
7. memo 证据并集守恒，不新增 evidence_id，其他 route 的 Delta 被拒绝，缺失已选 Delta 明确显示。
8. 重复运行 ID/对象/索引幂等；SQLite 不含合成 statement/rationale；全仓 pytest、ruff、pyright、真实迁移、完整性、外键和 Git 隐私检查通过。

## 7. 完成定义

只有当七种接口都有实际代码、失败路径测试、CLI、对象/安全索引、审计和验收报告时，P4.3 才能标为完成。仅有 YAML、Schema 或文档不算完成。
