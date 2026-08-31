# AStockMultiAgent 项目规则

## 目标与边界

- 构建可审计、可恢复、低成本的 A 股研究、证据和模拟交易系统。
- Codex 是主要自然语言交互入口；Python CLI 是确定性数据、计算、校验、风险和账本入口。
- 不承诺收益，不自动向券商发单。真实交易只能由用户人工确认并在券商端执行。
- 未实现的能力必须如实标记不可用，不得用叙述伪装成已经完成。

## 交互模式：投资者默认，开发诊断显式开启

- 用户询问“某股票现在能不能买 / 公司怎么样 / 组合怎么配 / 持仓怎么办”等投资问题时，**默认进入 INVESTOR_MODE**；只有用户明确要求“调试、排查系统、看命令/状态码/工件/数据库/provider 错误”时才进入 DEVELOPER_MODE。Skill 加载失败也不得改变这个默认模式。
- INVESTOR_MODE 的最终回复只谈投资：结论与置信度、公司质量与盈利驱动、估值/赔率、催化因素、主要风险、什么情况会改变判断，以及确实仍需用户提供的资料。不得展示 CLI、阶段名、协议/Schema/Class 名、reason code、artifact/hash、SQLite/migration、provider 故障、内部 Agent/Committee 编排或“这套系统如何工作”的元评论。
- INVESTOR_MODE 禁止直接出现 `MarketPriceAnchor / ClassifiedTradeProtocol / InstrumentReferenceRelease / FrozenEvidencePack / BaseCase / TradingClassification / NEEDS_INFO / CLAIM_IDS_REQUIRED / EVIDENCE_PACK_REQUIRED / research-plan / research-run-company / current_stage` 等内部词；也不得输出命令执行流水。发送前使用 `research-investor-answer-audit` 的同等规则自检，不通过就重写。
- 运行时诊断、fallback 路径、错误码和工件身份可以完整保存在内部日志，供 DEVELOPER_MODE 复盘；**日志可观测性与投资者答案必须分层**。
- 当前投资咨询遇到数据缺口时，在单次任务内优先自动解决，自动恢复预算上限为 1800 秒：定位失败原因 → 对 retryable 故障有限重试/熔断 → 备用 provider/更合适的同源端点 → 交易所/CNINFO/发行人 IR/监管机构等权威 Web 多源核验。只有这些自动渠道都无法解决的必要事项，才允许在最终一次性整理成人工协助清单。
- 自动渠道尚未完全闭合但已有足够权威材料时，可以给出明确标注的不确定性和研究层判断；不得为了“正式状态”而把后台阻塞详情倒给用户，也不得为了给结论而伪造精确买卖价。
- INVESTOR_MODE 默认短答：**结论（1–2句）→ 2–4个决定性理由 → 最大风险/改变判断的条件**。禁止把同一观点在“为什么/总结/建议”里重复三遍；专业金融/统计术语、论文结论或公式只有确实影响判断时才出现，并在首次出现时用一句括号解释给普通股民。来源用正常引用呈现，不解释内部抓取链。
- 每次投资类会话先恢复用户态：若 paper account 存在，先同步到账户本地镜像 `user_state/portfolio.md / orders.md / trades.md`，再冻结/读取 `portfolio-local-snapshot`，随后读取 Continuous Monitor 的未解决事件/研究任务并对当前持仓做**增量**复核；已持久化的购买时间、数量、平均成本不得在后续会话无故重复询问。外部真实交易事实只进入本机 trade lane，模拟成交仍只来自 SQLite paper fill。`user_state/` 永久 Git-ignore，不进入提交。低成本 deterministic monitor 可以常驻；需要语义判断的 Research Agent 只有在可用 worker/会话存在时消费持久任务，不得把排队状态冒充已完成分析。
- 用户明确说“买入/卖出/加仓/减仓”时，其指令覆盖模型的投资意见，但不覆盖模拟账户机械约束：现金/可用股数、工具特定交易单位、可交易状态、价格限制、账户确认和成交回放仍必须成立。当前 paper order/replay **只准入 STOCK**；ETF 当前仅开放正式研究/组合评估基础，未准入模拟执行，不能套用股票 100 股/T+1 规则。AI 主动下模拟单只允许在正式研究结果允许模拟、当前入场条件实际满足且本地 `auto_ai_paper_order_on_approved_entry=true` 时进入既有订单确认流程；下单不等于持仓，只有 fill 后才更新持仓。

## 唯一事实源

- 原始响应：`runtime/objects/sha256/`，内容不可覆盖。
- 分析事实：Parquet；DuckDB 只建视图，不复制事实。
- 任务、游标、工件注册和模拟账本：SQLite。
- Codex 草稿：`runtime/codex_runs/<run_id>/`，校验后才能进入 ArtifactStore。
- 不直接编辑 SQLite，不在聊天结论和数据库之间建立旁路。
- 根目录《低成本A股多Agent投研系统方案》只写长期设计，《开发计划》只写未完成项，《验收报告》只写有证据的当前事实；验收时必须在同一次修改中迁移状态。

## 数据与证据

- 禁止未来函数。所有输入必须带可得时间、来源和版本。
- 来源访问由版本化 `source-access-policy` 评分：官方性、capability match、recent health、freshness、transport、latency、cost/auth friction 与 retryability 共同决定自动路径；对强官方能力，只要存在可用 `PRIMARY_OFFICIAL`，低权威快源不得反超。Manual 永远最后，单个 provider 失败不是终止条件。
- 数据源按稳定性分层：① 可确定/低频规则事实优先使用“权威 Web/Search 核验一次 → 版本化本地冻结 → 后续零网络运行”；② 公告、政策、制度日期等低频外部事实优先权威网页/Search，不为了结构化而强依赖脆弱 API；③ 实时价格、连续 K 线、全市场 Universe 等高频结构化数据才使用多 Provider API/fallback。Agent 动态发现或提议新的 Web/Search 来源时必须先通过 `uv run astock source-proposal-check` 的确定性能力/官方域名/完整性策略校验；`DISCOVERY_ONLY` 结果不得直接进入正式证据，`ADMIT_AFTER_SNAPSHOT` 也必须先形成不可变快照与 provenance。Search 不得替代 OHLCV、全市场覆盖证明，也不得用“没搜到”证明某事项不存在。
- 交易日历属于低频确定性事实：当前年份若已有 `official_trading_calendar.yaml` 的交易所官方核验记录，`sync-calendar --live` 必须优先本地确定性生成，不得先打 BaoStock/API；新年份配置缺失时，Chat/Codex 自动 Search 上交所/深交所/北交所年度休市通知并交叉核验后更新版本化配置，再恢复任务。仅 Search/Provider 均无法形成可审计日历时才允许降级。
- 上一层已满足时，不通过下一层重复抓取同一内容。
- 投资结论必须引用 evidence_id/source_snapshot_id，或明确标记为推断/缺口。
- 社区内容只能作线索；关键事实必须回到公告、交易所、财报等更强来源。
- 委员会只读冻结工件，禁止重新联网、抓取或启动新研究；缺证据返回 `NEEDS_INFO`。
- Phase 5 原始**来源材料**（白名单正文、图片、SourceSnapshot、原 PDF/DOCX）继续保持不可变；historical composite 653 的成员身份/hash 只保留于 audit decision/tombstone 供 provenance。用户已于 2026-08-16 明确要求节省空间，因此 426 条 `RETIRE` Skill 的 Skill payload、活动数据库行及其未被其他工件引用的 Skill ObjectStore 对象允许通过 0056 compaction 物理删除；这不授权删除原始来源材料或 237 条 active/revised/curated Skill。Research Runtime 只能读取最新 audited active registry。
- KGA 发布并完成全尺寸恢复演练后，旧 `Semantic/Distillation/Reviewed/Book/Private` 生产流水允许通过 0057 **冷归档**：必须先计算所有仍存活热表的 FK 父级闭包，受现役 visual/no-skill 引用的最小父行继续留在 SQLite；其余历史行写入按表 zstd Parquet，manifest 同时进入 ObjectStore，并通过文件 hash、逐表行数、`foreign_key_check` 与完整 restore 验证后才能从热库删除。原始 Zhihu content/comment version、SourceSnapshot、Evidence、ObjectStore 原文及当前 Direct/Visual/Audited registry 不属于该删除授权。单行 knowledge Parquet index 可按现有 author/content_type/year 分区合并，历史 schema 只允许 additive union（缺失列补 NULL）；最后仅在全部 archive/Parquet audit 通过后执行一次 `VACUUM`。
- Phase 5 蒸馏粒度固定为 `SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate`；Paragraph 是存储、定位及本地语义辅助视图单位，只有完整 ArgumentUnit 可产生最终语义分数、DeepSeek 输入和 Skill 候选。
- 图片证据必须经过不可变图片快照、PDF bbox 或 DOM 定位、逐图 OCR、类型和前后 Paragraph 回填；图片 Paragraph 永远不能独立蒸馏，夹在论点与结论之间时必须 `MERGE_WITH_BOTH`。OCR 或上下文不完整时 AU 保持 `NEEDS_REVIEW`。
- 《价值投资功法》历史视觉覆盖证据仍为 249 页、57 个含图页、74/74 placements；71 个非装饰图映射到 55 个 AU，11 个 READY、44 个 REVIEW。三位知乎作者的真实视觉支线已于 2026-08-10 完成：2,503/2,503 placements、2,306 unique assets、2,503 READY、0 REVIEW/BLOCKED；三份 `VisualEvidencePack` 均 READY。visual Skill generation 的历史事实仍是 951 个真实视觉关联 AU、422 个 admitted overlay Skill、529 个 NO_SKILL，baseline 231 + overlay 422 = historical composite 653。2026-08-14 KGA-R1 对 653 条逐条裁决为 KEEP_SCOPED=190、REVISE=37、RETIRE=426，并新增 10 条多源权威证据支持的 curated Skills；2026-08-16 又为常见会计、估值、回测、动量/反转、交易成本/容量、集中/分散、周期和竞价命题增加 proposition-specific evidence routing；当前 audited active registry 为 237，`KnowledgeSkillProvider` 状态必须为 `AUDITED_REGISTRY_READY`。不得绕过 audited registry 直接读取 knowledge 表；historical composite 只允许 provenance/re-audit 显式读取。
- Serenity 当前活动方法注册表为 `research-skills-v3`。muxuuu/haskaomni 上游代码只经版本化 v4 audit 编译成 typed Specialist contracts；`JuglarCycleStageSkill` 是当前新增的固定资产周期方法。任何 Serenity/scorecard/Juglar 输出都只是 evidence-bound Delta 或 report-only metric，不得直接产生交易权重、目标价、仓位或订单。
- 新当前公司研究默认要求 Phase 9 机构级基本面层：`EvidenceSufficiencyReport → IndustryProfile / CompanyEconomicsProfile → DriverTree → ForecastPack → ValuationPack → FundamentalModelBundle → InstitutionalDecisionContext`。Forecast/Valuation 数值由 Python 确定性复算；Serenity Growth/Valuation 不得维护第二套平行数值账本。expected return / market-implied expectations 只能使用绑定注册 artifact/hash 且满足 PIT 的 `MarketPriceAnchor`。Bundle/DecisionContext 作为 Committee 共享 PRIMARY 冻结输入，不新增投票席位或权重；历史 Phase 6 recorded 链只作兼容。

## Adaptive Edge / Deterministic Core

- 外围适应层可以动态变化：Provider/endpoint 选择、TransportProfile、ProviderDialect、Current Research capability schedule、ResearchPlanner、ProviderRecovery、SchemaRepair、Portfolio allocator plugin 与 Specialist 预算都必须通过版本化 config/registry/policy 描述，不得在业务 Service 中复制第二份阈值或 provider 顺序。
- Agent 只能提交 `PROPOSED` proposal；Python deterministic validator 决定是否形成 `VALIDATED` artifact。Planner 必须自动补回 active policy 的 core acquisition 与 Evidence/PIT/Financial Integrity/Fundamental 等 mandatory gate；Agent 不得删除硬门。
- Provider Recovery 只能使用 registry allowlist 中声明目标 capability 且 health/transport 合法的 adapter，Manual-last 不可被 proposal 改写。
- Schema Repair 必须 raw-first：至少满足 active policy 的多样本 SourceSnapshot、官方 artifact type 与仓库真实 contract test，才能从 PROPOSED 进入 VALIDATED；之后还需显式批准才可生成 ADMITTED candidate dialect。Candidate release 不自动修改 active dialect/config、不写正式事实，可 audit/rollback。
- Deterministic Core 继续永久强制：官方事实认证、PIT/未来函数、会计与数学恒等式、不可变 ObjectStore、账本平衡、模拟盘人工确认、`paper_ledger_write_allowed=false` 的研究链、`broker_execution_allowed=false`。AI 不得通过 prompt、proposal、Skill 或 fallback 绕过这些边界。
- `CurrentResearchSchedule / ValidatedResearchPlan / ProviderRecoveryValidation / SchemaRepairValidation / ProviderDialectCandidateRelease` 等内部 artifact 只在 DEVELOPER_MODE 可见；INVESTOR_MODE 继续只输出自然语言投资判断，并由动态 internal vocabulary audit 拦截内部术语。

## 行情与模拟盘

- 模拟账户保留账户、订单、冻结现金、成交、费用、T+1、确认和回放。Continuous Monitor 可对**已确认开放模拟订单**常驻执行 deterministic replay，但不得创建新订单或直接改持仓；Agent 会话仍负责用户态镜像、语义复核和需要人工/Agent 判断的动作。
- 默认回放使用未复权原始 **60m**，东方财富/新浪交叉质量校验后仍标记 `PROVIDER_1H_APPROX`；小时 OHLC 只能证明限价在该小时内被触及，不能证明盘口队列或小时内先后路径，因此小时级限价成交采用保守价。存在执行路径歧义时才显式切换 `--resolution 5m`。
- 5m 保留为高精度 fallback，不再作为月/年级策略的默认存储/回放负担；1m 不在默认链。缺失数据不得静默插值或虚构。
- Git-ignore 的 `user_state/portfolio.md / orders.md / trades.md` 是 Agent/用户可读本机状态；其中外部既成交易以 `trades.md` 的 IMPORT 事实确定性回放到 `portfolio.md`，paper fills 由 SQLite paper ledger 同步投影。SQLite paper ledger 仍是模拟订单/成交/资金的唯一事实源；本地外部持仓不得反向写入 paper ledger，镜像也不得绕过账本制造 fill。
- 组合风险研究序列由原始价格与**当时可见且 TERMS_VERIFIED 的版本化公司行为**派生 gross total-return 研究序列；原始未复权价继续拥有真实成交/数量语义。公司行动 release 缺失时必须明确降级，复权/总回报研究价不得当作真实成交价。
- 免费 reference 的 provider/operation 顺序只能读取 `market-reference-v2` route；业务代码不得另写 BaoStock/EastMoney/Sina 固定顺序。BaoStock 可用时仍保留批量/低频能力并受 active preflight/circuit policy 保护；live route 会结合 provider health 跳过 UNAVAILABLE/CORRUPT。原始响应先入 ObjectStore，未复权日线在上海收盘前不可见。
- 公司行动结构化结果只作线索；精确官方文档和条款核验完成前不得写模拟账本。
- Repo Skill 不得直接修改账本，只能调用已校验的 `astock` 命令。

## 常用任务路由

- 宽泛研究或跨模块任务：`$astock-research-orchestrator`
- 候选/观察名单：`$candidate-scan`
- 单公司深度研究：`$company-deep-research`
- 财务可信度：`$financial-integrity-audit`
- 宏观状态、政策传导与数据 vintage：`$macro-policy-regime`
- 行业价值链、盈利池与可比公司：`$industry-value-chain`
- 催化剂与风险事件时间线：`$catalyst-event-research`
- 控制、激励、资本配置与披露质量：`$governance-management-quality`
- 独立多空之后的假设、反证与决策脆弱性复核：`$investment-red-team`
- 模型、预测、因子与回测的泄漏/多重检验/成本验证：`$model-risk-backtest-validation`
- 外部持仓事实落库、持仓变化、事件失效与目标带再平衡：`$holding-monitor`
- 已分析/推荐标的的持续观察、事件增量与入场/退出条件跟踪：`$continuous-investment-monitor`
- 组合评估、计划买入后的组合补全、风险贡献、成本/no-trade band 与对冲语义：`$portfolio-manager`
- 模拟盘启动恢复：`$paper-trading-recovery`
- 白名单知识采集：`$knowledge-ingest`
- 明确证据缺口：`$evidence-investigation`
- GitHub/投研平台/社媒技术侦察与外部能力去重：`$research-tech-scout`

## Skill 与 Workflow 文档

- canonical Repo Skills 位于 `.agents/skills/*/SKILL.md`，供 Agent 自动发现；顶层 `skills/README.md` 只做人类可见目录，不复制第二套 Skill。
- 跨 Skill 用户任务统一记录在 `docs/workflows/`。Skill 定义能力边界，Workflow 定义步骤、依赖、并发、fallback 与停止条件；两者都不得绕过代码/Schema/Config 的硬门禁。
- 新增稳定能力域时更新 Skill；新增跨 2 个以上 Skill 的完整用户路径时更新 Workflow，并同步文档合同测试。
- 执行 canonical Repo Skill 的项目任务结束时，Agent 应写入恰好一个 `agent-observation-register` 观测：记录本次 eligible / selected / completed Skill 与端到端耗时。`expected_skill_ids` 只允许来自人工标注、fixture 或独立评测，不得在普通任务中由执行 Agent 自己猜标签；因此日常可稳定统计 selection/execution hit rate，precision/recall 仅在有真实标签的子集上计算。`agent-observability-report` 同时复用 ResearchRun checkpoint 的 wall time/provider/cache 指标及 canonical 双源行情对齐指标，不另建第二套运行账本。

## 稳定命令

使用 `uv run astock --help` 查看完整参数。稳定入口包括：`init`、`probe`、`sync-market`、`sync-hourly`、`sync-5m`、`quality-report`、`market-canonical-gc`、`agent-observation-register`、`agent-observability-report`、`pit-temporal-schema`、`pit-temporal-audit`、`pit-knowledge-cutoff-diagnostic`、`pit-temporal-artifact-audit`、`paper-status`、`paper-replay`、`local-portfolio-init/status/sync-paper/review/audit/rebuild`、`portfolio-import-declared-trade`、`portfolio-local-snapshot`、`portfolio-etf-profile-register`、`portfolio-etf-metrics`、`portfolio-complement-screen`、`portfolio-hedge-evaluate`、`portfolio-transition`、`portfolio-decision-status`、`portfolio-decision-audit`、`context-plan`、`codex-run-init`、`codex-run-import`。`probe` 只做轻量只读 capability/schema 健康检查，不再执行全库 `PRAGMA integrity_check`；完整 SQLite 体检必须显式使用 `state-integrity-audit`。Phase 3 M3.1 入口包括：`financial-audit-schema`、`financial-audit`、`financial-audit-status`；同行分位和 PyOD 尚未启用。

Phase 5 论证链入口包括：`knowledge-semantic-plan`、`knowledge-semantic-run`、`knowledge-semantic-status`、`knowledge-semantic-model-status`、`knowledge-semantic-embedding-run` 和 `knowledge-semantic-packet-export`。未校准相似度不得自动删除，DeepSeek 包不得自动外发。

Provider/reference 稳定入口包括：`provider-list`、`provider-probe`、`provider-status`、`sync-instruments`、`sync-calendar`、`sync-daily`、`sync-corporate-actions`、`reference-status` 和 `reference-audit`。Provider 默认使用 recorded 探针；live 必须显式开启。当前单股投资咨询优先使用 `research-acquire-current <company_id> --market <market>`：先建立目标证券精确身份，再对行情、公司行动和年度/最新中期财务做 bounded fallback/并行采集，采集结束后才冻结当前决策快照；不得用用户提问的瞬间截断同一轮几分钟内新取得的公开数据。若本地/API 仍缺资料，Agent 必须继续按交易所/CNINFO/发行人 IR/监管机构优先做权威 Web 检索，自动渠道全部耗尽后才一次性请求人工协助。历史回放、正式前瞻研究和 backtest 仍保留严格 source-availability/PIT 边界；新增或修改时间序列 feature、as-of join、retrieval/window/resample 管线时，必须用 `pit-temporal-audit` 证明 active dependency graph 的 temporal non-interference，并为 row-aligned transform 增加 truncation-invariance property test。LLM knowledge-cutoff 前后表现只能通过 `pit-knowledge-cutoff-diagnostic` 作为描述性偏差诊断，不能直接产生生产准入或调权。广泛荐股探索必须先创建 `research-team-plan`，按 `docs/workflows/workflow-full-market-research-team.md` 执行按需全市场团队 DAG；只有 `research-recommendation-readiness` 返回 `formal_recommendation_allowed=true` 才能形成正式买入排序。候选发现仍使用 `research-seeds --live`，它按轻薄本自适应并发现场获取 XSHG/XSHE/BJSE 市场快照，先保留不依赖私有 Skill 的 blind market tranche，再叠加 bounded Expert overlay；Expert Domain 准入只认 audited Skill 绝对命中数，不再使用“占作者全部 Skill 的比例”门槛或加分。若当前 Universe/market Seeds 无法证明，必须 fail closed，禁止从 Web/新闻人工挑股票替代全市场候选 lineage。随后优先使用 `research-seeds-promote <ResearchSeedReport-artifact-id> --live` 自动冻结 bounded instrument proof、reference/质量/官方公司行动/公告/财务输入并运行 Candidate Scan。`candidate-input-schema`、`candidate-input-stage`、`candidate-input-run` 只保留为手工/诊断 fallback。候选仍只表示研究优先级，不得输出交易方向、目标价、订单或持仓。

财务来源稳定入口包括：`sync-financial`、`financial-source-status` 和 `financial-source-audit`。结构化财务来源顺序只读取 `financial-sources-v2.provider_order`；当前配置中 Sina 优先、EastMoney 备用/交叉，但业务 Service 不得复制这两个名字。任一 live schema 缺少源生 scope/currency 时必须降级而不是猜值；结构化源均只是 `SECONDARY_STRUCTURED`，最终仍由 CNINFO/交易所/发行人正式报告精确证明表名、合并口径、期间列、科目、数值和单位。Current Acquisition 先发现**实际已经披露**的最新 report period，不再按固定月份猜 Q1/H1/Q3。机构级基本面入口包括 `institutional-research-schema`、`institutional-research-finalize`、`institutional-decision-context-freeze`、`fundamental-model-status`、`fundamental-model-audit`。Research Runtime 稳定入口包括 `research-plan`、`research-run-company`、`research-status`、`research-audit`、`research-recover`、`trade-plan-view`；LIVE 当前研究允许省略 `--as-of` 并在命令实际执行时冻结时间，recorded/historical 必须显式提供。开发诊断入口新增 `research-capability-status`、`provider-dialect-status`、`adaptive-edge-status`、`adaptive-edge-schema`；Agent proposal 的受控入口为 `adaptive-plan-validate`、`adaptive-recovery-validate`、`adaptive-schema-repair-validate`，candidate dialect 准入必须显式 `adaptive-schema-repair-admit --approve`，并可用 `adaptive-artifact-audit` / `adaptive-dialect-rollback` 审计和回滚。这些命令属于 DEVELOPER_MODE，正常投资者回复仍使用 `research-acquisition-investor-view` / `research-investor-view` 的自然语言边界，并由 `research-investor-answer-audit` 阻止后台术语泄露；执行条件只在用户明确询问具体买卖规则时解释。组合入口包括 `portfolio-paper-evaluate`、`portfolio-evaluate`、`portfolio-construct`、`portfolio-status`、`portfolio-audit` 以及 `portfolio-import-declared-trade`、`portfolio-local-snapshot`、`portfolio-etf-profile-register`、`portfolio-etf-metrics`、`portfolio-complement-screen`、`portfolio-hedge-evaluate`、`portfolio-transition`、`portfolio-decision-status`、`portfolio-decision-audit`。正式 `NATURAL_HEDGE` 必须由同一 `as_of` 的 ETFResearchMetrics、PIT 正常/压力期复算、官方经济机制 provenance、风险改善阈值和独立已验证成本上限共同支持；风险指标与费用不得跨量纲相减；当前 long-only 股票/ETF 工具链不产出 `EXPLICIT_HEDGE`。ETF paper order/replay 尚未准入；模拟下单 prepare/确认链只对已准入 STOCK 生效，任何账本写入仍要求独立人工确认，真实券商执行始终不存在。

## 强制开发工作流与恢复契约

- 所有正式开发任务统一压缩为 5 个门：`范围/根因与必要研究 → 架构与实施方案 → 实现 + defect-first Review + 分级验证 → 按风险发布 → 文档归档 + 临时文件清理 + 终局 Review/complete`。详细要求仍由下列条款展开；任何门失败都返回受影响阶段返工。**发布不是最后一步，发布后归档/清理/终局核验才是闭环终点**。
- 开工前必须以需求分析师身份明确需求、问题边界、不可改变约束和验收口径；先使用代码、日志、数据库、运行工件和测试定位根因，不得凭聊天印象直接改代码。涉及投研策略、数据源、算法、外部规范或安全治理时，优先检索官方文档、原始论文、监管机构或高认可度资料，并记录采用与不采用的理由。
- 架构师与算法工程师必须评估算法复杂度、CPU、内存、I/O、并发、故障恢复、兼容性、风险与回滚；禁止为了实现功能建立第二套事实源、平行 Router/Evidence/Paper 架构或可漂移状态副本。
- `开发计划.md` 只保存当前未完成任务。新任务编码前必须写入需求、根因、权威依据、方案、实施步骤、验收标准、测试与 Review 门、风险和回滚；已经完成且验收通过的子任务不得继续保留在计划中。长任务必须同步维护唯一 durable run，记录步骤状态、故障断点和真实证据；未验证的中间进度不得冒充验收事实。
- 开发完成后必须按开发计划执行独立 Code Review；不符合架构、安全、性能或验收标准时，附明确意见打回返工，返工后重新 Review。验证按风险分级：**L0 文档/流程/注释**只跑文档合同/文本检查（若存在）、必要格式/静态检查和 `git diff --check`；**L1 单模块低风险代码**跑 targeted regression + 受影响 lint/typecheck；**L2 跨模块/状态/数据库/API/运行态**增加相关集成/负向与必要 smoke；**L3 PIT/Universe/财务/估值/账本/安全/迁移/正式发布**才要求完整专业门、全仓测试与真实/发布级验证。命中多个等级取最高级；Review 发现影响面扩大时必须升级，禁止为 L0/L1 机械跑全仓 pytest，也禁止以提速为由削弱 L3。
- **发布前文档迁移**：每个子任务 Review 和测试通过后，立即从 `开发计划.md` 删除对应实现任务，并将当时已经成立的实现内容、代码修改、测试、Review、性能、迁移事实写入 `验收报告.md`；`docs/architecture/`、`docs/workflows/` 与 canonical Skills 同步维护当前真实结构。此阶段不得预写尚未发生的 commit SHA、tag/Release、remote digest、clean worktree 或“已发布”等未来事实。
- **release baseline 发布**：完成显式路径暂存、staged diff 审核、secret/private/runtime 审计、release baseline commit、push、annotated tag/GitHub Release、构建资产上传与远端校验。已发布 tag 是不可变 release baseline；禁止为了后续文档收尾移动、重打或强推该 tag。
- **发布后文档归档与终局核验是强制阶段，不得省略**：release baseline 远端验证完成后，必须重新读取 `开发计划.md`、`验收报告.md`、相关 `docs/architecture/`、`docs/workflows/`、Repo Skills 和根文档合同测试，再执行第二遍状态迁移。`验收报告.md` 必须回写只有发布后才成立的最终 frozen-tree 测试数字、release baseline SHA、tag/Release、资产 digest、真实 smoke、远端验证和安全边界；architecture/Workflow/Skill 中的“待发布/仍需发布/候选基线”等发布前措辞必须改为真实发布后状态。
- **开发计划零残留规则**：准备终局 Review 前，必须针对当前 durable run 的 `run title`、所有 step id/step title、当前版本主线名称、release label，以及 `DONE / COMPLETE / 已完成 / 发布过程` 等完成态上下文做一次搜索。除仍未完成并被明确重新分类为“长期运行/数据义务”的事项外，`开发计划.md` 中这些当前主线的任务、完成清单、进度、历史发布过程和“当前状态里列举已完成项目名”的文字必须为零；当前状态只能说明是否存在未完成代码主线和真正未完成的义务。
- **临时文件清理硬门**：终局 diff 审计前必须清理仅由本任务生成且可证明可再生的临时脚本/patch helper、一次性测试日志/XML、临时 build/smoke 目录、工具缓存和已被正式证据替代的编排副产物；清理后重新查看 worktree。禁止自动删除 `runtime/` 正式对象/数据库、`user_state/`、私有材料、正式构建/Release 资产、未知来源文件或其它会话/用户已有未提交改动；无法证明归属的文件一律保留并说明。
- **发布后 docs-only closeout commit**：发布后回写 tracked Markdown/合同测试属于独立 docs-only closeout revision，必须按其实际 L0/L1 风险运行文档合同、必要静态检查和 `git diff --check`，并在 commit **之前**完成一次 defect-first closeout diff Review，再显式暂存、commit、push；不因“发布收尾”机械重跑全仓 pytest。此提交不得修改 release 代码、版本号、已发布 tag 或 Release 资产。最终一致性语义固定为：`tag commit == release baseline commit`；`origin/main == closeout commit`；`closeout commit` 必须是 release baseline/tag 的后代；不得再要求发布后 `HEAD == tag commit`。
- **终局 durable completion**：closeout push 后只做远端关系、worktree、后台 task 与 durable state 的终局核验；若 commit 后 tracked 内容没有再变化，不重复审查同一份 diff。发布前的 PASS Review 不能覆盖新的 closeout 内容，但 commit 前完成的 closeout diff Review 可以作为内容审查依据；随后用 `long_run_review(pass)` 固化最终 revision。只有在“计划零残留、验收/架构/Workflow/Skill 已处于最终事实状态、release baseline/tag 未被移动、closeout commit 已推送、main/远端关系正确、worktree clean、无 running/unknown task”全部成立后，才允许调用 `long_run_complete`。任何 final receipt/最终回复在此之前不得声称 COMPLETE。
- **无 GitHub Release / 纯文档流程任务不得形成自引用提交循环**：如果任务本身不创建产品 tag/Release，必须在最终 commit **之前**完成计划清理、验收/架构/Workflow/Skill 归档、文档合同和 defect-first diff Review；随后一次 final closeout commit/push 即可。push 后只验证远端/clean/durable completion，不要求再次修改 tracked Markdown写回自身 SHA，也不重复内容 Review，除非 commit 后内容又发生变化。该提交自身 SHA 只记录在 durable evidence/最终回复。
- MCP 断连、新会话或上下文丢失后，恢复事实源顺序固定为：`AGENTS.md → docs/architecture/ 现行架构合同 → 开发计划.md → 验收报告.md → 唯一 durable run → Git/测试/运行工件`。禁止依赖聊天记忆、创建平行 long-run，或重新编写超长接管提示词替代仓库事实。

## 工程约定

- Python 版本固定为 `>=3.12,<3.13`，依赖以 `uv.lock` 为准。
- 修改后的验证集合由上述 L0–L3 决定；文档/流程类 L0 默认只跑文档合同/文本检查、必要格式/静态检查和 `git diff --check`，不机械运行全仓 `uv run pytest`。运行时代码、高风险数据/PIT/账本/安全/发布任务再按 L1–L3 升级到 targeted、全仓、真实 smoke 或发布级验证。
- 外部 Provider 同时维护 recorded fixture 和低频 live smoke；日常测试不得依赖外网。
- Windows 路径、UTF-8 中文文件名、原子写入和崩溃恢复必须有测试。
- 不提交 `runtime/`、密钥、Cookie、浏览器 Profile、私有 PDF、`.ai-bridge/` 或缓存工件。
