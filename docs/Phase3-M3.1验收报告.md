# Phase 3 M3.1 确定性财务可信度核心验收报告

日期：2026-07-16

分支：`feature/phase3-financial-integrity`

结论：M3.1 已完成并通过离线验收；Phase 3 整体尚未完成，M3.2 和 M3.3 保持禁用。

## 已交付能力

1. 新增严格 `FinancialAuditRequest`、`FinancialFact`、规则/行业配置和 `FinancialIntegrityEvidencePack` Schema。所有金额使用 Decimal，保留原披露单位精度后统一换算为元。
2. 每个数字依次经过原始对象存在性、快照状态与可得时间、PIT 报告期与发布时间、Evidence 等级/公司/文档/快照一致性门禁。不合格数字不会进入公式。
3. M3.1 默认执行资产负债表恒等式、现金变动恒等式、利润表与现金流补充资料的净利润勾稽，以及 CFO/净利润、应收/收入、存货/成本、预付款/资产、其他应收/资产五项描述性复算。
4. 规则注册表共 13 条：8 条 M3.1 默认规则可执行；Beneish、Altman、Piotroski、Sloan、DuPont 只登记版本、字段和适用性，明确延期至 M3.2，不生成假分数。
5. 行业配置覆盖一般工业、银行、保险、证券、房地产、早期生物科技和其他。银行、保险对 Beneish/Altman 返回 `NOT_APPLICABLE`，不会机械套用工业企业模型。
6. 同期同科目多个等价单位会合并；多个不同官方值会形成开放文档冲突、停止选值并生成补证任务。缺字段、未来信息、社区弱证据和断裂来源链均返回 `NEEDS_INFO`。
7. migration `0010` 只在 SQLite 保存运行、尝试、检查点和人工任务元数据；输入和报告进入不可变 ObjectStore，完整报告注册到 ArtifactStore。相同请求和规则版本返回相同 run id、工件哈希，崩溃后可重跑。
8. 新增 `financial-audit-schema`、`financial-audit`、`financial-audit-status` 三个稳定 CLI；`FinancialIntegrityEvidencePack` 也能走 Codex 草稿的 Schema、引用和 Policy 校验后受控导入。

## 关键验收场景

| 场景 | 结果 |
|---|---|
| 工业企业 golden case | 三项勾稽差额为 0，五项比率与录制期望完全一致 |
| 元与万元表达同一数字 | 统一为相同 CNY 值，不产生假冲突 |
| 资产恒等式真实不平 | 产生证据充分的高严重度 `FLAG`，不输出“造假”结论 |
| 缺少存货 | 不补造数字，相关比率不计算，返回缺口和人工任务 |
| 净利润为 0 | 比率明确不可计算，但不误报为缺证据或要求人工补资料 |
| 银行 Profile | Beneish/Altman 等工业模型明确 `NOT_APPLICABLE` |
| 未来快照/PIT/Evidence | 全部排除，不进入 verified numbers、公式或模型 |
| 社区内容作为财务主证据 | 被证据等级门禁拒绝 |
| 两份官方值冲突 | 不擅自选值，形成 document conflict 和补证任务 |
| 请求延期能力 | 返回 capability-disabled 缺口，不生成虚构分数 |
| 同一请求重复运行 | run id、工件哈希不变，不增加完成尝试 |
| 遗留 RUNNING 尝试 | 标记 `INTERRUPTED_RECOVERED` 后用同一身份恢复 |
| Codex 导入 | 要求每个 evidence_id 有非空引用，校验后才注册工件 |

## 自动化与实测结果

- Windows 10 / Python 3.12.10；`astock probe` 返回 `python_supported=true`、SQLite `state_integrity=ok`。
- `astock --help` 已显示三个 M3.1 命令。
- 全仓离线回归：109 passed / 8 skipped；跳过项仍是既有显式 acceptance/live 开关，不是失败。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 本阶段没有新增外部 Provider，因此日常财务公式测试不依赖网络；官方文件下载与 Evidence/PIT 沿用 Phase 2 已验收入口。

## 仍未实现、不得宣称可用

- 自动从任意财报表格抽取并映射所有财务科目；M3.1 输入必须是已经绑定 Phase 2 证据/PIT 的规范事实。
- TTM、同比、环比、每股值和完整跨年度评分；属于 M3.2。
- 同行样本口径、同行分位、稳健 Z-score 和 Isolation Forest；属于 M3.2/M3.3。
- PyOD；属于 M3.3，当前未加入依赖。
- “是否造假”结论、处罚结果回灌历史输入、账本修改、风险硬阻断和任何真实交易动作；这些均被明确禁止。

## 回滚与人工事项

规则和行业配置均有独立版本；旧请求、原始对象和报告不覆盖。撤回 M3.1 代码不会删除已有 ObjectStore 工件。

本里程碑没有新增注册、登录、验证码或购买数据事项。真正审计某家公司前需要先把目标官方财报数字做成带 Evidence/PIT 的规范输入；这是研究任务，不需要用户现在手工操作。知乎 Chrome 登录状态和两份私有蒸馏原料均未被本阶段改动。
