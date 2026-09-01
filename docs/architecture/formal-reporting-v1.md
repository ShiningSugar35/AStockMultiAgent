# 正式投研报告与展示偏好架构 v1

## 1. 定位

正式报告是既有研究事实的衍生发布层，不是新的研究事实源。报告服务只消费已经冻结的 `ResearchNarrativeBundle`、`InvestorPresentationModel`、Artifact Registry、ObjectStore 与引用/资产清单；它不重新抓取数据、不改变 PIT、Evidence、Research Team、Committee、推荐门、组合或模拟账本。

## 2. 核心合同

- `ReportRequest`：一次报告请求、冻结研究叙事、输入工件、引用、资产、格式与隐私要求。
- `CitationManifest`：引用标签、URL、Snapshot/Object 哈希等审计信息。
- `AssetManifest`：图片/资产位置、哈希、权利状态、说明与替代文本。
- `ReportManifest`：幂等键、输入哈希、模板/renderer/converter 版本、实际格式、降级原因、输出哈希与公开安全引用。
- `ReportPublishResult`：只暴露报告 ID、状态、格式、哈希、安全文件名/引用，不暴露私人绝对路径。
- `PresentationPreferences`：独立跨会话展示偏好，不写入模拟账本或持仓事实。

## 3. 发布链

`ReportRequest → 输入工件/ObjectStore 验证 → 目标目录解析 → staging → renderer → 完整性检查 → SHA-256 → 原子 replace → manifest/checkpoint → 安全公共引用`

默认 renderer 为 `python-docx`，不依赖 Microsoft Office。DOCX 生成后执行 ZIP/OpenXML 完整性检查。DOCX renderer 失败时稳定回退 Markdown；PDF 仅在存在可用转换器且转换结果通过 PDF 签名检查时声明成功，缺失或失败时准确降级而不伪造 PDF。

同一请求内容、模板、隐私和目标策略生成稳定幂等键；已发布文件与 checkpoint 哈希一致时直接恢复，不重复发布。中断阶段只存在 staging/checkpoint，不留下伪成功目标。

## 4. 保存位置与隐私

Windows 使用 Known Folder API 解析桌面，不硬编码用户名；服务器/Linux 可由 `ASTOCK_REPORT_ROOT` 提供报告根；目标不可写时回退 `runtime/reports/output`。文件名经过确定性清洗，禁止路径穿越与路径分隔符注入。

用户报告位于 `.gitignore` 覆盖的 runtime 或外部报告目录；代码、schema、配置、迁移与测试 fixture 才进入 Git。公共返回只使用安全文件名/相对引用。

资产只有在声明为 `OWNED / PUBLIC_DOMAIN / LICENSED / PUBLIC_DISCLOSURE` 且必要哈希检查通过时才允许嵌入；其它权利状态记录在 manifest 并排除嵌入。资产数量、字节数和引用数量受版本化 `report_policy.yaml` 限制。

## 5. 展示偏好

`presentation_preference` 独立持久化：默认长度、报告格式、目录策略、引用级别、隐私默认值和 PDF 偏好可 get/set/update/delete，并跨 repository/进程恢复。公开偏好投影不会回显自定义目录绝对路径。

## 6. CLI

稳定机器 JSON 入口：

- `report-schema`
- `preference-get`
- `preference-set`
- `preference-delete`（删除临时 override，恢复 BASE）
- `preference-reset`（删除 BASE/override，恢复策略默认值）
- `report-policy-status`
- `report-publish`
- `report-status`
- `report-recover`

早期 `presentation-preferences-get/set/delete` 名称仅作为隐藏兼容别名保留，不再作为新调用方合同。

普通投资者网页答复仍由 Response Gateway 控制；深入研究完成后只在聊天中给核心结论、必要风险、数据时间和安全报告引用，不粘贴整份报告。

## 7. 回滚

报告服务可退化为 MD-only，不影响研究工件、Evidence、推荐门和账本。删除/禁用 PDF 转换器不会影响 DOCX/MD；展示偏好可删除恢复默认值。`broker_execution_allowed=false` 不受报告层影响。
