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
- 每次投资类会话先恢复用户态：若 paper account 存在，先同步到账户本地镜像 `user_state/portfolio.md / orders.md / trades.md`，再对当前持仓做**增量**复核；这些文件属于用户本机状态，`user_state/` 永久 Git-ignore，不进入提交。系统不要求月度/年度策略常驻后台 Agent。
- 用户明确说“买入/卖出/加仓/减仓”时，其指令覆盖模型的投资意见，但不覆盖模拟账户机械约束：现金/可用股数、100股整手、可交易状态、价格限制、账户确认和成交回放仍必须成立。AI 主动下模拟单只允许在正式研究结果允许模拟、当前入场条件实际满足且本地 `auto_ai_paper_order_on_approved_entry=true` 时进入既有订单确认流程；下单不等于持仓，只有 fill 后才更新持仓。

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

- 模拟账户是**会话式恢复组件**，不是常驻自动交易服务：保留账户、订单、冻结现金、成交、费用、T+1、确认和回放；每次 Agent 投资会话按需补齐离线区间。
- 默认回放使用未复权原始 **60m**，东方财富/新浪交叉质量校验后仍标记 `PROVIDER_1H_APPROX`；小时 OHLC 只能证明限价在该小时内被触及，不能证明盘口队列或小时内先后路径，因此小时级限价成交采用保守价。存在执行路径歧义时才显式切换 `--resolution 5m`。
- 5m 保留为高精度 fallback，不再作为月/年级策略的默认存储/回放负担；1m 不在默认链。缺失数据不得静默插值或虚构。
- Git-ignore 的 `user_state/portfolio.md / orders.md / trades.md` 是 Agent/用户可读镜像；SQLite paper ledger 是订单/成交/资金的确定性事实源。镜像不得反向绕过账本制造 fill。
- 复权研究序列由原始价格与版本化公司行为派生；复权价不得当作真实成交价。
- 免费 reference 的 provider/operation 顺序只能读取 `market-reference-v2` route；业务代码不得另写 BaoStock/EastMoney/Sina 固定顺序。BaoStock 可用时仍保留批量/低频能力并受 active preflight/circuit policy 保护；live route 会结合 provider health 跳过 UNAVAILABLE/CORRUPT。原始响应先入 ObjectStore，未复权日线在上海收盘前不可见。
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
- GitHub/投研平台/社媒技术侦察与外部能力去重：`$research-tech-scout`

## Skill 与 Workflow 文档

- canonical Repo Skills 位于 `.agents/skills/*/SKILL.md`，供 Agent 自动发现；顶层 `skills/README.md` 只做人类可见目录，不复制第二套 Skill。
- 跨 Skill 用户任务统一记录在 `docs/workflows/`。Skill 定义能力边界，Workflow 定义步骤、依赖、并发、fallback 与停止条件；两者都不得绕过代码/Schema/Config 的硬门禁。
- 新增稳定能力域时更新 Skill；新增跨 2 个以上 Skill 的完整用户路径时更新 Workflow，并同步文档合同测试。
- 执行 canonical Repo Skill 的项目任务结束时，Agent 应写入恰好一个 `agent-observation-register` 观测：记录本次 eligible / selected / completed Skill 与端到端耗时。`expected_skill_ids` 只允许来自人工标注、fixture 或独立评测，不得在普通任务中由执行 Agent 自己猜标签；因此日常可稳定统计 selection/execution hit rate，precision/recall 仅在有真实标签的子集上计算。`agent-observability-report` 同时复用 ResearchRun checkpoint 的 wall time/provider/cache 指标及 canonical 双源行情对齐指标，不另建第二套运行账本。

## 稳定命令

使用 `uv run astock --help` 查看完整参数。稳定入口包括：`init`、`probe`、`sync-market`、`sync-hourly`、`sync-5m`、`quality-report`、`market-canonical-gc`、`agent-observation-register`、`agent-observability-report`、`pit-temporal-schema`、`pit-temporal-audit`、`pit-knowledge-cutoff-diagnostic`、`pit-temporal-artifact-audit`、`paper-status`、`paper-replay`、`local-portfolio-init/status/sync-paper/review/audit/rebuild`、`context-plan`、`codex-run-init`、`codex-run-import`。`probe` 只做轻量只读 capability/schema 健康检查，不再执行全库 `PRAGMA integrity_check`；完整 SQLite 体检必须显式使用 `state-integrity-audit`。Phase 3 M3.1 入口包括：`financial-audit-schema`、`financial-audit`、`financial-audit-status`；同行分位和 PyOD 尚未启用。

Phase 5 论证链入口包括：`knowledge-semantic-plan`、`knowledge-semantic-run`、`knowledge-semantic-status`、`knowledge-semantic-model-status`、`knowledge-semantic-embedding-run` 和 `knowledge-semantic-packet-export`。未校准相似度不得自动删除，DeepSeek 包不得自动外发。

Provider/reference 稳定入口包括：`provider-list`、`provider-probe`、`provider-status`、`sync-instruments`、`sync-calendar`、`sync-daily`、`sync-corporate-actions`、`reference-status` 和 `reference-audit`。Provider 默认使用 recorded 探针；live 必须显式开启。当前单股投资咨询优先使用 `research-acquire-current <company_id> --market <market>`：先建立目标证券精确身份，再对行情、公司行动和年度/最新中期财务做 bounded fallback/并行采集，采集结束后才冻结当前决策快照；不得用用户提问的瞬间截断同一轮几分钟内新取得的公开数据。若本地/API 仍缺资料，Agent 必须继续按交易所/CNINFO/发行人 IR/监管机构优先做权威 Web 检索，自动渠道全部耗尽后才一次性请求人工协助。历史回放、正式前瞻研究和 backtest 仍保留严格 source-availability/PIT 边界；新增或修改时间序列 feature、as-of join、retrieval/window/resample 管线时，必须用 `pit-temporal-audit` 证明 active dependency graph 的 temporal non-interference，并为 row-aligned transform 增加 truncation-invariance property test。LLM knowledge-cutoff 前后表现只能通过 `pit-knowledge-cutoff-diagnostic` 作为描述性偏差诊断，不能直接产生生产准入或调权。广泛荐股探索先使用 `research-seeds --live`：它只合并已有 Candidate、市场流动性/规模 Seeds 和由当前已发布大 V Skills 动态推导的 Expert Domain Seeds，不产生 CandidateRecord 或推荐权；随后优先使用 `research-seeds-promote <ResearchSeedReport-artifact-id> --live` 自动冻结 bounded instrument proof、reference/质量/官方公司行动/公告/财务输入并运行 Candidate Scan。`candidate-input-schema`、`candidate-input-stage`、`candidate-input-run` 只保留为手工/诊断 fallback。候选仍只表示研究优先级，不得输出交易方向、目标价、订单或持仓。

财务来源稳定入口包括：`sync-financial`、`financial-source-status` 和 `financial-source-audit`。结构化财务来源顺序只读取 `financial-sources-v2.provider_order`；当前配置中 Sina 优先、EastMoney 备用/交叉，但业务 Service 不得复制这两个名字。任一 live schema 缺少源生 scope/currency 时必须降级而不是猜值；结构化源均只是 `SECONDARY_STRUCTURED`，最终仍由 CNINFO/交易所/发行人正式报告精确证明表名、合并口径、期间列、科目、数值和单位。Current Acquisition 先发现**实际已经披露**的最新 report period，不再按固定月份猜 Q1/H1/Q3。机构级基本面入口包括 `institutional-research-schema`、`institutional-research-finalize`、`institutional-decision-context-freeze`、`fundamental-model-status`、`fundamental-model-audit`。Research Runtime 稳定入口包括 `research-plan`、`research-run-company`、`research-status`、`research-audit`、`research-recover`、`trade-plan-view`；LIVE 当前研究允许省略 `--as-of` 并在命令实际执行时冻结时间，recorded/historical 必须显式提供。开发诊断入口新增 `research-capability-status`、`provider-dialect-status`、`adaptive-edge-status`、`adaptive-edge-schema`；Agent proposal 的受控入口为 `adaptive-plan-validate`、`adaptive-recovery-validate`、`adaptive-schema-repair-validate`，candidate dialect 准入必须显式 `adaptive-schema-repair-admit --approve`，并可用 `adaptive-artifact-audit` / `adaptive-dialect-rollback` 审计和回滚。这些命令属于 DEVELOPER_MODE，正常投资者回复仍使用 `research-acquisition-investor-view` / `research-investor-view` 的自然语言边界，并由 `research-investor-answer-audit` 阻止后台术语泄露；执行条件只在用户明确询问具体买卖规则时解释。组合入口包括 `portfolio-paper-evaluate`、`portfolio-evaluate`、`portfolio-construct`、`portfolio-status`、`portfolio-audit`。模拟下单 prepare/确认链已验收，但任何账本写入仍要求独立人工确认，真实券商执行始终不存在。

## 开发约定

- Python 版本固定为 `>=3.12,<3.13`，依赖以 `uv.lock` 为准。
- 修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run pyright`。
- 外部 Provider 同时维护 recorded fixture 和低频 live smoke；日常测试不得依赖外网。
- Windows 路径、UTF-8 中文文件名、原子写入和崩溃恢复必须有测试。
- 不提交 `runtime/`、密钥、Cookie、浏览器 Profile 或私有 PDF。
