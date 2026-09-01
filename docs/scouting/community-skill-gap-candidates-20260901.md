# 已批准社区能力与 Repo Skills 实施清单（2026-09-01）

## 1. 决策状态

本清单冻结首批已获实施授权的外部能力和项目自有 Repo Skills。授权表示可以进入适配、资格验证和试点，不等同于已经获得生产资格；每一项仍须由统一资格框架形成可审计裁决。

所有能力继续复用现有 Provider Registry、SourcePolicyGate、SourceSnapshot、ObjectStore、Evidence、PIT、Research Team、Committee、报告链、Skill observability 和模拟账本。外部项目不得建立第二套事实源、路由、证据、用户状态、Agent 调度或交易执行体系。

候选的具体运行范围由版本化 capability metadata、资格报告和任务上下文共同确定。资格结果可为 `DISCOVERY_ONLY`、`SHADOW`、`PRODUCTION_BACKUP` 或 `REJECT`，并带有效期、撤销记录和退出合同。

## 2. 已批准的外部能力

| 候选 | 预期用途 | 接入位置 | 主要验收重点 | 目标状态 |
|---|---|---|---|---|
| Arelle | XBRL taxonomy、context、unit、dimension 与计算一致性验证 | Financial Integrity / Evidence | 固定材料对拍、异常定位、版本与许可证、性能、卸载回归 | `SHADOW`，通过后可作专项验证备用 |
| Docling | 复杂 PDF、Office、HTML、版面与表格结构解析 | documents / ObjectStore / Evidence | 页码与表格定位、解析器分歧、原始材料绑定、资源成本、故障恢复 | `SHADOW`，通过后可作解析备用 |
| Microsoft Playwright MCP | 动态网页与交互页面的浏览器 transport | ExternalCapabilityRegistry / Provider Registry | capability 描述、版本与供应链、任务范围、快照 lineage、资源预算、失败与退出 | 按 capability 资格决定 |
| AKShare | A 股、基金、ETF、指数等明确端点的结构化 Provider 备用 | Provider Registry / provider dialect / SourcePolicyGate | 逐 endpoint 上游、字段、时间语义、权利、限流、漂移、recorded/live 对拍 | 至少一个 endpoint 争取 `PRODUCTION_BACKUP` |
| Crawl4AI | 网页抓取、正文清洗和结构化提取 | ExternalCapabilityRegistry / documents / Evidence | 许可证与安全记录、依赖锁定、输出稳定性、资源成本、故障与退出 | `SHADOW`，通过后按 capability 升级 |
| changedetection.io | 网站变化检测与事件线索生成 | monitoring / durable tasks / evidence investigation | 变化快照、去重、恢复、误报漏报、维护成本、事件 lineage | `SHADOW`，通过后可作监测备用 |

外部能力的 stars、下载量和社区热度只作为采用度与维护信号。正式裁决必须重新获取上游版本、发布、License、ToS、数据权利、安全记录和依赖信息，并冻结审计快照。

## 3. 已批准的新 Repo Skills

### `source-qualification-auditor`

负责统一执行外部能力资格取证和裁决：固定版本与哈希、License/ToS、数据权利、真实上游、PIT/provenance、凭证处理、SBOM、安全记录、性能、成本、故障、有效期、撤销和退出验证。该 Skill 只组织现有确定性资格命令和工件，不自行授予来源权威性。

### `report-visual-qa`

负责正式报告的视觉与交付验收：DOCX/PDF 渲染、分页、标题孤行、表格截断、字体回退、中文显示、引用、图片权利、隐私扫描、输出哈希和 Manifest 对账。它消费报告服务产物，不建立第二套报告事实源。

### `schema-drift-recorder`

负责数据或页面合同漂移的 raw-first 记录：冻结原始响应、生成结构差异和最小回归 fixture、形成 Provider dialect 或 Schema Repair 提案、记录验证与回滚。它不得绕过现有准入流程直接修改活动生产解析器。

## 4. 分项接入策略

1. **先完成 M-06 基础合同**：ExternalCapabilityRegistry、资格请求/报告、有效期、撤销、退出、适配器发现和路由属性。
2. **再建立候选 dossier**：每个外部项目独立冻结版本、仓库与包哈希、License/ToS、SBOM、维护和安全材料。
3. **实现最小适配器或 Skill**：只覆盖一个可验收 capability，不让单一项目同时接管采集、研究、报告和执行。
4. **运行 recorded 验证**：固定输入、固定原始响应、失败注入、Schema 漂移、缓存、超时、卸载和回滚。
5. **运行受控 live 验证**：验证真实上游、数据时间、provenance、延迟、成本、限流和故障表现。
6. **独立资格裁决**：通过项按 capability 进入 `PRODUCTION_BACKUP`；证据不足项继续留在 `SHADOW` 或 `DISCOVERY_ONLY`。
7. **接入运行观测**：记录选择原因、命中、失败、回退、延迟、成本、资格有效期和撤销状态。

## 5. 候选专属验证矩阵

| 候选 | Recorded fixture | 受控 live | 负向与恢复 | 退出验证 |
|---|---|---|---|---|
| Arelle | 代表性 XBRL、无效 context/unit/dimension、计算不一致 | 可公开取得的代表性 XBRL 文档 | taxonomy 缺失、版本不兼容、损坏文件 | 删除可选依赖后现有财务审计正常 |
| Docling | 年报表格、跨页表格、复杂版面、Office/HTML | 代表性正式公开文档 | 解析失败、表格冲突、资源超限 | 关闭解析器后现有文档链正常 |
| Playwright MCP | 固定网页交互录制、DOM/截图/下载工件合同 | 代表性动态公开页面 | 页面漂移、超时、进程异常、输出损坏 | 撤销 capability 后其他 transport 正常 |
| AKShare | 每个候选 endpoint 的冻结响应与字段合同 | 代表性证券、基金或指数端点 | 上游改名、空数据、限流、时间语义缺失 | 卸载包后 Provider 路由正常 |
| Crawl4AI | 静态/动态页面、表格、长正文和编码 fixture | 代表性公开页面 | 提取漂移、依赖异常、资源超限 | 关闭适配器后文档链正常 |
| changedetection.io | 初始快照、内容变化、重复变化和无变化 | 代表性监测目标 | 误报、漏报、断点恢复、事件重复 | 删除集成后监控主链正常 |
| 三个 Repo Skills | 触发、否定、冲突路由和固定工件 | 仅在其底层 capability 需要时执行 | 事实保护、权限冲突、任务中断与恢复 | 删除 Skill 后现有代码和命令正常 |

## 6. 验收与状态迁移

- 每项候选独立形成资格报告，不以“同类工具已通过”替代自身验证。
- 通过的外部能力进入 `configs/external_capabilities.yaml` 的冻结 release；到期、上游重大变更或安全事件触发重新资格验证。
- 新 Repo Skills 位于 `.agents/skills/*/SKILL.md`，并补齐 Workflow、触发测试、Skill observability 和卸载回归。
- 完成项从《开发计划》迁移到《验收报告》；未通过项保留具体原因、可恢复条件和回滚结论。
- 真实券商执行能力仍不在准入范围，`broker_execution_allowed=false` 保持不变。
