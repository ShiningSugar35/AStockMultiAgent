# Phase 2 真实验收报告

日期：2026-07-13  
分支：`feature/phase2-evidence`  
状态：进行中；本报告按子里程碑累积，未完成项不会标记通过。

## M2.1 官方公告和财报检索、下载与快照

### 实现

- `CninfoDisclosureProvider`：低频调用巨潮官方检索入口，搜索响应先进入 SHA-256 ObjectStore，再解析公告元数据。
- PDF 下载器只接受 `https://static.cninfo.com.cn/*.PDF`，拒绝非官方主机、非 HTTPS、路径穿越和非 PDF 后缀。
- 下载响应先形成不可变 SourceSnapshot，再验证 PDF 文件头；100 MiB 大小上限防止异常响应占满本地磁盘。
- `SourceDocument`、`DisclosureAnnouncement`、`DisclosureSearchBatch`、`DownloadedDocument` 和 `DisclosureSyncReport` 已成为公共 Schema。
- SQLite migration `0003_documents.sql` 保存完整快照详情、官方文档元数据和文档—快照关系；重复同步保持一份文档和一条相同快照关系。
- CLI：`astock disclosure-search` 和 `astock disclosure-sync`。

### 录制与错误测试

- 录制夹具验证请求中的证券组织标识、年报分类、公告字段和官方 PDF URL。
- 覆盖原始索引快照、PDF 快照、Document Registry 幂等、Job/Attempt 成功状态、网络断开重试分类，以及非官方 URL 在发出网络请求前被拒绝。
- 全量离线结果：`60 passed, 7 skipped`。
- `ruff check .`：通过。
- `pyright`：0 errors、0 warnings。

### 真实官方样本

请求：巨潮，`000001`，深交所，年报，2025-01-01 至 2026-07-13。

结果：

- 公告：平安银行《2025年年度报告》；
- disclosure/document ID：`1225022887` / `cninfo:1225022887`；
- 官方 PDF：`https://static.cninfo.com.cn/finalpage/2026-03-21/1225022887.PDF`；
- PDF 大小：1,975,076 bytes；
- SHA-256：`2273565ecbe1b32536631fd4a019a4f4a990f4c793cfd5b70eae90d44d3ff16c`；
- live test：`1 passed`；
- 正式 CLI 同步：成功，SQLite 和 ObjectStore 均已落地。

巨潮检索网页和 PDF 均属于官方域名。当前结构化检索端点按“2026-07-13 已验证但未承诺稳定”管理：保留原始响应、录制夹具和失败分类，不把它假设为永久协议。

## M2.2 通用 PDF/OCR、章节和页码级定位

### 实现

- 依赖锁定：PyMuPDF 1.28.0、pdfplumber 0.11.10、RapidOCR ONNXRuntime 1.4.4；均运行在 Python 3.12。
- `PdfParseService` 逐页检查原生文本；可见字符低于版本化阈值时才以 200 DPI 渲染页图并调用 OCR，不整本盲目 OCR。
- 每页形成 `DocumentPage`：1-based 页码、页面尺寸、原生/最终字符数、文本对象哈希、页图哈希、提取方法、OCR 引擎/版本/置信度、解析器版本、章节路径和告警。
- 文本、OCR 页图和 ParseReport 都进入 ObjectStore；SQLite migration `0004_document_pages.sql` 保存版本化 page/run 元数据。
- 同一 snapshot + parser_version + page scope 直接返回缓存；阈值、DPI、OCR 开关或解析规则变化产生新版本，旧页不覆盖。
- 章节恢复先实现确定性标题规则（“第X章”、中文序号、数字多级标题）；后续版面模型可通过 parser_version 并存。
- CLI：`astock pdf-parse <document_id> --page N --ocr/--no-ocr`，只输出元数据和哈希，不把私有正文打印到终端。

### 自动测试

- 原生 PDF：确认不调用 OCR、保留文本对象并恢复标题。
- 扫描 PDF：用真实 RapidOCR 识别本地栅格页中的 `ANNUAL REPORT`；同时用可控 OCR fixture 验证页图落库、置信度和重复运行只调用一次 OCR。
- 解析版本变化：同一页保留两个版本；非法页码明确拒绝。
- 全量离线结果：`65 passed, 7 skipped`。
- `ruff check .`：通过。
- `pyright`：0 errors、0 warnings。

### 真实官方 PDF 样本

对 `cninfo:1225022887`（平安银行 2025 年报）抽测第 1–3 页：

- 源 PDF：288 页；
- 原生文本页：2；OCR 页：1；空页/失败页：0/0；
- 提取字符：3,712；
- parser：`pymupdf-1.28.0+rapidocr-1.4.4+dpi-200+threshold-24+rules-v1`；
- report SHA-256：`794a937795fa7b271ea51e7a604eeae300f4eae99f7b22239562489dd5a03849`；
- 第二次 CLI 运行的 page_id、parse_run_id 和 report hash 全部不变，耗时由约 4 秒降至约 0.9 秒。

## M2.3 Claim—Evidence 关系

### 实现

- SQLite migration `0005_claim_evidence.sql` 建立 `evidence_record`、`claim_record`、`claim_evidence_link` 和 `evidence_conflict`，支持一个 Claim 连接多条 Evidence，也支持一条 Evidence 被多个 Claim 复用。
- `EvidenceLocator` 固定页码、章节路径、字符起止位置和 parser version；证据正文片段单独进入 ObjectStore，SQLite 仅保存哈希与定位元数据，不保存正文副本。
- Evidence ID 由文档/快照/页/定位/片段哈希/证据等级/事实状态/实体/有效期/系统可得时间共同决定；相同输入重复创建返回同一条记录。
- Claim 创建前强制检查所有 Evidence 已存在，且 `available_to_system_at <= as_of`，同时检查可选的 `valid_from/valid_to`，拒绝把未来才拿到的资料用于过去时点的结论。
- Claim 与全部 Evidence link 在同一 SQLite 事务中提交；同时出现 SUPPORT 和 REFUTE 时自动把 Claim 标记为 `CONFLICTED`，并创建状态为 `OPEN` 的 `EvidenceConflict`。
- Evidence 和 Claim—Evidence Bundle 均形成内容寻址工件，可从 Claim 回到 Evidence、页码、解析版本、原文片段对象和原始 PDF 快照。

### 自动测试

- 原生 PDF 页的精确字符区间可生成 Evidence；重复调用 ID、记录和工件保持一致。
- 明确检查证据片段原文不出现在 SQLite `evidence_json`，但可按哈希从 ObjectStore 原样取回。
- 越界和空白片段被拒绝；未来证据被拒绝；一对多、多对多关系均可持久化。
- SUPPORT/REFUTE 并存时自动生成开放冲突记录。
- 全量离线结果：`69 passed, 7 skipped`。
- `ruff check .`：通过。
- `pyright`：0 errors、0 warnings。

## M2.4 PIT 元数据与修订链

### 实现

- 公共 `PointInTimeMetadata` 明确分开保存：报告期末、发布时间、生效时间、摄入时间、系统可得时间、修订时间、被替代来源、PIT 等级和可得性依据。
- PIT 等级固定为 `CERTIFIED`、`DOCUMENT_RECONSTRUCTED`、`APPROXIMATED`、`NOT_PIT_SAFE`；可得性依据固定为官方发布时间、实际抓取观察、用户声明或 Provider 当前值。
- SQLite migration `0006_point_in_time.sql` 追加保存每个来源版本；更正版本以新的 `source_id` 和 `supersedes_source_id` 指向旧版本，不覆盖旧记录，并能恢复从原版到最新版的完整修订链。
- 时间线校验拒绝“系统可得时间晚于摄入却声称已摄入”等不可能状态；官方文档重建必须同时具备文档、快照和发布时间。
- 正式历史使用门只接受 `CERTIFIED` 和 `DOCUMENT_RECONSTRUCTED`；`APPROXIMATED` 必须显式放行并单独报告，`NOT_PIT_SAFE` 无法进入正式历史评测。
- 使用时同时检查发布、生效和系统可得时间，未来发布、未来生效、未来才抓到的资料都不能进入更早时点。
- `DisclosureSyncService` 已自动为官方 CNINFO 文档登记 `DOCUMENT_RECONSTRUCTED + FETCH_OBSERVED` PIT 元数据；重复同步复用第一次实际拿到该快照的时间。

### 自动测试与真实官方样本

- 覆盖不可能时间线、追加式修订链、未知前序版本、未来可得、未来生效、近似值显式放行、当前 Provider 值禁止正式回测，以及官方同步幂等。
- 全量离线结果：`74 passed, 7 skipped`。
- `ruff check .`：通过。
- `pyright`：0 errors、0 warnings。
- CNINFO live：`1 passed`；再次同步 `cninfo:1225022887` 后生成 PIT ID `pit:b807eb9e8b9077118d855b9a96ba08272d504f13f7ac4613e90778f6e0d7b1c7`，状态 `DOCUMENT_RECONSTRUCTED`，系统可得时间保持第一次成功摄入时点 `2026-07-13T12:02:50.572758Z`。

## M2.5 私有书籍/PDF 摄入接口

### 实现与安全边界

- `PrivatePdfIngestService` 和 `astock private-pdf-ingest` 提供通用本地 PDF 入口；整文件先验证大小、PDF 文件头和可打开性，再进入同一 SHA-256 ObjectStore。
- 私有源与官方文档复用 SourceSnapshot、SourceDocument、PIT、PdfParseService、DocumentPage、Evidence 和解析版本；私有源固定为 `LOCAL_PRIVATE_RESEARCH + NOT_PIT_SAFE`，不能进入正式历史评测。
- 本地路径、原始文件名和正文不会写入 SQLite、工件清单或 CLI 输出；只保留文件名哈希。CLI 只输出对象哈希、页码、提取方式、字符数和版本元数据。
- 默认只登记原文件而不解析；只有显式传入页码才解析，单次样本上限 12 页，防止 Phase 2 误做整书粗切。原始对象永久保留，任何后续清洗必须可从原始对象和版本化派生工件重建。
- SQLite migration `0007_private_books.sql` 保存 `BookSourceManifest` 和 `BookParseReport`；相同源、文件版本、文件字节和解析设置重复执行返回同一 manifest/report。
- 已实现并导出 `BookSourceManifest`、`BookParseReport`、`BookCleaningReport`、`BookMethodCoverageReport`、`BookViewpointCard`、`BookSkillCandidate`、`HumanReviewDecision`。
- `BookCleaningReport` 强制保留全部规定的降权类、保留类、九项统计指标字段、原始内容永久保留和可重建标记；未实际运行时指标为未测量，不能伪装为完成。
- `BookMethodCoverageReport` 分别保存选股、建仓、持仓、加仓、减仓、退出、风险和复盘计数；样本不足只能标记 `INSUFFICIENT_SOURCE`。只有完整覆盖并经人工批准后才允许 `AUTHOR_SILENT`。
- Viewpoint 和 Skill 候选必须同时引用 Evidence、1-based 页码和原文片段 SHA-256；人工批准也必须带证据引用。

### 自动测试

- 私有两页 fixture 验证整文件字节可按哈希原样恢复、只解析显式样本页、重复摄入幂等、PIT 正式使用被拒绝，以及样本页可直接进入统一 Evidence 服务。
- 明确检查路径、文件名和正文不在 manifest/report/page/evidence 的 SQLite JSON 中；CLI 输出同样不含路径、文件名或正文。
- 覆盖非 PDF 在登记前拒绝、默认零页解析、样本页上限、清洗未运行状态、虚假完成状态、无依据 `AUTHOR_SILENT`、无页码/片段引用的观点和 Skill 候选、无证据的人工批准。
- 全量离线结果：`82 passed, 7 skipped`。
- `ruff check .`：通过。
- `pyright`：0 errors、0 warnings。

### 《价值投资功法》真实本地样本

- 原始文件：9,270,970 bytes，249 页；原文件继续由 `.gitignore` 排除，不进入 Git。
- 文件与原始对象 SHA-256：`fd50555650b197d352d123d629697bcd4fa2428a6a7490b1dc00b56efbb623e0`。
- manifest：`book-manifest:52dc08cfd59bb9893a280283343965cec8e26261fe152bd016c2ff0f28edc0d8`，版本 `v1-local-2026-07-13`，策略为 Git 排除、禁止外部再发布、永久保留、清洗可重建。
- 只抽测物理页 1、125、249：3/3 成功，原生文本 2 页、OCR 1 页、失败 0 页，共提取 1,099 字符；未运行整书清洗、分类或 Skill 蒸馏。
- parser：`pymupdf-1.28.0+rapidocr-1.4.4+dpi-200+threshold-24+rules-v1`。
- parse report：`book-parse:700169f8735fc33124fef0c2bea40e0144aec6ad12c31392e90f1c245b544fd1`；对象 SHA-256 `27bd72624455a5917a69a21a7d8fd67721b2a35a7b4c8876ea6a4d1514d39e9a`。
- 重复运行后 manifest ID、文件哈希、parse report ID、report hash、页 ID 和计数均保持不变。

## M2.6 代表性综合验收

### 30 份受控标注文档基准

为避免用“看起来成功”代替可量化结果，`scripts/phase2_acceptance.py` 会生成 15 份原生文本 PDF 和 15 份扫描 PDF。它们是明确标记为 `CONTROLLED_LABELED_NON_PRODUCTION` 的测试文档，不冒充真实上市公司公告；每份有固定标题、证券代码和关键字段，扫描组使用真实 RapidOCR，不使用 mock。

- 文档数：30（原生 15、扫描 15）。
- 原生文本字符召回：780/780，100%，门槛 98%。
- 扫描页标题/关键字段召回：45/45，100%，门槛 95%。
- 引用可追溯：30/30，100%；每条都验证原始 PDF 对象、SourceSnapshot、页文本对象、Evidence 片段对象、1-based 页码和 parser version。
- 幂等：30/30，100%；相同 snapshot、解析版本和页范围的第二次结果完全一致。
- 昂贵基准测试：`ASTOCK_RUN_ACCEPTANCE=1 uv run pytest tests/acceptance/test_phase2_document_benchmark.py -q`，结果 `1 passed`。

受控集便于精确计算召回率，但不代表覆盖所有真实中文财报版式。因此最终报告同时要求真实 CNINFO 年报和真实私有书样本通过，不能只凭受控集宣布完成。

### 冻结的真实验收工件

- 官方样本 `cninfo:1225022887`：原始快照存在且哈希验证通过，3 条真实页解析记录全部可回溯，PIT 为 `DOCUMENT_RECONSTRUCTED`。
- 《价值投资功法》：原始对象存在且哈希验证通过，249 页源文件只抽测 1、125、249 页，3/3 成功，状态保持 `SAMPLE_ONLY`。
- Phase 2 AcceptanceReport：`phase2-acceptance:29e5a08de70afa7fec47301057494c4570f50a42b47bc2e2020b94283ea58f43`。
- AcceptanceReport 对象 SHA-256：`5a49b3232873c74a17897f0ef8cfad43bc25ce0a6ee414b985337464668d81ad`。

### 最终质量与泄漏门禁

- 默认离线套件：`82 passed, 8 skipped`；跳过的是需显式开启的昂贵 acceptance 和 live Provider 测试。
- 30 文档真实 OCR acceptance：`1 passed`。
- CNINFO live：`1 passed`。
- `ruff check .`：通过；`pyright`：0 errors、0 warnings。
- Git 跟踪 PDF：0；全分支历史 PDF 路径：0；私有原文件名进入 tracked 文件：0；高风险凭据模式命中：0。
- 私有 PDF 由 `.gitignore` 命中；其 SHA-256 对象在本地 ObjectStore 存在；`git diff --check` 通过。

## Phase 2 结论

M2.1–M2.6 全部完成。Phase 5 的知乎全集采集、整书清洗、人工审核、观点卡片和 Skill 蒸馏仍明确未运行、未标记完成；后三位知乎作者身份确认任务继续保持 OPEN。
