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

## 后续子里程碑

- M2.3 Claim—Evidence：待完成。
- M2.4 PIT：待完成。
- M2.5 私有书籍/PDF 摄入：待完成。
- M2.6 代表性综合验收：待完成。
