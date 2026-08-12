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
- Phase 5 已完成并冻结：白名单作者正文、三位知乎视觉证据、direct-source 243 Skill、baseline 231 admitted 与 visual overlay 422 admitted 均保留不可变历史；当前 Research Runtime 只通过 composite Knowledge registry 653 使用知识资产。专栏真实无凭据枚举仍是独立外部观察边界，不得猜接口或把未取得的覆盖冒充完成。
- Phase 5 蒸馏粒度固定为 `SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate`；Paragraph 是存储、定位及本地语义辅助视图单位，只有完整 ArgumentUnit 可产生最终语义分数、DeepSeek 输入和 Skill 候选。
- 图片证据必须经过不可变图片快照、PDF bbox 或 DOM 定位、逐图 OCR、类型和前后 Paragraph 回填；图片 Paragraph 永远不能独立蒸馏，夹在论点与结论之间时必须 `MERGE_WITH_BOTH`。OCR 或上下文不完整时 AU 保持 `NEEDS_REVIEW`。
- 《价值投资功法》历史视觉覆盖证据仍为 249 页、57 个含图页、74/74 placements；71 个非装饰图映射到 55 个 AU，11 个 READY、44 个 REVIEW。三位知乎作者的真实视觉支线已于 2026-08-10 完成：2,503/2,503 placements、2,306 unique assets、2,503 READY、0 REVIEW/BLOCKED；三份 `VisualEvidencePack` 均 READY。visual Skill generation 评估 951 个真实视觉关联 AU，生成 422 个 admitted overlay Skill、529 个 NO_SKILL；baseline 231 + overlay 422 形成 composite registry 653，`KnowledgeSkillProvider` 状态为 `COMPOSITE_REGISTRY_READY`。后续不得回退为 `PROVISIONAL_TEXT_VIEW`，也不得绕过 composite registry 直接读取 knowledge 表。
- Serenity 当前活动方法注册表为 `research-skills-v3`。muxuuu/haskaomni 上游代码只经版本化 v4 audit 编译成 typed Specialist contracts；`JuglarCycleStageSkill` 是当前新增的固定资产周期方法。任何 Serenity/scorecard/Juglar 输出都只是 evidence-bound Delta 或 report-only metric，不得直接产生交易权重、目标价、仓位或订单。
- 新当前公司研究默认要求 Phase 9 机构级基本面层：`EvidenceSufficiencyReport → IndustryProfile / CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`。Forecast/Valuation 数值由 Python 确定性复算；Serenity Growth/Valuation 不得维护第二套平行数值账本。expected return / market-implied expectations 只能使用绑定注册 artifact/hash 且满足 PIT 的 `MarketPriceAnchor`。Bundle/DecisionContext 作为 Committee 共享 PRIMARY 冻结输入，不新增投票席位或权重；历史 Phase 6 recorded 链只作兼容。

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
- 组合评估、风险贡献与约束配置：`$portfolio-manager`
- 模拟盘启动恢复：`$paper-trading-recovery`
- 白名单知识采集：`$knowledge-ingest`
- 明确证据缺口：`$evidence-investigation`

## 稳定命令

使用 `uv run astock --help` 查看完整参数。M1 稳定入口包括：`init`、`probe`、`sync-market`、`sync-5m`、`quality-report`、`paper-status`、`paper-replay`、`context-plan`、`codex-run-init`、`codex-run-import`。Phase 3 M3.1 入口包括：`financial-audit-schema`、`financial-audit`、`financial-audit-status`；同行分位和 PyOD 尚未启用。

Phase 5 论证链入口包括：`knowledge-semantic-plan`、`knowledge-semantic-run`、`knowledge-semantic-status`、`knowledge-semantic-model-status`、`knowledge-semantic-embedding-run` 和 `knowledge-semantic-packet-export`。未校准相似度不得自动删除，DeepSeek 包不得自动外发。

Provider/reference 稳定入口包括：`provider-list`、`provider-probe`、`provider-status`、`sync-instruments`、`sync-calendar`、`sync-daily`、`sync-corporate-actions`、`reference-status` 和 `reference-audit`。Provider 默认使用 recorded 探针；live 必须显式开启。广泛荐股探索先使用 `research-seeds --live`：它只合并已有 Candidate、市场流动性/规模 Seeds 和由当前已发布大 V Skills 动态推导的 Expert Domain Seeds，不产生 CandidateRecord 或推荐权；随后优先使用 `research-seeds-promote <ResearchSeedReport-artifact-id> --live` 自动冻结 bounded instrument proof、reference/质量/官方公司行动/公告/财务输入并运行 Candidate Scan。`candidate-input-schema`、`candidate-input-stage`、`candidate-input-run` 只保留为手工/诊断 fallback。候选仍只表示研究优先级，不得输出交易方向、目标价、订单或持仓。面向用户的买卖判断必须继续完成单股 Research Runtime 和投委会链。

财务来源稳定入口包括：`sync-financial`、`financial-source-status` 和 `financial-source-audit`。东方财富/Sina 财务值仅为 `SECONDARY_STRUCTURED` 定位线索；只有与 P5X-2 instrument release 一致且由官方原生 PDF 精确证明表名、合并口径、期间列、科目、数值和单位的事实才能进入现有财务审计。机构级基本面入口包括 `institutional-research-schema`、`institutional-research-finalize`、`institutional-decision-context-freeze`、`fundamental-model-status`、`fundamental-model-audit`。Research Runtime 稳定入口包括 `research-plan`、`research-run-company`、`research-status`、`research-audit`、`research-recover`、`trade-plan-view`；新当前公司意见要求 `institutional_research_required=true` 和 exact Bundle/DecisionContext。组合入口包括 `portfolio-paper-evaluate`、`portfolio-evaluate`、`portfolio-construct`、`portfolio-status`、`portfolio-audit`。模拟下单 prepare/确认链已验收，但任何账本写入仍要求独立人工确认，真实券商执行始终不存在。

## 开发约定

- Python 版本固定为 `>=3.12,<3.13`，依赖以 `uv.lock` 为准。
- 修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run pyright`。
- 外部 Provider 同时维护 recorded fixture 和低频 live smoke；日常测试不得依赖外网。
- Windows 路径、UTF-8 中文文件名、原子写入和崩溃恢复必须有测试。
- 不提交 `runtime/`、密钥、Cookie、浏览器 Profile 或私有 PDF。
