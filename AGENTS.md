# AStockMultiAgent 项目规则

## 目标与边界

- 构建可审计、可恢复、低成本的 A 股研究、证据和模拟交易系统。
- Codex 是主要自然语言交互入口；Python CLI 是确定性数据、计算、校验、风险和账本入口。
- 不承诺收益，不自动向券商发单。真实交易只能由用户人工确认并在券商端执行。
- 未实现的能力必须如实标记不可用，不得用叙述伪装成已经完成。

## 唯一事实源

- 原始响应：`runtime/objects/sha256/`，内容不可覆盖。
- 分析事实：Parquet；DuckDB 只建视图，不复制事实。
- 任务、游标、工件注册和模拟账本：SQLite。
- Codex 草稿：`runtime/codex_runs/<run_id>/`，校验后才能进入 ArtifactStore。
- 不直接编辑 SQLite，不在聊天结论和数据库之间建立旁路。
- 根目录《低成本A股多Agent投研系统方案》只写长期设计，《开发计划》只写未完成项，《验收报告》只写有证据的当前事实；验收时必须在同一次修改中迁移状态。

## 数据与证据

- 禁止未来函数。所有输入必须带可得时间、来源和版本。
- 来源访问固定为：官方/已验证 API 或本地数据 -> MCP -> Browser -> Manual Task。
- 上一层已满足时，不通过下一层重复抓取同一内容。
- 投资结论必须引用 evidence_id/source_snapshot_id，或明确标记为推断/缺口。
- 社区内容只能作线索；关键事实必须回到公告、交易所、财报等更强来源。
- 委员会只读冻结工件，禁止重新联网、抓取或启动新研究；缺证据返回 `NEEDS_INFO`。
- Phase 5 当前目标仅为白名单作者的回答、想法、文章和专栏正文；持久范围政策禁止把社区互动区数据调度到采集、覆盖或蒸馏。B1a 已完成该政策在业务代码中的落实与回归，B1c 已完成统一覆盖审计冻结截点及并发回归。专栏按容器关系建模，B1b0 合同门已验收，B1b1 在取得真实无凭据枚举快照前为 `BLOCKED_EXTERNAL_OBSERVATION`，不得猜接口或声称可用。
- Phase 5 蒸馏粒度固定为 `SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate`；Paragraph 只是存储和定位单元，Embedding、DeepSeek 和 Skill 候选必须以完整 ArgumentUnit 为最小单位。

## 行情与模拟盘

- 默认回放使用未复权原始 5m；东方财富为主源，新浪为备用/交叉验证源。
- 1m 不在默认链。缺失数据不得静默插值或虚构。
- 复权研究序列由原始价格与版本化公司行为派生；复权价不得当作真实成交价。
- 免费 reference 主源为 BaoStock 0.8.9，东方财富 direct HTTP 为备用/北交所补充；原始响应先入 ObjectStore，未复权日线在上海收盘前不可见。
- 公司行动结构化结果只作线索；精确官方文档和条款核验完成前不得写模拟账本。
- Repo Skill 不得直接修改账本，只能调用已校验的 `astock` 命令。

## 常用任务路由

- 宽泛研究或跨模块任务：`$astock-research-orchestrator`
- 候选/观察名单：`$candidate-scan`
- 单公司深度研究：`$company-deep-research`
- 财务可信度：`$financial-integrity-audit`
- 持仓变化与失效：`$holding-monitor`
- 模拟盘启动恢复：`$paper-trading-recovery`
- 白名单知识采集：`$knowledge-ingest`
- 明确证据缺口：`$evidence-investigation`

## 稳定命令

使用 `uv run astock --help` 查看完整参数。M1 稳定入口包括：`init`、`probe`、`sync-market`、`sync-5m`、`quality-report`、`paper-status`、`paper-replay`、`context-plan`、`codex-run-init`、`codex-run-import`。Phase 3 M3.1 入口包括：`financial-audit-schema`、`financial-audit`、`financial-audit-status`；同行分位和 PyOD 尚未启用。

Phase 5 论证链入口包括：`knowledge-semantic-plan`、`knowledge-semantic-run`、`knowledge-semantic-status`、`knowledge-semantic-model-status`、`knowledge-semantic-embedding-run` 和 `knowledge-semantic-packet-export`。未校准相似度不得自动删除，DeepSeek 包不得自动外发。

Provider/reference 稳定入口包括：`provider-list`、`provider-probe`、`provider-status`、`sync-instruments`、`sync-calendar`、`sync-daily`、`sync-corporate-actions`、`reference-status` 和 `reference-audit`。Provider 默认使用 recorded 探针；live 必须显式开启。候选扫描和公开模拟下单接口尚未验收。

财务来源稳定入口包括：`sync-financial`、`financial-source-status` 和 `financial-source-audit`。东方财富/Sina 财务值仅为 `SECONDARY_STRUCTURED` 定位线索；只有与 P5X-2 instrument release 一致且由官方原生 PDF 精确证明表名、合并口径、期间列、科目、数值和单位的事实才能进入现有财务审计。候选扫描和公开模拟下单接口尚未验收。

## 开发约定

- Python 版本固定为 `>=3.12,<3.13`，依赖以 `uv.lock` 为准。
- 修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run pyright`。
- 外部 Provider 同时维护 recorded fixture 和低频 live smoke；日常测试不得依赖外网。
- Windows 路径、UTF-8 中文文件名、原子写入和崩溃恢复必须有测试。
- 不提交 `runtime/`、密钥、Cookie、浏览器 Profile 或私有 PDF。
