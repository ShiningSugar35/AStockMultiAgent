# Phase 4：Serenity 改造与通用投资内核开发计划

计划日期：2026-07-17

状态：P4.1～P4.5 已实现并验收，Phase 4 完成。

## 1. 目标

Phase 4 把已经完成的证据、PIT、财务核验和行情基础设施接成一条可审计的研究内核：同一公司、同一信息截面只做一次共性分析，专家只补充真正的行业或方法差分，开放持仓只读取上次冻结时点之后的新增证据。

本阶段交付研究工件和建议，不承诺收益，不批准 Phase 5 的作者 Skill，不自动修改模拟账本，更不向真实券商发单。

## 2. 固定边界

1. `BaseCasePack`、`SpecialistDelta`、研究 memo、监控计划和复核包属于版本化 Artifact，正文只进入 ObjectStore；SQLite 只保存 ID、对象哈希、输入哈希、版本、计数和状态。
2. 每条关键判断必须引用 `evidence_id`；社区内容只能作线索，涉及公司事实时必须有官方或更强证据。
3. 构建时按 `as_of` 校验证据可得时间；晚于研究时点的证据一律拒绝，`NOT_PIT_SAFE` 输入不得伪装成正式历史评测可用。
4. 共性分析只构建一次。专家输入必须引用同一个冻结 BaseCase；专家 Schema 不提供重新书写完整公司介绍、通用财务和整套风险的字段。
5. 路由默认最多 3 个专家；超过上限直接拒绝，而不是悄悄截断。首版只用显式业务规则、标签和关键词，不引入 embedding 或小模型的不确定调用。
6. 缺一致预期、缺小时数据、缺行业证据或专家覆盖不足时返回明确降级码、置信度上限和信息缺口，不插值、不编造。
7. 日线趋势和 HourlySwing 使用独立 manifest、输入频率和规则版本；小时线不得复用日线阈值。
8. 所有持仓动作只是建议；`PositionActionProposal.requires_user_confirmation` 在 Schema 层固定为 `true`。
9. Phase 5 自动摘录和候选 Skill 保持 `PENDING/NOT_RUN`，未经人工综合和评测不得进入 Phase 4 已批准 manifest。

## 3. P4.1：冻结证据与 BaseCase

### 3.1 工件

- `FrozenEvidencePack`：company_id、as_of、claim_ids、evidence_ids、冲突、PIT 状态分布、coverage、输入对象哈希和冻结哈希。
- `CitedResearchFinding`：statement、finding_type、confidence、critical、evidence_ids；关键判断至少一个 evidence_id。
- `ResearchGap`：缺口类别、影响、所需证据和是否阻塞。
- `BaseCasePack`：业务模式、收入/利润驱动、现金流质量、资本回报、再投资、治理、竞争、产业供需、估值预期、趋势背景、风险、缺口、专家标签、coverage、base_confidence 和 evidence_ids。

### 3.2 构建规则

1. 先从 Claim—Evidence 图按 company_id 和 as_of 冻结精确证据集合；开放冲突进入 pack，不能被遗漏。
2. BaseCase 草稿只能引用冻结 evidence_ids；引用集合、section 汇总和总 evidence_ids 必须守恒。
3. 相同冻结输入、内核版本和草稿内容生成相同 base_case_id/object hash；重复运行不得新增逻辑重复工件。
4. BaseCase 不自动给出买卖结论；证据不足时使用 gap 和低 coverage，不用常识补事实。

## 4. P4.2：Skill manifest、专家路由和差分

### 4.1 版本化 manifest

首版配置登记：

- `IndustryBottleneckSkill`
- `EventToAlphaSkill`
- `GrowthProbabilitySkill`
- `GrowthValuationLens`
- `DailyTrendHealthSkill`
- `HourlySwingSkill`
- `ResearchMemoComposer`

每个 manifest 包含来源固定 commit/本地设计来源、触发标签、市场、行业、周期、必需输入、必需证据、推理步骤、正负信号、失效条件、失败模式、成本级别、兼容/冲突 Skill、规则版本和批准状态。首版通用内核可标为 `APPROVED_INTERNAL_RULESET`；来自 Phase 5 作者的候选不得混入。

### 4.2 路由

- 输入：BaseCase 的业务/行业/风险/事件/周期标签、可用数据频率和一致预期可用性。
- 顺序：硬性业务规则 → 显式标签 → 关键词；稳定分数与 skill_id 打破并列。
- 输出：`SpecialistRoutePlan`，记录命中理由、未命中原因、降级码、覆盖状态、置信度上限和最多 3 个 Skill。
- `ResearchMemoComposer` 是最终格式化节点，不计入专家上限；没有合格专家时返回 `INSUFFICIENT`，禁止最高置信度并生成外部证据需求。

### 4.3 差分

`SpecialistDelta` 只允许：增量发现、BaseCase 更正、行业指标、附加证据请求、失败模式、confidence_delta、估值调整、风险调整、coverage_delta 和 evidence_ids。每个发现/更正/调整都必须引用证据；全部引用必须属于专家的冻结 EvidenceScope。

## 5. P4.3：七个通用 Skill 的确定性诊断接口

首版不复制上游固定总分，也不让诊断分直接成为交易信号。

1. 产业瓶颈：验证“系统变化 → 必要环节 → 稀缺性/可替代性 → 上市公司价值捕获”链；缺任一层则 `INSUFFICIENT_EVIDENCE`。
2. Event-to-Alpha：事件必须落到可验证的经营指标、财务方向、时间窗和反证；只有新闻标题时不产生 Alpha 结论。
3. 成长概率：场景概率、增长驱动、持续时间和失败条件显式输入；概率必须守恒。
4. 成长估值：区分当前价格隐含预期和研究场景；一致预期不可得时标 `CONSENSUS_UNAVAILABLE`，不以 0 替代。
5. 日线趋势：只消费通过质量门的日线派生指标，输出趋势背景而非独立买入建议。
6. HourlySwing：独立 60 分钟规则和数据质量门；缺小时数据时 `FREQUENCY_UNAVAILABLE`，不得退用日线阈值冒充。
7. Research memo：只组合 BaseCase 和 Delta 的引用，不重新研究、不引入新 evidence_id。

每个 Skill 都以同一 `SpecialistObservation`/`SpecialistDelta` 合同运行，具体规则由版本化 YAML 定义，测试覆盖缺字段、错误频率、证据不足和降级路径。

## 6. P4.4：版本化通用持仓生命周期内核

### 6.1 规则集

建立 `generic-position-lifecycle-v1`，覆盖 ENTRY、HOLDING、ADD、TRIM、EXIT、REVIEW。规则只定义可审计优先级和安全门，不用一套固定百分比止损强加给所有风格。

- 论点明确失效或不可交易硬阻断：EXIT；
- 核心证据冲突、数据质量冻结、重大公告待核：REVIEW，并禁止 ADD；
- 风险/估值减仓条件触发：TRIM；
- ADD 必须同时有显式加仓条件、新证据支持且没有任何硬阻断；
- 无新增证据和无触发条件：HOLD，不等同于论点增强；
- 到期、重大事件和人工信息需求触发 REVIEW。

### 6.2 工件与增量

- `PositionMonitoringPlan`：引用 decision/base case/evidence snapshot、规则集版本、监控来源、频率、加减仓/退出/复核条件。
- `HoldingEvidenceUpdate`：严格描述上次冻结时点到当前时点的新增、变化、失效和冲突证据。
- `HoldingRuleSignal`：只声明已触发的已登记 rule_id、观测值、时间和 evidence_ids。
- `HoldingReviewPack`：确定性优先级合并后的建议、硬阻断、触发规则、冲突和下次复核条件。
- `PositionActionProposal`：永远需要用户确认；本服务不写模拟账本。

## 7. P4.5：存储、CLI、Codex 和恢复

- migration：只建研究 Artifact 的安全索引、版本和父子引用，不保存 statement、memo、论点或本地路径。
- ObjectStore：保存完整冻结 pack、BaseCase、Delta、route plan、监控计划和复核包。
- Artifact registry：登记对象哈希与输入哈希，重复执行幂等。
- checkpoint：BaseCase、route、delta 和 holding review 分别记录成功边界；对象已写但索引未提交时可重跑恢复。
- CLI 计划交付：`research-evidence-freeze`、`research-base-case-build/status/audit`、`research-specialist-route`、`research-delta-import`、`position-plan-create`、`holding-review-run/status/audit`。CLI 状态输出只含 ID、哈希、计数、覆盖、降级码和状态。
- CodexRunService 支持导入 `FrozenEvidencePack`、`BaseCasePack`、`SpecialistDelta`、`SpecialistRoutePlan`、`PositionMonitoringPlan` 和 `HoldingReviewPack`，继续执行 evidence_id citation 校验。

## 8. 测试与验收

### 8.1 自动测试

- 相同输入稳定 ID/object hash、重复运行幂等；版本升级并存。
- 未来证据、冻结范围外引用、关键判断无 evidence_id、开放冲突丢失均拒绝。
- BaseCase 只注册一次；多个 Delta 引用同一个 base_case_id。
- 路由稳定、最多 3 个专家、无匹配覆盖降级、ResearchMemoComposer 不占专家名额。
- 缺一致预期、缺小时数据、日/小时频率错用均返回明确降级。
- Delta Schema 无完整 BaseCase 字段；所有调整有证据。
- 生命周期动作优先级、ADD 硬门、无新增证据为 HOLD、所有 proposal 强制人工确认。
- SQLite 不含测试 statement/论点/路径；ObjectStore、SQLite 索引和 artifact registry 对账为 0。
- Windows UTF-8/路径、migration 保留数据、崩溃恢复和 CLI 安全输出。

### 8.2 阶段验收

- `pytest`、`ruff`、`pyright` 全通过；SQLite integrity 与外键违规为 0。
- 用合成但具完整证据/PIT 的公司案例跑通冻结证据 → BaseCase → 1～3 个 Delta → memo 引用集合 → 监控计划 → 增量复核。
- 另跑缺一致预期、缺专家和缺小时数据三个降级案例，证明系统不补造。
- Git 不包含私有 PDF/DOCX、runtime、正文、Cookie、浏览器 Profile 或测试中的真实作者原文。

## 9. 明确不在 Phase 4 完成的事项

- 统一委员会、CounterCase、DecisionPack 和硬风险裁决属于 Phase 6。
- 冻结权重影子账户、walk-forward、样本外增量价值属于 Phase 7。
- 动态权重、多臂赌博机和 RL 属于 Phase 8，未满足 Phase 7 门槛时保持禁用。
- Phase 5 的 401 条摘录人工综合、40 个候选 Skill 评测和三位作者采集缺口继续单独跟踪。

## 10. 停止条件

只有当需要用户选择投资风格、提供无法从已冻结证据判断的真实仓位语义，或批准作者候选 Skill 时才暂停对应支路。单个 Skill 输入不足时继续完成其他通用内核，并把该支路标成结构化缺口。

## 11. 实施进度

- [x] P4.1：冻结证据、PIT/冲突门禁、一次性 BaseCase、ObjectStore/安全 SQLite 索引、CLI、审计和验收。
- [x] P4.2：版本化 Skill manifest、确定性专家路由、最多 3 个专家和 SpecialistDelta。
- [x] P4.3：七个通用 Skill 的确定性诊断接口和降级路径。
- [x] P4.4：版本化通用持仓生命周期内核与增量 HoldingReview。
- [x] P4.5：Codex/Repo Skill 集成、整体恢复与 Phase 4 终验。

P4.1～P4.5 验收依据见 `docs/Phase4-P4.1验收报告.md`、`docs/Phase4-P4.2验收报告.md`、`docs/Phase4-P4.3验收报告.md`、`docs/Phase4-P4.4验收报告.md` 与 `docs/Phase4-P4.5验收报告.md`。
