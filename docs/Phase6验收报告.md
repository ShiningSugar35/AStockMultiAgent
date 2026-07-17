# Phase 6 委员会与选择性反方验收报告

验收日期：2026-07-17

验收结论：通过。Phase 6 已交付“严格冻结输入 → 确定性硬门槛 → 选择性反方 → 不可变 DecisionPack → 每项决定对应一份 TradeProtocol → NEEDS_INFO 调查闭环 → 审计与恢复”的完整本地链路。该结论只表示决策基础设施可用，不表示任何股票已经通过研究，不表示 Phase 5 已完成，也不授权真实交易或自动写入模拟账本。

## 1. 冻结输入和无网络边界

- 委员会只接受白名单内、已登记到 ArtifactRegistry、ObjectStore 哈希可重算、公司和 `as_of` 一致、PIT/引用/Skill 版本合格的冻结工件。
- 未知 ID、类型不符、索引哈希不符、对象损坏、未来工件、公司错配、未批准 Skill 和冻结范围外证据都会在产生半条记录前被拒绝。
- `CommitteeAccessPolicy` 强制 network/API/MCP/browser/完整文档/新研究全部为 `false`；缺材料只能返回 `NEEDS_INFO`，委员会不能自己上网补证。
- `probe` 实测公开 `network_access=false`、`api_access=false`、`mcp_access=false`、`browser_access=false`、`full_document_access=false`，可选外部 narrative 默认为 `OPTIONAL_DISABLED`。

## 2. 确定性决定与不可覆盖硬门槛

- 版本化规则为 `committee-rules-v1`，引擎为 `committee-deterministic-v1`。相同请求、冻结输入、规则和引擎生成相同 bundle、decision、protocol ID 与对象哈希。
- 固定 verdict 为 `REJECT`、`NEEDS_INFO`、`WATCH`、`PAPER_ELIGIBLE`、`PAPER_HOLD`、`PAPER_EXIT`；退出和硬阻断优先于乐观收益或文字说明。
- 不可交易、禁买、紧急停止、杠杆、严重财务勾稽失败、明确 thesis 失效和单公司仓位超限会硬拒绝。
- 首版组合闸门固定为：计划后总暴露上限 80%、行业暴露上限 25%、最大绝对相关性上限 80%；组合回撤达到 20% 或连续亏损达到 5 次时冻结新增风险。
- 重大公告待核验、数据异常、关键 PIT/证据/专家覆盖不足、核心来源冲突或社区内容成为关键事实唯一依据时进入 `NEEDS_INFO`，不会用较弱语气冒充结论。
- FinancialIntegrity 工件是新候选获得模拟资格的必要输入；严重高等级财务身份勾稽失败不能被其他专家或 narrative 覆盖。
- 可选 narrative 只解释已经冻结的结构化结果。预算超限、Provider 未启用、探针失败或成本超限时确定性降级；它永远不能改变 verdict、硬阻断、仓位或协议状态。

## 3. 选择性反方和补证闭环

- CounterCase 只在高仓位、实质分歧、低边际覆盖、异常财务、高潜在收益但弱假设、重大公告、估值接近边界或组合风险显著变化时触发，未触发时不制造形式化反方成本。
- 已触发但缺少合格 CounterCasePack 时固定返回 `NEEDS_INFO`；反方只能挑战冻结集合内的 claim/evidence，不能引入未登记事实。
- 每个缺口会生成结构化 `CommitteeInvestigationTask`。任务解决必须引用新登记的解决工件；旧 DecisionPack 不修改，重跑必须创建新 bundle 和新 decision。
- 任务状态命令只更新安全索引，不让委员会自行执行搜索、抓取或人工步骤。

## 4. DecisionPack、TradeProtocol 和交易边界

- 每次决定都生成不可变 DecisionPack，并保存冻结输入哈希、规则/引擎版本、硬阻断、缺口、反方结果、引用和决定哈希。
- 每个 DecisionPack 都有一份 TradeProtocol。`WATCH`、`NEEDS_INFO` 和 `REJECT` 对应 `BLOCKED`；其他 verdict 也只代表模拟资格、持有或退出协议。
- 所有协议强制 `requires_user_confirmation=true`、`broker_execution_allowed=false`、`ledger_write_allowed=false`。Phase 6 没有券商接口，也没有订单、成交、持仓或账本写入路径。
- Codex 严格已登记输出扩展到 CounterCasePack、DecisionPack 和 TradeProtocol；未登记、未冻结、哈希不符或内容被改写的包装输出不能导入 ArtifactStore。

## 5. 持久化、审计、恢复和 CLI

- migration `0027_committee.sql` 新增规则、评估、反方、bundle/input、决定、协议和调查任务安全索引；完整正文只进入不可变 ObjectStore。
- 写入使用外键、唯一约束、稳定身份和父哈希；对象已写而索引中断时可按原请求恢复，不会重新研究或换输入。
- `committee-audit` 核对 registry、ObjectStore、输入父哈希、规则、决定、协议和调查任务；篡改不会通过联网重抓被掩盖。
- 已交付 `committee-schema`、`committee-input-resolve`、`committee-plan`、`committee-decide`、`committee-status`、`committee-audit`、`committee-recover`、`committee-task-status`、`committee-task-resolve`。
- `$astock-research-orchestrator`、`$company-deep-research`、`$holding-monitor` 和 `$evidence-investigation` 已使用真实 Phase 6 命令，并保留不得直写 SQLite、不得联网委员会、不得声称真实成交的边界。

## 6. 自动化和真实运行实测

- Python：3.12.10，符合项目固定的 `>=3.12,<3.13`。
- 全仓 Pytest collection：222 项；实际结果为 **214 passed / 8 skipped**，耗时 110.75 秒。
- 8 项跳过均由明确环境开关控制：1 项 30 文档真实 OCR benchmark、1 项巨潮 live probe、6 项行情 Provider live smoke；没有把失败改成 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实 `uv run astock probe`：委员会 `AVAILABLE`、规则 v1、无网络能力，状态库完整性 `ok`。
- 真实运行库 migration 最新为 0027，`PRAGMA integrity_check=ok`，外键违规 0。
- 8 张委员会表实测全部为 0 行；`committee-status company:not-run` 返回 `NOT_RUN`，`committee-audit decision:not-run` 返回 `NOT_RUN/DECISION_NOT_RUN`。没有为了验收制造公司、决定或交易协议。
- 私有 PDF、DOCX、知乎原文、Cookie、浏览器 Profile 和 `runtime/` 继续被 Git 排除；验收报告不包含私有原文。

## 7. 完成边界与下一步

Phase 6 现在提供的是“可靠地拒绝、要求补证、观察或给出受限模拟资格”的本地确定性裁判，不是预测收益的魔法模型。Phase 5 仍因 7 个采集 gap、616 个未完成根评论范围以及人工观点/Skill 审核未完成而保持 `PARTIAL`。下一步按“先计划、后代码”进入 Phase 7：只做冻结权重的影子评测，验证各研究组件是否真的在样本外增加价值；样本不足必须如实报告，不允许自动调权或真实下单。
