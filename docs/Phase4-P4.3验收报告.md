# Phase 4 P4.3 七个确定性诊断接口验收报告

验收日期：2026-07-17

验收结论：通过。六个专家诊断器和一个引用保真 memo 组合器均已有实际代码、失败路径、CLI、不可变对象、安全索引和审计。该结论只证明规则合同可执行且不越过证据边界，不证明投资收益，不生成自动交易指令，也不批准 Phase 5 作者候选 Skill。

## 1. 公共实现

- `configs/research_diagnostics.yaml`：`research-diagnostics-v1`，固定产业可替代性阈值、估值中性带、日线/小时线各自的最低样本、回撤、波动和分数边界。
- 六种请求以固定 `skill_id`/`skill_version` 字面量区分；日线频率固定 `1d`，HourlySwing 固定 `60m`，Schema 层不能互换。
- `ResearchDiagnosticsService` 只允许 route plan 已选中的精确 Skill 运行，并把规则结果交回 P4.2 `build_delta` 复用冻结证据、关键官方证据、幂等和 Artifact 门禁。
- `SpecialistDiagnosticReport` 保存规则版本、配置内容哈希、PASS/PARTIAL/INSUFFICIENT、信号码、降级码、指标名、证据请求码、Delta ID 和引用并集。
- 同一 `diagnostics_version` 下偷偷修改阈值或使相同输入产生不同输出会被拒绝，必须显式升级版本。
- `ResearchMemoArtifact` 只保存 BaseCase/Delta 的结构化引用，不重新联网、不重新研究、不产生新 statement 或 evidence_id。
- migration 0024 只保存诊断/memo 的 ID、父子外键、版本、哈希、计数、覆盖和状态；完整内容只进 ObjectStore。

## 2. 六个专家诊断结果

### 2.1 产业瓶颈

- 完整链必须逐层验证系统变化、必要环节、稀缺性、可替代性和上市公司价值捕获。
- 合成完整官方链返回 `BOTTLENECK_CHAIN_VERIFIED`，重复运行 ID、对象和索引幂等。
- 价值捕获任一层断裂返回 `BOTTLENECK_CHAIN_INCOMPLETE`、`INSUFFICIENT_EVIDENCE` 和补证请求，不生成正向 finding。

### 2.2 Event-to-Alpha

- 完整输入要求已验证事件、经营指标及方向、财务指标及方向、起止时间窗、反证条件和证据。
- 只有标题的案例返回 `HEADLINE_ONLY`/`EVENT_TRANSMISSION_INCOMPLETE`，增量 finding 为空。
- 完整案例只形成可观察的经营—财务传导假设，并保留时间窗和反证；不形成订单。

### 2.3 成长概率

- 2～5 个场景使用 Decimal；概率不等于 1 时 Schema 直接拒绝，不自动归一化。
- 0.7×10% + 0.3×30% 的合成案例可复算为 `0.160` 概率加权年增长，并同时计算概率加权持续期。
- 缺一致预期时返回 `PARTIAL` 和 `CONSENSUS_UNAVAILABLE`，未以 0 替代。

### 2.4 成长估值

- 研究增长先显式扣除稀释，再与市场隐含增长比较；版本化 2% 中性带产生 INCREASE/NEUTRAL/DECREASE 三条可复算路径。
- 输出是有界的预期差调整，不是目标价；缺一致预期时置信度不允许上调。

### 2.5 日线趋势

- 只接受 `1d`、质量 `PASS` 且至少 60 根的指标；合成健康案例返回 `DAILY_TREND_HEALTHY` 并明确不是买入信号。
- 质量 FAIL 且样本不足案例同时返回 `QUALITY_GATE_FAILED` 与 `INSUFFICIENT_DAILY_BARS`，不计算有效趋势结论。

### 2.6 HourlySwing

- 只接受独立 `60m`、质量 `PASS` 且至少 40 根的指标，使用独立回撤 8%、波动 6% 和小时分数规则。
- 合成正向案例返回 `HOURLY_SWING_POSITIVE` 并明确不是订单；质量失败和 5 根样本案例返回小时线不足，不退用日线阈值。

## 3. ResearchMemoComposer

- 逐一引用 12 个 BaseCase section 的 finding/evidence，并为每个 Delta 列出增量 finding、correction、metric、adjustment 和 evidence ID。
- 只提供产业 Delta、尚缺事件 Delta 时，memo 为 `PARTIAL` 并列出 `EventToAlphaSkill`；补齐同 route 事件 Delta 后变为 `SUFFICIENT`。
- memo 总 evidence_ids 等于 BaseCase 与所列 Delta 引用并集；重复组合幂等。
- 来自另一个 route plan 的估值 Delta 被拒绝，不能混入同一研究 memo。

## 4. CLI、审计和隐私

- 新增 `research-diagnostic-schema`、`research-specialist-diagnose`、`research-memo-compose`、`research-diagnostic-status/audit`。
- CLI 只输出 ID、哈希、状态、信号码、降级码和计数；无效诊断/memo 请求中的秘密标记和输入路径不回显。
- 审计核对诊断对象、配置哈希、Delta 身份、索引计数、Artifact registry、冻结证据范围、Evidence 存在性、未来信息和 memo 父子引用。
- 合成事件指标、反证和诊断 statement 已验证不会进入 SQLite。

## 5. 自动化与真实运行库

- 全仓 `pytest`：187 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实运行库 migration 0001～0024 已应用，`integrity_check=ok`，外键违规 0。
- 真实 `specialist_diagnostic_index=0`、`research_memo_index=0`：当前没有用户指定的真实公司 BaseCase，因此未制造示例结论。
- 私有 PDF、DOCX 和 `runtime/state.sqlite` 继续由 `.gitignore` 命中；代码、测试和本报告不包含作者原文。

## 6. 后续边界

P4.4 将实现版本化通用持仓生命周期规则、监控计划、上次冻结点之后的增量 EvidenceUpdate 和只产生人工确认建议的 HoldingReview。P4.3 不修改模拟账本，也不处理真实券商下单。
