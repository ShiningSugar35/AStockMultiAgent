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

## 后续子里程碑

- M2.2 通用 PDF/OCR、章节和页码级证据定位：待完成。
- M2.3 Claim—Evidence：待完成。
- M2.4 PIT：待完成。
- M2.5 私有书籍/PDF 摄入：待完成。
- M2.6 代表性综合验收：待完成。
