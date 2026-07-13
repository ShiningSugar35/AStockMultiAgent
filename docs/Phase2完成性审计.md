# Phase 2 完成性审计

日期：2026-07-13  
范围：只审计本轮明确要求的计划补齐与 Phase 2；M1 已由用户确认完成，不重复开发或重复审计。

## 结论

Phase 2 六个子里程碑均已落地并通过自动、live 和本地私有样本验收。没有提前批量采集知乎，没有整书粗切、清洗或蒸馏，也没有把任何 PDF、Cookie 或凭据提交 Git。

## 要求—证据映射

| 明确要求 | 落地位置 | 验收证据 | 结论 |
|---|---|---|---|
| 四位知乎作者进入正式范围 | `开发计划.md`、`configs/knowledge_sources.yaml` | `tests/unit/test_phase2_scope_configs.py` | 通过 |
| 后三位不得猜身份并形成 OPEN 人工任务 | `configs/manual_investigation_tasks.yaml` | profile/user/token 为空、`PENDING_IDENTITY_CONFIRMATION`、enabled=false | 通过 |
| Phase 5 全历史采集边界及未跑不得完成 | `开发计划.md`、作者配置 collection_scope | 计划明确 answers/questions/thoughts/articles/comments/replies/checkpoints/incremental/coverage/gaps；状态未完成 | 通过 |
| 《价值投资功法》作为正式私有源 | `configs/book_sources.yaml`、BookSourceManifest | 真实文件 manifest、哈希、249 页、永久保留和禁止再发布策略 | 通过 |
| 官方公告/财报检索、下载、快照 | migration 0003、`astock.documents`、CLI | recorded、异常、幂等、CNINFO live 和真实平安银行年报 | 通过 |
| 通用 PDF/OCR、章节、页码、版本 | migration 0004、`PdfParseService` | 原生、真实 RapidOCR、缓存、版本并存、官方 3 页与私有书 3 页 | 通过 |
| Claim—Evidence 多对多与冲突 | migration 0005、`astock.evidence` | 原文不进 SQLite、未来证据拒绝、SUPPORT/REFUTE 自动冲突 | 通过 |
| PIT 时间与修订链 | migration 0006、`astock.pit` | 发布/生效/摄入/可得分离；修订追加；NOT_PIT_SAFE 禁止正式历史使用 | 通过 |
| 私有 PDF 通用入口 | migration 0007、`astock.books`、`private-pdf-ingest` | 默认不解析、显式样本上限、路径/文件名/正文不输出、统一 Evidence 接口 | 通过 |
| 七类书籍工件 Schema | `schemas/books.py` | 必要指标、八类方法覆盖、AUTHOR_SILENT 防伪、页码/片段引用、人工批准证据门禁 | 通过 |
| 30 文档与召回门槛 | `astock.acceptance.phase2`、acceptance test | 原生字符 100%；扫描关键字段 100%；引用和幂等均 100% | 通过 |
| 原始私有资料零 Git 泄漏 | `.gitignore` 与最终安全审计 | tracked PDF 0、历史 PDF 路径 0、原文件名 tracked 命中 0 | 通过 |

## 真实样本冻结值

- CNINFO 文档：`cninfo:1225022887`；PDF SHA-256 `2273565ecbe1b32536631fd4a019a4f4a990f4c793cfd5b70eae90d44d3ff16c`；PIT `DOCUMENT_RECONSTRUCTED`。
- 私有书文件 SHA-256：`fd50555650b197d352d123d629697bcd4fa2428a6a7490b1dc00b56efbb623e0`；249 页；样本页 1/125/249；状态 `SAMPLE_ONLY`。
- 最终 AcceptanceReport 对象：`5a49b3232873c74a17897f0ef8cfad43bc25ce0a6ee414b985337464668d81ad`。

## 明确未做且不得误报完成

- 未批量访问或采集知乎；Chrome 登录态未被本阶段使用。
- 未确认黄彦臻、派大星皮皮、寒武纪的鳄鱼的唯一主页或用户标识。
- 未对《价值投资功法》执行全书解析、重复/噪声分类、自动清洗、人工审核、观点卡片、规则蒸馏或评测。
- 未把样本不足解释为作者没有某类方法；当前只能使用 `INSUFFICIENT_SOURCE`。

## 唯一待用户手工补充的信息

Phase 5 开始前三位待确认作者各需一个唯一知乎主页 URL 或 url_token。它们已作为 OPEN 人工任务保留，不阻塞已经完成的 Phase 2。
